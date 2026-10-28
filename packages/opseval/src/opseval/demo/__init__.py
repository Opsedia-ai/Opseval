# SPDX-License-Identifier: Apache-2.0
"""The reference refund agent and its example catalogue, used by ``opseval demo``."""

from importlib.resources import files
from pathlib import Path


def catalogue_dir() -> Path:
    return Path(str(files("opseval.demo") / "catalogue"))


def voice_catalogue_dir() -> Path:
    """The voice demo's catalogue (``opseval demo --voice``, ADR-0053)."""
    return Path(str(files("opseval.demo") / "voice_catalogue"))
