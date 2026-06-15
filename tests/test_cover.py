"""Tests for the FinderYouCover entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.cover import ATTR_POSITION
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.finder_you.api import Shutter
from custom_components.finder_you.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)
from custom_components.finder_you.cover import FinderYouCover, async_setup_entry


def _make_coord(shutters=None, data=None):
    coord = MagicMock()
    coord.shutters = shutters or [Shutter("uuid-1", "Salotto", "Living room")]
    coord.data = data
    coord.async_open = AsyncMock()
    coord.async_close_shutter = AsyncMock()
    coord.async_set_position = AsyncMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.last_update_success = True
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    return coord


# ---- async_setup_entry --------------------------------------------------


async def test_async_setup_entry_registers_one_entity_per_shutter(hass):
    coord = _make_coord(shutters=[Shutter("u1", "S1"), Shutter("u2", "S2")])
    hass.data.setdefault(DOMAIN, {})["entry-1"] = coord
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "u", CONF_PASSWORD: "p"},
        entry_id="entry-1",
    )

    added = []

    def add_entities(items):
        added.extend(items)

    await async_setup_entry(hass, entry, add_entities)
    assert len(added) == 2
    assert {e._shutter.uuid for e in added} == {"u1", "u2"}


# ---- entity construction ------------------------------------------------


def test_entity_init_attributes():
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("uuid-1", "Salotto", "Living room"))
    assert e.unique_id == f"{DOMAIN}_uuid-1"
    assert e.name == "Salotto"
    assert e.device_info["suggested_area"] == "Living room"
    assert e.assumed_state is True


# ---- async_added_to_hass: restore branches ------------------------------


async def test_added_to_hass_restores_int_position(hass):
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("u1", "S1"))
    e.hass = hass

    restored = MagicMock()
    restored.attributes = {"current_position": 42}
    with patch.object(FinderYouCover, "async_get_last_state", new=AsyncMock(return_value=restored)):
        await e.async_added_to_hass()
    assert e._last_commanded_position == 42


async def test_added_to_hass_with_no_last_state_uses_default(hass):
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("u1", "S1"))
    e.hass = hass
    with patch.object(FinderYouCover, "async_get_last_state", new=AsyncMock(return_value=None)):
        await e.async_added_to_hass()
    assert e._last_commanded_position == 100  # default


async def test_added_to_hass_ignores_non_numeric_position(hass):
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("u1", "S1"))
    e.hass = hass
    restored = MagicMock()
    restored.attributes = {"current_position": "not a number"}
    with patch.object(FinderYouCover, "async_get_last_state", new=AsyncMock(return_value=restored)):
        await e.async_added_to_hass()
    assert e._last_commanded_position == 100


# ---- current_cover_position + is_closed ---------------------------------


def test_position_uses_observed_outside_command_window():
    """When no command was issued recently, observed wins so external
    state (wall switch, app) propagates into HA."""
    coord = _make_coord(data={"uuid-1": 33})
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    # _last_command_at defaults to 0 → way outside the window.
    assert e.current_cover_position == 33


def test_position_prefers_commanded_inside_command_window():
    """Right after a command, observed is stale (gateway cache lags), so
    we report the commanded target instead."""
    coord = _make_coord(data={"uuid-1": 100})
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    import time

    e._last_commanded_position = 0
    e._last_command_at = time.time()  # just now
    assert e.current_cover_position == 0


def test_position_falls_back_to_last_commanded_when_no_data():
    coord = _make_coord(data=None)
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    e._last_commanded_position = 80
    assert e.current_cover_position == 80


def test_position_falls_back_when_observed_is_none_explicitly():
    coord = _make_coord(data={"uuid-1": None})
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    e._last_commanded_position = 55
    assert e.current_cover_position == 55


def test_is_closed_reflects_position():
    coord = _make_coord(data={"uuid-1": 0})
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    assert e.is_closed is True
    e2 = FinderYouCover(_make_coord(data={"uuid-1": 100}), Shutter("uuid-1", "S"))
    assert e2.is_closed is False


def test_is_closed_returns_none_when_no_position_at_all():
    # current_cover_position can yield None only if last_commanded is also None;
    # we never set it None, but we can monkeypatch to force the branch.
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    # Force the property to return None.
    with patch.object(FinderYouCover, "current_cover_position", new=property(lambda self: None)):
        assert e.is_closed is None


# ---- async_open_cover / async_close_cover / async_set_cover_position -----


async def test_open_cover_updates_state(hass):
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    e.hass = hass
    e.entity_id = "cover.test"
    e.async_write_ha_state = MagicMock()
    await e.async_open_cover()
    coord.async_open.assert_called_once_with("uuid-1")
    assert e._last_commanded_position == 100
    e.async_write_ha_state.assert_called_once()


async def test_close_cover_updates_state(hass):
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    e.hass = hass
    e.entity_id = "cover.test"
    e.async_write_ha_state = MagicMock()
    await e.async_close_cover()
    coord.async_close_shutter.assert_called_once_with("uuid-1")
    assert e._last_commanded_position == 0


async def test_set_cover_position_updates_state(hass):
    coord = _make_coord()
    e = FinderYouCover(coord, Shutter("uuid-1", "S"))
    e.hass = hass
    e.entity_id = "cover.test"
    e.async_write_ha_state = MagicMock()
    import time

    before = time.time()
    await e.async_set_cover_position(**{ATTR_POSITION: 55})
    coord.async_set_position.assert_called_once_with("uuid-1", 55)
    assert e._last_commanded_position == 55
    # Timestamp is captured so the position property knows we're inside the
    # recent-command window and should report the commanded target.
    assert e._last_command_at >= before
