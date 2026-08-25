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
    limits = RiskLimits(
        max_order_notional=float(risk["max_order_notional"]),
        max_daily_notional=float(risk["max_daily_notional"]),
        max_open_positions=int(risk["max_open_positions"]),
        daily_loss_limit=float(risk["daily_loss_limit"]),
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
    )

    if config.is_live:
        _require_live_credentials()
    return config


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
