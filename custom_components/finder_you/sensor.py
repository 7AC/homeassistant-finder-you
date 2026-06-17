"""Diagnostic sensors for the Finder YOU gateway.

These don't drive any device — they exist to help characterize when
and why the YESLY puck's cloud-to-gateway pipe goes silent. Watching
``Telemetry age`` over a few days makes the wedge pattern visible
(does it grow unbounded? plateau at a known idle level? spike right
before a verify failure?) without a packet capture.
"""

from __future__ import annotations

import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FinderYouCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FinderYouCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TelemetryAgeSensor(coordinator),
            LastCommandAgeSensor(coordinator),
        ]
    )


class _GatewayDiagnosticSensor(CoordinatorEntity[FinderYouCoordinator], SensorEntity):
    """Common base — both sensors share the gateway-level identity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, coordinator: FinderYouCoordinator, suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_gateway_{suffix}"


class TelemetryAgeSensor(_GatewayDiagnosticSensor):
    """Seconds since the gateway last produced any fresh per-shutter slice.

    A healthy puck sees ``0`` whenever a shutter is moved or telemetry
    refreshes; a quiet plant climbs steadily. If this stops resetting
    even while you're issuing commands the puck-to-cloud pipe is
    wedged — that's the signal we want to catch *before* a scene fails.
    """

    _attr_name = "Gateway telemetry age"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator, "telemetry_age")

    @property
    def native_value(self) -> float | None:
        ts = self.coordinator.last_telemetry_change_ts
        if ts is None:
            return None
        return round(time.time() - ts, 1)


class LastCommandAgeSensor(_GatewayDiagnosticSensor):
    """Seconds since the integration last verified a command (motor evidence).

    Pairs with ``Telemetry age``: if commands are succeeding the gateway
    is alive; if both metrics keep climbing, the puck is idle in a way
    that may precede a wedge.
    """

    _attr_name = "Gateway last command age"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator, "last_command_age")

    @property
    def native_value(self) -> float | None:
        ts = self.coordinator.last_successful_command_ts
        if ts is None:
            return None
        return round(time.time() - ts, 1)
