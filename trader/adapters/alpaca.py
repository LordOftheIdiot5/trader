"""Stocks, via Alpaca.

Alpaca keys cannot move money to a bank - ACH transfer is a separate
authenticated flow the trading API does not expose - so a trading key is
already withdrawal-incapable. That is a property of their API, not something
this file enforces, which is why the paper/live distinction below is treated
as the dangerous part.

The clients are injectable so the adapter can be tested without credentials
and without touching the network. Nothing here reaches for os.environ except
`from_env`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .base import Fill, Position

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"


class AlpacaVenue:
    name = "alpaca"

    def __init__(self, trading_client, data_client, *, paper: bool) -> None:
        self._trading = trading_client
        self._data = data_client
        self.paper = paper

    @classmethod
    def from_env(cls) -> "AlpacaVenue":
        """Build from .env, refusing anything ambiguous.

        The base URL is the only thing separating practice from real money, so
        it is parsed strictly rather than defaulted. An unrecognised host is an
        error: guessing wrong here means guessing with real money.
        """
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        base = (os.environ.get("ALPACA_BASE_URL") or "").strip()

        if not key or not secret:
            raise RuntimeError(
                "ALPACA_API_KEY and ALPACA_API_SECRET must be set. "
                "Copy .env.example to .env and fill it in."
            )
        if PAPER_HOST in base:
            paper = True
        elif LIVE_HOST in base:
            paper = False
        else:
            raise RuntimeError(
                f"ALPACA_BASE_URL is {base!r}, which is neither the paper "
                f"endpoint (https://{PAPER_HOST}) nor the live one "
                f"(https://{LIVE_HOST}). Refusing to guess."
            )

        return cls(
            TradingClient(key, secret, paper=paper),
            StockHistoricalDataClient(key, secret),
            paper=paper,
        )

    def last_price(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest

        response = self._data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol)
        )
        trade = response.get(symbol) if hasattr(response, "get") else response[symbol]
        price = float(getattr(trade, "price", 0) or 0)
        if price <= 0:
            raise ValueError(f"Alpaca returned no last price for {symbol}")
        return price

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for position in self._trading.get_all_positions():
            out[position.symbol] = Position(
                symbol=position.symbol,
                quantity=float(position.qty),
                average_price=float(position.avg_entry_price),
            )
        return out

    def cash(self) -> float:
        return float(self._trading.get_account().cash)

    def equity(self) -> float:
        return float(self._trading.get_account().equity)

    def market_order(self, symbol: str, side: str, quantity: float) -> Fill:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if side not in ("buy", "sell"):
            raise ValueError(f"unknown side {side!r}")
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")

        order = self._trading.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )

        # A market order accepted outside market hours is queued, not filled.
        # Reporting a zero-price fill would let the engine book a trade that
        # has not happened, so an unfilled order is an error here.
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        filled_price = float(getattr(order, "filled_avg_price", 0) or 0)
        if filled_qty <= 0 or filled_price <= 0:
            raise ValueError(
                f"Alpaca accepted order {getattr(order, 'id', '?')} for {symbol} "
                f"but it is not filled (status={getattr(order, 'status', '?')}). "
                "Queued orders are not booked; check market hours."
            )

        return Fill(
            order_id=str(getattr(order, "id", "")),
            symbol=symbol,
            side=side,
            quantity=filled_qty,
            price=filled_price,
            # Alpaca is commission-free on US equities; venue fees, if any,
            # arrive on the monthly statement rather than per fill.
            fee=0.0,
            filled_at=getattr(order, "filled_at", None) or datetime.now(timezone.utc),
            venue=self.name,
        )
