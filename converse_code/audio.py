"""Downlink audio format handling.

The broker's assistant-audio frames are PCM16 little-endian mono 16 kHz — its
current wire default, and what we pin explicitly in the start frame via
`audio.output_encoding` so a future default change can't silently break us.

The vendored browser SDK (`@trelis/converse` 0.4.5 from npm) decodes `pcm_f32le`,
which was the *previous* server default; the published package has not caught up
with the server yet. Rather than editing vendored code, we convert here — one
place, under our control, tested — and hand the page the Float32 its decoder
expects.

(Worth revisiting once a newer SDK is published: then this becomes a passthrough
and `OUTPUT_ENCODING` can go straight to the page.)
"""

import struct

OUTPUT_ENCODING = "pcm16"
SAMPLE_RATE = 16000


def pcm16_to_f32le(data: bytes) -> bytes:
    """Convert PCM16-LE bytes to Float32-LE bytes in [-1.0, 1.0].

    A trailing odd byte is dropped rather than raising: a partial sample is not
    worth killing an audio stream over.
    """
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return b""
    samples = struct.unpack(f"<{usable // 2}h", data[:usable])
    return struct.pack(
        f"<{len(samples)}f",
        *[s / 32768.0 if s < 0 else s / 32767.0 for s in samples],
    )
