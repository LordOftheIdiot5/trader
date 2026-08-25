"""What a strategy is, and what it is allowed to see.

A strategy is a pure-ish function: given prices, positions and cash, return a
list of OrderIntents. It does not place orders, it does not know which venue
is real, and it cannot see a credential. Everything it returns still has to
survive the risk gate, so a strategy bug costs at most one capped order.

Keeping it this narrow is deliberate. The moment a strategy can call a venue
directly there are two paths to a live order, and only one of them has the
caps on it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .adapters.base import Position
from .risk import OrderIntent


@dataclass(frozen=True)
class Context:
    """Everything a strategy gets to look at on one tick."""

    now: datetime
    prices: dict[str, float]
    positions: dict[str, Position]
    cash: float
    venue: str
    history: "PriceHistory"

    def held(self, symbol: str) -> float:
        position = self.positions.get(symbol)
        return position.quantity if position else 0.0


class PriceHistory:
    """A bounded ring of recent prices per symbol.

    Bounded on purpose: a long-running process that accumulates every tick
    forever eventually dies of memory, and no indicator here needs more than
    the last few hundred points. Old points fall off silently.
    """

    def __init__(self, maxlen: int = 500) -> None:
        self.maxlen = maxlen
        self._series: dict[str, deque[float]] = {}

    def record(self, symbol: str, price: float) -> None:
        if price <= 0:
            # A dead feed reporting zero would drag every average down and
            # look like a crash. Drop it rather than poison the series.
            return
        series = self._series.setdefault(symbol, deque(maxlen=self.maxlen))
        series.append(float(price))

    def series(self, symbol: str) -> list[float]:
        return list(self._series.get(symbol, ()))

    def mean(self, symbol: str, window: int) -> float | None:
        """Mean of the last `window` points, or None if there are not enough.

        None rather than a partial average: an indicator computed over three
        points when it wants thirty is not a weaker signal, it is a different
        and meaningless one.
        """
        if window <= 0:
            raise ValueError("window must be positive")
        series = self._series.get(symbol)
        if series is None or len(series) < window:
            return None
        recent = list(series)[-window:]
        return sum(recent) / window

    def __len__(self) -> int:
        return len(self._series)


@runtime_checkable
class Strategy(Protocol):
    name: str

    def decide(self, context: Context) -> list[OrderIntent]:
        """Return the orders this strategy wants placed on this tick.

        An empty list is the normal, expected answer most of the time. A
        strategy that always wants to trade is usually a strategy that has
        confused activity with edge.
        """
