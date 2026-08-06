import math
import struct
import wave

from converse_code.record import WavRecorder, analyse_pcm16
from converse_code.cli import _record_and_relay_audio


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


def test_recorder_writes_exact_complete_pcm16_frames(tmp_path):
    path = tmp_path / "downlink.wav"
    recorder = WavRecorder(path)
    recorder.add(b"\x01\x02\x03")
    recorder.add(b"\x04\x05\x06")
    recorder.close()
    recorder.close()

    assert recorder.bytes_written == 4
    assert recorder.seconds == 2 / RATE
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == RATE
        assert wav.readframes(wav.getnframes()) == b"\x01\x02\x04\x05"


def test_recorder_duration_uses_its_configured_sample_rate(tmp_path):
    recorder = WavRecorder(tmp_path / "eight-khz.wav", rate=8_000)
    recorder.add(b"\x00\x00" * 4_000)
    recorder.close()

    assert recorder.seconds == 0.5


async def test_record_audio_relay_captures_the_exact_bytes_sent_to_page(tmp_path):
    class FakeServer:
        def __init__(self):
            self.frames = []

        async def send_audio_to_proxy(self, frame):
            self.frames.append(frame)

    frame = _tone(0.01)
    path = tmp_path / "relay.wav"
    recorder = WavRecorder(path)
    server = FakeServer()

    await _record_and_relay_audio(recorder, server, frame)
    recorder.close()

    assert server.frames == [frame]
    with wave.open(str(path), "rb") as wav:
        assert wav.readframes(wav.getnframes()) == frame
