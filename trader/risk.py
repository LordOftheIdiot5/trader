"""The gate every order passes through before it can reach a venue.

This module is deliberately the least clever code in the project. A strategy
being wrong loses money slowly; this being wrong loses it all at once, so it
is written to be read rather than to be elegant.

Two rules shape it:

  - Deny by default. An unknown symbol, an unparseable price, a missing
    account value: all of these are rejections, never "probably fine".
  - The kill switch is a file on disk, not a variable. A process that has
    lost its mind cannot be trusted to honour its own in-memory flag, and a
    file can be created by a human with one command while the bot is running.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


class RiskRejection(Exception):
    """Raised when an order must not be sent. Carries a human-readable why."""


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants to do, before any venue has seen it."""

    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    # Reference price used for notional maths. For market orders this is the
    # last trade; the actual fill will differ, which is what slippage_bps is
    # for when the caps are computed.
    reference_price: float
    venue: str
    strategy: str = "manual"
    # Optional and unused by the gate. It exists so an approval ticket can
    # show a human why, rather than asking them to authorise a bare order.
    rationale: str = ""

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.reference_price)


@dataclass
class RiskLimits:
    """Hard caps. Every one of these is a refusal, not a warning."""

    max_order_notional: float
    max_daily_notional: float
    max_open_positions: int
    daily_loss_limit: float
    symbol_allowlist: tuple[str, ...]
    allow_short: bool = False
    # Assume the fill is this much worse than the reference price when
    # checking caps, so a cap set at 1000 cannot be squeezed past by a market
    # order that fills high.
    slippage_bps: float = 50.0

    def __post_init__(self) -> None:
        for name in (
            "max_order_notional",
            "max_daily_notional",
            "daily_loss_limit",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if self.max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive")
        if not self.symbol_allowlist:
            raise ValueError(
                "symbol_allowlist is empty, which would deny everything. "
                "List the symbols this bot may trade."
            )


@dataclass
class DayState:
    """Rolling per-day counters. Reset on the first order of a new UTC day."""

    day: date
    notional_traded: float = 0.0
    realised_pnl: float = 0.0
    orders: int = 0

    def roll_if_stale(self, now: datetime) -> None:
        if now.date() != self.day:
            self.day = now.date()
            self.notional_traded = 0.0
            self.realised_pnl = 0.0
            self.orders = 0


class KillSwitch:
    """A file whose existence stops all trading.

    Deliberately crude. `touch HALT` from any shell, on any machine with the
    volume mounted, and the next order is refused. Nothing in this process can
    clear it: removal is a human action.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def engaged(self) -> bool:
        return self.path.exists()

    def engage(self, reason: str) -> None:
        """Trip the switch. Safe to call repeatedly; the first reason wins."""
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "engaged_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def reason(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("reason")
        except (ValueError, OSError):
            # A malformed or unreadable halt file still halts. Failing to read
            # the reason is not a reason to start trading again.
            return "halt file present but unreadable"


class RiskGuard:
    def __init__(
        self,
        limits: RiskLimits,
        kill_switch: KillSwitch,
        state: DayState | None = None,
    ) -> None:
        self.limits = limits
        self.kill_switch = kill_switch
        self.state = state or DayState(day=datetime.now(timezone.utc).date())

    def check(
        self,
        intent: OrderIntent,
        open_positions: int,
        position_qty: float = 0.0,
        now: datetime | None = None,
    ) -> None:
        """Raise RiskRejection unless every limit permits this order.

        `position_qty` is the current signed holding in this symbol, used to
        tell a closing sell from a short sale.
        """
        now = now or datetime.now(timezone.utc)
        self.state.roll_if_stale(now)

        if self.kill_switch.engaged:
            raise RiskRejection(f"kill switch engaged: {self.kill_switch.reason()}")

        if intent.side not in ("buy", "sell"):
            raise RiskRejection(f"unknown side {intent.side!r}")

        if intent.quantity <= 0:
            raise RiskRejection(f"quantity must be positive, got {intent.quantity}")

        if intent.reference_price <= 0:
            raise RiskRejection(
                f"reference price must be positive, got {intent.reference_price}. "
                "A missing or zero price means we cannot size the order."
            )

        if intent.symbol not in self.limits.symbol_allowlist:
            raise RiskRejection(
                f"{intent.symbol} is not in the allowlist "
                f"({', '.join(self.limits.symbol_allowlist)})"
            )

        # Size the check against a pessimistic fill, not the quoted price.
        worst = intent.notional * (1 + self.limits.slippage_bps / 10_000)

        if worst > self.limits.max_order_notional:
            raise RiskRejection(
                f"order notional {worst:,.2f} (incl. {self.limits.slippage_bps:g}bps "
                f"slippage) exceeds per-order cap {self.limits.max_order_notional:,.2f}"
            )

        projected = self.state.notional_traded + worst
        if projected > self.limits.max_daily_notional:
            raise RiskRejection(
                f"daily traded notional would reach {projected:,.2f}, "
                f"over the cap {self.limits.max_daily_notional:,.2f}"
            )

        if not self.limits.allow_short and intent.side == "sell":
            if intent.quantity > position_qty + 1e-12:
                raise RiskRejection(
                    f"selling {intent.quantity} {intent.symbol} but only "
                    f"{position_qty} held, and shorting is disabled"
                )

        # Only an order that opens a new symbol can breach the position cap.
        opening_new = position_qty == 0 and intent.side == "buy"
        if opening_new and open_positions >= self.limits.max_open_positions:
            raise RiskRejection(
                f"already holding {open_positions} positions, "
                f"cap is {self.limits.max_open_positions}"
            )

    def record_fill(self, notional: float, realised_pnl: float = 0.0) -> None:
        """Book a completed fill against the day's counters.

        Trips the kill switch if the day's realised loss breaches the limit,
        so the halt outlives this process rather than being re-learned on the
        next start.
        """
        self.state.notional_traded += abs(notional)
        self.state.realised_pnl += realised_pnl
        self.state.orders += 1

        if self.state.realised_pnl <= -abs(self.limits.daily_loss_limit):
            self.kill_switch.engage(
                f"daily loss limit hit: realised {self.state.realised_pnl:,.2f} "
                f"against limit {-abs(self.limits.daily_loss_limit):,.2f}"
            )

    def snapshot(self) -> dict:
        """State for the dashboard. Contains no credentials."""
        return {
            "day": self.state.day.isoformat(),
            "orders_today": self.state.orders,
            "notional_today": round(self.state.notional_traded, 2),
            "realised_pnl_today": round(self.state.realised_pnl, 2),
            "halted": self.kill_switch.engaged,
            "halt_reason": self.kill_switch.reason(),
            "limits": {
                "max_order_notional": self.limits.max_order_notional,
                "max_daily_notional": self.limits.max_daily_notional,
                "max_open_positions": self.limits.max_open_positions,
                "daily_loss_limit": self.limits.daily_loss_limit,
                "allow_short": self.limits.allow_short,
            },
        }
