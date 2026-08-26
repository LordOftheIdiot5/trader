"""Review and approve the tickets the desk has raised.

    python scripts/approve.py                 # list what is waiting
    python scripts/approve.py T-4F2A          # approve one
    python scripts/approve.py T-4F2A --reject "sizing too aggressive"
    python scripts/approve.py --all           # show settled ones too

Approving here does not execute anything. The engine picks up approved tickets
on its next tick and re-checks the price before sending, so a ticket approved
just as the market moves is refused rather than filled at a level nobody
agreed to.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import config as config_module
from trader.tickets import PENDING, TicketBook


def age(stamp: str) -> str:
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "?"
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes}m ago" if minutes < 90 else f"{minutes // 60}h ago"


def show(tickets, title: str) -> None:
    if not tickets:
        print(f"{title}: none")
        return
    print(f"\n{title}:")
    for t in sorted(tickets, key=lambda x: x.created_at, reverse=True):
        notional = abs(t.quantity * t.reference_price)
        print(f"\n  {t.id}  [{t.status}]  raised {age(t.created_at)}")
        print(f"    {t.side.upper()} {t.quantity} {t.symbol} @ ~{t.reference_price:,.4f}"
              f"   ({notional:,.2f} notional)")
        print(f"    why: {t.rationale[:300]}")
        if t.note:
            print(f"    note: {t.note}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticket", nargs="?", help="ticket id to approve")
    parser.add_argument("--reject", metavar="REASON", help="reject instead of approving")
    parser.add_argument("--all", action="store_true", help="include settled tickets")
    args = parser.parse_args()

    config = config_module.load()
    book = TicketBook(
        path=config.paths.journal.parent / "tickets.json",
        ttl_minutes=float(config.desk.get("ticket_ttl_minutes", 30)),
    )
    book.expire_stale()

    if not args.ticket:
        show(book.pending(), "Waiting for approval")
        if args.all:
            settled = [t for t in book.all() if t.status != PENDING]
            show(settled, "Settled")
        print("\nApprove with:  python scripts/approve.py <TICKET-ID>")
        return 0

    if args.reject:
        ok, message = book.reject(args.ticket, args.reject)
    else:
        ok, message = book.approve(args.ticket)
    print(message)
    if ok and not args.reject:
        print("The engine will execute it on the next tick, if the price still holds.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
