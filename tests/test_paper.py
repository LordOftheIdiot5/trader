"""Tests for the simulated venue.

The point of these is that the simulation must never flatter the strategy.
Where there is a choice, the paper broker has to take the worse side of it.
"""

from __future__ import annotations

import pytest

from trader.adapters.paper import PaperVenue


def venue(price=100.0, **overrides) -> PaperVenue:
    prices = {"AAPL": price}
    base = dict(
        price_source=lambda symbol: prices[symbol],
        starting_cash=10_000.0,
        slippage_bps=10.0,
        fee_bps=10.0,
    )
    base.update(overrides)
    return PaperVenue(**base)


class TestFillsArePessimistic:
    def test_buys_fill_above_the_quote(self):
        fill = venue().market_order("AAPL", "buy", 1)
        assert fill.price == pytest.approx(100.10)

    def test_sells_fill_below_the_quote(self):
        v = venue()
        v.market_order("AAPL", "buy", 1)
        fill = v.market_order("AAPL", "sell", 1)
        assert fill.price == pytest.approx(99.90)

    def test_a_round_trip_at_a_flat_price_loses_money(self):
        # Spread plus fees both ways. If this ever passes at breakeven the
        # simulation has stopped charging for something real.
        v = venue()
        v.market_order("AAPL", "buy", 10)
        v.market_order("AAPL", "sell", 10)
        assert v.cash() < 10_000.0
        assert v.realised_pnl < 0

    def test_fees_are_charged_on_both_sides(self):
        v = venue(fee_bps=0.0)
        v.market_order("AAPL", "buy", 10)
        without_fees = v.cash()

        v2 = venue(fee_bps=25.0)
        v2.market_order("AAPL", "buy", 10)
        assert v2.cash() < without_fees


class TestAccounting:
    def test_average_price_is_weighted_across_buys(self):
        prices = {"AAPL": 100.0}
        v = PaperVenue(price_source=lambda s: prices[s], starting_cash=10_000.0,
                       slippage_bps=0.0, fee_bps=0.0)
        v.market_order("AAPL", "buy", 1)
        prices["AAPL"] = 200.0
        v.market_order("AAPL", "buy", 1)
        assert v.positions()["AAPL"].average_price == pytest.approx(150.0)

    def test_partial_sale_keeps_the_average_and_books_pnl(self):
        prices = {"AAPL": 100.0}
        v = PaperVenue(price_source=lambda s: prices[s], starting_cash=10_000.0,
                       slippage_bps=0.0, fee_bps=0.0)
        v.market_order("AAPL", "buy", 10)
        prices["AAPL"] = 120.0
        v.market_order("AAPL", "sell", 4)

        position = v.positions()["AAPL"]
        assert position.quantity == pytest.approx(6)
        assert position.average_price == pytest.approx(100.0)
        assert v.realised_pnl == pytest.approx(80.0)

    def test_closing_out_removes_the_position(self):
        v = venue()
        v.market_order("AAPL", "buy", 3)
        v.market_order("AAPL", "sell", 3)
        assert "AAPL" not in v.positions()

    def test_equity_marks_holdings_at_the_live_price(self):
        prices = {"AAPL": 100.0}
        v = PaperVenue(price_source=lambda s: prices[s], starting_cash=10_000.0,
                       slippage_bps=0.0, fee_bps=0.0)
        v.market_order("AAPL", "buy", 10)
        prices["AAPL"] = 150.0
        assert v.equity() == pytest.approx(10_500.0)

    def test_equity_survives_a_dead_price_feed(self):
        # A feed outage must not make the account look like it went to zero.
        prices = {"AAPL": 100.0}
        v = PaperVenue(price_source=lambda s: prices[s], starting_cash=10_000.0,
                       slippage_bps=0.0, fee_bps=0.0)
        v.market_order("AAPL", "buy", 10)
        prices.clear()
        assert v.equity() == pytest.approx(10_000.0)


class TestRefusals:
    def test_cannot_spend_cash_it_does_not_have(self):
        with pytest.raises(ValueError, match="insufficient paper cash"):
            venue().market_order("AAPL", "buy", 1_000)

    def test_cannot_sell_what_is_not_held(self):
        with pytest.raises(ValueError, match="does not simulate shorting"):
            venue().market_order("AAPL", "sell", 1)

    def test_cannot_oversell_a_holding(self):
        v = venue()
        v.market_order("AAPL", "buy", 2)
        with pytest.raises(ValueError, match="only 2"):
            v.market_order("AAPL", "sell", 5)

    def test_a_zero_price_is_an_error_not_a_free_share(self):
        v = venue(price=0.0)
        with pytest.raises(ValueError, match="price source returned"):
            v.market_order("AAPL", "buy", 1)

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_non_positive_quantity(self, quantity):
        with pytest.raises(ValueError, match="quantity must be positive"):
            venue().market_order("AAPL", "buy", quantity)

    def test_unknown_side(self):
        with pytest.raises(ValueError, match="unknown side"):
            venue().market_order("AAPL", "yolo", 1)
