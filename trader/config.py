"""Loads config.yaml and .env into the objects the engine needs.

Split on purpose: config.yaml is committed and public, .env is not. If a value
would be embarrassing on a web page it belongs in .env, and nothing here reads
a credential out of the YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .risk import RiskLimits

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    journal: Path
    halt: Path
    publish: Path


@dataclass(frozen=True)
class Config:
    mode: str
    limits: RiskLimits
    paths: Paths
    symbols: tuple[str, ...]
    paper_starting_cash: float
    paper_slippage_bps: float
    paper_fee_bps: float
    quote_currency: str
    desk: dict

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = path.resolve().parent

    mode = raw.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")

    symbol_groups = raw.get("symbols") or {}
    symbols = tuple(
        symbol
        for group in symbol_groups.values()
        for symbol in (group or [])
    )
    if not symbols:
        raise ValueError(
            f"{path.name} lists no symbols. An empty allowlist would deny "
            "every order; list what this bot may trade."
        )

    risk = raw.get("risk") or {}
    paper = raw.get("paper") or {}
    equity = float(paper.get("starting_cash", 100_000))

    def cap(absolute_key: str, percent_key: str, default_pct: float) -> float:
        """Resolve a cap from a percentage of equity, or an explicit amount.

        Percentages are the safer way to express these. An absolute cap set
        for one account size silently becomes nonsense against another: this
        project ran for a day with a 1,000 per-order cap against a 100,000
        account, so a strategy sizing at any sensible fraction of cash had
        every order refused. It looked like the strategy never wanting to
        trade, which is a very different diagnosis.
        """
        if risk.get(absolute_key) is not None:
            return float(risk[absolute_key])
        return equity * float(risk.get(percent_key, default_pct)) / 100

    limits = RiskLimits(
        max_order_notional=cap("max_order_notional", "max_order_pct", 10),
        max_daily_notional=cap("max_daily_notional", "max_daily_pct", 30),
        max_open_positions=int(risk["max_open_positions"]),
        daily_loss_limit=cap("daily_loss_limit", "daily_loss_pct", 5),
        symbol_allowlist=symbols,
        allow_short=bool(risk.get("allow_short", False)),
        slippage_bps=float(risk.get("slippage_bps", 50)),
    )

    paper = raw.get("paper") or {}
    paths_raw = raw.get("paths") or {}

    config = Config(
        mode=mode,
        limits=limits,
        paths=Paths(
            journal=_resolve(root, paths_raw.get("journal", "var/journal.jsonl")),
            halt=_resolve(root, paths_raw.get("halt", "var/HALT")),
            publish=_resolve(root, paths_raw.get("publish", "site/data/state.json")),
        ),
        symbols=symbols,
        paper_starting_cash=float(paper.get("starting_cash", 100_000)),
        paper_slippage_bps=float(paper.get("slippage_bps", 10)),
        paper_fee_bps=float(paper.get("fee_bps", 10)),
        quote_currency=str(paper.get("quote_currency", "USD")),
        desk=dict(raw.get("desk") or {}),
    )

    _warn_if_caps_are_unreachable(config, raw)
    if config.is_live:
        _require_live_credentials()
    return config


def _warn_if_caps_are_unreachable(config: Config, raw: dict) -> None:
    """Say so when the caps make trading impossible.

    A cap far below what any single order could be does not read as a cap. It
    reads as a strategy that never finds anything worth doing, and that
    misdiagnosis cost this project a day of an engine that was working
    perfectly and refusing every order it produced.
    """
    equity = config.paper_starting_cash
    cap = config.limits.max_order_notional
    # The desk's own ceiling, plus the slippage the gate assumes. Checked with
    # slippage included because that is what the gate measures: a cap that the
    # quoted price fits under but the assumed fill does not is still a cap
    # nothing can pass, and the failure looks identical.
    DESK_MAX_FRACTION = 0.08
    largest = equity * DESK_MAX_FRACTION * (1 + config.limits.slippage_bps / 10_000)
    if largest > cap:
        print(
            f"WARNING: the desk's largest order would be {largest:,.0f} "
            f"(incl. {config.limits.slippage_bps:g}bps slippage) against a cap "
            f"of {cap:,.0f}. Its biggest conviction trades will be refused."
        )
    if cap >= equity * 0.01:
        return
    print(
        f"WARNING: max_order_notional is {cap:,.0f} against an account of "
        f"{equity:,.0f} ({cap / equity * 100:.2f}%). Any strategy sizing as a "
        "fraction of cash will have every order refused, and it will look "
        "like the strategy having no views. Raise the cap or lower the "
        "account."
    )


def _require_live_credentials() -> None:
    """Refuse to start live with half a configuration.

    Going live is meant to take more than one edit. Flipping the YAML without
    also swapping the keys and base URL should stop the process, not send an
    order somewhere unexpected.
    """
    missing = [
        name
        for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_BASE_URL")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            f"mode is live but {', '.join(missing)} not set. "
            "Fill in .env before trading real money."
        )
    if "paper" in os.environ.get("ALPACA_BASE_URL", ""):
        raise RuntimeError(
            "mode is live but ALPACA_BASE_URL still points at the paper "
            "endpoint. Set it to https://api.alpaca.markets, or set mode back "
            "to paper."
        )
