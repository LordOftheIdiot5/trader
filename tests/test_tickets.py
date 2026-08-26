"""Tests for the approval gate.

An approval gate that can be bypassed, or that approves a price which no
longer exists, is worse than none - it produces the feeling of control without
the fact of it. These tests are all about the ways that happens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.tickets import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    STALE,
    TicketBook,
)


def book(tmp_path, **kwargs) -> TicketBook:
    return TicketBook(path=tmp_path / "tickets.json", **kwargs)


def raise_one(b, symbol="BTC/USD", side="buy", quantity=1.0, price=100.0):
    return b.raise_ticket(symbol, side, quantity, price, "because", "desk")


class TestLifecycle:
    def test_a_new_ticket_is_pending_and_executes_nothing(self, tmp_path):
        b = book(tmp_path)
        t = raise_one(b)
        assert t.status == PENDING
        assert b.ready({"BTC/USD": 100.0})[0] == [], "pending must not be executable"

    def test_approval_makes_it_executable(self, tmp_path):
        b = book(tmp_path)
        t = raise_one(b)
        assert b.approve(t.id)[0]
        executable, _ = b.ready({"BTC/USD": 100.0})
        assert [x.id for x in executable] == [t.id]

    def test_rejection_is_final(self, tmp_path):
        b = book(tmp_path)
        t = raise_one(b)
        b.reject(t.id, "too big")
        assert b.get(t.id).status == REJECTED
        assert not b.approve(t.id)[0], "a rejected ticket must not be approvable"

    def test_approving_twice_is_refused(self, tmp_path):
        b = book(tmp_path)
        t = raise_one(b)
        b.approve(t.id)
        assert not b.approve(t.id)[0]

    def test_an_unknown_id_is_refused(self, tmp_path):
        assert not book(tmp_path).approve("T-NOPE")[0]

    def test_ids_are_case_insensitive_for_humans(self, tmp_path):
        b = book(tmp_path)
        t = raise_one(b)
        assert b.approve(t.id.lower())[0]


class TestExpiry:
    def test_an_old_ticket_cannot_be_approved(self, tmp_path):
        # Approving twenty minutes late approves a price that is gone.
        b = book(tmp_path, ttl_minutes=0)
        t = raise_one(b)
        ok, message = b.approve(t.id)
        assert not ok and "expired" in message
        assert b.get(t.id).status == EXPIRED

    def test_expire_stale_sweeps_pending_and_approved(self, tmp_path):
        b = book(tmp_path, ttl_minutes=30)
        pending = raise_one(b)
        approved = raise_one(b, symbol="ETH/USD")
        b.approve(approved.id)
        # Wind both clocks back past the window.
        for t in (pending, approved):
            b.get(t.id).expires_at = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat()
        assert {t.id for t in b.expire_stale()} == {pending.id, approved.id}

    def test_an_unparseable_expiry_fails_closed(self, tmp_path):
        # A corrupt ticket must not execute. One skipped trade beats an order
        # of unknown age.
        b = book(tmp_path)
        t = raise_one(b)
        b.approve(t.id)
        b.get(t.id).expires_at = "not a date"
        assert b.ready({"BTC/USD": 100.0})[0] == []


class TestPriceDrift:
    def test_a_drifted_price_is_not_executed(self, tmp_path):
        b = book(tmp_path, drift_tolerance_bps=100)  # 1%
        t = raise_one(b, price=100.0)
        b.approve(t.id)
        executable, drifted = b.ready({"BTC/USD": 105.0})  # 5% away
        assert executable == []
        assert [x.id for x in drifted] == [t.id]
        assert b.get(t.id).status == STALE

    def test_a_small_move_still_executes(self, tmp_path):
        b = book(tmp_path, drift_tolerance_bps=100)
        t = raise_one(b, price=100.0)
        b.approve(t.id)
        executable, drifted = b.ready({"BTC/USD": 100.5})  # 0.5%
        assert [x.id for x in executable] == [t.id] and drifted == []

    def test_drift_is_symmetric(self, tmp_path):
        b = book(tmp_path, drift_tolerance_bps=100)
        t = raise_one(b, price=100.0)
        b.approve(t.id)
        assert b.ready({"BTC/USD": 90.0})[0] == [], "a favourable move is still a move"

    def test_a_missing_price_does_not_execute(self, tmp_path):
        # Cannot verify the price, so cannot honour the approval.
        b = book(tmp_path)
        t = raise_one(b)
        b.approve(t.id)
        assert b.ready({})[0] == []


class TestPersistence:
    def test_tickets_survive_a_restart(self, tmp_path):
        b = book(tmp_path)
        t = raise_one(b)
        b.approve(t.id)
        assert book(tmp_path).get(t.id).status == APPROVED

    def test_a_corrupt_book_executes_nothing(self, tmp_path):
        # Losing pending approvals is the safe direction.
        (tmp_path / "tickets.json").write_text("{ broken")
        b = book(tmp_path)
        assert b.all() == []

    def test_prune_keeps_pending_and_drops_old_settled(self, tmp_path):
        b = book(tmp_path)
        keep = raise_one(b)
        for _ in range(5):
            done = raise_one(b, symbol="ETH/USD")
            b.reject(done.id)
        b.prune(keep=2)
        ids = {t.id for t in b.all()}
        assert keep.id in ids, "a pending ticket must never be pruned"
        assert len(ids) == 3
