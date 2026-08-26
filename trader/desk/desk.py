"""Runs the desk: analysts in parallel, then the chair, then the risk seat.

At most four API calls per run, and usually two: when no analyst has a view -
which is most runs in a quiet market - the chair and risk seats are never
asked. Measured on Haiku 4.5 with six symbols, a quiet run is about a third of
a cent and a full one about $0.013.

The engine ticks far more often than the desk should run. `DeskBudget` below
enforces both a minimum gap between runs and a hard daily ceiling, and the
ceiling is checked before the first call rather than after the fourth.

On caching: each brief is marked cacheable, but they run about 700 tokens each
against a minimum cacheable prefix of roughly 1024, so in practice the cache
does not engage - observed cache_read has been zero across every run so far.
It is left in place because it costs nothing and starts working if the briefs
grow. It is not where the money goes: output tokens are, by roughly two to
one. The snapshot still belongs in the user turn regardless, since a price in
the system prompt would invalidate the prefix on every call.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import roles
from .schema import AnalystReport, ChairDecision, RiskRuling

# Opus 5 by default. Lower it if you want, but do it knowingly: the whole
# premise of this arrangement is that the seats disagree usefully, and a
# weaker model agrees with whatever it was shown last.
DEFAULT_MODEL = "claude-opus-5"

# Models that accept adaptive thinking and output_config.effort. Everything
# older takes neither: Haiku 4.5 rejects `effort` outright and still expects
# the fixed budget_tokens form of thinking. Matching on family prefix rather
# than an exact list so a new point release does not silently fall back.
_ADAPTIVE_FAMILIES = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5",
)


def supports_adaptive_thinking(model: str) -> bool:
    return any(model.startswith(family) for family in _ADAPTIVE_FAMILIES)


class DeskError(Exception):
    """Raised when the desk cannot produce a usable answer."""


@dataclass
class DeskBudget:
    """Rate and spend control. Checked before the first call, not after."""

    min_seconds_between_runs: float = 3600.0
    max_runs_per_day: int = 24
    _day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    _runs_today: int = 0
    _last_run: float = 0.0

    def _roll(self, now_utc: datetime) -> None:
        if now_utc.date() != self._day:
            self._day = now_utc.date()
            self._runs_today = 0

    def may_run(self, now: float | None = None) -> tuple[bool, str]:
        now = now if now is not None else time.time()
        self._roll(datetime.now(timezone.utc))

        if self._runs_today >= self.max_runs_per_day:
            return False, f"desk has used its {self.max_runs_per_day} runs for today"
        waited = now - self._last_run
        if self._last_run and waited < self.min_seconds_between_runs:
            remaining = int(self.min_seconds_between_runs - waited)
            return False, f"desk ran {int(waited)}s ago, next in {remaining}s"
        return True, "ok"

    def record_run(self, now: float | None = None) -> None:
        self._last_run = now if now is not None else time.time()
        self._runs_today += 1

    @property
    def runs_today(self) -> int:
        return self._runs_today


@dataclass
class DeskResult:
    decisions: list
    summary: str
    vetoed: dict[str, str]
    views: dict[str, list]
    usage: dict


class TradingDesk:
    def __init__(
        self,
        client,
        model: str = DEFAULT_MODEL,
        budget: DeskBudget | None = None,
        analyst_effort: str = "low",
        chair_effort: str = "high",
    ) -> None:
        self.client = client
        self.model = model
        self.budget = budget or DeskBudget()
        self.analyst_effort = analyst_effort
        # The chair is the seat that has to weigh conflicting cases, so it is
        # the one worth spending effort on. Analysts mostly report.
        self.chair_effort = chair_effort
        # Per-run, reset each time run() is called. The journal entry for a
        # run should say what that run cost; a lifetime counter reads like a
        # runaway loop when it is really fourteen quiet runs added up.
        self._usage = {"input": 0, "output": 0, "cache_read": 0, "calls": 0}
        self.lifetime = {"input": 0, "output": 0, "cache_read": 0, "calls": 0}

    def _request_extras(self, effort: str) -> dict:
        """Parameters that only some models accept.

        Adaptive thinking and `output_config.effort` exist on the 4.6-and-later
        family. On Haiku 4.5 `effort` is rejected outright and thinking still
        wants the older fixed `budget_tokens` form, so sending the modern shape
        to a cheaper model fails every call rather than degrading. Cost tuning
        by swapping the model is exactly what this class is for, so the check
        belongs here rather than in a comment telling people not to.
        """
        if not supports_adaptive_thinking(self.model):
            return {}
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }

    def _ask(self, brief: str, prompt: str, schema, effort: str):
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[
                {
                    "type": "text",
                    "text": brief,
                    # Frozen prefix. Marked cacheable, though note each brief is
                    # ~700 tokens and the minimum cacheable prefix is ~1024, so
                    # today this mostly does not engage. It costs nothing to
                    # leave, and matters if the briefs grow.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            **self._request_extras(effort),
        )

        usage = getattr(response, "usage", None)
        counted = {"calls": 1}
        if usage is not None:
            counted["input"] = getattr(usage, "input_tokens", 0) or 0
            counted["output"] = getattr(usage, "output_tokens", 0) or 0
            counted["cache_read"] = getattr(usage, "cache_read_input_tokens", 0) or 0
        for key, value in counted.items():
            self._usage[key] = self._usage.get(key, 0) + value
            self.lifetime[key] = self.lifetime.get(key, 0) + value

        # A refusal is a 200 with no usable content. Treat it as "no opinion"
        # rather than letting a None reach the caller as a decision.
        if getattr(response, "stop_reason", None) == "refusal":
            raise DeskError(f"model declined: {getattr(response, 'stop_details', None)}")

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise DeskError("model returned no parseable output")
        return parsed

    def run(self, snapshot: dict) -> DeskResult | None:
        """Convene the desk. Returns None when the budget says not to."""
        allowed, why = self.budget.may_run()
        if not allowed:
            return None

        self.budget.record_run()
        self._usage = {"input": 0, "output": 0, "cache_read": 0, "calls": 0}
        market = json.dumps(snapshot, indent=2, default=str)
        prompt = (
            "Here is the current market snapshot and the desk's book.\n\n"
            f"{market}\n\n"
            "Give your view. Remember that having no view is a valid answer."
        )

        # The two analysts do not see each other's work, on purpose: shown a
        # colleague's opinion first, a model tends to agree with it, and two
        # correlated opinions are worth less than one independent one.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                "technical": pool.submit(
                    self._ask, roles.TECHNICAL_ANALYST, prompt,
                    AnalystReport, self.analyst_effort,
                ),
                "portfolio": pool.submit(
                    self._ask, roles.PORTFOLIO_ANALYST, prompt,
                    AnalystReport, self.analyst_effort,
                ),
            }
            reports = {}
            for seat, future in futures.items():
                try:
                    reports[seat] = future.result()
                except Exception as error:
                    # One seat failing is a quieter desk, not a broken one.
                    print(f"desk: {seat} seat failed: {error}")

        if not reports:
            raise DeskError("every analyst seat failed")

        views = {
            seat: [view.model_dump() for view in report.views]
            for seat, report in reports.items()
        }
        if not any(views.values()):
            # Nobody had anything. Do not spend two more calls confirming it.
            return DeskResult([], "No analyst had a view this run.", {}, views, dict(self._usage))

        chair_prompt = (
            f"{prompt}\n\n"
            "The analysts have reported:\n\n"
            f"{json.dumps(views, indent=2)}\n\n"
            "Decide what the desk does."
        )
        chair: ChairDecision = self._ask(
            roles.CHAIR, chair_prompt, ChairDecision, self.chair_effort
        )

        if not chair.decisions:
            return DeskResult([], chair.summary, {}, views, dict(self._usage))

        risk_prompt = (
            f"{prompt}\n\n"
            "The analysts reported:\n\n"
            f"{json.dumps(views, indent=2)}\n\n"
            "The chair has decided:\n\n"
            f"{json.dumps(chair.model_dump(), indent=2)}\n\n"
            "Review these decisions. Approve or veto each one."
        )
        ruling: RiskRuling = self._ask(
            roles.RISK_OFFICER, risk_prompt, RiskRuling, self.chair_effort
        )

        # Approval must be explicit. A symbol the risk seat did not mention at
        # all is not approved - silence is not consent on a risk desk.
        approved = {s.upper() for s in ruling.approved}
        survived = [d for d in chair.decisions if d.symbol.upper() in approved]
        vetoed = {
            d.symbol: ruling.reasoning
            for d in chair.decisions
            if d.symbol.upper() not in approved
        }

        return DeskResult(survived, chair.summary, vetoed, views, dict(self._usage))

    @property
    def usage(self) -> dict:
        return dict(self._usage)
