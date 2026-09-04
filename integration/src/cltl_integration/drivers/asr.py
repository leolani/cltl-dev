"""A recording stand-in for a speech recogniser.

Every ASR backend ``ASRContainer`` can build — Whisper, Wav2Vec, SpeechBrain,
Google — either pulls in torch or calls a paid cloud API. None of that belongs
in the tier that runs on every build, and none of it is what a *slice* test is
about: the contract between cltl-vad and cltl-asr is that a ``VadMentionEvent``
names an audio segment which ASR can fetch from storage and turn into an
``AsrTextSignalEvent``. Transcription accuracy is cltl-asr's own problem, and
tier 2 is where a real model gets exercised.

So this returns a fixed transcript and records what it was handed. That turns
the audio round-trip into something assertable: if the segment offsets in the
VAD event are wrong, or the storage service serves the wrong range, the sample
count here is wrong even though the pipeline still produces a text event.
"""
import threading
from dataclasses import dataclass
from typing import List

import numpy as np
from cltl.asr.api import ASR
from cltl.combot.infra.di_container import singleton
from cltl_service.asr.service import AsrService


@dataclass(frozen=True)
class Transcription:
    """One call into the recogniser."""

    samples: int
    sampling_rate: int


class RecordingASR(ASR):
    """Returns *transcript* for any input, and remembers every call."""

    def __init__(self, transcript: str = "the stub heard something"):
        self._transcript = transcript
        self._lock = threading.Lock()
        self._calls: List[Transcription] = []

    def speech_to_text(self, audio: np.ndarray, sampling_rate: int) -> str:
        with self._lock:
            self._calls.append(Transcription(len(audio), sampling_rate))

        return self._transcript

    @property
    def transcript(self) -> str:
        return self._transcript

    @property
    def calls(self) -> List[Transcription]:
        with self._lock:
            return list(self._calls)


def asr_override(asr: ASR) -> dict:
    """Container override wiring *asr* into the real ``AsrService``.

    Only the recogniser is replaced. ``AsrService.from_config`` still reads the
    topics, the buffering and gap-timeout behaviour, and the audio loader from
    configuration, so everything this slice is testing is the shipped code.
    """
    @property
    @singleton
    def asr_service(self):
        return AsrService.from_config(asr, self.event_bus, self.resource_manager,
                                      self.config_manager)

    return {"asr_service": asr_service}
