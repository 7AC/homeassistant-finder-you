"""Cover entity for each Finder YOU roller shutter."""
from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Shutter
from .const import DOMAIN
from .coordinator import FinderYouCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FinderYouCoordinator = hass.data[DOMAIN][entry.entry_id]
    # First refresh populates the shutter list.
    await coordinator.async_config_entry_first_refresh()
    async_add_entities(
        FinderYouCover(coordinator, shutter) for shutter in coordinator.shutters
    )


class FinderYouCover(CoordinatorEntity[FinderYouCoordinator], CoverEntity):
    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: FinderYouCoordinator, shutter: Shutter) -> None:
        super().__init__(coordinator)
        self._shutter = shutter
        self._attr_unique_id = f"{DOMAIN}_{shutter.uuid}"
        self._attr_name = shutter.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, shutter.uuid)},
            name=shutter.name,
            manufacturer="Finder",
            model="YESLY Roller Shutter",
            suggested_area=shutter.room,
        )

    @property
    def current_cover_position(self) -> int | None:
        # v1: no position feedback. Returning None makes the UI show an
        # unknown state but still allows open/close/set commands.
        return self.coordinator.data.get(self._shutter.uuid) if self.coordinator.data else None

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_open(self._shutter.uuid)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_close_shutter(self._shutter.uuid)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_position(
            self._shutter.uuid, kwargs[ATTR_POSITION]
        )
