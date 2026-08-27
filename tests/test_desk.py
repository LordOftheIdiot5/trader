"""Tests for the multi-agent desk. No API calls, no key required.

The behaviours worth pinning down are the ones that cost money when wrong:
the budget actually stopping runs, a veto actually removing a trade, and a
model outage producing no trades rather than a guessed one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trader.desk.desk import DeskBudget, DeskError, TradingDesk
from trader.desk.schema import AnalystReport, ChairDecision, Decision, RiskRuling, View
from trader.strategies.desk import DeskStrategy
from trader.strategy import Context, PriceHistory

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    """Returns canned parsed_output by schema, recording every call."""

    def __init__(self, answers=None, error=None):
        self.answers = answers or {}
        self.error = error
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        schema = kwargs["output_format"]
        answer = self.answers.get(schema.__name__)
        if answer is None:
            raise AssertionError(f"no canned answer for {schema.__name__}")
        return SimpleNamespace(
            parsed_output=answer,
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100, output_tokens=50, cache_read_input_tokens=900
            ),
        )


def view(symbol="BTC/USD", stance="buy", conviction=4):
    return View(
        symbol=symbol, stance=stance, conviction=conviction,
        rationale="10-period mean above 30-period for four observations.",
        key_risk="The move is three ticks of noise.",
    )


def answers(decisions=None, approved=None, vetoed=None):
    decisions = decisions if decisions is not None else [
        Decision(symbol="BTC/USD", action="buy", fraction_of_cash=0.05,
                 rationale="Both seats agree, sized small.")
    ]
    return {
        "AnalystReport": AnalystReport(views=[view()]),
        "ChairDecision": ChairDecision(decisions=decisions, summary="Desk summary."),
        "RiskRuling": RiskRuling(
            approved=approved if approved is not None else ["BTC/USD"],
            vetoed=vetoed or [],
            reasoning="Checked sizing against conviction.",
        ),
    }


def context(cash=10_000.0, prices=None, positions=None) -> Context:
    history = PriceHistory()
    for price in range(100, 140):
        history.record("BTC/USD", float(price))
    return Context(
        now=NOW,
        prices=prices if prices is not None else {"BTC/USD": 139.0},
        positions=positions or {},
        cash=cash,
        venue="paper",
        history=history,
    )


class TestBudget:
    def test_first_run_is_allowed(self):
        assert DeskBudget().may_run(now=1000.0)[0]

    def test_a_second_run_too_soon_is_refused(self):
        budget = DeskBudget(min_seconds_between_runs=3600)
        budget.record_run(now=1000.0)
        allowed, why = budget.may_run(now=1100.0)
        assert not allowed and "next in" in why

    def test_a_run_after_the_gap_is_allowed(self):
        budget = DeskBudget(min_seconds_between_runs=3600)
        budget.record_run(now=1000.0)
        assert budget.may_run(now=4700.0)[0]

    def test_the_daily_ceiling_stops_everything(self):
        # The expensive failure: a loop that keeps convening all night.
        budget = DeskBudget(min_seconds_between_runs=0, max_runs_per_day=3)
        for _ in range(3):
            budget.record_run(now=1000.0)
        allowed, why = budget.may_run(now=9999.0)
        assert not allowed and "runs for today" in why

    def test_budget_is_checked_before_any_api_call(self):
        client = FakeClient(answers())
        desk = TradingDesk(client, budget=DeskBudget(max_runs_per_day=0))
        assert desk.run({"symbols": {}}) is None
        assert client.calls == [], "budget must gate before spending anything"


class TestDeskRun:
    def test_a_full_run_returns_the_approved_decisions(self):
        desk = TradingDesk(FakeClient(answers()), budget=DeskBudget(min_seconds_between_runs=0))
        result = desk.run({"symbols": {"BTC/USD": {}}})
        assert [d.symbol for d in result.decisions] == ["BTC/USD"]
        assert result.vetoed == {}

    def test_a_veto_removes_the_trade(self):
        desk = TradingDesk(
            FakeClient(answers(approved=[], vetoed=["BTC/USD"])),
            budget=DeskBudget(min_seconds_between_runs=0),
        )
        result = desk.run({"symbols": {"BTC/USD": {}}})
        assert result.decisions == []
        assert "BTC/USD" in result.vetoed

    def test_silence_from_the_risk_seat_is_not_approval(self):
        # A symbol the risk seat never mentions must not slip through.
        desk = TradingDesk(
            FakeClient(answers(approved=["ETH/USD"])),
            budget=DeskBudget(min_seconds_between_runs=0),
        )
        result = desk.run({"symbols": {"BTC/USD": {}}})
        assert result.decisions == []
        assert "BTC/USD" in result.vetoed

    def test_an_empty_chair_list_skips_the_risk_call(self):
        client = FakeClient(answers(decisions=[]))
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        result = desk.run({"symbols": {"BTC/USD": {}}})
        assert result.decisions == []
        schemas = [c["output_format"].__name__ for c in client.calls]
        assert "RiskRuling" not in schemas, "no decisions means nothing to review"

    def test_the_system_prompt_is_cached_and_prices_are_not_in_it(self):
        client = FakeClient(answers())
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        desk.run({"symbols": {"BTC/USD": {"price": 12345.67}}})

        for call in client.calls:
            system = call["system"][0]
            assert system["cache_control"] == {"type": "ephemeral"}
            # A price in the cached prefix would invalidate it every run.
            assert "12345.67" not in system["text"]

    def test_a_refusal_is_not_treated_as_a_decision(self):
        class Refusing(FakeClient):
            def parse(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    parsed_output=None, stop_reason="refusal",
                    stop_details=SimpleNamespace(category="other"), usage=None,
                )

        desk = TradingDesk(Refusing(), budget=DeskBudget(min_seconds_between_runs=0))
        with pytest.raises(DeskError, match="every analyst seat failed"):
            desk.run({"symbols": {"BTC/USD": {}}})


class TestDeskStrategy:
    def _strategy(self, client_answers=None, **kwargs):
        desk = TradingDesk(
            FakeClient(client_answers if client_answers is not None else answers()),
            budget=DeskBudget(min_seconds_between_runs=0),
            **kwargs,
        )
        return DeskStrategy(desk, symbols=("BTC/USD",))

    def test_a_buy_is_sized_as_a_fraction_of_cash(self):
        [intent] = self._strategy().decide(context(cash=10_000.0))
        assert intent.side == "buy"
        # 5% of 10,000 at 139.
        assert intent.quantity == pytest.approx(500 / 139, rel=1e-6)
        assert intent.strategy == "desk"

    def test_a_sell_closes_the_whole_position(self):
        from trader.adapters.base import Position

        strategy = self._strategy(answers(
            decisions=[Decision(symbol="BTC/USD", action="sell",
                                fraction_of_cash=0, rationale="Thesis gone.")]
        ))
        held = {"BTC/USD": Position("BTC/USD", 2.5, 100.0)}
        [intent] = strategy.decide(context(positions=held))
        assert intent.side == "sell" and intent.quantity == 2.5

    def test_a_sell_with_nothing_held_is_dropped(self):
        strategy = self._strategy(answers(
            decisions=[Decision(symbol="BTC/USD", action="sell",
                                fraction_of_cash=0, rationale="Misread the book.")]
        ))
        assert strategy.decide(context()) == []

    def test_hold_produces_no_order(self):
        strategy = self._strategy(answers(
            decisions=[Decision(symbol="BTC/USD", action="hold",
                                fraction_of_cash=0, rationale="Waiting.")]
        ))
        assert strategy.decide(context()) == []

    def test_an_api_outage_produces_no_trades(self):
        # The important one: unavailable must mean no action, never a guess.
        desk = TradingDesk(
            FakeClient(error=RuntimeError("connection reset")),
            budget=DeskBudget(min_seconds_between_runs=0),
        )
        strategy = DeskStrategy(desk, symbols=("BTC/USD",))
        assert strategy.decide(context()) == []

    def test_no_prices_means_no_desk_call(self):
        strategy = self._strategy()
        assert strategy.decide(context(prices={})) == []
        assert strategy.desk.client.calls == []

    def test_a_symbol_with_no_price_is_skipped_not_guessed(self):
        strategy = self._strategy(answers(
            decisions=[Decision(symbol="DOGE/USD", action="buy",
                                fraction_of_cash=0.05, rationale="Hallucinated.")]
        ))
        assert strategy.decide(context()) == []

    def test_the_snapshot_carries_no_credentials(self):
        strategy = self._strategy()
        snapshot = repr(strategy._snapshot(context())).lower()
        for leak in ("key", "secret", "token", "password"):
            assert leak not in snapshot


class TestModelCompatibility:
    """Swapping the model for cost is the main tuning knob, so the parameters
    that only some models accept have to follow the model automatically."""

    def _params_for(self, model):
        client = FakeClient(answers())
        desk = TradingDesk(client, model=model, budget=DeskBudget(min_seconds_between_runs=0))
        desk.run({"symbols": {"BTC/USD": {}}})
        return client.calls[0]

    def test_modern_models_get_adaptive_thinking_and_effort(self):
        call = self._params_for("claude-opus-5")
        assert call["thinking"] == {"type": "adaptive"}
        assert call["output_config"]["effort"] == "low"

    def test_haiku_gets_neither(self):
        # Haiku 4.5 rejects output_config.effort outright; sending the modern
        # shape would fail every call instead of just costing less.
        call = self._params_for("claude-haiku-4-5")
        assert "thinking" not in call
        assert "output_config" not in call

    def test_sonnet_5_is_modern(self):
        assert "thinking" in self._params_for("claude-sonnet-5")

    def test_the_model_reaches_the_api_call(self):
        assert self._params_for("claude-haiku-4-5")["model"] == "claude-haiku-4-5"


class TestNoLengthConstraints:
    """A max_length on a response field is validated after the model has been
    paid for. Two live runs were discarded over a summary one character long,
    so these guard against reintroducing the constraint."""

    def test_no_response_field_caps_length(self):
        from trader.desk import schema as s
        for model in (s.View, s.Decision, s.ChairDecision, s.RiskRuling):
            for name, field in model.model_fields.items():
                caps = [m for m in field.metadata if hasattr(m, "max_length")]
                assert not caps, f"{model.__name__}.{name} caps length"

    def test_a_long_summary_parses(self):
        from trader.desk.schema import ChairDecision
        decision = ChairDecision(decisions=[], summary="x" * 5000)
        assert len(decision.summary) == 5000

    def test_a_long_rationale_parses(self):
        from trader.desk.schema import Decision
        d = Decision(symbol="BTC/USD", action="buy", fraction_of_cash=0.05,
                     rationale="y" * 3000)
        assert len(d.rationale) == 3000


class TestUsageIsPerRun:
    def test_a_second_run_does_not_inherit_the_first_run_count(self):
        # A lifetime counter in the journal reads like a runaway loop.
        client = FakeClient(answers())
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        first = desk.run({"symbols": {"BTC/USD": {}}}).usage["calls"]
        second = desk.run({"symbols": {"BTC/USD": {}}}).usage["calls"]
        assert first == second, "usage must be per-run, not cumulative"

    def test_lifetime_still_accumulates(self):
        client = FakeClient(answers())
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        desk.run({"symbols": {"BTC/USD": {}}})
        desk.run({"symbols": {"BTC/USD": {}}})
        assert desk.lifetime["calls"] == 2 * desk.usage["calls"]


class TestResearchSeat:
    """Research is an input, never a dependency. Every failure mode has to
    leave the desk working exactly as it did before the seat existed."""

    def _client_with_research(self, research_text="BTC: nothing material found."):
        client = FakeClient(answers())
        def create(**kwargs):
            client.calls.append(kwargs)
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=research_text)],
            )
        client.create = create
        return client

    def test_the_brief_reaches_every_seat(self):
        client = self._client_with_research("XRP: court ruling published 2026-08-25.")
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        desk.run({"symbols": {"BTC/USD": {}}})

        seat_calls = [c for c in client.calls if "output_format" in c]
        assert seat_calls, "seats should still have run"
        for call in seat_calls:
            assert "court ruling published" in call["messages"][0]["content"]

    def test_a_failing_search_does_not_stop_the_desk(self):
        client = FakeClient(answers())
        def boom(**kwargs):
            raise RuntimeError("search backend down")
        client.create = boom
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        result = desk.run({"symbols": {"BTC/USD": {}}})
        assert result is not None and [d.symbol for d in result.decisions] == ["BTC/USD"]

    def test_an_empty_brief_is_not_pasted_in(self):
        client = self._client_with_research("")
        desk = TradingDesk(client, budget=DeskBudget(min_seconds_between_runs=0))
        desk.run({"symbols": {"BTC/USD": {}}})
        for call in [c for c in client.calls if "output_format" in c]:
            assert "research seat reports" not in call["messages"][0]["content"]

    def test_research_can_be_switched_off(self):
        client = self._client_with_research()
        desk = TradingDesk(client, use_research=False,
                           budget=DeskBudget(min_seconds_between_runs=0))
        desk.run({"symbols": {"BTC/USD": {}}})
        assert all("output_format" in c for c in client.calls), "no search call expected"

    def test_the_search_tool_variant_follows_the_model(self):
        from trader.desk.research import BASIC_SEARCH, MODERN_SEARCH, search_tool
        # Sending the modern variant to Haiku is a hard error, not a downgrade.
        assert search_tool("claude-haiku-4-5")["type"] == BASIC_SEARCH
        assert search_tool("claude-opus-5")["type"] == MODERN_SEARCH

    def test_searches_are_capped(self):
        from trader.desk.research import search_tool
        assert search_tool("claude-haiku-4-5")["max_uses"] <= 5


class TestPerpContext:
    """Positioning data is a bonus. Every failure has to leave the desk
    working exactly as it did without it."""

    def test_a_stock_ticker_has_no_perp(self):
        from trader.adapters.perps import to_perp
        assert to_perp("AAPL") is None
        assert to_perp("EQNR.OL") is None

    def test_a_pair_maps_to_the_perp_symbol(self):
        from trader.adapters.perps import to_perp
        assert to_perp("BTC/USD") == "BTC/USDC:USDC"
        assert to_perp("doge/usd") == "DOGE/USDC:USDC"

    def test_a_failing_feed_returns_nothing_rather_than_raising(self):
        from trader.adapters.perps import PerpContext
        p = PerpContext()
        p._fetch = lambda perp: (_ for _ in ()).throw(RuntimeError("down"))
        assert p.for_symbol("BTC/USD") == {}

    def test_results_are_cached(self):
        from trader.adapters.perps import PerpContext
        p = PerpContext(cache_seconds=600)
        calls = []
        p._fetch = lambda perp: calls.append(perp) or {"funding_rate": 1e-5}
        p.for_symbol("BTC/USD")
        p.for_symbol("BTC/USD")
        assert len(calls) == 1, "funding settles hourly; do not refetch every tick"

    def test_the_snapshot_survives_a_dead_perp_feed(self):
        from trader.adapters.perps import PerpContext
        p = PerpContext()
        p._fetch = lambda perp: {}
        strategy = DeskStrategy(TradingDesk(client=None), symbols=("BTC/USD",), perps=p)
        snap = strategy._snapshot(context())
        assert "BTC/USD" in snap["symbols"]
        assert "perp_context" not in snap["symbols"]["BTC/USD"]


class TestSizingFitsUnderTheRiskCap:
    """The failure that cost a day: the desk's largest sensible order was ten
    times the per-order cap, so every decision it made was refused and it
    looked like a strategy with no opinions."""

    def test_the_schema_ceiling_leaves_room_for_slippage(self):
        from trader import config as config_module
        from trader.desk.schema import Decision

        cfg = config_module.load()
        ceiling = [m for m in Decision.model_fields["fraction_of_cash"].metadata
                   if hasattr(m, "le")][0].le
        largest = (cfg.paper_starting_cash * float(ceiling)
                   * (1 + cfg.limits.slippage_bps / 10_000))
        assert largest <= cfg.limits.max_order_notional, (
            f"the desk's largest order ({largest:,.0f}) exceeds the per-order "
            f"cap ({cfg.limits.max_order_notional:,.0f}); every high-conviction "
            "trade would be refused"
        )

    def test_a_fraction_above_the_ceiling_is_rejected(self):
        import pydantic
        from trader.desk.schema import Decision
        with pytest.raises(pydantic.ValidationError):
            Decision(symbol="BTC/USD", action="buy", fraction_of_cash=0.5,
                     rationale="all in")
