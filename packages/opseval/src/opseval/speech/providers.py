# SPDX-License-Identifier: Apache-2.0
"""Build the configured speech providers from :class:`~opseval.config.Settings` (ADR-0054).

Every provider, built-in or plugin, comes back wrapped in its guard
(:mod:`opseval.speech.guard`). Construction is where the configuration rules are enforced,
before anything is sent:

- every provider, built-in or plugin, needs a URL, and that URL is allowlisted, with TLS
  unless it is loopback (``check_url_allowed``): TTS receives the customer's script, STT the
  agent's voice, and a plugin with no URL could send them anywhere unchecked;
- a provider name is the built-in ``openai`` or a plugin, loaded fail closed
  (:func:`opseval.plugins.load_speech_provider`);
- a plugin reads secrets only by its own settings' names (``OPSEVAL_TTS_*`` for TTS,
  ``OPSEVAL_STT_*`` for the STT, ``OPSEVAL_STT_SECOND_*`` for the second STT), never another
  role's key or an unrelated secret;
- the second STT must differ from the first in provider or model, or the run is refused: the
  same STT twice proves nothing (ADR-0053);
- an operator canary (``OPSEVAL_STT_CANARY_WAV``) comes with its words and its language.

:func:`check_speech_settings` applies every rule at once, before anything is sent; building
a provider applies its own again, so a provider is never built from a refused setting.

``None`` means "not configured": the built-in without a URL and a model. A plugin named in
the settings is always configured, and refused without a URL.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from opseval.config import Settings, check_url_allowed
from opseval.errors import ConfigError
from opseval.llm import UsageMeter
from opseval.speech.base import SpeechConfig
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech
from opseval.speech.openai import OpenAISpeechToText, OpenAITextToSpeech

BUILTIN = "openai"

LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
"""A BCP-47 language tag, as far as the canary needs one (``en``, ``ar-EG``)."""

SECRET_PREFIXES = {
    "TTS": ("OPSEVAL_TTS_",),
    "STT": ("OPSEVAL_STT_",),
    "second STT": ("OPSEVAL_STT_SECOND_",),
}
"""The secret names a speech plugin may read, by which provider it is."""


def _secret_reader(settings: Settings, label: str) -> Callable[[str], str | None]:
    """``SpeechConfig.secret`` for one provider: only names of its own settings. The first
    STT may not read the second's (another vendor's key), nor any role another's."""
    prefixes = SECRET_PREFIXES[label]

    def read(name: str) -> str | None:
        own = isinstance(name, str) and name.startswith(prefixes)
        if label == "STT" and own and name.startswith("OPSEVAL_STT_SECOND_"):
            own = False
        if not own:
            raise ConfigError(
                f"the {label} provider may read only secrets named "
                f"{' or '.join(p + '*' for p in prefixes)}"
                + (" (not OPSEVAL_STT_SECOND_*)" if label == "STT" else "")
            )
        return settings.secret_named(name)

    return read


def _check_url(settings: Settings, url: str | None, *, provider: str, setting: str) -> str:
    """The provider's URL, allowlisted. Every provider needs one (ADR-0054)."""
    if not url:
        raise ConfigError(
            f"speech provider {provider!r} has no URL: set {setting}. Every TTS and STT, "
            "plugin or built-in, sends conversation content to one allowlisted host"
        )
    check_url_allowed(url, settings.allowlist(), secure_schemes=("https",))
    return url


def _config(
    settings: Settings,
    *,
    role: Literal["tts", "stt"],
    provider: str,
    url: str | None,
    url_setting: str,
    model: str | None,
    key: str | None,
    timeout: float,
    label: str,
) -> SpeechConfig:
    checked = _check_url(settings, url, provider=provider, setting=url_setting)
    return SpeechConfig(
        role=role,
        provider=provider,
        url=checked,
        model=model,
        key=key,
        timeout_seconds=timeout,
        voice_map=dict(settings.tts_voice_map) if role == "tts" else {},
        secret=_secret_reader(settings, label),
        label=label,
    )


def tts_configured(settings: Settings) -> bool:
    provider = settings.tts_provider.strip() or BUILTIN
    return provider != BUILTIN or bool(settings.tts_url and settings.tts_model)


def stt_configured(settings: Settings) -> bool:
    provider = settings.stt_provider.strip() or BUILTIN
    return provider != BUILTIN or bool(settings.stt_url and settings.stt_model)


def second_stt_configured(settings: Settings) -> bool:
    return bool(
        (settings.stt_second_provider or "").strip()
        or settings.stt_second_url
        or settings.stt_second_model
    )


def text_to_speech(settings: Settings, meter: UsageMeter) -> GuardedTextToSpeech | None:
    """The configured TTS, or ``None`` when none is."""
    if not tts_configured(settings):
        return None
    from opseval.plugins import TTS_GROUP, load_speech_provider

    provider = settings.tts_provider.strip() or BUILTIN
    config = _config(
        settings,
        role="tts",
        provider=provider,
        url=settings.tts_url,
        url_setting="OPSEVAL_TTS_URL",
        model=settings.tts_model,
        key=settings.tts_key.get_secret_value() if settings.tts_key else None,
        timeout=settings.tts_timeout_seconds,
        label="TTS",
    )
    if provider == BUILTIN:
        builtin = OpenAITextToSpeech(
            config,
            response_format=settings.tts_format,
            pcm_rate=settings.tts_pcm_rate,
            meter=meter,
        )
        return GuardedTextToSpeech(builtin, config, meter, trusted=True)
    return GuardedTextToSpeech(load_speech_provider(TTS_GROUP, provider, config), config, meter)


def _speech_to_text(
    settings: Settings,
    meter: UsageMeter,
    *,
    provider: str,
    url: str | None,
    model: str | None,
    key: str | None,
    label: str,
    setting: str,
    url_setting: str,
) -> GuardedSpeechToText:
    from opseval.plugins import STT_GROUP, load_speech_provider

    config = _config(
        settings,
        role="stt",
        provider=provider,
        url=url,
        url_setting=url_setting,
        model=model,
        key=key,
        timeout=settings.stt_timeout_seconds,
        label=label,
    )
    if provider == BUILTIN:
        builtin = OpenAISpeechToText(config, meter=meter)
        return GuardedSpeechToText(builtin, config, meter, trusted=True)
    plugin = load_speech_provider(STT_GROUP, provider, config, setting=setting)
    return GuardedSpeechToText(plugin, config, meter)


def speech_to_text(settings: Settings, meter: UsageMeter) -> GuardedSpeechToText | None:
    """The configured STT, or ``None`` when none is."""
    if not stt_configured(settings):
        return None
    return _speech_to_text(
        settings,
        meter,
        provider=settings.stt_provider.strip() or BUILTIN,
        url=settings.stt_url,
        model=settings.stt_model,
        key=settings.stt_key.get_secret_value() if settings.stt_key else None,
        label="STT",
        setting="OPSEVAL_STT_PROVIDER",
        url_setting="OPSEVAL_STT_URL",
    )


def check_second_stt(settings: Settings) -> None:
    """Refuse a second STT that cannot be a second opinion. Raises :class:`ConfigError`."""
    if not second_stt_configured(settings):
        return
    provider = (settings.stt_second_provider or "").strip() or BUILTIN
    if not stt_configured(settings):
        raise ConfigError(
            "OPSEVAL_STT_SECOND_* is set but no first STT is: set OPSEVAL_STT_URL and "
            "OPSEVAL_STT_MODEL (or OPSEVAL_STT_PROVIDER) first"
        )
    if provider == BUILTIN and not (settings.stt_second_url and settings.stt_second_model):
        raise ConfigError(
            "the second STT needs OPSEVAL_STT_SECOND_URL and OPSEVAL_STT_SECOND_MODEL"
        )
    first = (settings.stt_provider.strip() or BUILTIN, settings.stt_model)
    if (provider, settings.stt_second_model) == first:
        raise ConfigError(
            f"the second STT is the same provider and model as the first ({provider}, "
            f"{settings.stt_model or 'no model'}): the same STT twice proves nothing. "
            "Use a different provider or model, or unset OPSEVAL_STT_SECOND_*"
        )


def check_canary_settings(settings: Settings) -> None:
    """An operator canary is a WAV, its words and its language, all three or none: a WAV
    without its words cannot be scored, and one transcribed in the wrong language would make
    a healthy STT look deaf. Raises :class:`ConfigError`."""
    wav, text = settings.stt_canary_wav, settings.stt_canary_text
    language = settings.stt_canary_language
    if not (wav or text or language):
        return
    missing = [
        name
        for name, value in (
            ("OPSEVAL_STT_CANARY_WAV", wav),
            ("OPSEVAL_STT_CANARY_TEXT", text and text.strip()),
            ("OPSEVAL_STT_CANARY_LANGUAGE", language),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "the operator STT canary needs OPSEVAL_STT_CANARY_WAV, OPSEVAL_STT_CANARY_TEXT "
            f"and OPSEVAL_STT_CANARY_LANGUAGE together; missing {', '.join(missing)}"
        )
    if not LANGUAGE_TAG.match(language or ""):
        raise ConfigError(
            "OPSEVAL_STT_CANARY_LANGUAGE must be a BCP-47 language tag such as en or ar-EG"
        )


def check_speech_settings(settings: Settings) -> None:
    """Every rule above, for every configured provider, before anything is sent: each URL
    present and allowlisted, the second STT a real second opinion, the operator canary
    complete. Raises :class:`ConfigError` (exit 3)."""
    if tts_configured(settings):
        _check_url(
            settings,
            settings.tts_url,
            provider=settings.tts_provider.strip() or BUILTIN,
            setting="OPSEVAL_TTS_URL",
        )
    if stt_configured(settings):
        _check_url(
            settings,
            settings.stt_url,
            provider=settings.stt_provider.strip() or BUILTIN,
            setting="OPSEVAL_STT_URL",
        )
    check_second_stt(settings)
    if second_stt_configured(settings):
        _check_url(
            settings,
            settings.stt_second_url,
            provider=(settings.stt_second_provider or "").strip() or BUILTIN,
            setting="OPSEVAL_STT_SECOND_URL",
        )
    check_canary_settings(settings)


def second_speech_to_text(settings: Settings, meter: UsageMeter) -> GuardedSpeechToText | None:
    """The optional second STT check (ADR-0053), or ``None``. Refused (``ConfigError``) when
    it is only partly configured or the same as the first."""
    check_second_stt(settings)
    if not second_stt_configured(settings):
        return None
    key = settings.stt_second_key
    return _speech_to_text(
        settings,
        meter,
        provider=(settings.stt_second_provider or "").strip() or BUILTIN,
        url=settings.stt_second_url,
        model=settings.stt_second_model,
        key=key.get_secret_value() if key else None,
        label="second STT",
        setting="OPSEVAL_STT_SECOND_PROVIDER",
        url_setting="OPSEVAL_STT_SECOND_URL",
    )


__all__ = [
    "BUILTIN",
    "check_canary_settings",
    "check_second_stt",
    "check_speech_settings",
    "second_speech_to_text",
    "second_stt_configured",
    "speech_to_text",
    "stt_configured",
    "text_to_speech",
    "tts_configured",
]
