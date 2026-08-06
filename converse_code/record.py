"""Recording and inspecting assistant audio.

Audio bugs in this project have been diagnosed by guesswork more than once, and
guesswork lost every time. These helpers write the exact bytes that crossed the
wire to a WAV file, and describe them numerically, so "it sounds like noise" can
be turned into evidence: play the file, or read the statistics.
"""

import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from .audio import SAMPLE_RATE


class WavRecorder:
    """Appends PCM16 frames to a WAV file, finalising the header on close."""

    def __init__(self, path: str | Path, rate: int = SAMPLE_RATE):
        self.path = Path(path)
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(rate)
        self.bytes_written = 0
        self._closed = False

    def add(self, pcm16: bytes) -> None:
        if self._closed or not pcm16:
            return
        usable = len(pcm16) - (len(pcm16) % 2)
        if usable <= 0:
            return
        self._wav.writeframes(pcm16[:usable])
        self.bytes_written += usable

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wav.close()

    @property
    def seconds(self) -> float:
        return self.bytes_written / 2 / SAMPLE_RATE


@dataclass
class AudioReport:
    samples: int
    seconds: float
    peak: float
    rms: float
    clipped: int
    dc_offset: float
    max_step: float
    big_steps: int          # neighbour jumps too large for speech at this rate
    silent_gaps_ms: list[int]

    @property
    def has_stalled_stream_gaps(self) -> bool:
        """Whether silence resembles a repeatedly stalled audio stream.

        A single pause, or a couple of pauses between words, is normal speech.
        Repeated gaps occupying a large share of the recording are the useful
        failure signature here.
        """
        return (
            len(self.silent_gaps_ms) >= 3
            and sum(self.silent_gaps_ms) > self.seconds * 1000 * 0.25
        )

    @property
    def looks_like_speech(self) -> bool:
        """Speech is smooth, well inside full scale, and not mostly silence.

        Noise from a format mismatch fails on `big_steps`: neighbouring samples
        are uncorrelated, so the waveform jumps wildly. A stalled or chopped
        stream fails on `silent_gaps_ms`.
        """
        return (
            self.samples > 0
            and self.rms > 0.001
            and self.peak <= 1.0
            and self.big_steps == 0
            and not self.has_stalled_stream_gaps
        )

    def summary(self) -> str:
        verdict = "looks like speech" if self.looks_like_speech else "does NOT look like speech"
        lines = [
            f"{self.seconds:.2f}s ({self.samples} samples) — {verdict}",
            f"  peak={self.peak:.4f} rms={self.rms:.4f} dc={self.dc_offset:+.5f} clipped={self.clipped}",
            f"  largest jump between samples={self.max_step:.4f} (jumps too big for speech: {self.big_steps})",
        ]
        if self.silent_gaps_ms:
            lines.append(f"  silent gaps mid-stream: {self.silent_gaps_ms[:8]} ms")
        return "\n".join(lines)


def analyse_pcm16(data: bytes, rate: int = SAMPLE_RATE) -> AudioReport:
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return AudioReport(0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0, [])
    raw = struct.unpack(f"<{usable // 2}h", data[:usable])
    samples = [v / 32768.0 for v in raw]
    n = len(samples)

    peak = max(abs(s) for s in samples)
    rms = (sum(s * s for s in samples) / n) ** 0.5
    dc = sum(samples) / n
    clipped = sum(1 for s in samples if abs(s) >= 0.999)

    # At 16 kHz, speech energy stops well below Nyquist, so consecutive samples
    # cannot differ by much. Uncorrelated noise routinely exceeds this.
    step_limit = 0.6
    max_step = 0.0
    big_steps = 0
    for i in range(1, n):
        step = abs(samples[i] - samples[i - 1])
        max_step = max(max_step, step)
        if step > step_limit:
            big_steps += 1

    gaps: list[int] = []
    run = 0
    for i, s in enumerate(samples):
        if s == 0.0:
            run += 1
            continue
        if run > rate * 0.08 and i - run > 0:   # ignore leading silence
            gaps.append(round(run / rate * 1000))
        run = 0

    return AudioReport(
        samples=n,
        seconds=n / rate,
        peak=peak,
        rms=rms,
        clipped=clipped,
        dc_offset=dc,
        max_step=max_step,
        big_steps=big_steps,
        silent_gaps_ms=gaps,
    )
