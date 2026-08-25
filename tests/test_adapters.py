"""Adapter tests using fakes: no credentials, no network.

These cover the cases that would otherwise be discovered with real money —
an order that was accepted but never filled, a fill that came back without a
price, and a base URL that points somewhere unintended.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trader.adapters.alpaca import AlpacaVenue
from trader.adapters.exchange import ExchangeVenue


# --------------------------------------------------------------------------
# Alpaca
# --------------------------------------------------------------------------

class FakeTrading:
    def __init__(self, order=None, positions=(), cash=1000.0):
        self._order = order
        self._positions = positions
        self._cash = cash
        self.submitted = []

    def get_all_positions(self):
        return list(self._positions)

    def get_account(self):
        return SimpleNamespace(cash=self._cash, equity=self._cash)

    def submit_order(self, request):
        self.submitted.append(request)
        return self._order


def alpaca(order=None, **kwargs) -> AlpacaVenue:
    return AlpacaVenue(FakeTrading(order=order, **kwargs), data_client=None, paper=True)


class TestAlpacaEnvironmentParsing:
    def test_missing_credentials_is_an_error(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="must be set"):
            AlpacaVenue.from_env()

    def test_an_unrecognised_base_url_is_refused_rather_than_guessed(self, monkeypatch):
        # The base URL is the only thing between practice and real money.
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_API_SECRET", "s")
        monkeypatch.setenv("ALPACA_BASE_URL", "https://example.invalid")
        with pytest.raises(RuntimeError, match="Refusing to guess"):
            AlpacaVenue.from_env()


class TestAlpacaOrders:
    def test_an_accepted_but_unfilled_order_is_not_booked(self):
        # Market orders placed outside trading hours are queued. Booking one
        # as a fill would invent a trade that has not happened.
        queued = SimpleNamespace(
            id="abc", filled_qty="0", filled_avg_price=None, status="accepted"
        )
        with pytest.raises(ValueError, match="not filled"):
            alpaca(order=queued).market_order("AAPL", "buy", 1)

    def test_a_filled_order_becomes_a_fill(self):
        filled = SimpleNamespace(
            id="xyz", filled_qty="3", filled_avg_price="101.5",
            status="filled", filled_at=None,
        )
        fill = alpaca(order=filled).market_order("AAPL", "buy", 3)
        assert (fill.quantity, fill.price, fill.venue) == (3.0, 101.5, "alpaca")
        assert fill.notional == pytest.approx(304.5)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_quantity(self, bad):
        with pytest.raises(ValueError, match="quantity must be positive"):
            alpaca().market_order("AAPL", "buy", bad)

    def test_unknown_side(self):
        with pytest.raises(ValueError, match="unknown side"):
            alpaca().market_order("AAPL", "moon", 1)

    def test_positions_are_mapped(self):
        held = SimpleNamespace(symbol="MSFT", qty="5", avg_entry_price="400.25")
        positions = alpaca(positions=[held]).positions()
        assert positions["MSFT"].quantity == 5.0
        assert positions["MSFT"].average_price == 400.25


# --------------------------------------------------------------------------
# ccxt exchange
# --------------------------------------------------------------------------

class FakeExchange:
    id = "kraken"

    def __init__(self, order=None, fetched=None, balance=None, ticker=None):
        self._order = order or {}
        self._fetched = fetched or {}
        self._balance = balance or {"total": {}, "free": {}}
        self._ticker = ticker or {"last": 100.0}
        self.created = []

    def fetch_ticker(self, symbol):
        return dict(self._ticker)

    def fetch_balance(self):
        return self._balance

    def create_order(self, symbol, type_, side, quantity):
        self.created.append((symbol, type_, side, quantity))
        return dict(self._order)

    def fetch_order(self, order_id, symbol):
        return dict(self._fetched)


class TestExchangeOrders:
    def test_an_unpriced_response_is_re_fetched(self):
        # Several venues return the order before the fill is priced.
        venue = ExchangeVenue(
            FakeExchange(
                order={"id": "1", "price": None, "filled": 0},
                fetched={"id": "1", "average": 250.0, "filled": 2},
            )
        )
        fill = venue.market_order("ETH/USD", "buy", 2)
        assert fill.price == 250.0
        assert fill.quantity == 2

    def test_a_permanently_unpriced_fill_is_refused(self):
        venue = ExchangeVenue(
            FakeExchange(order={"id": "1", "price": None}, fetched={"id": "1"})
        )
        with pytest.raises(ValueError, match="no fill price"):
            venue.market_order("ETH/USD", "buy", 2)

    def test_fees_are_carried_through(self):
        venue = ExchangeVenue(
            FakeExchange(order={"id": "1", "average": 100.0, "filled": 1,
                                "fee": {"cost": 0.26}})
        )
        assert venue.market_order("BTC/USD", "buy", 1).fee == 0.26

    def test_unknown_side_never_reaches_the_exchange(self):
        exchange = FakeExchange()
        with pytest.raises(ValueError, match="unknown side"):
            ExchangeVenue(exchange).market_order("BTC/USD", "sideways", 1)
        assert exchange.created == []


class TestExchangeBalances:
    def test_quote_currency_is_cash_not_a_position(self):
        venue = ExchangeVenue(
            FakeExchange(balance={"total": {"USD": 500, "BTC": 0.5}, "free": {"USD": 500}}),
            quote_currency="USD",
        )
        positions = venue.positions()
        assert "USD/USD" not in positions
        assert positions["BTC/USD"].quantity == 0.5
        assert venue.cash() == 500.0

    def test_zero_balances_are_not_positions(self):
        venue = ExchangeVenue(
            FakeExchange(balance={"total": {"BTC": 0, "ETH": 0.0}, "free": {}})
        )
        assert venue.positions() == {}

    def test_a_zero_ticker_is_an_error(self):
        venue = ExchangeVenue(FakeExchange(ticker={"last": 0}))
        with pytest.raises(ValueError, match="no last price"):
            venue.last_price("BTC/USD")
