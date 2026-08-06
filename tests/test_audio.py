"""Downlink audio format is pinned, not inferred.

The broker default changed once (pcm_f32le -> pcm16) and silently produced pure
noise in a client that assumed the old format. Pinning it in the start frame
means a future change surfaces as a rejected session, not as noise.
"""

from converse_code.audio import OUTPUT_ENCODING, SAMPLE_RATE
from converse_code.tools import manifest  # noqa: F401  (import smoke)


def test_encoding_and_rate_are_pinned():
    assert OUTPUT_ENCODING == "pcm16"
    assert SAMPLE_RATE == 16000
