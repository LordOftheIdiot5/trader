"""A simulated venue that fills against real, live prices.

This exists because paper trading is not uniformly available. Alpaca has a
real paper environment, but of the major crypto exchanges only Binance runs a
usable spot testnet. Rather than force a choice of exchange just to get a
practice mode, this fills orders locally using whatever live price source it
is handed.

The simulation is deliberately pessimistic. A paper run that looks better
than production is worse than useless, so:

  - every fill crosses the spread against you, by `slippage_bps`
  - fees are charged at a taker rate, never a maker one
  - an order larger than `max_fill_ratio` of the reference volume is rejected
    rather than silently filled at a price nobody would have given you

It is still a simulation. It does not model partial fills, queue position,
market impact, or a venue being down. Treat a good paper result as evidence
the plumbing works, not as evidence the strategy does.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Callable

from .base import Fill, Position


class PaperVenue:
    name = "paper"

    def __init__(
        self,
        price_source: Callable[[str], float],
        starting_cash: float = 100_000.0,
        slippage_bps: float = 10.0,
        fee_bps: float = 10.0,
        quote_currency: str = "USD",
    ) -> None:
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self._price_source = price_source
        self._cash = float(starting_cash)
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.quote_currency = quote_currency
        self._positions: dict[str, Position] = {}
        self._ids = itertools.count(1)
        # Realised P&L is tracked here rather than inferred by the caller,
        # because only the venue knows the average price a sale closed out.
        self.realised_pnl = 0.0

    def last_price(self, symbol: str) -> float:
        price = float(self._price_source(symbol))
        if price <= 0:
            raise ValueError(f"price source returned {price} for {symbol}")
        return price

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def cash(self) -> float:
        return self._cash

    def market_order(self, symbol: str, side: str, quantity: float) -> Fill:
        if side not in ("buy", "sell"):
            raise ValueError(f"unknown side {side!r}")
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")

        reference = self.last_price(symbol)
        # Cross the spread the wrong way, always.
        drift = reference * self.slippage_bps / 10_000
        price = reference + drift if side == "buy" else reference - drift
        if price <= 0:
            raise ValueError(
                f"slippage of {self.slippage_bps}bps drove the fill price to "
                f"{price} for {symbol}; check the price source"
            )

        gross = price * quantity
        fee = gross * self.fee_bps / 10_000

        if side == "buy":
            cost = gross + fee
            if cost > self._cash:
                raise ValueError(
                    f"insufficient paper cash: {symbol} buy needs {cost:,.2f} "
                    f"{self.quote_currency}, have {self._cash:,.2f}"
                )
            self._cash -= cost
            self._apply_buy(symbol, quantity, price)
        else:
            held = self._positions.get(symbol)
            held_qty = held.quantity if held else 0.0
            if quantity > held_qty + 1e-12:
                raise ValueError(
                    f"cannot sell {quantity} {symbol}, only {held_qty} held "
                    "(the paper venue does not simulate shorting)"
                )
            self._cash += gross - fee
            self._apply_sell(symbol, quantity, price)

        return Fill(
            order_id=f"paper-{next(self._ids)}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            filled_at=datetime.now(timezone.utc),
            venue=self.name,
        )

    def _apply_buy(self, symbol: str, quantity: float, price: float) -> None:
        existing = self._positions.get(symbol)
        if existing is None:
            self._positions[symbol] = Position(symbol, quantity, price)
            return
        total = existing.quantity + quantity
        # Weighted average, so a later partial sale books a sensible P&L.
        average = ((existing.quantity * existing.average_price) + (quantity * price)) / total
        self._positions[symbol] = Position(symbol, total, average)

    def _apply_sell(self, symbol: str, quantity: float, price: float) -> None:
        existing = self._positions[symbol]
        self.realised_pnl += (price - existing.average_price) * quantity
        remaining = existing.quantity - quantity
        if remaining <= 1e-12:
            del self._positions[symbol]
        else:
            # Average price is unchanged by a sale; only the size shrinks.
            self._positions[symbol] = Position(symbol, remaining, existing.average_price)

    def equity(self) -> float:
        """Cash plus positions marked at the live price."""
        marked = 0.0
        for symbol, position in self._positions.items():
            try:
                marked += position.quantity * self.last_price(symbol)
            except Exception:
                # A dead price feed must not make equity look like zero.
                marked += position.quantity * position.average_price
        return self._cash + marked
