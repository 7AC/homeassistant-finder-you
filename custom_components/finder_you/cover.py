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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GatewayOfflineError, Shutter
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
    async_add_entities(FinderYouCover(coordinator, shutter) for shutter in coordinator.shutters)


class FinderYouCover(CoordinatorEntity[FinderYouCoordinator], CoverEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
    )
    # The cloud's reply to SetOpenPercent is a synchronous ack — the gateway
    # drives the motor asynchronously and v1 has no real-time position
    # feedback. Marking the state as assumed makes HomeKit accept the
    # commanded position as terminal instead of hanging on "Opening…".
    _attr_assumed_state = True

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
        self._last_commanded_position: int = 100

    async def async_added_to_hass(self) -> None:
        """Restore the last commanded position from before HA restarted.

        Without this, HA boots with every cover defaulted to position 100
        (open). The HomeKit bridge remembers the user's last target
        (often 0 if they closed everything before bed) and shows the
        Home tile stuck on "Closing…" forever because CurrentPosition
        and TargetPosition disagree until the user issues a new command.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            pos = last.attributes.get("current_position")
            if isinstance(pos, (int, float)):
                self._last_commanded_position = int(pos)

    @property
    def current_cover_position(self) -> int | None:
        """Return the observed position, falling back to last-commanded.

        ``coordinator.async_set_position`` blocks until the gateway's
        plant cache confirms the action, so by the time HomeKit asks for
        the position after a successful command the observed value will
        already reflect it. The last-commanded fallback only kicks in
        when the coordinator hasn't populated data yet (e.g. just after
        startup).
        """
        observed = self.coordinator.data.get(self._shutter.uuid) if self.coordinator.data else None
        return observed if observed is not None else self._last_commanded_position

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    async def _send(self, op, target_position: int) -> None:
        try:
            await op()
        except GatewayOfflineError as err:
            # The coordinator waited up to VERIFY_TIMEOUT for the
            # gateway's plant cache to reflect the command and never
            # saw a change. Surface the failure to HA so HomeKit reverts
            # to the actual observed state instead of falsely claiming
            # success.
            raise HomeAssistantError(
                f"YESLY gateway didn't acknowledge the {self._shutter.name} "
                f"command ({err}). Try power-cycling the gateway if this persists."
            ) from err
        self._last_commanded_position = target_position
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._send(lambda: self.coordinator.async_open(self._shutter.uuid), 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._send(lambda: self.coordinator.async_close_shutter(self._shutter.uuid), 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        pos = kwargs[ATTR_POSITION]
        await self._send(lambda: self.coordinator.async_set_position(self._shutter.uuid, pos), pos)
