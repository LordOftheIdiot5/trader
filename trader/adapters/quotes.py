"""Stock prices with no account, no key and no KYC.

Alpaca gates API access behind account verification, which is a real obstacle
if you only want to watch a paper strategy think. This uses Yahoo's public
chart endpoint instead - the same source stockwatch already runs on, so the
ticker conventions carry over:

    EQNR.OL   Oslo          VOLV-B.ST  Stockholm
    SAP.DE    XETRA         MC.PA      Paris
    SHEL.L    London        NOVO-B.CO  Copenhagen
    NOKIA.HE  Helsinki      AAPL       US (no suffix)

Prices are delayed by roughly fifteen minutes on most venues. That is fine for
a strategy on a five-minute tick and useless for anything faster - if you ever
want intraday precision this is the piece to replace, not the engine.

Stdlib only, deliberately: this is the one component that has to work on a
fresh VPS before anything else is configured.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"
# Yahoo refuses requests without a plausible user agent.
USER_AGENT = "Mozilla/5.0 (compatible; trader/1.0; +https://trader.nordl.dev)"
TIMEOUT = 20


class YahooQuotes:
    """Last price per ticker, cached briefly.

    The cache is not an optimisation so much as manners: a strategy watching
    six symbols on a fast loop would otherwise hit a free public endpoint six
    times a tick and get itself rate limited.
    """

    def __init__(self, cache_seconds: float = 30.0) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, float]] = {}

    def _fetch(self, ticker: str) -> float:
        url = f"{ENDPOINT}{urllib.parse.quote(ticker)}?range=1d&interval=1m"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read())

        result = (payload.get("chart") or {}).get("result")
        if not result:
            error = (payload.get("chart") or {}).get("error")
            raise ValueError(f"Yahoo has no data for {ticker}: {error}")

        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if not price:
            # Outside trading hours some tickers only carry a previous close.
            price = meta.get("previousClose")
        if not price or float(price) <= 0:
            raise ValueError(f"Yahoo returned no usable price for {ticker}")
        return float(price)

    def last_price(self, ticker: str) -> float:
        now = time.monotonic()
        cached = self._cache.get(ticker)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]
        price = self._fetch(ticker)
        self._cache[ticker] = (now, price)
        return price

    # So this can be handed straight to PaperVenue as a price_source.
    def __call__(self, ticker: str) -> float:
        return self.last_price(ticker)
