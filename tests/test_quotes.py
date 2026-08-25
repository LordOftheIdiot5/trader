"""Tests for the no-account stock quote source.

Network is stubbed out. What matters here is the behaviour around a public
endpoint that will, sooner or later, return something unhelpful: an empty
result, a ticker that does not exist, or a price outside trading hours.
"""

from __future__ import annotations

import pytest

from trader.adapters.quotes import YahooQuotes


class Recorder(YahooQuotes):
    """A YahooQuotes whose network call is replaced by a canned answer."""

    def __init__(self, prices=None, error=None, **kwargs):
        super().__init__(**kwargs)
        self._prices = prices or {}
        self._error = error
        self.calls = 0

    def _fetch(self, ticker):
        self.calls += 1
        if self._error:
            raise self._error
        return self._prices[ticker]


class TestCaching:
    def test_a_repeat_within_the_window_does_not_hit_the_network(self):
        # Six symbols on a fast loop would otherwise rate-limit themselves.
        quotes = Recorder({"AAPL": 100.0}, cache_seconds=60)
        assert quotes.last_price("AAPL") == 100.0
        assert quotes.last_price("AAPL") == 100.0
        assert quotes.calls == 1

    def test_zero_cache_always_refetches(self):
        quotes = Recorder({"AAPL": 100.0}, cache_seconds=0)
        quotes.last_price("AAPL")
        quotes.last_price("AAPL")
        assert quotes.calls == 2

    def test_symbols_are_cached_separately(self):
        quotes = Recorder({"AAPL": 100.0, "MSFT": 200.0}, cache_seconds=60)
        assert quotes.last_price("AAPL") == 100.0
        assert quotes.last_price("MSFT") == 200.0
        assert quotes.calls == 2


class TestFailures:
    def test_an_error_propagates_rather_than_returning_zero(self):
        # A zero price would size an enormous order. It must raise.
        quotes = Recorder(error=ValueError("Yahoo has no data for NOPE"))
        with pytest.raises(ValueError, match="no data"):
            quotes.last_price("NOPE")

    def test_a_failure_is_not_cached(self):
        quotes = Recorder(error=ValueError("transient"))
        for _ in range(2):
            with pytest.raises(ValueError):
                quotes.last_price("AAPL")
        assert quotes.calls == 2, "a transient failure must be retried, not memoised"


class TestParsing:
    """Exercises the real _fetch against canned payloads, no network."""

    def _parse(self, monkeypatch, payload):
        import json

        class FakeResponse:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: FakeResponse()
        )
        return YahooQuotes()._fetch("AAPL")

    def test_reads_the_regular_market_price(self, monkeypatch):
        payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 123.45}}]}}
        assert self._parse(monkeypatch, payload) == 123.45

    def test_falls_back_to_previous_close_outside_hours(self, monkeypatch):
        payload = {"chart": {"result": [{"meta": {"previousClose": 99.5}}]}}
        assert self._parse(monkeypatch, payload) == 99.5

    def test_an_empty_result_is_an_error(self, monkeypatch):
        payload = {"chart": {"result": None, "error": {"code": "Not Found"}}}
        with pytest.raises(ValueError, match="no data"):
            self._parse(monkeypatch, payload)

    def test_a_zero_price_is_an_error(self, monkeypatch):
        payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 0}}]}}
        with pytest.raises(ValueError, match="no usable price"):
            self._parse(monkeypatch, payload)
