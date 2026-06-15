"""Constants for the Finder YOU integration."""

from __future__ import annotations

DOMAIN = "finder_you"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_PLANT_ID = "plant_id"

DEFAULT_SCAN_INTERVAL_SECONDS = 60

PLATFORMS: list[str] = ["cover"]
