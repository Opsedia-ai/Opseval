# SPDX-License-Identifier: Apache-2.0
"""Scoring: deterministic scorers and LLM judges behind one registry."""

from opseval.scoring import deterministic as _deterministic  # noqa: F401 - registers scorers
from opseval.scoring import judges as _judges  # noqa: F401 - registers judges
from opseval.scoring import tools as _tools  # noqa: F401 - registers tool scorers
from opseval.scoring.base import (
    ALWAYS_ON,
    DEFAULT_PACK,
    INVARIANTS,
    REGISTRY,
    Scorer,
    ScoringContext,
    ScoringEngine,
    effective_specs,
    register,
)

__all__ = [
    "ALWAYS_ON",
    "DEFAULT_PACK",
    "INVARIANTS",
    "REGISTRY",
    "Scorer",
    "ScoringContext",
    "ScoringEngine",
    "effective_specs",
    "register",
]
