"""A moving-average crossover, included as a worked example of the interface.

READ THIS BEFORE RUNNING IT WITH MONEY.

This is not a recommendation and it is not expected to make money. Moving
average crossover is the textbook example of a mechanical rule - it is here
because it is easy to read, easy to test, and exercises every part of the
plumbing (history, entry, exit, sizing, the risk gate). It is a wiring
diagram, not a trading edge.

Its known weaknesses, none of which are subtle:

  - It buys strength and sells weakness, so in a sideways market it buys every
    top and sells every bottom, paying the spread each time.
  - It has no stop and no target. The only exit is the opposite crossover.
  - It is fitted to nothing, which is honest, but it also means nobody has
    checked it works on the symbols you point it at.

Write your own. This one shows you where the pieces go.
"""

from __future__ import annotations

from ..risk import OrderIntent
from ..strategy import Context


class Crossover:
    def __init__(
        self,
        symbols: tuple[str, ...],
        fast: int = 10,
        slow: int = 30,
        allocation: float = 0.02,
    ) -> None:
        if fast >= slow:
            raise ValueError(
                f"fast window ({fast}) must be shorter than slow ({slow}), "
                "or the two averages never cross meaningfully"
            )
        if not 0 < allocation <= 1:
            raise ValueError("allocation must be a fraction between 0 and 1")
        self.name = f"crossover-{fast}-{slow}"
        self.symbols = symbols
        self.fast = fast
        self.slow = slow
        self.allocation = allocation

    def decide(self, context: Context) -> list[OrderIntent]:
        intents: list[OrderIntent] = []

        for symbol in self.symbols:
            price = context.prices.get(symbol)
            if not price or price <= 0:
                # No price this tick means no opinion this tick. Acting on a
                # stale or missing quote is how a feed outage becomes a trade.
                continue

            fast = context.history.mean(symbol, self.fast)
            slow = context.history.mean(symbol, self.slow)
            if fast is None or slow is None:
                # Still filling the window. Silence until there is enough data
                # for the indicator to mean what it claims to mean.
                continue

            held = context.held(symbol)

            if fast > slow and held == 0:
                # Size from cash, not from conviction: a fixed fraction means
                # a losing streak shrinks the next bet automatically.
                budget = context.cash * self.allocation
                quantity = budget / price
                if quantity <= 0:
                    continue
                intents.append(
                    OrderIntent(
                        symbol=symbol,
                        side="buy",
                        quantity=round(quantity, 8),
                        reference_price=price,
                        venue=context.venue,
                        strategy=self.name,
                    )
                )

            elif fast < slow and held > 0:
                # Close the whole position. Partial exits need a reason to be
                # partial, and this strategy does not have one.
                intents.append(
                    OrderIntent(
                        symbol=symbol,
                        side="sell",
                        quantity=held,
                        reference_price=price,
                        venue=context.venue,
                        strategy=self.name,
                    )
                )

        return intents
