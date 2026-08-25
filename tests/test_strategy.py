"""Tests for the strategy interface and the worked example.

The interesting cases are all about silence: a strategy that trades when it
should not is the expensive failure, and "not enough data yet" is the state
it spends most of its life in.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.adapters.base import Position
from trader.strategies.crossover import Crossover
from trader.strategy import Context, PriceHistory

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def context(history, prices, positions=None, cash=10_000.0) -> Context:
    return Context(
        now=NOW,
        prices=prices,
        positions=positions or {},
        cash=cash,
        venue="paper",
        history=history,
    )


def feed(history, symbol, prices):
    for price in prices:
        history.record(symbol, price)
    return history


class TestPriceHistory:
    def test_mean_is_none_until_the_window_is_full(self):
        history = feed(PriceHistory(), "BTC/USD", [1, 2, 3])
        assert history.mean("BTC/USD", 5) is None
        assert history.mean("BTC/USD", 3) == pytest.approx(2.0)

    def test_unknown_symbol_is_none_not_an_error(self):
        assert PriceHistory().mean("NOPE", 3) is None

    def test_zero_and_negative_prices_are_dropped(self):
        # A dead feed reporting 0 would drag the average toward zero and read
        # as a crash the strategy would act on.
        history = feed(PriceHistory(), "BTC/USD", [100, 0, -5, 100])
        assert history.series("BTC/USD") == [100, 100]

    def test_the_ring_is_bounded(self):
        history = feed(PriceHistory(maxlen=10), "BTC/USD", range(1, 101))
        assert len(history.series("BTC/USD")) == 10
        assert history.series("BTC/USD")[0] == 91

    def test_window_must_be_positive(self):
        with pytest.raises(ValueError, match="window must be positive"):
            PriceHistory().mean("BTC/USD", 0)


class TestCrossoverConfiguration:
    def test_fast_must_be_shorter_than_slow(self):
        with pytest.raises(ValueError, match="must be shorter"):
            Crossover(("BTC/USD",), fast=30, slow=10)

    @pytest.mark.parametrize("allocation", [0, 1.5, -0.1])
    def test_allocation_must_be_a_sane_fraction(self, allocation):
        with pytest.raises(ValueError, match="allocation"):
            Crossover(("BTC/USD",), allocation=allocation)


class TestCrossoverSilence:
    """Most ticks should produce nothing. These are the cases that must."""

    def test_says_nothing_before_the_window_fills(self):
        strategy = Crossover(("BTC/USD",), fast=2, slow=4)
        history = feed(PriceHistory(), "BTC/USD", [100, 101])
        assert strategy.decide(context(history, {"BTC/USD": 101})) == []

    def test_says_nothing_when_the_price_is_missing(self):
        strategy = Crossover(("BTC/USD",), fast=2, slow=4)
        history = feed(PriceHistory(), "BTC/USD", [100, 101, 102, 103])
        assert strategy.decide(context(history, {})) == []

    def test_does_not_buy_twice_into_the_same_signal(self):
        strategy = Crossover(("BTC/USD",), fast=2, slow=4)
        history = feed(PriceHistory(), "BTC/USD", [100, 100, 100, 110, 120])
        held = {"BTC/USD": Position("BTC/USD", 1.0, 100.0)}
        assert strategy.decide(context(history, {"BTC/USD": 120}, held)) == []

    def test_does_not_sell_what_it_does_not_hold(self):
        # Without this it would emit a short on every downward cross.
        strategy = Crossover(("BTC/USD",), fast=2, slow=4)
        history = feed(PriceHistory(), "BTC/USD", [120, 120, 120, 100, 90])
        assert strategy.decide(context(history, {"BTC/USD": 90})) == []


class TestCrossoverSignals:
    def test_buys_when_fast_crosses_above_slow(self):
        strategy = Crossover(("BTC/USD",), fast=2, slow=4, allocation=0.1)
        history = feed(PriceHistory(), "BTC/USD", [100, 100, 100, 110, 130])
        [intent] = strategy.decide(context(history, {"BTC/USD": 130}, cash=10_000))

        assert intent.side == "buy"
        assert intent.symbol == "BTC/USD"
        assert intent.strategy == "crossover-2-4"
        # 10% of 10,000 at 130.
        assert intent.quantity == pytest.approx(1000 / 130, rel=1e-6)

    def test_sells_the_whole_position_on_the_downward_cross(self):
        strategy = Crossover(("BTC/USD",), fast=2, slow=4)
        history = feed(PriceHistory(), "BTC/USD", [130, 130, 130, 110, 90])
        held = {"BTC/USD": Position("BTC/USD", 2.5, 120.0)}
        [intent] = strategy.decide(context(history, {"BTC/USD": 90}, held))

        assert intent.side == "sell"
        assert intent.quantity == 2.5

    def test_sizing_shrinks_as_cash_shrinks(self):
        # A fixed fraction means a losing streak bets less, automatically.
        strategy = Crossover(("BTC/USD",), fast=2, slow=4, allocation=0.1)
        history = feed(PriceHistory(), "BTC/USD", [100, 100, 100, 110, 130])

        [rich] = strategy.decide(context(history, {"BTC/USD": 130}, cash=10_000))
        [poor] = strategy.decide(context(history, {"BTC/USD": 130}, cash=1_000))
        assert poor.quantity == pytest.approx(rich.quantity / 10)

    def test_handles_several_symbols_independently(self):
        strategy = Crossover(("BTC/USD", "ETH/USD"), fast=2, slow=4, allocation=0.1)
        history = PriceHistory()
        feed(history, "BTC/USD", [100, 100, 100, 110, 130])
        feed(history, "ETH/USD", [100, 100, 100, 100, 100])

        intents = strategy.decide(
            context(history, {"BTC/USD": 130, "ETH/USD": 100})
        )
        assert [i.symbol for i in intents] == ["BTC/USD"]
