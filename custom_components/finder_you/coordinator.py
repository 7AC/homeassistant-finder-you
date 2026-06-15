"""Update coordinator: keeps a long-lived h2 connection alive and polls the
plant for shutter state at a fixed interval. Commands flow through the same
connection so they share the gateway claim established by the boot
sequence."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FinderApiError,
    FinderHomeClient,
    OAuthError,
    Shutter,
    extract_shutter_positions,
    fetch_token,
    parse_plant,
    refresh_token,
)
from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN

# Minimum gap between consecutive command sends. Without this, a Home/Siri
# scene that fires six concurrent close commands swamps the YESLY gateway's
# MQTT link and the gateway silently drops half of them. With the gap, the
# gateway processes each command cleanly. The gap is small enough that
# single user-initiated taps feel instant.
COMMAND_SEND_GAP = 0.25
# How long after a command to trigger a refresh of the plant, so the real
# position propagates into HA quickly instead of waiting for the next
# scheduled scan. Empirically the gateway's plant-cache lags a few seconds
# behind a successful command.
POST_COMMAND_REFRESH_DELAY = 4.0

_LOGGER = logging.getLogger(__name__)

# Refresh the JWT a few minutes before it actually expires.
TOKEN_REFRESH_MARGIN = 300  # seconds


class FinderYouCoordinator(DataUpdateCoordinator[dict]):
    """Owns the FinderHomeClient lifecycle and produces shutter state for HA."""

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}/{email}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self._email = email
        self._password = password
        self._client: FinderHomeClient | None = None
        self._client_lock = asyncio.Lock()
        # Serializes the SEND step across all command paths. Verification
        # polls can still overlap because we now diff each shutter's own
        # state slice rather than the whole payload.
        self._send_lock = asyncio.Lock()
        self._last_send_ts: float = 0.0
        self._token: dict | None = None
        self._token_expiry: float = 0
        self._plant_id: bytes | None = None
        self._plant_name: str = ""
        self._shutters: list[Shutter] = []

    @property
    def plant_id(self) -> bytes | None:
        return self._plant_id

    @property
    def plant_name(self) -> str:
        return self._plant_name

    @property
    def shutters(self) -> list[Shutter]:
        return self._shutters

    async def _ensure_token(self) -> None:
        now = time.time()
        if self._token and now < self._token_expiry - TOKEN_REFRESH_MARGIN:
            return
        if self._token and self._token.get("refresh_token"):
            try:
                self._token = await refresh_token(self._token["refresh_token"])
            except OAuthError:
                _LOGGER.info("refresh failed, doing fresh login")
                self._token = None
        if not self._token:
            self._token = await fetch_token(self._email, self._password)
        self._token_expiry = time.time() + int(self._token.get("expires_in", 3600))

    async def _ensure_client(self) -> FinderHomeClient:
        if self._client is not None:
            return self._client
        await self._ensure_token()
        assert self._token is not None
        try:
            client = await FinderHomeClient.connect(self._token["access_token"])
            plants_msg = await client.handshake()
            # First GetUserPlants response carries the plant UUID in field 1
            # of the inner Plant message (field 3 of the wrapper).
            if 3 in plants_msg:
                from .api.proto import parse_fields  # local import to avoid cycle

                inner = parse_fields(plants_msg[3][0])
                self._plant_id = inner.get(1, [b""])[0]
        except Exception:
            # If anything during handshake fails, drop the client so we'll
            # reconnect on the next update.
            try:
                await client.close()  # type: ignore[name-defined]
            except Exception:
                pass
            raise
        self._client = client
        return client

    async def _drop_client(self) -> None:
        """Close + forget the current client so the next call reconnects."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def _run_or_reconnect(self, fn):
        """Run ``fn(client)`` once. If the connection looks dead (any error or
        timeout), drop the client and retry once with a fresh one.

        Without this, a silent GOAWAY or a coordinator update timeout leaves
        ``_client`` half-broken: HA service calls would write into a closed
        socket and hang forever, with no log line, because the underlying
        cloud protocol has no exception path that surfaces above the
        per-call read.
        """
        try:
            client = await self._ensure_client()
            return await asyncio.wait_for(fn(client), timeout=15)
        except Exception as err:
            _LOGGER.info("client call failed (%s); reconnecting", err)
            await self._drop_client()
            client = await self._ensure_client()
            return await asyncio.wait_for(fn(client), timeout=15)

    async def _async_update_data(self) -> dict[str, int | None]:
        """Fetch plant + return ``{shutter_uuid: position}`` for HA cover."""
        try:
            plant_payload = await self._run_or_reconnect(
                lambda c: c.get_plant(self._plant_id) if self._plant_id else c.handshake()
            )
        except (TimeoutError, FinderApiError, ConnectionError, OAuthError, OSError) as err:
            await self._drop_client()
            raise UpdateFailed(str(err)) from err

        # If we fell back to handshake (no plant_id yet), the payload is the
        # plants_msg dict, not the raw plant bytes.
        if isinstance(plant_payload, dict):
            return {s.uuid: None for s in self._shutters}

        plant_name, shutters = parse_plant(plant_payload)
        self._plant_name = plant_name or self._plant_name
        if shutters:
            self._shutters = shutters
        positions = extract_shutter_positions(plant_payload)
        return {s.uuid: positions.get(s.uuid) for s in self._shutters}

    async def _send_command(self, do_call) -> None:
        """Send a single command, serialized + spaced behind ``_send_lock``.

        The YESLY gateway's WiFi/MQTT link silently drops commands when
        more than one or two arrive within ~100 ms. A Home/Siri scene that
        closes six shutters triggers exactly that burst. We hold a lock
        across the send and require at least ``COMMAND_SEND_GAP`` seconds
        between consecutive sends — empirically that's the difference
        between the gateway acting on every command and silently
        swallowing half of them.

        We don't try to verify the command landed by polling the plant:
        the gateway's plant-state cache lags 30+ seconds behind a
        successful command, so verifying inside any reasonable HomeKit
        timeout produces false negatives. Instead we trust the
        cloud-level send (with serialization) and kick a delayed refresh
        so the real position propagates as soon as the cache catches up.
        """
        async with self._send_lock:
            loop = asyncio.get_event_loop()
            wait = COMMAND_SEND_GAP - (loop.time() - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            await self._run_or_reconnect(do_call)
            self._last_send_ts = asyncio.get_event_loop().time()
        self._schedule_post_command_refresh()

    def _schedule_post_command_refresh(self) -> None:
        """Trigger an async coordinator refresh after a short delay.

        The gateway's plant cache lags the actual command. A single
        refresh ~4 s after the send is usually enough for the new
        position to land in HA, instead of waiting up to a full scan
        interval for the periodic poll.
        """

        async def delayed() -> None:
            try:
                await asyncio.sleep(POST_COMMAND_REFRESH_DELAY)
                await self.async_request_refresh()
            except Exception:
                _LOGGER.debug("post-command refresh failed", exc_info=True)

        self.hass.async_create_task(delayed())

    async def async_set_position(self, shutter_uuid: str, percent: int) -> None:
        assert self._plant_id is not None
        await self._send_command(
            lambda c: c.set_open_percent(self._plant_id, shutter_uuid.encode(), percent),
        )

    async def async_open(self, shutter_uuid: str) -> None:
        assert self._plant_id is not None
        await self._send_command(
            lambda c: c.open_full(self._plant_id, shutter_uuid.encode()),
        )

    async def async_close_shutter(self, shutter_uuid: str) -> None:
        assert self._plant_id is not None
        await self._send_command(
            lambda c: c.close_full(self._plant_id, shutter_uuid.encode()),
        )

    async def async_shutdown(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
