# SPDX-License-Identifier: Apache-2.0
"""Entry-point discovery for third-party plugins (FR-SCR-10, ADR-0022).

A package declares a scorer in its own metadata::

    [project.entry-points."opseval.scorers"]
    acme_tone = "acme_opseval.scorers:ToneScorer"

and Opseval registers it alongside the built-ins.

**Discovery fails closed and loudly.** A plugin that cannot be imported, does not satisfy
the contract, or collides with an existing id raises :class:`ConfigError` (exit code 3)
rather than being skipped. Skipping would be worse than it sounds: a suite naming that
scorer would fall back to ``UNKNOWN_SCORER``, and because
:func:`~opseval.verdicts.attempt_verdict` drops ``infra_error`` from the measured set, a
scenario with one other passing scorer would still report green with the plugin's check
silently gone. A broken plugin must stop the run, not quietly narrow it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from opseval.errors import ConfigError
from opseval.models.results import Axis
from opseval.scoring.base import REGISTRY, Scorer

SCORER_GROUP = "opseval.scorers"
SINK_GROUP = "opseval.sinks"
VALID_REQUIRES = ("frames", "trace", "llm")

_loaded: set[str] = set()
"""Entry-point names already processed, so discovery is idempotent."""

THIRD_PARTY: dict[str, str] = {}
"""Scorer id -> the distribution that provides it, for ``opseval list-scorers``."""


def _iter_entry_points(group: str) -> Iterator[EntryPoint]:
    yield from entry_points(group=group)


def check_contract(obj: Any, *, source: str) -> type[Scorer]:
    """Validate one plugin against the scorer contract. Raises :class:`ConfigError`.

    Checked here rather than trusted, because a scorer that looks like one but behaves
    differently produces results the rest of the harness will treat as evidence.
    """
    if not inspect.isclass(obj) or not issubclass(obj, Scorer):
        raise ConfigError(
            f"{source}: a scorer plugin must be a class deriving from opseval.scoring.Scorer, "
            f"got {obj!r}"
        )
    scorer_id = getattr(obj, "id", None)
    if not isinstance(scorer_id, str) or not scorer_id:
        raise ConfigError(f"{source}: scorer {obj.__name__} has no 'id'")
    if not isinstance(getattr(obj, "axis", None), Axis):
        raise ConfigError(
            f"{source}: scorer {scorer_id!r} has no valid 'axis' "
            f"(one of {', '.join(a.value for a in Axis)})"
        )
    requires = getattr(obj, "requires", None)
    if requires not in VALID_REQUIRES:
        raise ConfigError(
            f"{source}: scorer {scorer_id!r} declares requires={requires!r}; "
            f"expected one of {', '.join(VALID_REQUIRES)}"
        )
    score = getattr(obj, "score", None)
    if not callable(score) or not inspect.iscoroutinefunction(score):
        raise ConfigError(f"{source}: scorer {scorer_id!r} must define 'async def score(...)'")
    version = getattr(obj, "version", None)
    if not isinstance(version, str) or not version:
        raise ConfigError(f"{source}: scorer {scorer_id!r} has no 'version' string")
    return obj


def load_scorers(*, group: str = SCORER_GROUP) -> dict[str, str]:
    """Register every declared scorer plugin. Returns ``{scorer id: distribution}``.

    Idempotent: calling it twice does not re-register, so importing Opseval from several
    places cannot trip the duplicate-id guard.
    """
    found: dict[str, str] = {}
    for entry in sorted(_iter_entry_points(group), key=lambda e: e.name):
        key = f"{group}:{entry.name}"
        if key in _loaded:
            continue
        source = f"plugin {entry.name!r} ({entry.value})"
        try:
            obj = entry.load()
        except Exception as exc:
            raise ConfigError(
                f"{source} could not be imported: {type(exc).__name__}: {exc}. "
                "Fix or uninstall it; Opseval will not run with a broken scorer plugin."
            ) from exc
        cls = check_contract(obj, source=source)
        if cls.id in REGISTRY and REGISTRY[cls.id] is not cls:
            raise ConfigError(
                f"{source} declares scorer id {cls.id!r}, which is already registered. "
                "Scorer ids must be unique; prefix yours with your own namespace."
            )
        if cls.id not in REGISTRY:
            REGISTRY[cls.id] = cls
        distribution = getattr(entry, "dist", None)
        name = getattr(distribution, "name", None) or entry.name
        THIRD_PARTY[cls.id] = name
        found[cls.id] = name
        _loaded.add(key)
    return found


def check_sink_contract(obj: Any, *, source: str) -> type[Any]:
    """Validate one sink plugin. Raises :class:`ConfigError`."""
    from opseval.sinks.base import Sink

    if not inspect.isclass(obj) or not issubclass(obj, Sink):
        raise ConfigError(
            f"{source}: a sink plugin must be a class deriving from opseval.sinks.Sink, got {obj!r}"
        )
    name = getattr(obj, "name", None)
    if not isinstance(name, str) or not name or name == "sink":
        raise ConfigError(
            f"{source}: sink {obj.__name__} needs its own 'name' (not the default 'sink')"
        )
    publish = getattr(obj, "publish", None)
    if not callable(publish) or not inspect.iscoroutinefunction(publish):
        raise ConfigError(f"{source}: sink {name!r} must define 'async def publish(result)'")
    return obj


def load_sinks(*, group: str = SINK_GROUP) -> dict[str, type[Any]]:
    """Discover sink plugins (ADR-0027). Fails closed, like scorers.

    A sink that cannot be imported is refused rather than skipped: an operator who
    installed a publisher and got silence would reasonably assume it had published.
    """
    found: dict[str, type[Any]] = {}
    for entry in sorted(_iter_entry_points(group), key=lambda e: e.name):
        key = f"{group}:{entry.name}"
        if key in _loaded:
            continue
        source = f"sink plugin {entry.name!r} ({entry.value})"
        try:
            obj = entry.load()
        except Exception as exc:
            raise ConfigError(
                f"{source} could not be imported: {type(exc).__name__}: {exc}. "
                "Fix or uninstall it; Opseval will not run with a broken sink plugin."
            ) from exc
        cls = check_sink_contract(obj, source=source)
        found[cls.name] = cls
        _loaded.add(key)
    return found


def reset_for_tests() -> None:
    """Forget what discovery has seen (tests only)."""
    _loaded.clear()
    THIRD_PARTY.clear()


__all__ = [
    "SCORER_GROUP",
    "SINK_GROUP",
    "THIRD_PARTY",
    "VALID_REQUIRES",
    "check_contract",
    "check_sink_contract",
    "load_scorers",
    "load_sinks",
    "reset_for_tests",
]
