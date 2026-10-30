# SPDX-License-Identifier: Apache-2.0
"""Scoring: deterministic scorers and LLM judges behind one registry."""

from opseval.scoring import content as _content  # noqa: F401 - registers content.* checks
from opseval.scoring import deterministic as _deterministic  # noqa: F401 - registers scorers
from opseval.scoring import judges as _judges  # noqa: F401 - registers judges
from opseval.scoring import rubric as _rubric  # noqa: F401 - registers judge.rubric
from opseval.scoring import tools as _tools  # noqa: F401 - registers tool scorers
from opseval.scoring import voice as _voice  # noqa: F401 - registers voice.intelligible
from opseval.scoring import voice_metrics as _voice_metrics  # noqa: F401 - voice.* metrics
from opseval.scoring.base import (
    ALWAYS_ON,
    DEFAULT_PACK,
    INVARIANTS,
    REGISTRY,
    NoParams,
    Scorer,
    ScoringContext,
    ScoringEngine,
    default_pack,
    describe_params,
    effective_specs,
    register,
)

__all__ = [
    "ALWAYS_ON",
    "DEFAULT_PACK",
    "INVARIANTS",
    "NoParams",
    "REGISTRY",
    "Scorer",
    "ScoringContext",
    "ScoringEngine",
    "default_pack",
    "describe_params",
    "effective_specs",
    "register",
]
