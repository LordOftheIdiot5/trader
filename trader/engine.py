"""Routes an order intent from a strategy to a venue, through the risk gate.

The whole design rests on one rule: a strategy never touches a venue. It
produces an OrderIntent and hands it here. That way there is exactly one code
path to a live order, and exactly one place the caps are enforced. Adding a
second path is the bug this structure exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .adapters.base import Fill, Venue
from .journal import Journal
from .risk import OrderIntent, RiskGuard, RiskRejection


class Engine:
    def __init__(
        self,
        venues: dict[str, Venue],
        guard: RiskGuard,
        journal: Journal,
    ) -> None:
        if not venues:
            raise ValueError("no venues configured")
        self.venues = venues
        self.guard = guard
        self.journal = journal

    def submit(self, intent: OrderIntent) -> Fill | None:
        """Send an order, or record why it was refused.

        Returns the Fill on success, None on refusal. Refusals are the normal
        case rather than an exception, because a strategy hitting its cap
        should keep running rather than crash the process.
        """
        venue = self.venues.get(intent.venue)
        if venue is None:
            self.journal.rejection(
                intent.symbol,
                intent.side,
                intent.quantity,
                f"unknown venue {intent.venue!r}; configured: {sorted(self.venues)}",
            )
            return None

        positions = venue.positions()
        held = positions.get(intent.symbol)

        try:
            self.guard.check(
                intent,
                open_positions=len(positions),
                position_qty=held.quantity if held else 0.0,
                now=datetime.now(timezone.utc),
            )
        except RiskRejection as rejection:
            self.journal.rejection(
                intent.symbol, intent.side, intent.quantity, str(rejection)
            )
            return None

        try:
            fill = venue.market_order(intent.symbol, intent.side, intent.quantity)
        except Exception as error:
            # A venue refusing is information, not a crash. Record it and let
            # the caller decide whether to retry.
            self.journal.rejection(
                intent.symbol,
                intent.side,
                intent.quantity,
                f"venue {intent.venue} rejected: {error}",
            )
            return None

        realised = self._realised_since(venue)
        self.guard.record_fill(notional=fill.notional, realised_pnl=realised)
        self.journal.fill(fill, strategy=intent.strategy)
        return fill

    def _realised_since(self, venue: Venue) -> float:
        """Realised P&L booked by the venue on the order just placed.

        Only the venue knows the average price a sale closed out against, so
        the delta is read from it rather than recomputed here. Venues that do
        not track it contribute nothing, which keeps the loss limit
        conservative instead of optimistic.
        """
        total = getattr(venue, "realised_pnl", None)
        if total is None:
            return 0.0
        seen = getattr(self, "_realised_seen", {})
        previous = seen.get(venue.name, 0.0)
        seen[venue.name] = total
        self._realised_seen = seen
        return total - previous

    def snapshot(self) -> dict:
        """Everything the dashboard needs. No credentials, by construction."""
        venues: dict[str, dict] = {}
        for name, venue in self.venues.items():
            try:
                positions = {
                    symbol: {
                        "quantity": position.quantity,
                        "average_price": round(position.average_price, 6),
                    }
                    for symbol, position in venue.positions().items()
                }
                equity = getattr(venue, "equity", None)
                venues[name] = {
                    "reachable": True,
                    "cash": round(venue.cash(), 2),
                    "equity": round(equity(), 2) if callable(equity) else None,
                    "positions": positions,
                }
            except Exception as error:
                # A venue being down must show as down, not as flat.
                venues[name] = {"reachable": False, "error": str(error)}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "risk": self.guard.snapshot(),
            "venues": venues,
            "recent": self.journal.entries(limit=50),
        }
