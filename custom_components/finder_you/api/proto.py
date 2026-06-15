"""Minimal protobuf varint + length-delimited field encoder/decoder.

Only what we need to encode Finder YOU gRPC request bodies and decode responses —
no full protoc dependency.
"""

from __future__ import annotations


def varint(n: int) -> bytes:
    """Encode an unsigned int as protobuf varint."""
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def field_string(field: int, value: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2)."""
    return varint((field << 3) | 2) + varint(len(value)) + value


def field_varint(field: int, value: int) -> bytes:
    """Encode a varint field (wire type 0)."""
    return varint((field << 3) | 0) + varint(value)


def parse_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a varint at pos; return (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def parse_fields(data: bytes) -> dict[int, list]:
    """Parse a protobuf message into a dict of {field_number: [values]}."""
    pos = 0
    out: dict[int, list] = {}
    while pos < len(data):
        tag, pos = parse_varint(data, pos)
        field = tag >> 3
        wire = tag & 7
        if wire == 0:  # varint
            v, pos = parse_varint(data, pos)
        elif wire == 2:  # length-delimited
            length, pos = parse_varint(data, pos)
            v = data[pos : pos + length]
            pos += length
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(field, []).append(v)
    return out


def build_client_info(
    client_uuid: bytes,
    version: int = 143,
    platform: bytes = b"Finder You/Android",
    app_version: bytes = b"1.4.4",
    device: bytes = b"Google/sdk_gphone64_arm64/14",
) -> bytes:
    """Build the ClientInfo envelope that wraps every Finder gRPC request."""
    return (
        field_string(1, client_uuid)
        + field_varint(2, version)
        + field_string(3, platform)
        + field_string(4, app_version)
        + field_string(5, device)
        + field_varint(6, 0)
    )
