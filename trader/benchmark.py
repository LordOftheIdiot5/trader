"""What doing nothing would have earned.

Without this, every question about the desk is unanswerable. It held for a
week: was that discipline or timidity? It made three trades: did they beat
simply owning the same coins? "The strategy is up 2%" is not information -
up against what, over what period, versus what alternative?

The benchmark is an equal-weight basket of the symbols being watched, bought
once at inception and never touched. That is the honest thing to beat: it is
what you would have had if you had skipped all of this and pressed buy.

Three choices worth stating, because each one could have flattered the desk:

  - The basket pays the same entry slippage and fees the paper venue charges.
    A benchmark that enters at a perfect mid price is not one anyone could
    have had, and the difference compounds into a free head start.
  - The basket is fixed at inception. Adding a symbol later does not
    retroactively buy it, because a benchmark that changes when the watchlist
    changes can be tuned after the fact into whatever you want to beat.
  - Elapsed time is always reported alongside the number. Two days of
    outperformance is noise wearing a percentage sign, and the figure should
    arrive with the context needed to dismiss it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Below this, any comparison is noise. Reported anyway, but labelled.
MEANINGFUL_DAYS = 30


class Benchmark:
    """An equal-weight basket, bought once and held."""

    def __init__(self, path: Path, starting_cash: float,
                 slippage_bps: float = 10.0, fee_bps: float = 10.0) -> None:
        self.path = Path(path)
        self.starting_cash = float(starting_cash)
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.started_at: str | None = None
        self.holdings: dict[str, float] = {}
        self.entry_prices: dict[str, float] = {}
        self.cash_left = 0.0
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # A corrupt benchmark is not worth crashing over, but it must not
            # silently restart at today's prices either - that would erase a
            # drawdown and make the desk look better than it is. Left unstarted
            # so the next tick re-establishes it and the reset is visible in
            # started_at.
            return
        self.started_at = stored.get("started_at")
        self.holdings = {k: float(v) for k, v in (stored.get("holdings") or {}).items()}
        self.entry_prices = {k: float(v) for k, v in (stored.get("entry_prices") or {}).items()}
        self.cash_left = float(stored.get("cash_left", 0.0))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "started_at": self.started_at,
            "holdings": self.holdings,
            "entry_prices": self.entry_prices,
            "cash_left": self.cash_left,
            "starting_cash": self.starting_cash,
        }, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # -- lifecycle -----------------------------------------------------------

    @property
    def started(self) -> bool:
        return bool(self.holdings)

    def start(self, prices: dict[str, float]) -> None:
        """Buy the basket once, at the first tick where prices are available."""
        priceable = {s: p for s, p in prices.items() if p and p > 0}
        if not priceable or self.started:
            return

        per_symbol = self.starting_cash / len(priceable)
        spent = 0.0
        for symbol, price in priceable.items():
            # The same pessimistic entry the paper venue would have charged.
            entry = price * (1 + self.slippage_bps / 10_000)
            gross = per_symbol / (1 + self.fee_bps / 10_000)
            quantity = gross / entry
            self.holdings[symbol] = quantity
            self.entry_prices[symbol] = entry
            spent += per_symbol

        self.cash_left = self.starting_cash - spent
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._save()

    def value(self, prices: dict[str, float]) -> float:
        """Mark the basket to market, holding un-priceable legs at entry."""
        total = self.cash_left
        for symbol, quantity in self.holdings.items():
            price = prices.get(symbol) or self.entry_prices.get(symbol, 0.0)
            total += quantity * price
        return total

    def compare(self, strategy_equity: float, prices: dict[str, float]) -> dict:
        """The comparison, with enough context to be dismissed if it is noise."""
        if not self.started:
            return {"available": False, "reason": "not started yet"}

        held = self.value(prices)
        days = 0.0
        if self.started_at:
            try:
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(self.started_at)
                days = round(elapsed.total_seconds() / 86400, 2)
            except (TypeError, ValueError):
                pass

        benchmark_return = (held / self.starting_cash - 1) * 100
        strategy_return = (strategy_equity / self.starting_cash - 1) * 100

        return {
            "available": True,
            "started_at": self.started_at,
            "days_running": days,
            "basket": sorted(self.holdings),
            "buy_and_hold_value": round(held, 2),
            "buy_and_hold_return_pct": round(benchmark_return, 2),
            "strategy_value": round(strategy_equity, 2),
            "strategy_return_pct": round(strategy_return, 2),
            # The only number that matters: did the work beat not working.
            "excess_return_pct": round(strategy_return - benchmark_return, 2),
            "meaningful": days >= MEANINGFUL_DAYS,
            "caveat": (
                ""
                if days >= MEANINGFUL_DAYS
                else f"only {days:.1f} days - too short to mean anything, "
                     f"treat any difference as noise until about {MEANINGFUL_DAYS}"
            ),
        }
