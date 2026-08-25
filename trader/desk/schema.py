"""What each seat at the desk is allowed to say.

Structured output rather than prose, for one reason: a rationale is for a
human reading the journal later, but the *decision* has to be a value the
engine can act on without parsing English. Anything the model says outside
these fields is commentary.

Note what a Decision cannot express: a venue, an absolute quantity, or a
symbol outside the allowlist. Sizing is a fraction of available cash that the
caller converts, so a model that has convinced itself to bet everything still
produces an order the risk gate measures against the same caps as any other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Stance = Literal["buy", "sell", "hold"]


class View(BaseModel):
    """One analyst's opinion on one symbol."""

    symbol: str = Field(description="Exactly as given in the market snapshot.")
    stance: Stance
    conviction: int = Field(
        ge=1, le=5, description="1 is a passing thought, 5 is the strongest view held."
    )
    rationale: str = Field(
        max_length=400,
        description="Why, in one or two sentences. Concrete and falsifiable.",
    )
    key_risk: str = Field(
        max_length=300,
        description="The single most likely way this view turns out wrong.",
    )


class AnalystReport(BaseModel):
    views: list[View] = Field(
        description="One view per symbol you have an opinion on. Omit symbols you "
        "have nothing useful to say about - an empty list is a valid report."
    )


class Decision(BaseModel):
    """A concrete instruction the engine can turn into an order."""

    symbol: str
    action: Stance
    fraction_of_cash: float = Field(
        ge=0.0,
        le=0.25,
        description="For a buy: the share of available cash to commit, at most "
        "0.25. Ignored for sell (which always closes the whole position) and "
        "hold. Sizing is a fraction so a smaller account bets smaller.",
    )
    rationale: str = Field(max_length=400)


class ChairDecision(BaseModel):
    decisions: list[Decision] = Field(
        description="Only symbols you are acting on. Doing nothing is the "
        "common and correct answer; return an empty list for it."
    )
    summary: str = Field(
        max_length=600,
        description="What the desk collectively concluded, including where the "
        "analysts disagreed. Disagreement is signal, not noise to smooth over.",
    )


class RiskRuling(BaseModel):
    """The risk seat's verdict. A veto here is final."""

    approved: list[str] = Field(
        description="Symbols from the chair's list that may proceed."
    )
    vetoed: list[str] = Field(
        description="Symbols that must not proceed."
    )
    reasoning: str = Field(
        max_length=600,
        description="Why anything was vetoed. If nothing was, say what you checked.",
    )
