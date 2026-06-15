"""Tests for the FinderYouCoordinator.

We mock the FinderHomeClient and OAuth helpers, so each test runs in
process without touching the network.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.finder_you import coordinator as mod
from custom_components.finder_you.api import (
    FinderApiError,
    OAuthError,
    Shutter,
)
from custom_components.finder_you.api.proto import field_string


@pytest.fixture
async def coord(hass):
    c = mod.FinderYouCoordinator(hass, "u@example.com", "pw")
    return c


# ---- properties ------------------------------------------------------------


async def test_property_accessors(coord):
    assert coord.plant_id is None
    assert coord.plant_name == ""
    assert coord.shutters == []


# ---- _ensure_token paths --------------------------------------------------


async def test_ensure_token_fetches_fresh_when_none(coord, monkeypatch):
    fetched = AsyncMock(return_value={"access_token": "T", "expires_in": 3600})
    monkeypatch.setattr(mod, "fetch_token", fetched)
    await coord._ensure_token()
    fetched.assert_called_once_with("u@example.com", "pw")
    assert coord._token["access_token"] == "T"


async def test_ensure_token_skips_when_still_valid(coord, monkeypatch):
    import time as _time

    coord._token = {"access_token": "X"}
    coord._token_expiry = _time.time() + 9999
    fetched = AsyncMock()
    monkeypatch.setattr(mod, "fetch_token", fetched)
    await coord._ensure_token()
    fetched.assert_not_called()


async def test_ensure_token_uses_refresh_when_available(coord, monkeypatch):
    coord._token = {"access_token": "X", "refresh_token": "R"}
    coord._token_expiry = 0
    refresh = AsyncMock(return_value={"access_token": "Y", "expires_in": 60})
    fetched = AsyncMock()
    monkeypatch.setattr(mod, "refresh_token", refresh)
    monkeypatch.setattr(mod, "fetch_token", fetched)
    await coord._ensure_token()
    refresh.assert_called_once_with("R")
    fetched.assert_not_called()
    assert coord._token["access_token"] == "Y"


async def test_ensure_token_falls_back_to_fresh_when_refresh_fails(coord, monkeypatch):
    coord._token = {"access_token": "X", "refresh_token": "R"}
    coord._token_expiry = 0
    refresh = AsyncMock(side_effect=OAuthError("nope"))
    fetched = AsyncMock(return_value={"access_token": "Z", "expires_in": 30})
    monkeypatch.setattr(mod, "refresh_token", refresh)
    monkeypatch.setattr(mod, "fetch_token", fetched)
    await coord._ensure_token()
    fetched.assert_called_once()
    assert coord._token["access_token"] == "Z"


# ---- _ensure_client / _drop_client ----------------------------------------


async def test_ensure_client_runs_handshake_and_captures_plant_id(coord, monkeypatch):
    monkeypatch.setattr(
        mod, "fetch_token", AsyncMock(return_value={"access_token": "T", "expires_in": 60})
    )
    fake_client = AsyncMock()
    inner = field_string(1, b"plant-99")
    fake_client.handshake = AsyncMock(return_value={3: [inner]})
    monkeypatch.setattr(
        mod.FinderHomeClient,
        "connect",
        AsyncMock(return_value=fake_client),
    )
    c = await coord._ensure_client()
    assert c is fake_client
    assert coord.plant_id == b"plant-99"
    # Calling again returns the cached client without re-handshaking.
    assert await coord._ensure_client() is fake_client


async def test_ensure_client_skips_plant_id_when_no_field_3(coord, monkeypatch):
    monkeypatch.setattr(
        mod, "fetch_token", AsyncMock(return_value={"access_token": "T", "expires_in": 60})
    )
    fake_client = AsyncMock()
    fake_client.handshake = AsyncMock(return_value={})
    monkeypatch.setattr(
        mod.FinderHomeClient,
        "connect",
        AsyncMock(return_value=fake_client),
    )
    await coord._ensure_client()
    assert coord.plant_id is None


async def test_ensure_client_closes_partial_client_on_handshake_failure(coord, monkeypatch):
    monkeypatch.setattr(
        mod, "fetch_token", AsyncMock(return_value={"access_token": "T", "expires_in": 60})
    )
    fake_client = AsyncMock()
    fake_client.handshake = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        mod.FinderHomeClient,
        "connect",
        AsyncMock(return_value=fake_client),
    )
    with pytest.raises(RuntimeError, match="boom"):
        await coord._ensure_client()
    fake_client.close.assert_called_once()


async def test_ensure_client_swallows_close_errors_during_cleanup(coord, monkeypatch):
    monkeypatch.setattr(
        mod, "fetch_token", AsyncMock(return_value={"access_token": "T", "expires_in": 60})
    )
    fake_client = AsyncMock()
    fake_client.handshake = AsyncMock(side_effect=RuntimeError("boom"))
    fake_client.close = AsyncMock(side_effect=RuntimeError("nested"))
    monkeypatch.setattr(
        mod.FinderHomeClient,
        "connect",
        AsyncMock(return_value=fake_client),
    )
    with pytest.raises(RuntimeError, match="boom"):
        await coord._ensure_client()


async def test_drop_client_closes_and_forgets(coord):
    coord._client = AsyncMock()
    await coord._drop_client()
    assert coord._client is None


async def test_drop_client_when_none_is_noop(coord):
    await coord._drop_client()
    assert coord._client is None


async def test_drop_client_swallows_close_errors(coord):
    fake = AsyncMock()
    fake.close.side_effect = RuntimeError("x")
    coord._client = fake
    await coord._drop_client()
    assert coord._client is None


# ---- _run_or_reconnect ----------------------------------------------------


async def test_run_or_reconnect_happy_path(coord, monkeypatch):
    fake = AsyncMock()
    coord._client = fake
    called = []

    async def fn(c):
        called.append(c)
        return "ok"

    monkeypatch.setattr(coord, "_ensure_client", AsyncMock(return_value=fake))
    assert await coord._run_or_reconnect(fn) == "ok"
    assert called == [fake]


async def test_run_or_reconnect_reconnects_on_other_error(coord, monkeypatch):
    bad = AsyncMock()
    good = AsyncMock()
    seq = [bad, good]
    monkeypatch.setattr(coord, "_ensure_client", AsyncMock(side_effect=lambda: seq.pop(0)))
    drops = []
    monkeypatch.setattr(coord, "_drop_client", AsyncMock(side_effect=lambda: drops.append(True)))

    calls = {"n": 0}

    async def fn(c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("dead")
        return "ok"

    assert await coord._run_or_reconnect(fn) == "ok"
    assert drops == [True]
    assert calls["n"] == 2


# ---- _async_update_data ---------------------------------------------------


async def test_update_data_with_plant_id_parses_shutters_and_positions(coord, monkeypatch):
    coord._plant_id = b"PID"

    async def fake_run(fn):
        return b"raw-plant"

    monkeypatch.setattr(coord, "_run_or_reconnect", fake_run)
    monkeypatch.setattr(
        mod,
        "parse_plant",
        lambda payload: ("Casa", [Shutter("u1", "S1"), Shutter("u2", "S2")]),
    )
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda payload: {"u1": 45, "u2": 0})
    out = await coord._async_update_data()
    assert out == {"u1": 45, "u2": 0}
    assert coord.plant_name == "Casa"
    assert [s.uuid for s in coord.shutters] == ["u1", "u2"]


async def test_update_data_missing_position_returns_none(coord, monkeypatch):
    coord._plant_id = b"PID"

    async def fake_run(fn):
        return b"raw-plant"

    monkeypatch.setattr(coord, "_run_or_reconnect", fake_run)
    monkeypatch.setattr(mod, "parse_plant", lambda p: ("Casa", [Shutter("u1", "S1")]))
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda p: {})
    out = await coord._async_update_data()
    assert out == {"u1": None}


async def test_update_data_keeps_old_plant_name_when_new_is_empty(coord, monkeypatch):
    coord._plant_id = b"PID"
    coord._plant_name = "Existing"
    coord._shutters = [Shutter("u1", "S1")]

    async def fake_run(fn):
        return b"raw"

    monkeypatch.setattr(coord, "_run_or_reconnect", fake_run)
    monkeypatch.setattr(mod, "parse_plant", lambda p: ("", []))
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda p: {})
    out = await coord._async_update_data()
    assert coord.plant_name == "Existing"
    assert out == {"u1": None}


async def test_update_data_handshake_branch_when_no_plant_id(coord, monkeypatch):
    coord._plant_id = None

    async def fake_run(fn):
        # The lambda passed in returns a dict (handshake's plants_msg).
        return {3: [b"x"]}

    monkeypatch.setattr(coord, "_run_or_reconnect", fake_run)
    out = await coord._async_update_data()
    assert out == {}


async def test_update_data_raises_update_failed_on_known_errors(coord, monkeypatch):
    coord._plant_id = b"PID"
    coord._client = AsyncMock()
    drops = []

    async def drop():
        drops.append(True)

    monkeypatch.setattr(coord, "_drop_client", drop)

    async def fake_run(fn):
        raise FinderApiError("X", 2)

    monkeypatch.setattr(coord, "_run_or_reconnect", fake_run)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert drops


@pytest.mark.parametrize(
    "exc",
    [ConnectionError("c"), OAuthError("o"), TimeoutError(), OSError("o")],
)
async def test_update_data_other_known_errors_raise_update_failed(coord, monkeypatch, exc):
    coord._plant_id = b"PID"

    async def fake_run(fn):
        raise exc

    monkeypatch.setattr(coord, "_run_or_reconnect", fake_run)
    monkeypatch.setattr(coord, "_drop_client", AsyncMock())
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


# ---- _send_command: the serialization + pacing lock + delayed refresh -----


async def _patch_refresh_to_noop(coord, monkeypatch):
    """Replace the post-command refresh with a sync no-op so tests don't
    have to wait POST_COMMAND_REFRESH_DELAY seconds."""
    coord._schedule_post_command_refresh = lambda: None  # type: ignore[assignment]


async def test_send_command_waits_between_consecutive_sends(coord, monkeypatch):
    """Two back-to-back sends must be spaced by at least COMMAND_SEND_GAP."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.05)
    await _patch_refresh_to_noop(coord, monkeypatch)

    timestamps: list[float] = []
    loop = asyncio.get_event_loop()

    async def runner(fn):
        timestamps.append(loop.time())
        return await fn(MagicMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]

    async def noop(c):
        return None

    await coord._send_command(noop)
    await coord._send_command(noop)
    assert len(timestamps) == 2
    assert timestamps[1] - timestamps[0] >= 0.05 - 0.005


async def test_send_command_serializes_concurrent_callers(coord, monkeypatch):
    """The lock must prevent overlap, not just enforce the time gap."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    await _patch_refresh_to_noop(coord, monkeypatch)
    in_flight = 0
    max_in_flight = 0

    async def runner(fn):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.005)
        in_flight -= 1
        return await fn(MagicMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]

    async def noop(c):
        return None

    await asyncio.gather(*(coord._send_command(noop) for _ in range(4)))
    assert max_in_flight == 1


async def test_send_command_schedules_delayed_refresh(coord, monkeypatch):
    """After a send, the coordinator must kick a follow-up refresh so the
    real position propagates without waiting for the next scheduled scan."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    # Fire the refresh after near-zero delay so the test runs fast.
    monkeypatch.setattr(mod, "POST_COMMAND_REFRESH_DELAY", 0.0)

    async def runner(fn):
        return await fn(MagicMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    refresh_calls = []

    async def fake_refresh():
        refresh_calls.append(True)

    coord.async_request_refresh = fake_refresh  # type: ignore[assignment]

    async def noop(c):
        return None

    await coord._send_command(noop)
    # The refresh runs in a hass.async_create_task — yield until it fires.
    for _ in range(5):
        await asyncio.sleep(0)
        if refresh_calls:
            break
    assert refresh_calls


async def test_schedule_post_command_refresh_swallows_errors(coord, monkeypatch):
    """If async_request_refresh raises, the delayed task must not crash HA."""
    monkeypatch.setattr(mod, "POST_COMMAND_REFRESH_DELAY", 0.0)

    async def boom():
        raise RuntimeError("network died")

    coord.async_request_refresh = boom  # type: ignore[assignment]
    coord._schedule_post_command_refresh()
    # Yield to let the delayed task run; should not propagate.
    for _ in range(5):
        await asyncio.sleep(0)


# ---- async_set_position / async_open / async_close_shutter wiring ----------


async def test_high_level_commands_pass_through(coord):
    coord._plant_id = b"PID"
    calls = []

    class FakeClient:
        async def open_full(self, plant_id, uuid):
            calls.append(("open", plant_id, uuid))

        async def close_full(self, plant_id, uuid):
            calls.append(("close", plant_id, uuid))

        async def set_open_percent(self, plant_id, uuid, percent):
            calls.append(("set", plant_id, uuid, percent))

    async def fake_send(do_call):
        await do_call(FakeClient())

    coord._send_command = fake_send  # type: ignore[assignment]
    await coord.async_open("u1")
    await coord.async_close_shutter("u2")
    await coord.async_set_position("u3", 33)
    assert calls == [
        ("open", b"PID", b"u1"),
        ("close", b"PID", b"u2"),
        ("set", b"PID", b"u3", 33),
    ]


# ---- async_shutdown -------------------------------------------------------


async def test_shutdown_closes_client(coord):
    coord._client = AsyncMock()
    await coord.async_shutdown()
    assert coord._client is None


async def test_shutdown_without_client_is_noop(coord):
    await coord.async_shutdown()
    assert coord._client is None
