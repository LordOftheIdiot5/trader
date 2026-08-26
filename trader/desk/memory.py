"""What the desk remembers about its own past calls.

Until now every run started from nothing. The desk reasoned freshly each hour
about a market it had already formed views on, with no idea whether those
views had been any good. That is not a desk, it is the same analyst rehired
hourly with amnesia.

Built in code from the journal rather than by another model call. The journal
already holds every decision, fill and refusal; summarising it is arithmetic,
and spending a call to have a model read its own diary would be paying for
something a loop does better. The judgment part - what the record *means* -
is left to the seats, which is the part they are actually good at.

One deliberate choice: outcomes are reported without a verdict attached. Saying
"this call lost money" invites the desk to over-correct on one sample, which is
how a strategy turns into a series of reactions to its last mistake. The record
is stated; the interpreting is the chair's job.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Far enough back to show a pattern, short enough that the desk is not
# reasoning about a market that no longer exists.
LOOKBACK_HOURS = 72
MAX_DECISIONS = 8
MAX_FILLS = 10


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def build(journal, positions: dict, prices: dict, lookback_hours: int = LOOKBACK_HOURS) -> dict:
    """Summarise what the desk has said and done recently.

    `positions` and `prices` are needed to mark open positions to market: a
    decision is not resolved until it is closed, and an unrealised number is
    the only honest thing to say about one that is still running.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        entries = journal.entries()
    except Exception:
        # No memory is survivable. A crash reading the diary is not.
        return {"available": False}

    recent = []
    for entry in entries:
        at = _parse(entry.get("at", ""))
        if at is not None and at >= cutoff:
            recent.append(entry)

    said = [
        {"at": e["at"], "summary": e.get("message", "")}
        for e in recent
        if e.get("kind") == "note" and e.get("message", "").startswith("desk:")
    ][-MAX_DECISIONS:]

    fills = [
        {
            "at": e["at"],
            "symbol": e.get("symbol"),
            "side": e.get("side"),
            "quantity": e.get("quantity"),
            "price": e.get("price"),
        }
        for e in recent
        if e.get("kind") == "fill"
    ][-MAX_FILLS:]

    refused = [
        {"at": e["at"], "symbol": e.get("symbol"), "reason": e.get("reason", "")[:200]}
        for e in recent
        if e.get("kind") == "rejected"
    ][-MAX_FILLS:]

    # Mark open positions. An open position is an unresolved decision, and the
    # desk should see how its live calls are actually doing.
    open_positions = []
    for symbol, position in (positions or {}).items():
        price = (prices or {}).get(symbol)
        entry_price = getattr(position, "average_price", None)
        quantity = getattr(position, "quantity", 0)
        marked = None
        if price and entry_price:
            marked = round((price - entry_price) * quantity, 2)
        open_positions.append({
            "symbol": symbol,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": price,
            "unrealised": marked,
        })

    return {
        "available": True,
        "lookback_hours": lookback_hours,
        "runs_recorded": len(said),
        "what_the_desk_said": said,
        "fills": fills,
        "orders_refused_by_risk_limits": refused,
        "open_positions": open_positions,
    }


def describe(memory: dict) -> str:
    """Render the digest for a prompt, or an empty string if there is nothing.

    Empty rather than "no history available" on purpose: a line saying there
    is no history is still a line the model reasons about, and on a first run
    it invites speculation about why.
    """
    if not memory.get("available"):
        return ""
    if not any((memory["what_the_desk_said"], memory["fills"], memory["open_positions"])):
        return ""

    lines = [f"The desk's own record, last {memory['lookback_hours']} hours:"]

    if memory["open_positions"]:
        lines.append("\nStill open (an unresolved decision):")
        for p in memory["open_positions"]:
            mark = "unknown" if p["unrealised"] is None else f"{p['unrealised']:+,.2f}"
            lines.append(
                f"  {p['symbol']}: {p['quantity']} at {p['entry_price']}, "
                f"now {p['current_price']}, unrealised {mark}"
            )

    if memory["fills"]:
        lines.append("\nOrders filled:")
        for f in memory["fills"]:
            lines.append(f"  {f['at'][:16]}  {f['side']} {f['quantity']} {f['symbol']} @ {f['price']}")

    if memory["orders_refused_by_risk_limits"]:
        lines.append("\nOrders the risk limits refused:")
        for r in memory["orders_refused_by_risk_limits"]:
            lines.append(f"  {r['at'][:16]}  {r['symbol']}: {r['reason']}")

    if memory["what_the_desk_said"]:
        lines.append("\nWhat the desk concluded on previous runs:")
        for s in memory["what_the_desk_said"]:
            lines.append(f"  {s['at'][:16]}  {s['summary'][:300]}")

    lines.append(
        "\nThis is the record, not a verdict. One losing call is not evidence "
        "of a bad process and one winner is not evidence of a good one; say so "
        "if you think the sample is too small to read anything into."
    )
    return "\n".join(lines)
