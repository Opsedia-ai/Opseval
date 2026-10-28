# SPDX-License-Identifier: Apache-2.0
"""Speech providers for voice evaluation (ADR-0054): the contracts, the built-in
OpenAI-compatible clients, the guards every call goes through, and the WER of the STT canary.

Build providers with :mod:`opseval.speech.providers`; never call one unguarded.
"""

from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript
from opseval.speech.wer import word_error_rate

__all__ = [
    "Audio",
    "SpeechConfig",
    "SpeechToText",
    "TextToSpeech",
    "Transcript",
    "word_error_rate",
]
