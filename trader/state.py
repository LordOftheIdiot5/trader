"""Deciding when a published snapshot is worth redeploying the site for.

Small, but it lives here rather than in the runner so it can be tested. The
cost of getting it wrong is not subtle: on a five-minute loop, treating every
tick as a change means 288 commits and 288 Pages deploys a day, for a
dashboard that mostly said the same thing.
"""

from __future__ import annotations

import hashlib
import json

# Fields that move on every tick regardless of whether anything happened.
VOLATILE_FIELDS = ("generated_at",)


def material_fingerprint(snapshot: dict) -> str:
    """Hash of the parts of a snapshot that represent actual state.

    Everything except the volatile fields: risk counters, venue balances,
    positions, and the recent journal entries. Any of those changing is a
    real event worth showing; the clock ticking is not.
    """
    material = {
        key: value
        for key, value in snapshot.items()
        if key not in VOLATILE_FIELDS
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()
