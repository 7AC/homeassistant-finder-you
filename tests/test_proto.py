"""Tests for the minimal protobuf encoder/decoder."""

from __future__ import annotations

import pytest

from custom_components.finder_you.api.proto import (
    build_client_info,
    field_string,
    field_varint,
    parse_fields,
    parse_varint,
    varint,
)


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (143, b"\x8f\x01"),
    ],
)
def test_varint_encode(n, expected):
    assert varint(n) == expected


@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 16384, 2**32 - 1])
def test_parse_varint_roundtrip(n):
    enc = varint(n)
    value, pos = parse_varint(enc, 0)
    assert value == n
    assert pos == len(enc)


def test_parse_varint_with_offset():
    data = b"\xff\xff" + varint(42)
    value, pos = parse_varint(data, 2)
    assert value == 42
    assert pos == 3


def test_field_string_format():
    out = field_string(3, b"hi")
    # tag = (3<<3)|2 = 0x1a, length 2, body "hi"
    assert out == b"\x1a\x02hi"


def test_field_varint_format():
    out = field_varint(7, 100)
    # tag = (7<<3)|0 = 0x38, varint 100 = 0x64
    assert out == b"\x38\x64"


def test_parse_fields_roundtrip():
    msg = field_varint(1, 5) + field_string(2, b"hello") + field_varint(3, 200)
    parsed = parse_fields(msg)
    assert parsed[1] == [5]
    assert parsed[2] == [b"hello"]
    assert parsed[3] == [200]


def test_parse_fields_repeated_tag_collects_into_list():
    msg = field_varint(1, 1) + field_varint(1, 2) + field_varint(1, 3)
    assert parse_fields(msg)[1] == [1, 2, 3]


def test_parse_fields_empty_input():
    assert parse_fields(b"") == {}


def test_parse_fields_unsupported_wire_type():
    # wire type 5 = fixed 32-bit, not handled
    bogus = bytes([(1 << 3) | 5, 0, 0, 0, 0])
    with pytest.raises(ValueError, match="unsupported wire type 5"):
        parse_fields(bogus)


def test_build_client_info_defaults():
    blob = build_client_info(b"abc")
    f = parse_fields(blob)
    assert f[1] == [b"abc"]
    assert f[2] == [143]
    assert f[3] == [b"Finder You/Android"]
    assert f[4] == [b"1.4.4"]
    assert f[5] == [b"Google/sdk_gphone64_arm64/14"]
    assert f[6] == [0]


def test_build_client_info_custom_kwargs():
    blob = build_client_info(b"uuid", version=144, platform=b"X", app_version=b"9.9", device=b"D")
    f = parse_fields(blob)
    assert f[2] == [144]
    assert f[3] == [b"X"]
    assert f[4] == [b"9.9"]
    assert f[5] == [b"D"]
