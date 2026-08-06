"""Downlink audio format.

Assistant-audio frames are PCM16 little-endian mono 16 kHz. That is the broker's
current wire default, but it has changed once before (from `pcm_f32le`), so the
start frame pins it explicitly rather than relying on the default — and the
vendored browser SDK decodes exactly this format, so frames pass straight
through to the page untouched.
"""

OUTPUT_ENCODING = "pcm16"
SAMPLE_RATE = 16000
