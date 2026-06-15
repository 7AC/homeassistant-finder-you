"""Tests for the integration setup/unload entry points."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.finder_you import async_setup_entry, async_unload_entry
from custom_components.finder_you.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)


def _entry():
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "u@example.com", CONF_PASSWORD: "pw"},
        unique_id="u@example.com",
    )


@pytest.fixture
def fake_coordinator():
    """Patch the coordinator so setup doesn't try to talk to the cloud."""
    with patch(
        "custom_components.finder_you.FinderYouCoordinator",
        autospec=True,
    ) as cls:
        instance = cls.return_value
        instance.async_config_entry_first_refresh = AsyncMock()
        instance.async_shutdown = AsyncMock()
        instance.shutters = []
        instance.data = {}
        yield instance


async def _setup(hass, entry):
    """Drive `async_setup_entry` without tripping HA's entry-state guard.

    Newer HA versions require the config entry to be in LOADED state before
    `async_forward_entry_setups` is called. The proper path through
    `hass.config_entries.async_setup` handles that, but we're testing our
    own setup function directly, so we patch the forward to a no-op.
    """
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock()
    ):
        return await async_setup_entry(hass, entry)


async def test_setup_entry_runs_first_refresh_and_registers(hass, fake_coordinator):
    entry = _entry()
    entry.add_to_hass(hass)
    assert await _setup(hass, entry)
    fake_coordinator.async_config_entry_first_refresh.assert_called_once()
    assert hass.data[DOMAIN][entry.entry_id] is fake_coordinator


async def test_unload_entry_pops_coordinator_and_shuts_down(hass, fake_coordinator):
    entry = _entry()
    entry.add_to_hass(hass)
    await _setup(hass, entry)

    # Force the platform-unload helper to claim success so the pop path runs.
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, entry)

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    fake_coordinator.async_shutdown.assert_called_once()


async def test_unload_entry_keeps_coordinator_when_platforms_fail(hass, fake_coordinator):
    entry = _entry()
    entry.add_to_hass(hass)
    await _setup(hass, entry)

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        assert not await async_unload_entry(hass, entry)

    # Still registered because unload didn't succeed.
    assert hass.data[DOMAIN][entry.entry_id] is fake_coordinator
    fake_coordinator.async_shutdown.assert_not_called()
