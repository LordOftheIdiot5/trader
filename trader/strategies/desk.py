"""Adapts the trading desk to the Strategy interface.

This is the seam that matters. The desk is not privileged: it produces
OrderIntents exactly like the crossover does, and they go through
RiskGuard.check before they can reach a venue. A model that talks itself into
a maximum-conviction bet on everything gets refused by the same per-order cap
that refuses a buggy moving average.

Which is the point. Language models are fluent, and fluency reads as
confidence. The gate does not care how well argued an oversized order was.
"""

from __future__ import annotations

from ..adapters.perps import PerpContext
from ..desk import memory as desk_memory
from ..desk.desk import DeskError, TradingDesk
from ..risk import OrderIntent
from ..strategy import Context

# How much history to show. Enough for a moving average to mean something,
# short enough that the snapshot does not dominate the token bill.
HISTORY_POINTS = 40


class DeskStrategy:
    def __init__(self, desk: TradingDesk, symbols: tuple[str, ...], journal=None,
                 perps: PerpContext | None = None) -> None:
        self.name = "desk"
        # Positioning data. Off unless a context is handed in: a default that
        # reaches the network turns every construction into an HTTP call, which
        # made the test suite take two minutes and hit a public endpoint
        # hundreds of times. Production wires one in explicitly.
        self.perps = perps
        self.desk = desk
        self.symbols = symbols
        # The desk's reasoning is the main artefact it produces. Losing it
        # would leave trades with no recorded why, which is most of the value.
        self.journal = journal

    def _snapshot(self, context: Context) -> dict:
        symbols = {}
        for symbol in self.symbols:
            price = context.prices.get(symbol)
            if not price:
                continue
            series = context.history.series(symbol)[-HISTORY_POINTS:]
            held = context.positions.get(symbol)
            symbols[symbol] = {
                "price": round(price, 8),
                "observations": len(context.history.series(symbol)),
                "recent_prices": [round(p, 8) for p in series],
                "mean_10": context.history.mean(symbol, 10),
                "mean_30": context.history.mean(symbol, 30),
                "held_quantity": held.quantity if held else 0,
                "average_entry_price": held.average_price if held else None,
            }
            # Funding and open interest describe positioning rather than
            # price, which is the one thing the desk could not see before.
            if self.perps is not None:
                context_data = self.perps.for_symbol(symbol)
                if context_data:
                    symbols[symbol]["perp_context"] = context_data
        return {
            "as_of": context.now.isoformat(),
            "cash_available": round(context.cash, 2),
            "open_positions": len(context.positions),
            "symbols": symbols,
        }

    def decide(self, context: Context) -> list[OrderIntent]:
        snapshot = self._snapshot(context)
        if not snapshot["symbols"]:
            return []  # No prices this tick; nothing to convene about.

        recalled = ""
        if self.journal is not None:
            digest = desk_memory.build(self.journal, context.positions, context.prices)
            recalled = desk_memory.describe(digest)

        try:
            result = self.desk.run(snapshot, memory=recalled)
        except DeskError as error:
            self._note(f"desk did not reach a decision: {error}")
            return []
        except Exception as error:
            # An API outage must not stop the engine. No decision is a safe
            # default in a way that a guessed decision would not be.
            self._note(f"desk unavailable: {error}")
            return []

        if result is None:
            return []  # Budget said not yet.

        self._note(
            f"desk: {result.summary}",
            vetoed=result.vetoed or None,
            usage=result.usage,
        )

        intents: list[OrderIntent] = []
        for decision in result.decisions:
            symbol = decision.symbol
            price = context.prices.get(symbol)
            if not price:
                # The chair named something we cannot price. Skip rather than
                # guess a price - a wrong reference price mis-sizes the order
                # and mis-measures every cap that depends on it.
                self._note(f"desk named {symbol} but it has no price this tick")
                continue

            if decision.action == "buy":
                budget = context.cash * decision.fraction_of_cash
                quantity = budget / price
                if quantity <= 0:
                    continue
            elif decision.action == "sell":
                quantity = context.held(symbol)
                if quantity <= 0:
                    continue  # Nothing to sell; the desk misread the book.
            else:
                continue  # hold

            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side=decision.action,
                    quantity=round(quantity, 8),
                    reference_price=price,
                    venue=context.venue,
                    strategy=self.name,
                    rationale=decision.rationale,
                )
            )
        return intents

    def _note(self, message: str, **fields) -> None:
        print(message)
        if self.journal is not None:
            try:
                self.journal.note(message, **fields)
            except Exception as error:
                # The journal refuses entries that look like credentials. That
                # refusal must not take the strategy down with it.
                print(f"desk note not journalled: {error}")
