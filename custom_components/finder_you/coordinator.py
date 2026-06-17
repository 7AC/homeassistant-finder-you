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
    GatewayOfflineError,
    OAuthError,
    Shutter,
    extract_shutter_positions,
    extract_shutter_states,
    fetch_token,
    parse_plant,
    refresh_token,
)
from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN

# Minimum gap between consecutive command sends. 250 ms was enough to stop
# the WiFi-side burst, but during 6-shutter scenes the gateway's BLE-mesh
# fan-out to the actual shutters still dropped the farthest hops (Cucina,
# Salotto). 2 s gives BLE-mesh time to land each command before the next
# one fires. Single user-initiated taps still feel instant because there's
# nothing queued ahead of them.
COMMAND_SEND_GAP = 2.0
# How long after a command to wait for the gateway's plant-state cache to
# reflect the change. Empirically the cache lags 30-60 s for solo taps but
# can stretch past 90 s during scene bursts. 180 s outwaits the worst case
# we've observed; the alternative (declaring failure when the shutter
# actually moved) is worse than making the user wait.
VERIFY_TIMEOUT = 180.0
# How often to re-fetch the plant during verification. Cheap call; tight
# polling makes successful commands feel as fast as possible.
VERIFY_POLL_INTERVAL = 2.0

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

    async def _send_command(self, shutter_uuid: str, do_call) -> None:
        """Send a command, then verify by watching the plant state.

        On verify timeout, the gateway's WiFi link is fine but its cloud
        subscription has gone stale (server restart, claim expired). The
        fix is a fresh 3-message OpenNotificationChannel handshake, which
        ``_drop_client`` triggers on the next call. We retry the command
        exactly once after dropping the client; if the retry also fails,
        the gateway is truly wedged and we raise.
        """
        try:
            await self._send_and_verify(shutter_uuid, do_call)
        except GatewayOfflineError:
            _LOGGER.info(
                "gateway didn't reflect %s; dropping client and retrying once",
                shutter_uuid,
            )
            await self._drop_client()
            await self._send_and_verify(shutter_uuid, do_call)

    async def _send_and_verify(self, shutter_uuid: str, do_call) -> None:
        """One send-then-verify cycle.

        The YESLY gateway's WiFi/MQTT link silently drops commands when
        more than one or two arrive within ~100 ms. A Home/Siri scene that
        closes six shutters triggers exactly that burst. To handle both
        bursts and single-command congestion:

          * Serialize sends through ``_send_lock`` with a small gap
            between consecutive calls so the gateway never sees a burst.
          * After the cloud accepts the send, capture a baseline of the
            target shutter's per-shutter state slice and poll the plant
            until that slice changes (proof the gateway recorded the
            action) or until ``VERIFY_TIMEOUT`` elapses.

        On verify timeout we raise ``GatewayOfflineError`` so the caller
        can drop the client and retry, or surface the failure to HA.
        """
        async with self._send_lock:
            loop = asyncio.get_event_loop()
            wait = COMMAND_SEND_GAP - (loop.time() - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            baseline_payload = await self._run_or_reconnect(lambda c: c.get_plant(self._plant_id))
            await self._run_or_reconnect(do_call)
            self._last_send_ts = asyncio.get_event_loop().time()

        baseline_slice = extract_shutter_states(baseline_payload).get(shutter_uuid)
        if not await self._wait_for_shutter_change(shutter_uuid, baseline_slice):
            raise GatewayOfflineError(
                f"gateway didn't reflect {shutter_uuid} within "
                f"{VERIFY_TIMEOUT:.0f}s — likely offline or congested"
            )
        # Verify succeeded: kick a coordinator refresh so HA's state
        # picks up the new position the next time HomeKit reads it.
        await self.async_request_refresh()

    async def _wait_for_shutter_change(
        self, shutter_uuid: str, baseline_slice: bytes | None
    ) -> bool:
        """Poll the plant until this shutter's state slice differs from baseline."""
        deadline = asyncio.get_event_loop().time() + VERIFY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(VERIFY_POLL_INTERVAL)
            try:
                payload = await self._run_or_reconnect(lambda c: c.get_plant(self._plant_id))
            except Exception:
                _LOGGER.debug("verify poll failed", exc_info=True)
                continue
            current = extract_shutter_states(payload).get(shutter_uuid)
            if current is not None and current != baseline_slice:
                return True
        return False

    async def async_set_position(self, shutter_uuid: str, percent: int) -> None:
        assert self._plant_id is not None
        await self._send_command(
            shutter_uuid,
            lambda c: c.set_open_percent(self._plant_id, shutter_uuid.encode(), percent),
        )

    async def async_open(self, shutter_uuid: str) -> None:
        assert self._plant_id is not None
        await self._send_command(
            shutter_uuid,
            lambda c: c.open_full(self._plant_id, shutter_uuid.encode()),
        )

    async def async_close_shutter(self, shutter_uuid: str) -> None:
        assert self._plant_id is not None
        await self._send_command(
            shutter_uuid,
            lambda c: c.close_full(self._plant_id, shutter_uuid.encode()),
        )

    async def async_shutdown(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
