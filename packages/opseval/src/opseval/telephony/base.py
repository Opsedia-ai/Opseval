# SPDX-License-Identifier: Apache-2.0
"""The telephony provider contract (ADR-0056), and the rules every phone call follows.

A :class:`TelephonyProvider` turns a profile's ``telephony:`` block into an
:class:`~opseval.transport.audio.AudioTransport` whose sessions are phone calls. The voice
bridge, the duplex driver, the scorers and the record then run on the call unchanged.

Twilio is built in (:mod:`opseval.telephony.twilio`). Any other provider, such as a SIP stack,
is a plugin of the ``opseval.telephony`` entry-point group, loaded only when the operator
names it in ``OPSEVAL_TELEPHONY`` and failing closed (:func:`opseval.plugins.
load_telephony_provider`).

Rules every provider gets for free, because they are applied here and not left to it:

- the number called must be in ``OPSEVAL_TELEPHONY_ALLOWED_NUMBERS`` (no wildcard): Opseval
  never dials a real customer (:func:`check_allowed_number`);
- a run may place at most ``OPSEVAL_TELEPHONY_MAX_CALLS`` calls (:func:`check_call_budget`);
- phone numbers never reach a report: a record keeps their last four digits (:func:`last4`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

from opseval.errors import ConfigError
from opseval.models.preflight import HealthCheck
from opseval.models.profile import E164, SutProfile, TelephonyConfig
from opseval.transport.audio import AudioTransport

if TYPE_CHECKING:
    from opseval.config import Settings
    from opseval.llm import UsageMeter
    from opseval.models.scenario import Scenario

Observable = Literal["dtmf", "hangup", "transfer"]
BUILTIN = "twilio"


@dataclass(frozen=True)
class TelephonyContext:
    """What a provider is given to build a transport or check its own health."""

    profile: SutProfile
    settings: Settings
    meter: UsageMeter | None = None
    """Where each call's cost goes (role ``telephony``), when the provider reports it."""

    @property
    def telephony(self) -> TelephonyConfig:
        if self.profile.telephony is None:
            raise ConfigError(
                f"profile {self.profile.name!r} has no telephony: block, which "
                "--transport phone needs (provider, to, from; ADR-0056)"
            )
        return self.profile.telephony


class TelephonyProvider(ABC):
    """A way to place phone calls. One instance serves a whole run; :meth:`transport` is
    called once per profile, and the transport opens one session (one call) per attempt."""

    name: ClassVar[str] = ""
    """The name a profile's ``telephony.provider`` uses (and ``OPSEVAL_TELEPHONY`` lists)."""
    capabilities: ClassVar[frozenset[str]] = frozenset()
    """What its sessions can do (``dtmf``, ``duplex``, ``realtime``), known before a call so a
    scenario the line cannot honour is refused at launch."""
    observes: ClassVar[tuple[Observable, ...]] = ()
    """What the agent does that the provider can see: a scorer reads an event it cannot see as
    *not measured*, never as "the agent did not"."""

    @abstractmethod
    def transport(self, context: TelephonyContext) -> AudioTransport:
        """The transport for ``context.profile``. Refuses a configuration it cannot use with a
        :class:`ConfigError` (exit 3) before anything is dialled."""

    async def preflight(self, context: TelephonyContext) -> list[HealthCheck]:
        """Health checks for the line (credentials, the inbound endpoint). A refusal of
        Opseval's own configuration (a rejected key) raises :class:`ConfigError`; an outage is
        an ``unavailable`` check, which stops the run as well (exit 3)."""
        return []


def allowed_numbers(settings: Settings) -> set[str]:
    """``OPSEVAL_TELEPHONY_ALLOWED_NUMBERS``, each checked: a malformed entry is refused, not
    skipped, so a typo cannot leave the list silently shorter."""
    found: set[str] = set()
    for raw in settings.telephony_allowed_numbers.split(","):
        number = raw.strip()
        if not number:
            continue
        if E164.fullmatch(number) is None:
            raise ConfigError(
                "OPSEVAL_TELEPHONY_ALLOWED_NUMBERS holds an entry that is not an E.164 number "
                f"(ending {last4(number) or '?'}); list numbers as +15551234567, comma-separated"
            )
        found.add(number)
    return found


def check_allowed_number(config: TelephonyConfig, settings: Settings) -> None:
    """Refuse (exit 3) a call to a number the operator has not listed as a test number."""
    if config.to not in allowed_numbers(settings):
        raise ConfigError(
            f"telephony.to (ending {last4(config.to)}) is not in "
            "OPSEVAL_TELEPHONY_ALLOWED_NUMBERS: Opseval calls only numbers listed there as the "
            "agent's test numbers, so it can never dial a real customer"
        )


def check_call_budget(scenarios: Iterable[Scenario], settings: Settings) -> int:
    """How many calls the run would place (one per attempt), or :class:`ConfigError` when that
    is more than ``OPSEVAL_TELEPHONY_MAX_CALLS``: every call is billed."""
    calls = sum(s.execution.repeats for s in scenarios if not s.is_blocked)
    if calls > settings.telephony_max_calls:
        raise ConfigError(
            f"this run would place {calls} phone calls, more than "
            f"OPSEVAL_TELEPHONY_MAX_CALLS ({settings.telephony_max_calls}); run fewer scenarios "
            "or repeats, or raise the cap"
        )
    return calls


def last4(number: str | None) -> str | None:
    """The last four digits of a phone number, the only part a record keeps."""
    if not number:
        return None
    digits = "".join(ch for ch in number if ch.isdigit())
    return digits[-4:] or None


def masked(number: str | None) -> str:
    """``tel:…1234``: how a phone target is shown (records, reports, logs)."""
    return f"tel:…{last4(number) or '????'}"


def provider_names(settings: Settings) -> list[str]:
    return [n.strip() for n in settings.telephony.split(",") if n.strip()]


def provider_for(profile: SutProfile, settings: Settings) -> TelephonyProvider:
    """The provider the profile names: the built-in Twilio, or a plugin named in
    ``OPSEVAL_TELEPHONY`` too (only that one is imported). Anything else is exit 3."""
    if profile.telephony is None:
        raise ConfigError(
            f"profile {profile.name!r} has no telephony: block, which --transport phone needs "
            "(provider, to, from; ADR-0056)"
        )
    name = profile.telephony.provider
    if name == BUILTIN:
        from opseval.telephony.twilio import TwilioProvider

        return TwilioProvider()
    if name not in provider_names(settings):
        raise ConfigError(
            f"profile {profile.name!r} uses telephony provider {name!r}, which is not named in "
            "OPSEVAL_TELEPHONY: a telephony plugin is loaded only when the operator names it"
        )
    from opseval.plugins import load_telephony_provider

    provider: TelephonyProvider = load_telephony_provider(name)
    return provider


__all__ = [
    "BUILTIN",
    "Observable",
    "TelephonyContext",
    "TelephonyProvider",
    "allowed_numbers",
    "check_allowed_number",
    "check_call_budget",
    "last4",
    "masked",
    "provider_for",
    "provider_names",
]
