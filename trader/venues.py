"""Builds the set of venues the engine will route to.

One place decides what is real. In paper mode every venue is the simulator,
fed by live public market data; in live mode they are the real adapters. A
strategy cannot tell the difference, which is the point - the same code path
runs in both, so practice actually rehearses production.
"""

from __future__ import annotations

import os

import ccxt

from .adapters.paper import PaperVenue
from .config import Config

# Public market data needs no credentials, so paper mode runs on a clean
# machine with nothing configured.
PUBLIC_PRICE_EXCHANGE = "kraken"


def _public_price_source(exchange_id: str = PUBLIC_PRICE_EXCHANGE):
    """Route each symbol to a feed that can actually quote it.

    A crypto exchange has no opinion on AAPL, so asking one for a stock price
    fails on every tick. Pairs (BTC/USD) go to ccxt; bare tickers (AAPL) go to
    Alpaca's market data API, which works with paper keys and needs no funded
    account. Without those keys stocks simply have no price in paper mode,
    which the strategy treats as "no opinion" rather than as a crash.
    """
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    stock_data = None
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET"):
        from alpaca.data.historical import StockHistoricalDataClient

        stock_data = StockHistoricalDataClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"]
        )

    def source(symbol: str) -> float:
        if "/" in symbol:
            ticker = exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if not price:
                raise ValueError(f"{exchange_id} returned no last price for {symbol}")
            return float(price)

        if stock_data is None:
            raise ValueError(
                f"{symbol} is a stock and no Alpaca keys are set, so it cannot "
                "be quoted in paper mode. Add ALPACA_API_KEY/SECRET to .env "
                "(paper keys are enough) or remove it from config.yaml."
            )
        from alpaca.data.requests import StockLatestTradeRequest

        response = stock_data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol)
        )
        trade = response.get(symbol) if hasattr(response, "get") else response[symbol]
        price = float(getattr(trade, "price", 0) or 0)
        if price <= 0:
            raise ValueError(f"Alpaca returned no last price for {symbol}")
        return price

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
