"""Downlink audio conversion.

The broker sends PCM16; the vendored browser SDK decodes Float32. This
conversion sits between them, so it is the one place a format mistake could
reintroduce the "pure noise" bug.
"""

import struct

from converse_code.audio import OUTPUT_ENCODING, pcm16_to_f32le


def test_converts_known_values():
    pcm = struct.pack("<5h", 0, 16384, -16384, 32767, -32768)
    floats = struct.unpack("<5f", pcm16_to_f32le(pcm))
    assert floats[0] == 0.0
    assert abs(floats[1] - 16384 / 32767) < 1e-6
    assert abs(floats[2] - -0.5) < 1e-6
    assert abs(floats[3] - 1.0) < 1e-6
    assert abs(floats[4] - -1.0) < 1e-6


def test_output_is_four_bytes_per_sample():
    """The SDK decoder rejects payloads that aren't divisible by 4."""
    out = pcm16_to_f32le(b"\x00\x01" * 320)
    assert len(out) == 320 * 4


def test_all_values_stay_in_range():
    pcm = struct.pack("<%dh" % 1000, *[(i * 67 - 32768) % 65536 - 32768 for i in range(1000)])
    floats = struct.unpack("<1000f", pcm16_to_f32le(pcm))
    assert all(-1.0 <= f <= 1.0 for f in floats)


def test_odd_trailing_byte_is_dropped_not_fatal():
    assert len(pcm16_to_f32le(b"\x01\x02\x03")) == 4
    assert pcm16_to_f32le(b"") == b""
    assert pcm16_to_f32le(b"\x01") == b""


def test_encoding_is_pinned():
    assert OUTPUT_ENCODING == "pcm16"
