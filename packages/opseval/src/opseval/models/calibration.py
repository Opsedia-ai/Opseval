# SPDX-License-Identifier: Apache-2.0
"""Judge calibration: how well a scorer agrees with human labels (ADR-0018).

Every model forbids unknown fields, so a mistake in a label file fails at load time
instead of quietly shrinking the sample a judge is measured on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CALIBRATION_SCHEMA = "opseval/v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Label(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


REVIEW_SENTINEL = "REVIEW"
"""What ``--template-for`` writes. Rejected on load, so a half-filled template cannot be
scored as if every label agreed with the judge."""


class HumanLabel(Strict):
    """One human judgement of one scorer on one attempt."""

    test_id: str = Field(min_length=1)
    attempt_index: Annotated[int, Field(ge=0)]
    scorer_id: str = Field(min_length=1)
    verdict: Label
    note: str | None = Field(default=None, max_length=2000)


class LabelSet(Strict):
    schema_: str = Field(default=CALIBRATION_SCHEMA, alias="schema")
    labels: list[HumanLabel] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AgreementStatus(StrEnum):
    AGREES = "agrees"
    DISAGREES = "disagrees"
    NOT_MEASURED = "not_measured"
    """Too few usable labels to say anything. Never reported as agreement."""


class ScorerAgreement(Strict):
    """How one scorer compared with the humans, on the labels that could be used."""

    scorer_id: str
    status: AgreementStatus
    reason: str | None = None

    labels: int = 0
    """Labels supplied for this scorer."""
    usable: int = 0
    """Labels whose score was actually measured (``infra_error`` cannot be compared)."""
    not_measured: int = 0
    unmatched: int = 0
    """Labels with no corresponding score in the run."""

    agreed: int = 0
    agreement: float | None = None
    """``None`` when there is nothing to compare — never 0.0."""
    kappa: float | None = None
    """Cohen's kappa, or ``None`` when it is undefined for this sample."""
    kappa_note: str | None = None

    false_pass: int = 0
    """Judge passed what a human failed. The dangerous direction: a false green."""
    false_fail: int = 0
    false_pass_rate: float | None = None
    """Of the attempts humans failed, the fraction the judge passed. ``None`` if humans
    failed none, because a rate over an empty denominator is not 0."""


class CalibrationResult(Strict):
    schema_version: str = CALIBRATION_SCHEMA
    opseval_version: str
    run_id: str
    suite: str
    labelled_by: str | None = None
    scorers: list[ScorerAgreement] = Field(default_factory=list)
    min_labels: int
    min_agreement: float
    max_false_pass_rate: float
    exit_code: int
    summary: str

    @property
    def status(self) -> Literal["agrees", "disagrees", "not_measured"]:
        if any(s.status is AgreementStatus.DISAGREES for s in self.scorers):
            return "disagrees"
        if not self.scorers or any(s.status is AgreementStatus.NOT_MEASURED for s in self.scorers):
            return "not_measured"
        return "agrees"


__all__ = [
    "CALIBRATION_SCHEMA",
    "AgreementStatus",
    "CalibrationResult",
    "HumanLabel",
    "Label",
    "LabelSet",
    "ScorerAgreement",
]
