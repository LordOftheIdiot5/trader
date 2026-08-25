"""Tests for the order gate.

Every test here is a loss that does not happen. They are written as "this
order must be refused" rather than "this function returns X", because the
refusal is the product.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.risk import (
    DayState,
    KillSwitch,
    OrderIntent,
    RiskGuard,
    RiskLimits,
    RiskRejection,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def limits(**overrides) -> RiskLimits:
    base = dict(
        max_order_notional=1_000.0,
        max_daily_notional=5_000.0,
        max_open_positions=3,
        daily_loss_limit=500.0,
        symbol_allowlist=("AAPL", "BTC/USD"),
        allow_short=False,
        slippage_bps=50.0,
    )
    base.update(overrides)
    return RiskLimits(**base)


def guard(tmp_path, **overrides) -> RiskGuard:
    return RiskGuard(
        limits=limits(**overrides),
        kill_switch=KillSwitch(tmp_path / "HALT"),
        state=DayState(day=NOW.date()),
    )


def intent(**overrides) -> OrderIntent:
    base = dict(
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        reference_price=100.0,
        venue="paper",
    )
    base.update(overrides)
    return OrderIntent(**base)


class TestConfigurationIsRefusedIfUseless:
    def test_empty_allowlist_is_an_error_not_a_free_for_all(self):
        with pytest.raises(ValueError, match="deny everything"):
            limits(symbol_allowlist=())

    @pytest.mark.parametrize(
        "field", ["max_order_notional", "max_daily_notional", "daily_loss_limit"]
    )
    def test_non_positive_caps_are_rejected(self, field):
        with pytest.raises(ValueError, match=field):
            limits(**{field: 0})


class TestTheKillSwitch:
    def test_blocks_everything_once_engaged(self, tmp_path):
        g = guard(tmp_path)
        g.check(intent(), open_positions=0, now=NOW)  # fine before

        g.kill_switch.engage("manual stop")
        with pytest.raises(RiskRejection, match="manual stop"):
            g.check(intent(), open_positions=0, now=NOW)

    def test_a_bare_file_still_halts(self, tmp_path):
        # Someone types `touch HALT`. There is no JSON in it. It must still stop.
        g = guard(tmp_path)
        (tmp_path / "HALT").write_text("")
        with pytest.raises(RiskRejection):
            g.check(intent(), open_positions=0, now=NOW)

    def test_first_reason_wins(self, tmp_path):
        g = guard(tmp_path)
        g.kill_switch.engage("first")
        g.kill_switch.engage("second")
        assert g.kill_switch.reason() == "first"

    def test_breaching_the_daily_loss_limit_engages_it(self, tmp_path):
        g = guard(tmp_path, daily_loss_limit=500.0)
        g.record_fill(notional=1_000, realised_pnl=-300)
        assert not g.kill_switch.engaged

        g.record_fill(notional=1_000, realised_pnl=-250)  # cumulative -550
        assert g.kill_switch.engaged
        assert "daily loss limit" in g.kill_switch.reason()

        with pytest.raises(RiskRejection):
            g.check(intent(), open_positions=0, now=NOW)


class TestOrdersThatMustBeRefused:
    def test_symbol_outside_the_allowlist(self, tmp_path):
        with pytest.raises(RiskRejection, match="not in the allowlist"):
            guard(tmp_path).check(intent(symbol="DOGE/USD"), open_positions=0, now=NOW)

    def test_zero_or_negative_price(self, tmp_path):
        # A dead feed reporting 0.0 would otherwise size an infinite order.
        with pytest.raises(RiskRejection, match="reference price"):
            guard(tmp_path).check(intent(reference_price=0.0), open_positions=0, now=NOW)

    def test_zero_quantity(self, tmp_path):
        with pytest.raises(RiskRejection, match="quantity"):
            guard(tmp_path).check(intent(quantity=0), open_positions=0, now=NOW)

    def test_unknown_side(self, tmp_path):
        with pytest.raises(RiskRejection, match="unknown side"):
            guard(tmp_path).check(intent(side="hodl"), open_positions=0, now=NOW)

    def test_order_over_the_per_order_cap(self, tmp_path):
        with pytest.raises(RiskRejection, match="per-order cap"):
            guard(tmp_path).check(intent(quantity=20), open_positions=0, now=NOW)

    def test_slippage_closes_the_gap_under_the_cap(self, tmp_path):
        # 9.98 x 100 = 998, under the 1000 cap on the quoted price alone, but
        # 1002.99 once 50bps of slippage is assumed. It must be refused.
        with pytest.raises(RiskRejection, match="per-order cap"):
            guard(tmp_path).check(intent(quantity=9.98), open_positions=0, now=NOW)

    def test_selling_more_than_held_when_shorting_is_off(self, tmp_path):
        with pytest.raises(RiskRejection, match="shorting is disabled"):
            guard(tmp_path).check(
                intent(side="sell", quantity=5),
                open_positions=1,
                position_qty=2,
                now=NOW,
            )

    def test_shorting_allowed_when_explicitly_enabled(self, tmp_path):
        guard(tmp_path, allow_short=True).check(
            intent(side="sell", quantity=5), open_positions=1, position_qty=2, now=NOW
        )

    def test_position_cap_blocks_only_new_symbols(self, tmp_path):
        g = guard(tmp_path, max_open_positions=3)

        with pytest.raises(RiskRejection, match="cap is 3"):
            g.check(intent(), open_positions=3, position_qty=0, now=NOW)

        # Adding to something already held is not a new position.
        g.check(intent(), open_positions=3, position_qty=10, now=NOW)


class TestTheDailyBudget:
    def test_accumulated_notional_eventually_refuses(self, tmp_path):
        g = guard(tmp_path, max_daily_notional=2_000.0)
        for _ in range(2):
            g.check(intent(quantity=9), open_positions=0, now=NOW)
            g.record_fill(notional=900)

        with pytest.raises(RiskRejection, match="daily traded notional"):
            g.check(intent(quantity=9), open_positions=0, now=NOW)

    def test_counters_reset_on_a_new_utc_day(self, tmp_path):
        g = guard(tmp_path, max_daily_notional=2_000.0)
        g.record_fill(notional=1_900)

        with pytest.raises(RiskRejection):
            g.check(intent(quantity=9), open_positions=0, now=NOW)

        tomorrow = NOW + timedelta(days=1)
        g.check(intent(quantity=9), open_positions=0, now=tomorrow)
        assert g.state.notional_traded == 0.0

    def test_a_loss_halt_survives_the_day_rolling_over(self, tmp_path):
        # The budget resets at midnight; a tripped kill switch must not.
        g = guard(tmp_path)
        g.record_fill(notional=1_000, realised_pnl=-600)
        assert g.kill_switch.engaged

        with pytest.raises(RiskRejection, match="kill switch"):
            g.check(intent(), open_positions=0, now=NOW + timedelta(days=1))


def test_snapshot_carries_no_credentials(tmp_path):
    # This dict is published to a public dashboard.
    snapshot = guard(tmp_path).snapshot()
    flat = repr(snapshot).lower()
    for leak in ("key", "secret", "token", "password", "api"):
        assert leak not in flat
