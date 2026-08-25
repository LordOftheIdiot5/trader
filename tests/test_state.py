"""Tests for the publish-worthiness check.

The failure this guards against is quiet and expensive in a different way to
the trading ones: a dashboard that redeploys every five minutes forever,
burning Actions minutes and burying real changes in noise.
"""

from __future__ import annotations

from trader.state import material_fingerprint


def snapshot(**overrides) -> dict:
    base = {
        "generated_at": "2026-08-25T12:00:00+00:00",
        "risk": {"orders_today": 0, "halted": False},
        "venues": {"paper": {"cash": 100_000.0, "positions": {}}},
        "recent": [],
    }
    base.update(overrides)
    return base


def test_the_clock_alone_is_not_a_change():
    # The whole point: a quiet tick must not trigger a deploy.
    a = snapshot()
    b = snapshot(generated_at="2026-08-25T18:30:00+00:00")
    assert material_fingerprint(a) == material_fingerprint(b)


def test_a_new_order_is_a_change():
    a = snapshot()
    b = snapshot(risk={"orders_today": 1, "halted": False})
    assert material_fingerprint(a) != material_fingerprint(b)


def test_a_halt_is_a_change():
    # The most important one to publish promptly.
    a = snapshot()
    b = snapshot(risk={"orders_today": 0, "halted": True})
    assert material_fingerprint(a) != material_fingerprint(b)


def test_a_new_position_is_a_change():
    a = snapshot()
    b = snapshot(venues={"paper": {"cash": 95_000.0, "positions": {"BTC/USD": 1}}})
    assert material_fingerprint(a) != material_fingerprint(b)


def test_a_journal_entry_is_a_change():
    # Refusals matter too: a strategy hitting its cap should show up.
    a = snapshot()
    b = snapshot(recent=[{"kind": "rejected", "reason": "over cap"}])
    assert material_fingerprint(a) != material_fingerprint(b)


def test_key_order_does_not_matter():
    # Dict ordering must not manufacture a spurious change.
    a = {"generated_at": "x", "risk": {"a": 1, "b": 2}}
    b = {"risk": {"b": 2, "a": 1}, "generated_at": "y"}
    assert material_fingerprint(a) == material_fingerprint(b)


def test_non_serialisable_values_do_not_raise():
    # snapshot() carries whatever a venue returned; it must never crash here.
    from datetime import datetime

    assert material_fingerprint({"risk": {"at": datetime(2026, 1, 1)}})
