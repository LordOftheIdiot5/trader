"""An append-only record of everything that happened.

Append-only on purpose. When a run goes wrong the first question is always
"what did it actually do, in what order", and a log that can be rewritten in
place cannot answer that. Each line is one self-contained JSON object, so a
truncated write costs the last entry rather than the whole file.

Nothing written here may contain a credential. The dashboard publishes from
this file, and the dashboard is a public web page.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .adapters.base import Fill

# Keys that must never appear in a journal entry, checked on the way in
# rather than hoped about later.
_FORBIDDEN = ("key", "secret", "token", "password", "passphrase", "apikey")


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, kind: str, payload: dict) -> None:
        flat = json.dumps(payload).lower()
        for banned in _FORBIDDEN:
            if banned in flat:
                raise ValueError(
                    f"refusing to journal an entry containing {banned!r}; "
                    "the journal is published publicly"
                )
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def fill(self, fill: Fill, strategy: str) -> None:
        self._append(
            "fill",
            {
                "order_id": fill.order_id,
                "venue": fill.venue,
                "symbol": fill.symbol,
                "side": fill.side,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "notional": round(fill.notional, 2),
                "strategy": strategy,
            },
        )

    def rejection(self, symbol: str, side: str, quantity: float, reason: str) -> None:
        """Refusals are recorded too.

        A strategy quietly having every order denied looks identical to a
        strategy that decided not to trade, unless the denials are written
        down.
        """
        self._append(
            "rejected",
            {"symbol": symbol, "side": side, "quantity": quantity, "reason": reason},
        )

    def note(self, message: str, **fields) -> None:
        self._append("note", {"message": message, **fields})

    def entries(self, limit: int | None = None) -> list[dict]:
        """Read entries back, newest last. Skips any corrupt line."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # A half-written final line is expected after a crash.
                    continue
        return out[-limit:] if limit else out

    def fills(self) -> Iterator[dict]:
        return (entry for entry in self.entries() if entry.get("kind") == "fill")
