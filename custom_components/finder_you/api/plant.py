"""Decode the GetPlant response into structured shutter metadata.

The plant payload is ~27 KB of nested protobuf. We only need the list of
roller shutters: their UUID (used by SetOpenPercent) and their display
name.
"""
from __future__ import annotations

from dataclasses import dataclass

from .proto import parse_fields

SHUTTER_TYPE_MARKER = b"device_roller_shutter_50"


@dataclass(frozen=True)
class Shutter:
    """A roller shutter discovered in the plant."""

    uuid: str
    name: str
    room: str | None = None


def parse_plant(payload: bytes) -> tuple[str, list[Shutter]]:
    """Extract ``(plant_name, [Shutter, ...])`` from a GetPlant response body.

    The plant has many nested messages. Roller shutters live in entries
    that we'll call "rooms"::

        room {                       (field 3 of the outer plant message)
          #1 string room_uuid
          #2 device { #1 string device_uuid }
          #3 varint                  (some flag)
          #4 varint                  (some flag)
          #7 string display_name     (e.g. "Living room", "Bedroom")
          #8 message { #1 string "device_roller_shutter_50" }
          ... other devices (lights) attached to the same room
        }

    We walk the plant looking for messages whose serialized bytes contain
    the shutter type marker, then pull the device UUID + display name out.
    """
    top = parse_fields(payload)
    plant_name = ""
    if 3 in top:
        plant = parse_fields(top[3][0])
        if 2 in plant:
            plant_name = plant[2][0].decode("utf-8", errors="replace")
    shutters: list[Shutter] = []
    _walk(payload, shutters)
    # De-duplicate by UUID (shouldn't happen but be safe).
    seen: set[str] = set()
    unique: list[Shutter] = []
    for s in shutters:
        if s.uuid not in seen:
            seen.add(s.uuid)
            unique.append(s)
    unique.sort(key=lambda s: s.name.lower())
    return plant_name, unique


def _walk(payload: bytes, out: list[Shutter]) -> None:
    """Walk the protobuf tree, extracting shutters."""
    try:
        fields = parse_fields(payload)
    except Exception:
        return
    for field_id, values in fields.items():
        for v in values:
            if not isinstance(v, bytes):
                continue
            if SHUTTER_TYPE_MARKER not in v:
                continue
            shutter = _extract(v)
            if shutter is not None:
                out.append(shutter)
            _walk(v, out)


def _extract(room_bytes: bytes) -> Shutter | None:
    """Pull a single shutter (uuid, name) out of a 'room' submessage."""
    if SHUTTER_TYPE_MARKER not in room_bytes:
        return None
    try:
        fields = parse_fields(room_bytes)
    except Exception:
        return None
    # Display name = field 7 (string).
    name_b = fields.get(7, [None])[0]
    if not isinstance(name_b, bytes):
        return None
    name = name_b.decode("utf-8", errors="replace")
    # Field 8 holds the type marker — confirm it's the shutter marker (so we
    # don't pick up sibling light entries that happen to live in the same
    # parent room).
    type_msg = fields.get(8, [None])[0]
    if not isinstance(type_msg, bytes):
        return None
    type_sub = parse_fields(type_msg)
    if type_sub.get(1, [None])[0] != SHUTTER_TYPE_MARKER:
        return None
    # Device UUID = field 2's field 1 (string).
    device_msg = fields.get(2, [None])[0]
    if not isinstance(device_msg, bytes):
        return None
    device_sub = parse_fields(device_msg)
    uuid_b = device_sub.get(1, [None])[0]
    if not isinstance(uuid_b, bytes) or len(uuid_b) != 36:
        return None
    return Shutter(uuid=uuid_b.decode(), name=name)
