# SPDX-License-Identifier: Apache-2.0
"""Language packs, re-exported: they live in :mod:`opseval.langpacks` (ADR-0059) so the
catalogue can check a persona without importing the simulator."""

from __future__ import annotations

from opseval.langpacks import (
    PACKS,
    LanguagePack,
    Resolution,
    UnknownLanguage,
    lookup,
    register,
    resolve,
)

__all__ = [
    "PACKS",
    "LanguagePack",
    "Resolution",
    "UnknownLanguage",
    "lookup",
    "register",
    "resolve",
]
