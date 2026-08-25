"""Crypto, via ccxt.

ccxt speaks one dialect to ~100 exchanges, so which venue this trades on is a
line in .env rather than a rewrite. Kraken, Coinbase and Binance all behave
the same from here.

The withdrawal lock is not in this file and cannot be. ccxt exposes a
`withdraw` method on every exchange; this adapter simply never calls it, and
the Venue protocol has no way to ask for it. What actually stops a withdrawal
is the key scope set on the exchange - trade enabled, withdraw disabled, IP
bound. `verify_no_withdrawal_permission` below checks that as best the API
allows, so a misconfigured key is caught at startup rather than discovered
later.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .base import Fill, Position


class ExchangeVenue:
    def __init__(self, client, *, quote_currency: str = "USD") -> None:
        self._client = client
        self.name = getattr(client, "id", "exchange")
        self.quote_currency = quote_currency

    @classmethod
    def from_env(cls, quote_currency: str = "USD") -> "ExchangeVenue":
        import ccxt

        exchange_id = (os.environ.get("EXCHANGE_ID") or "").strip()
        if not exchange_id:
            raise RuntimeError("EXCHANGE_ID is not set (e.g. kraken, coinbase, binance)")
        if not hasattr(ccxt, exchange_id):
            raise RuntimeError(f"ccxt has no exchange {exchange_id!r}")

        key = os.environ.get("EXCHANGE_API_KEY")
        secret = os.environ.get("EXCHANGE_API_SECRET")
        if not key or not secret:
            raise RuntimeError(
                "EXCHANGE_API_KEY and EXCHANGE_API_SECRET must be set. "
                "Create the key with trading enabled and withdrawals DISABLED."
            )

        credentials = {"apiKey": key, "secret": secret, "enableRateLimit": True}
        passphrase = os.environ.get("EXCHANGE_PASSPHRASE")
        if passphrase:
            # Coinbase and OKX require this; most others reject it.
            credentials["password"] = passphrase

        return cls(getattr(ccxt, exchange_id)(credentials), quote_currency=quote_currency)

    def verify_no_withdrawal_permission(self) -> str:
        """Best-effort check that this key cannot withdraw.

        Exchanges do not expose key scopes uniformly, so this cannot be a
        guarantee - it returns what it managed to determine rather than
        pretending to certainty. Treat a "could not determine" as a prompt to
        check the exchange's own key settings page, not as a pass.
        """
        try:
            permissions = self._client.fetch_permissions()  # not universally implemented
        except Exception:
            return (
                f"{self.name}: could not read key permissions via the API. "
                "Confirm on the exchange that withdrawals are disabled for this key."
            )

        flat = repr(permissions).lower()
        if "withdraw" in flat and "false" not in flat:
            return f"{self.name}: WARNING - this key may have withdrawal permission."
        return f"{self.name}: no withdrawal permission detected."

    def last_price(self, symbol: str) -> float:
        ticker = self._client.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price or float(price) <= 0:
            raise ValueError(f"{self.name} returned no last price for {symbol}")
        return float(price)

    def positions(self) -> dict[str, Position]:
        """Spot balances, expressed as positions in SYMBOL/QUOTE pairs.

        Spot holdings carry no cost basis on the exchange side, so the average
        price is marked at the current price. That makes unrealised P&L read
        as zero here rather than as a fabricated number; realised P&L still
        comes from the journal, which does know what was paid.
        """
        balances = self._client.fetch_balance()
        totals = balances.get("total") or {}
        out: dict[str, Position] = {}
        for asset, amount in totals.items():
            if not amount or asset == self.quote_currency:
                continue
            symbol = f"{asset}/{self.quote_currency}"
            try:
                price = self.last_price(symbol)
            except Exception:
                # An asset with no pair against our quote currency is still
                # held; showing it at zero would understate the account.
                continue
            out[symbol] = Position(symbol, float(amount), price)
        return out

    def cash(self) -> float:
        balances = self._client.fetch_balance()
        free = balances.get("free") or {}
        return float(free.get(self.quote_currency, 0) or 0)

    def market_order(self, symbol: str, side: str, quantity: float) -> Fill:
        if side not in ("buy", "sell"):
            raise ValueError(f"unknown side {side!r}")
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")

        order = self._client.create_order(symbol, "market", side, quantity)

        price = order.get("average") or order.get("price")
        filled = order.get("filled") or order.get("amount") or 0
        if not price or float(price) <= 0:
            # Some venues return the order before the fill is priced. Ask
            # again rather than booking a trade at an unknown price.
            fetched = self._client.fetch_order(order["id"], symbol)
            price = fetched.get("average") or fetched.get("price")
            filled = fetched.get("filled") or filled
        if not price or float(price) <= 0 or float(filled) <= 0:
            raise ValueError(
                f"{self.name} accepted order {order.get('id')} for {symbol} but "
                "reported no fill price. Not booking an unpriced trade."
            )

        fee_info = order.get("fee") or {}
        return Fill(
            order_id=str(order.get("id", "")),
            symbol=symbol,
            side=side,
            quantity=float(filled),
            price=float(price),
            fee=float(fee_info.get("cost") or 0),
            filled_at=datetime.now(timezone.utc),
            venue=self.name,
        )
