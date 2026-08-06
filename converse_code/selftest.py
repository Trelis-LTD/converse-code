"""`converse-code selftest` — is the audio path healthy, without a browser?

Speaks a synthesized sentence into a real Converse session, records the reply,
and reports whether what came back looks like speech. This isolates the layers
that have each been the culprit at some point:

  session/wire wrong -> the recording itself is noise or silent
  session/wire fine  -> the recording is clean speech, so anything you hear
                        wrong is in the browser page or the audio device

The WAV is left on disk so it can be played: ears are the final judge.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from . import broker as brokermod
from . import config, tools
from .record import analyse_pcm16

PROMPT = "Please count slowly from one to five."


def _synthesise(text: str) -> bytes | None:
    """16 kHz mono PCM16 of `text` using macOS `say`, or None if unavailable."""
    if not (shutil.which("say") and shutil.which("afconvert")):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="converse-selftest-"))
    aiff, wav = tmp / "p.aiff", tmp / "p.wav"
    try:
        subprocess.run(["say", "-o", str(aiff), text], check=True, capture_output=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
            check=True, capture_output=True,
        )
        with wave.open(str(wav), "rb") as w:
            return w.readframes(w.getnframes())
    except (subprocess.CalledProcessError, wave.Error):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def run(broker_url: str, out_dir: Path | None = None) -> int:
    api_key = config.get_api_key()
    if not api_key:
        print("No API key. Run: converse-code login", file=sys.stderr)
        return 1

    print("1. checking credentials…", flush=True)
    try:
        if not await brokermod.validate_key(api_key, url=broker_url):
            print("   REJECTED — run: converse-code login", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"   could not reach Converse: {exc}", file=sys.stderr)
        return 1
    print("   ok", flush=True)

    speech = _synthesise(PROMPT)
    if speech is None:
        print("Cannot synthesise a prompt (needs macOS `say`/`afconvert`).", file=sys.stderr)
        return 1
    print(f"2. speaking {len(speech)/2/16000:.1f}s into a real session: {PROMPT!r}", flush=True)

    client = brokermod.BrokerClient(
        api_key, session_id=f"cc-selftest-{os.urandom(3).hex()}",
        tools=tools.manifest(), url=broker_url,
    )
    downlink = bytearray()
    heard: list[str] = []
    said: list[str] = []
    done = asyncio.Event()

    async def on_json(msg: dict) -> None:
        kind = msg.get("type")
        if kind == "asr" and msg.get("text"):
            heard.append(msg["text"])
        elif kind == "utterance" and msg.get("text"):
            said.append(msg["text"])
        elif kind in ("done", "bye"):
            done.set()

    client.on_json = on_json
    client.on_audio = lambda frame: _collect(downlink, frame)
    client.on_tool_call = lambda call: asyncio.sleep(0)   # none expected here

    await client.connect()
    runner = asyncio.create_task(client.run())
    try:
        for i in range(0, len(speech), 640):
            await client.send_audio(speech[i : i + 640])
            await asyncio.sleep(0.02)
        for _ in range(90):                     # trailing silence ends the turn
            await client.send_audio(b"\x00\x00" * 320)
            await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(done.wait(), timeout=40)
        except asyncio.TimeoutError:
            print("   (no reply completed within 40s)", flush=True)
        await asyncio.sleep(1.0)
    finally:
        await client.close()
        await runner

    print(f"   it heard:  {heard[-1] if heard else '(nothing)'}")
    print(f"   it said:   {said[-1] if said else '(nothing)'}")

    if not downlink:
        print("\n3. NO AUDIO came back — nothing to inspect.", file=sys.stderr)
        print("   The session worked but sent no audio; that is a server-side or "
              "session-config problem, not a browser one.", file=sys.stderr)
        return 1

    out_dir = out_dir or Path(tempfile.gettempdir())
    out = out_dir / "converse-code-selftest.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(downlink))

    report = analyse_pcm16(bytes(downlink))
    print(f"\n3. reply audio, exactly as it arrived on the wire:")
    print(report.summary())
    print(f"\n   saved: {out}")
    print("   PLAY IT. That file is what the browser is given to play.")
    if report.looks_like_speech:
        print("\n   The wire is healthy. If what you hear is noise, the problem is")
        print("   in the browser page or the audio device — not the session.")
        return 0
    print("\n   The audio is already wrong before any browser touches it.", file=sys.stderr)
    return 1


async def _collect(buf: bytearray, frame: bytes) -> None:
    buf.extend(frame)
