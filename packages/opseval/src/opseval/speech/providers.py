# SPDX-License-Identifier: Apache-2.0
"""Build the configured speech providers from :class:`~opseval.config.Settings` (ADR-0054).

Every provider, built-in or plugin, comes back wrapped in its guard
(:mod:`opseval.speech.guard`). Construction is where the configuration rules are enforced,
before anything is sent:

- the URL of every provider is allowlisted, with TLS unless it is loopback
  (``check_url_allowed``): TTS receives the customer's script, STT the agent's voice;
- a provider name is the built-in ``openai`` or a plugin, loaded fail closed
  (:func:`opseval.plugins.load_speech_provider`);
- the second STT must differ from the first in provider or model, or the run is refused: the
  same STT twice proves nothing (ADR-0053).

``None`` means "not configured": the built-in without a URL and a model. A plugin named in
the settings is always configured (it decides what it needs).
"""

from __future__ import annotations

from typing import Literal

from opseval.config import Settings, check_url_allowed
from opseval.errors import ConfigError
from opseval.llm import UsageMeter
from opseval.speech.base import SpeechConfig
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech
from opseval.speech.openai import OpenAISpeechToText, OpenAITextToSpeech

BUILTIN = "openai"


def _config(
    settings: Settings,
    *,
    role: Literal["tts", "stt"],
    provider: str,
    url: str | None,
    model: str | None,
    key: str | None,
    timeout: float,
    label: str,
) -> SpeechConfig:
    if url:
        check_url_allowed(url, settings.allowlist(), secure_schemes=("https",))
    return SpeechConfig(
        role=role,
        provider=provider,
        url=url,
        model=model,
        key=key,
        timeout_seconds=timeout,
        voice_map=dict(settings.tts_voice_map) if role == "tts" else {},
        secret=settings.secret_named,
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
        model=settings.tts_model,
        key=settings.tts_key.get_secret_value() if settings.tts_key else None,
        timeout=settings.tts_timeout_seconds,
        label="TTS",
    )
    if provider == BUILTIN:
        builtin = OpenAITextToSpeech(
            config, response_format=settings.tts_format, pcm_rate=settings.tts_pcm_rate
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
) -> GuardedSpeechToText:
    from opseval.plugins import STT_GROUP, load_speech_provider

    config = _config(
        settings,
        role="stt",
        provider=provider,
        url=url,
        model=model,
        key=key,
        timeout=settings.stt_timeout_seconds,
        label=label,
    )
    if provider == BUILTIN:
        return GuardedSpeechToText(OpenAISpeechToText(config), config, meter, trusted=True)
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
    )


__all__ = [
    "BUILTIN",
    "check_second_stt",
    "second_speech_to_text",
    "second_stt_configured",
    "speech_to_text",
    "stt_configured",
    "text_to_speech",
    "tts_configured",
]
