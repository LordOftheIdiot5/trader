"""Perpetual-futures context from Hyperliquid, without an account.

Funding rate and open interest are public, free, and say something the spot
price cannot: how the leveraged side of the market is positioned.

  - Funding is a crowding signal. Positive funding means longs are paying
    shorts to hold the position, which happens when the long side is crowded.
    Extreme funding has historically preceded squeezes in both directions -
    not because the number predicts anything, but because it measures how many
    people are already in the trade and how much it costs them to stay.
  - Open interest is how much leverage is riding on the move. A price rise on
    rising OI is new money; the same rise on falling OI is people closing
    shorts, which is a different thing wearing the same clothes.

This is the first input the desk gets that is about positioning rather than
price. That matters more than another price feed would: the desk already had
prices and correctly concluded they were flat.

Read-only and keyless. Hyperliquid as an actual venue is a separate step and a
much bigger one - it is perps, so it brings leverage and shorting, and neither
belongs anywhere near this system until the desk has demonstrated it can be
right about something without them.
"""

from __future__ import annotations

import time

# Hyperliquid quotes perps as BASE/USDC:USDC. Our symbols are spot-style
# pairs, so the base asset is the only part that carries over.
def to_perp(symbol: str) -> str | None:
    if "/" not in symbol:
        return None  # A stock ticker has no perp.
    base = symbol.split("/")[0].upper()
    return f"{base}/USDC:USDC"


class PerpContext:
    """Funding and open interest per symbol, cached briefly.

    Funding settles every eight hours, so re-fetching it every five-minute
    tick is asking a free public endpoint for an answer that has not changed.
    """

    def __init__(self, cache_seconds: float = 600.0) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict]] = {}
        self._client = None

    def _exchange(self):
        if self._client is None:
            import ccxt

            self._client = ccxt.hyperliquid({"enableRateLimit": True})
        return self._client

    def _fetch(self, perp: str) -> dict:
        exchange = self._exchange()
        out: dict = {}

        try:
            funding = exchange.fetch_funding_rate(perp)
            rate = funding.get("fundingRate")
            if rate is not None:
                out["funding_rate"] = float(rate)
                # Hyperliquid funds hourly; the annualised figure is the one a
                # human (or a model) can actually judge as large or small.
                out["funding_annualised_pct"] = round(float(rate) * 24 * 365 * 100, 2)
                out["next_funding"] = funding.get("fundingDatetime")
        except Exception:
            pass

        try:
            interest = exchange.fetch_open_interest(perp)
            value = interest.get("openInterestAmount") or interest.get("openInterestValue")
            if value is not None:
                out["open_interest"] = float(value)
        except Exception:
            pass

        return out

    def for_symbol(self, symbol: str) -> dict:
        """Perp context for one symbol. Empty dict if unavailable, never raises.

        Every caller treats this as a bonus. A desk that breaks because a free
        public endpoint is slow is a worse desk than one that reasons without
        the extra field.
        """
        perp = to_perp(symbol)
        if perp is None:
            return {}

        now = time.monotonic()
        cached = self._cache.get(perp)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]

        try:
            data = self._fetch(perp)
        except Exception:
            return {}
        self._cache[perp] = (now, data)
        return data
