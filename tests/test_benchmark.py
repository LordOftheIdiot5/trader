"""Tests for the benchmark.

Every test here guards against the benchmark flattering the strategy. A
benchmark that is easier to beat than reality is worse than none: it converts
"we do not know if this works" into a confident wrong answer.
"""

from __future__ import annotations

import json

import pytest

from trader.benchmark import MEANINGFUL_DAYS, Benchmark


def bench(tmp_path, cash=10_000.0, **kwargs) -> Benchmark:
    return Benchmark(path=tmp_path / "benchmark.json", starting_cash=cash, **kwargs)


class TestEntry:
    def test_the_basket_is_equal_weight(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0, "ETH/USD": 200.0})
        # 5,000 into each.
        assert b.holdings["BTC/USD"] == pytest.approx(50.0)
        assert b.holdings["ETH/USD"] == pytest.approx(25.0)

    def test_entry_pays_slippage_and_fees(self, tmp_path):
        # A benchmark entering at a perfect mid is one nobody could have had,
        # and the difference is a free head start over the strategy.
        free = bench(tmp_path / "a", slippage_bps=0, fee_bps=0)
        free.start({"BTC/USD": 100.0})
        costly = bench(tmp_path / "b", slippage_bps=50, fee_bps=50)
        costly.start({"BTC/USD": 100.0})
        assert costly.holdings["BTC/USD"] < free.holdings["BTC/USD"]

    def test_starting_twice_does_nothing(self, tmp_path):
        b = bench(tmp_path)
        b.start({"BTC/USD": 100.0})
        first = dict(b.holdings)
        b.start({"BTC/USD": 500.0})
        assert b.holdings == first, "the basket is bought once, at inception"

    def test_unpriceable_symbols_are_skipped(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0, "BROKEN/USD": 0})
        assert list(b.holdings) == ["BTC/USD"]

    def test_no_prices_means_not_started(self, tmp_path):
        b = bench(tmp_path)
        b.start({})
        assert not b.started


class TestValuation:
    def test_a_flat_market_returns_roughly_the_starting_cash(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0, "ETH/USD": 200.0})
        assert b.value({"BTC/USD": 100.0, "ETH/USD": 200.0}) == pytest.approx(10_000.0)

    def test_a_doubling_doubles_the_basket(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0})
        assert b.value({"BTC/USD": 200.0}) == pytest.approx(20_000.0)

    def test_a_missing_price_holds_that_leg_at_entry(self, tmp_path):
        # A feed outage must not read as the position going to zero.
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0, "ETH/USD": 200.0})
        assert b.value({"BTC/USD": 100.0}) == pytest.approx(10_000.0)


class TestComparison:
    def test_excess_return_is_the_difference(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0})
        # Basket doubles to 20,000; the strategy only reached 15,000.
        result = b.compare(15_000.0, {"BTC/USD": 200.0})
        assert result["buy_and_hold_return_pct"] == pytest.approx(100.0)
        assert result["strategy_return_pct"] == pytest.approx(50.0)
        assert result["excess_return_pct"] == pytest.approx(-50.0)

    def test_beating_the_basket_shows_positive_excess(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0})
        assert b.compare(12_000.0, {"BTC/USD": 100.0})["excess_return_pct"] > 0

    def test_a_short_sample_is_labelled_as_noise(self, tmp_path):
        b = bench(tmp_path)
        b.start({"BTC/USD": 100.0})
        result = b.compare(11_000.0, {"BTC/USD": 100.0})
        assert not result["meaningful"]
        assert "noise" in result["caveat"]

    def test_a_long_sample_drops_the_caveat(self, tmp_path):
        b = bench(tmp_path)
        b.start({"BTC/USD": 100.0})
        b.started_at = "2020-01-01T00:00:00+00:00"
        result = b.compare(11_000.0, {"BTC/USD": 100.0})
        assert result["meaningful"] and result["caveat"] == ""
        assert result["days_running"] > MEANINGFUL_DAYS

    def test_unstarted_reports_unavailable_rather_than_zero(self, tmp_path):
        assert bench(tmp_path).compare(10_000.0, {})["available"] is False


class TestPersistence:
    def test_the_basket_survives_a_restart(self, tmp_path):
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0})
        assert bench(tmp_path).holdings["BTC/USD"] == pytest.approx(100.0)

    def test_a_restart_does_not_rebuy_at_todays_price(self, tmp_path):
        # Re-entering at the current price would erase any drawdown and make
        # the strategy look better than it is.
        b = bench(tmp_path, slippage_bps=0, fee_bps=0)
        b.start({"BTC/USD": 100.0})
        reloaded = bench(tmp_path, slippage_bps=0, fee_bps=0)
        reloaded.start({"BTC/USD": 999.0})
        assert reloaded.entry_prices["BTC/USD"] == pytest.approx(100.0)

    def test_a_corrupt_file_leaves_it_unstarted_rather_than_wrong(self, tmp_path):
        (tmp_path / "benchmark.json").write_text("{ not json")
        assert not bench(tmp_path).started
