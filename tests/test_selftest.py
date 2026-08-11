import asyncio
import math
import struct
import wave

from converse_code import selftest


def _speech_pcm(seconds: float = 0.2) -> bytes:
    count = round(seconds * 16_000)
    values = [round(3000 * math.sin(2 * math.pi * 220 * i / 16_000)) for i in range(count)]
    return struct.pack(f"<{count}h", *values)


async def test_selftest_records_exact_wire_audio(monkeypatch, tmp_path, capsys):
    reply = _speech_pcm()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.on_json = None
            self.on_audio = None
            self.sent_reply = False
            self.closed = asyncio.Event()

        async def connect(self):
            pass

        async def run(self):
            await self.closed.wait()

        async def send_audio(self, frame):
            if not self.sent_reply:
                self.sent_reply = True
                await self.on_json({"type": "asr", "text": "one two three"})
                await self.on_json({"type": "utterance", "text": "one two three four five"})
                await self.on_audio(reply)
                await self.on_json({"type": "done"})

        async def close(self):
            self.closed.set()

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(selftest.config, "get_api_key", lambda: "ck_test")
    monkeypatch.setattr(selftest.converse, "validate_key", lambda *args, **kwargs: _true())
    monkeypatch.setattr(selftest.brokermod, "BrokerClient", FakeClient)
    monkeypatch.setattr(selftest, "_synthesise", lambda _text: _speech_pcm())
    monkeypatch.setattr(selftest.asyncio, "sleep", no_sleep)

    result = await selftest.run("wss://example.invalid", out_dir=tmp_path)

    assert result == 0
    with wave.open(str(tmp_path / "converse-code-selftest.wav"), "rb") as wav:
        assert wav.getframerate() == 16_000
        assert wav.readframes(wav.getnframes()) == reply
    output = capsys.readouterr().out
    assert "looks like speech" in output
    assert "one two three four five" in output


async def _true():
    return True


async def test_selftest_stops_before_network_without_api_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(selftest.config, "get_api_key", lambda: None)

    assert await selftest.run("wss://example.invalid", out_dir=tmp_path) == 1
    assert "No API key" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
