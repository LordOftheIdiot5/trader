"""The one interface every venue has to satisfy.

Keeping this narrow is what lets the paper broker, Alpaca and any ccxt
exchange be swapped by config rather than by rewrite. If a method here starts
needing venue-specific arguments, that is a sign it belongs in the adapter
rather than in the protocol.

Note what is deliberately absent: there is no withdraw, no transfer, no
deposit. The interface cannot express moving money off a venue, so no
strategy and no bug in this codebase can ask for it. The API keys are scoped
to forbid it as well; this is the second lock, not the only one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Fill:
    """What actually happened, as reported by the venue."""

    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    venue: str

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.price)


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float  # signed; negative is short
    average_price: float

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.average_price)


@runtime_checkable
class Venue(Protocol):
    """A place orders can be sent. Read, quote, trade. Nothing else."""

    name: str

    def last_price(self, symbol: str) -> float:
        """Most recent trade price. Raises if the symbol is unknown."""

    def positions(self) -> dict[str, Position]:
        """Current holdings keyed by symbol."""

    def cash(self) -> float:
        """Settled buying power in the account's quote currency."""

    def market_order(self, symbol: str, side: str, quantity: float) -> Fill:
        """Place a market order and return the resulting fill.

        Implementations must raise on rejection rather than returning a
        partial or zero-quantity Fill, so a caller can never mistake a
        refusal for an execution.
        """
