"""Tests for ``api/plant.py`` -- GetPlant payload parsing."""

from __future__ import annotations

from custom_components.finder_you.api.plant import (
    SHUTTER_TYPE_MARKER,
    Shutter,
    _extract,
    _walk,
    extract_shutter_positions,
    extract_shutter_states,
    parse_plant,
)
from custom_components.finder_you.api.proto import field_string, field_varint

# ---- Tiny fixture helpers --------------------------------------------------


def _make_room(name: str, uuid: str, *, marker: bytes = SHUTTER_TYPE_MARKER) -> bytes:
    """Build a 'room' submessage that parse_plant should recognize.

    Mirrors the on-wire shape we care about: field 2 = device{ #1 uuid },
    field 7 = display name, field 8 = type-marker{ #1 marker_string }.
    """
    device = field_string(1, uuid.encode())
    type_msg = field_string(1, marker)
    return (
        field_string(1, b"room-" + uuid.encode())
        + field_string(2, device)
        + field_string(7, name.encode())
        + field_string(8, type_msg)
    )


def _make_plant(plant_name: str, rooms: list[bytes]) -> bytes:
    """Wrap rooms in the outer plant envelope expected by parse_plant."""
    body = field_string(1, b"status") + field_string(2, plant_name.encode())
    for r in rooms:
        body += field_string(11, r)
    # Outer wrapper: field 3 holds the plant body.
    return field_string(3, body)


# ---- parse_plant -----------------------------------------------------------


def test_parse_plant_extracts_shutters_and_sorts():
    payload = _make_plant(
        "Casa",
        [
            _make_room("Salotto", "11111111-1111-1111-1111-111111111111"),
            _make_room("Bagno", "22222222-2222-2222-2222-222222222222"),
        ],
    )
    name, shutters = parse_plant(payload)
    assert name == "Casa"
    assert [s.name for s in shutters] == ["Bagno", "Salotto"]
    assert shutters[0].uuid == "22222222-2222-2222-2222-222222222222"


def test_parse_plant_dedups_duplicate_uuids():
    same = _make_room("Salotto", "11111111-1111-1111-1111-111111111111")
    payload = _make_plant("Casa", [same, same])
    _, shutters = parse_plant(payload)
    assert len(shutters) == 1


def test_parse_plant_no_name_returns_empty_string():
    # Plant envelope without field 2 (name) — parse_plant returns "".
    body = field_string(11, _make_room("X", "33333333-3333-3333-3333-333333333333"))
    payload = field_string(3, body)
    name, shutters = parse_plant(payload)
    assert name == ""
    assert len(shutters) == 1


def test_parse_plant_no_outer_field_returns_empty_name():
    name, shutters = parse_plant(b"")
    assert name == ""
    assert shutters == []


# ---- _extract --------------------------------------------------------------


def test_extract_returns_none_without_marker():
    room = field_string(7, b"X") + field_string(2, field_string(1, b"u" * 36))
    assert _extract(room) is None


def test_extract_returns_none_when_name_missing():
    type_msg = field_string(1, SHUTTER_TYPE_MARKER)
    room = field_string(2, field_string(1, b"u" * 36)) + field_string(8, type_msg)
    assert _extract(room) is None


def test_extract_returns_none_when_type_marker_mismatches():
    type_msg = field_string(1, b"device_light_bulb")
    # Include SHUTTER_TYPE_MARKER as a raw byte run so the marker check in
    # parse_plant fires but the field-level check in _extract rejects it.
    room = (
        field_string(2, field_string(1, b"u" * 36))
        + field_string(7, b"Light")
        + field_string(8, type_msg)
        + field_string(99, SHUTTER_TYPE_MARKER)
    )
    assert _extract(room) is None


def test_extract_returns_none_when_uuid_wrong_length():
    type_msg = field_string(1, SHUTTER_TYPE_MARKER)
    room = (
        field_string(2, field_string(1, b"too-short"))
        + field_string(7, b"X")
        + field_string(8, type_msg)
    )
    assert _extract(room) is None


def test_extract_returns_none_when_name_not_bytes():
    # name field present but as varint (wire mismatch) -> not bytes -> None
    type_msg = field_string(1, SHUTTER_TYPE_MARKER)
    room = (
        field_string(2, field_string(1, b"u" * 36)) + field_varint(7, 1) + field_string(8, type_msg)
    )
    assert _extract(room) is None


def test_extract_returns_none_when_type_msg_not_bytes():
    room = (
        field_string(2, field_string(1, b"u" * 36))
        + field_string(7, b"X")
        + field_varint(8, 0)
        + field_string(99, SHUTTER_TYPE_MARKER)
    )
    assert _extract(room) is None


def test_extract_returns_none_when_device_not_bytes():
    type_msg = field_string(1, SHUTTER_TYPE_MARKER)
    room = (
        field_varint(2, 0)
        + field_string(7, b"X")
        + field_string(8, type_msg)
        + field_string(99, SHUTTER_TYPE_MARKER)
    )
    assert _extract(room) is None


def test_extract_returns_none_on_corrupted_room_bytes():
    # Bytes containing the marker but unparseable at the top level.
    bad = SHUTTER_TYPE_MARKER + b"\xff"
    assert _extract(bad) is None


def test_extract_returns_shutter_on_well_formed_room():
    room = _make_room("Salotto", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    sh = _extract(room)
    assert sh == Shutter(uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", name="Salotto")


# ---- _walk -----------------------------------------------------------------


def test_walk_ignores_varint_fields():
    # parse_fields parses (1, varint=5); _walk should skip the int.
    payload = field_varint(1, 5)
    out: list[Shutter] = []
    _walk(payload, out)
    assert out == []


def test_walk_skips_byte_chunks_without_marker():
    payload = field_string(99, b"some random bytes")
    out: list[Shutter] = []
    _walk(payload, out)
    assert out == []


def test_walk_swallows_parse_exceptions():
    # Malformed inner bytes that contain the marker but blow up parse_fields.
    bad = SHUTTER_TYPE_MARKER + b"\xff"
    out: list[Shutter] = []
    _walk(bad, out)
    assert out == []


# ---- extract_shutter_states / extract_shutter_positions --------------------


def _make_state(uuid: str, *, position: int = 100, motion: int = 2) -> bytes:
    """Build a per-shutter state submessage matching the gateway's schema.

    Fields {1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13} must all be present for the
    schema filter in plant.py to recognize it. Field 1 is the UUID, field
    13.#1 is the open percentage, field 12 is the motion flag.
    """
    return (
        field_string(1, uuid.encode())
        + field_string(2, b"plant-uuid-here-1234-1234-1234-1234abcd")
        + field_string(3, b"13S2")
        + field_string(4, field_string(1, b"00:11:22:33:44:55"))
        + field_string(6, field_string(1, b"{}"))
        + field_string(7, field_varint(1, 1700000000) + field_varint(2, 100))
        + field_string(8, field_string(1, b"gw-uuid"))
        + field_string(9, field_varint(1, 21))
        + field_varint(11, 2)
        + field_varint(12, motion)
        + field_string(13, field_varint(1, position))
    )


def _wrap_states(states: list[bytes]) -> bytes:
    return b"".join(field_string(12, s) for s in states)


def test_extract_shutter_states_returns_uuid_to_slice_map():
    s1 = _make_state("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", position=0)
    s2 = _make_state("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", position=100)
    payload = _wrap_states([s1, s2])
    out = extract_shutter_states(payload)
    assert set(out) == {
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    }
    assert out["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"] == s1
    assert out["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"] == s2


def test_extract_shutter_states_changes_between_baselines():
    """The whole point of the slice: two payloads with one shutter moved
    must produce different bytes for that shutter and identical bytes for
    the other."""
    a_idle = _make_state("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", position=0)
    b_idle = _make_state("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", position=0)
    a_moved = _make_state("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", position=42)
    baseline = extract_shutter_states(_wrap_states([a_idle, b_idle]))
    current = extract_shutter_states(_wrap_states([a_moved, b_idle]))
    a_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert baseline[a_uuid] != current[a_uuid]
    assert baseline[b_uuid] == current[b_uuid]


def test_extract_shutter_states_skips_messages_with_wrong_field_set():
    # A submessage at field 12 that doesn't have the full schema is ignored.
    payload = field_string(12, field_string(1, b"u" * 36))
    assert extract_shutter_states(payload) == {}


def test_extract_shutter_states_skips_non_bytes_field_12():
    # Field 12 as a varint (wrong wire type) is silently skipped.
    payload = field_varint(12, 42)
    assert extract_shutter_states(payload) == {}


def test_extract_shutter_states_handles_malformed_payload():
    assert extract_shutter_states(b"\xff\xff\xff") == {}


def test_extract_shutter_states_skips_message_with_wrong_uuid_length():
    # Build a state msg where field 1 holds bytes of the wrong length.
    base = _make_state("a" * 36, position=10)
    # Hack: replace field-1 bytes with a short string.
    bad_field_1 = field_string(1, b"too-short")
    rest = base[len(field_string(1, b"a" * 36)) :]
    bad_state = bad_field_1 + rest
    payload = _wrap_states([bad_state])
    assert extract_shutter_states(payload) == {}


def test_extract_shutter_positions_returns_uuid_to_percent_map():
    s1 = _make_state("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", position=45)
    s2 = _make_state("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", position=0)
    payload = _wrap_states([s1, s2])
    assert extract_shutter_positions(payload) == {
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": 45,
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb": 0,
    }


def test_extract_shutter_positions_skips_out_of_range_values():
    # A position field with a varint outside 0..100 is rejected as garbage.
    s = (
        field_string(1, b"a" * 36)
        + field_string(2, b"plant")
        + field_string(3, b"13S2")
        + field_string(4, b"")
        + field_string(6, b"")
        + field_string(7, b"")
        + field_string(8, b"")
        + field_string(9, b"")
        + field_varint(11, 2)
        + field_varint(12, 2)
        + field_string(13, field_varint(1, 999))
    )
    payload = _wrap_states([s])
    assert extract_shutter_positions(payload) == {}


def test_extract_shutter_positions_skips_when_position_field_missing():
    s = (
        field_string(1, b"a" * 36)
        + field_string(2, b"")
        + field_string(3, b"")
        + field_string(4, b"")
        + field_string(6, b"")
        + field_string(7, b"")
        + field_string(8, b"")
        + field_string(9, b"")
        + field_varint(11, 2)
        + field_varint(12, 2)
        + field_string(13, b"")
    )
    payload = _wrap_states([s])
    assert extract_shutter_positions(payload) == {}


def test_extract_shutter_positions_skips_when_field_13_not_bytes():
    # field 13 as varint is wrong type
    s = (
        field_string(1, b"a" * 36)
        + field_string(2, b"")
        + field_string(3, b"")
        + field_string(4, b"")
        + field_string(6, b"")
        + field_string(7, b"")
        + field_string(8, b"")
        + field_string(9, b"")
        + field_varint(11, 2)
        + field_varint(12, 2)
        + field_varint(13, 99)
    )
    payload = _wrap_states([s])
    # field set doesn't match (#13 is varint instead of length-delimited
    # but parse_fields still records it as int); schema filter should still
    # accept the field set but extract_positions skips non-bytes #13.
    assert extract_shutter_positions(payload) == {}


def test_extract_shutter_positions_skips_when_uuid_wrong_length():
    s = (
        field_string(1, b"short")
        + field_string(2, b"")
        + field_string(3, b"")
        + field_string(4, b"")
        + field_string(6, b"")
        + field_string(7, b"")
        + field_string(8, b"")
        + field_string(9, b"")
        + field_varint(11, 2)
        + field_varint(12, 2)
        + field_string(13, field_varint(1, 42))
    )
    payload = _wrap_states([s])
    assert extract_shutter_positions(payload) == {}


def test_extract_shutter_positions_handles_malformed_payload():
    assert extract_shutter_positions(b"\xff\xff") == {}


def _malformed_state_message() -> bytes:
    """Build a length-delimited field-12 body that itself looks valid until
    the inner parser hits an unsupported wire type."""
    # Build a "state-shaped" submessage where field 13's body is itself a
    # tag with wire type 3 (unsupported by parse_fields → raises).
    bad_pos_msg = bytes([(1 << 3) | 3])  # field 1, wire 3 → unsupported
    return (
        field_string(1, b"a" * 36)
        + field_string(2, b"")
        + field_string(3, b"")
        + field_string(4, b"")
        + field_string(6, b"")
        + field_string(7, b"")
        + field_string(8, b"")
        + field_string(9, b"")
        + field_varint(11, 2)
        + field_varint(12, 2)
        + field_string(13, bad_pos_msg)
    )


def test_extract_shutter_positions_swallows_parse_exception_inside_pos_msg():
    payload = _wrap_states([_malformed_state_message()])
    assert extract_shutter_positions(payload) == {}


def test_iter_state_submessages_swallows_parse_exception_in_field_12_body():
    # Field 12 carrying a body that parse_fields can't decode (wire type 3).
    bad_sub = bytes([(1 << 3) | 3])
    payload = field_string(12, bad_sub)
    assert extract_shutter_states(payload) == {}
