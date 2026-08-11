import math
import struct

from converse_code.record import analyse_pcm16


RATE = 16_000


def _tone(seconds: float, frequency: float = 220.0, amplitude: float = 0.1) -> bytes:
    samples = int(seconds * RATE)
    values = [
        round(32767 * amplitude * math.sin(2 * math.pi * frequency * i / RATE))
        for i in range(samples)
    ]
    return struct.pack(f"<{samples}h", *values)


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


def test_natural_speech_pauses_do_not_fail_audio_report():
    pcm = _tone(0.5) + _silence(0.2) + _tone(0.5) + _silence(0.25) + _tone(0.5)

    report = analyse_pcm16(pcm)

    assert report.silent_gaps_ms == [200, 250]
    assert report.looks_like_speech


def test_regular_stalled_stream_gaps_fail_audio_report():
    pcm = b"".join((_tone(0.15) + _silence(0.15)) for _ in range(4)) + _tone(0.15)

    report = analyse_pcm16(pcm)

    assert len(report.silent_gaps_ms) == 4
    assert sum(report.silent_gaps_ms) > report.seconds * 1000 * 0.25
    assert not report.looks_like_speech
