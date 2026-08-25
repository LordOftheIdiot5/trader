"""Builds the set of venues the engine will route to.

One place decides what is real. In paper mode every venue is the simulator,
fed by live public market data; in live mode they are the real adapters. A
strategy cannot tell the difference, which is the point - the same code path
runs in both, so practice actually rehearses production.
"""

from __future__ import annotations

import ccxt

from .adapters.paper import PaperVenue
from .adapters.quotes import YahooQuotes
from .config import Config

# Public market data needs no credentials, so paper mode runs on a clean
# machine with nothing configured.
PUBLIC_PRICE_EXCHANGE = "kraken"


def _public_price_source(exchange_id: str = PUBLIC_PRICE_EXCHANGE):
    """Route each symbol to a feed that can actually quote it.

    A crypto exchange has no opinion on AAPL, so asking one for a stock price
    fails on every tick. Pairs (BTC/USD) go to ccxt; bare tickers (AAPL) go to
    Yahoo's public chart endpoint, which needs no account at all. That keeps
    paper mode usable on a machine where nothing has been configured and
    nobody has completed a KYC flow.
    """
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    stocks = YahooQuotes()

    def source(symbol: str) -> float:
        if "/" in symbol:
            ticker = exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if not price:
                raise ValueError(f"{exchange_id} returned no last price for {symbol}")
            return float(price)
        return stocks.last_price(symbol)

    return source


def build(config: Config) -> dict:
    """Return venues keyed by the name a strategy uses to address them."""
    if not config.is_live:
        # Both names point at the same simulator so a strategy written
        # against "alpaca" and "crypto" runs unchanged when it goes live.
        paper = PaperVenue(
            price_source=_public_price_source(),
            starting_cash=config.paper_starting_cash,
            slippage_bps=config.paper_slippage_bps,
            fee_bps=config.paper_fee_bps,
            quote_currency=config.quote_currency,
        )
        return {"paper": paper, "alpaca": paper, "crypto": paper}

    from .adapters.alpaca import AlpacaVenue
    from .adapters.exchange import ExchangeVenue

    alpaca = AlpacaVenue.from_env()
    crypto = ExchangeVenue.from_env(quote_currency=config.quote_currency)

    # Surface a misconfigured key at startup rather than on the first order.
    print(crypto.verify_no_withdrawal_permission())
    if not alpaca.paper:
        print("alpaca: LIVE endpoint - orders will use real money.")

    return {"alpaca": alpaca, "crypto": crypto}
