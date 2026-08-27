"""Order tickets that a human has to approve before anything executes.

The pattern this borrows: a strategy - model-driven or otherwise - never sends
an order directly. It raises a ticket. A person approves it by id, and only
then does the engine execute. Pre-authorising a model to trade is a different
and much larger decision than approving the specific trade it just proposed,
and the difference should be visible in the code rather than assumed.

Two things make an approval gate real rather than ceremonial:

  - Tickets expire. An approval given twenty minutes late is an approval of a
    price that no longer exists, and crypto can move several percent while you
    read the rationale.
  - The price is re-checked at execution, not just at approval. Even inside the
    window, a ticket raised at one level and filled at another is not the trade
    that was approved. Beyond the tolerance it is refused and reissued rather
    than filled at whatever the market now says.

Stored as one JSON file rather than a database. The whole point is that a
human can read it, and `cat tickets.json` should be enough to see what the
desk is asking for.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Long enough to read a rationale and think, short enough that the quoted
# price still means something.
DEFAULT_TTL_MINUTES = 30
# How far the price may move between raising and executing before the ticket
# stops describing the trade that was approved.
DEFAULT_DRIFT_TOLERANCE_BPS = 100.0

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"
EXPIRED = "expired"
STALE = "stale"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Ticket:
    id: str
    symbol: str
    side: str
    quantity: float
    reference_price: float
    rationale: str
    strategy: str
    created_at: str
    expires_at: str
    status: str = PENDING
    note: str = ""

    @property
    def expired(self) -> bool:
        try:
            # >= rather than >, so an expiry exactly equal to now counts as
            # expired. Fail closed on the boundary: the cost is one skipped
            # trade, and the alternative is a race whose outcome depends on
            # microseconds.
            return _now() >= datetime.fromisoformat(self.expires_at)
        except (TypeError, ValueError):
            # An unparseable expiry is treated as expired. Failing closed on a
            # corrupt ticket costs one skipped trade; failing open executes an
            # order of unknown age.
            return True

    def drift_bps(self, price: float) -> float:
        if not self.reference_price:
            return float("inf")
        return abs(price - self.reference_price) / self.reference_price * 10_000


@dataclass
class TicketBook:
    path: Path
    ttl_minutes: float = DEFAULT_TTL_MINUTES
    drift_tolerance_bps: float = DEFAULT_DRIFT_TOLERANCE_BPS
    _tickets: dict[str, Ticket] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.load()

    # -- persistence ---------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # A corrupt book must not execute anything. Starting empty means
            # pending approvals are lost, which is the safe direction.
            self._tickets = {}
            return
        self._tickets = {
            key: Ticket(**value)
            for key, value in (raw or {}).items()
            if isinstance(value, dict)
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({k: asdict(v) for k, v in self._tickets.items()}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    # -- lifecycle -----------------------------------------------------------

    def raise_ticket(self, symbol, side, quantity, reference_price, rationale, strategy) -> Ticket:
        # Short and typeable: a human reads this off a screen and types it back.
        ticket_id = f"T-{secrets.token_hex(2).upper()}"
        while ticket_id in self._tickets:
            ticket_id = f"T-{secrets.token_hex(2).upper()}"

        created = _now()
        ticket = Ticket(
            id=ticket_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=reference_price,
            rationale=rationale,
            strategy=strategy,
            created_at=created.isoformat(),
            expires_at=(created + timedelta(minutes=self.ttl_minutes)).isoformat(),
        )
        self._tickets[ticket_id] = ticket
        self.save()
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id.upper())

    def set_status(self, ticket_id: str, status: str, note: str = "") -> Ticket | None:
        ticket = self.get(ticket_id)
        if ticket is None:
            return None
        ticket.status = status
        if note:
            ticket.note = note
        self.save()
        return ticket

    def approve(self, ticket_id: str) -> tuple[bool, str]:
        ticket = self.get(ticket_id)
        if ticket is None:
            return False, f"no ticket {ticket_id}"
        if ticket.status != PENDING:
            return False, f"{ticket.id} is {ticket.status}, not pending"
        if ticket.expired:
            self.set_status(ticket.id, EXPIRED, "expired before approval")
            return False, f"{ticket.id} expired at {ticket.expires_at}"
        self.set_status(ticket.id, APPROVED)
        return True, f"{ticket.id} approved"

    def reject(self, ticket_id: str, reason: str = "") -> tuple[bool, str]:
        ticket = self.get(ticket_id)
        if ticket is None:
            return False, f"no ticket {ticket_id}"
        if ticket.status not in (PENDING, APPROVED):
            return False, f"{ticket.id} is {ticket.status}"
        self.set_status(ticket.id, REJECTED, reason or "rejected by operator")
        return True, f"{ticket.id} rejected"

    def expire_stale(self) -> list[Ticket]:
        """Mark anything past its window. Called every tick."""
        expired = []
        for ticket in self._tickets.values():
            if ticket.status in (PENDING, APPROVED) and ticket.expired:
                ticket.status = EXPIRED
                ticket.note = "expired before execution"
                expired.append(ticket)
        if expired:
            self.save()
        return expired

    def ready(self, prices: dict) -> tuple[list[Ticket], list[Ticket]]:
        """Approved tickets whose price still holds, and those that drifted."""
        executable, drifted = [], []
        for ticket in self._tickets.values():
            if ticket.status != APPROVED or ticket.expired:
                continue
            price = prices.get(ticket.symbol)
            if price is None:
                continue  # Cannot verify, so do not execute.
            if ticket.drift_bps(price) > self.drift_tolerance_bps:
                ticket.status = STALE
                ticket.note = (
                    f"price moved {ticket.drift_bps(price):.0f}bps from "
                    f"{ticket.reference_price} to {price} before execution"
                )
                drifted.append(ticket)
            else:
                executable.append(ticket)
        if drifted:
            self.save()
        return executable, drifted

    def pending(self) -> list[Ticket]:
        return [t for t in self._tickets.values() if t.status == PENDING and not t.expired]

    def all(self) -> list[Ticket]:
        return list(self._tickets.values())

    def prune(self, keep: int = 50) -> None:
        """Keep the file readable. Settled tickets are history, not state."""
        settled = [t for t in self._tickets.values() if t.status not in (PENDING, APPROVED)]
        if len(settled) <= keep:
            return
        settled.sort(key=lambda t: t.created_at)
        for ticket in settled[: len(settled) - keep]:
            self._tickets.pop(ticket.id, None)
        self.save()
