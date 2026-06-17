"""Tests for the FinderYouCoordinator.

We mock the FinderHomeClient and OAuth helpers, so each test runs in
process without touching the network.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.finder_you import coordinator as mod
from custom_components.finder_you.api import (
    FinderApiError,
    GatewayOfflineError,
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


# ---- _send_command: serialization + verify-by-plant-diff -----------------


def _position_stub(position_sequence):
    """Build an extract_shutter_positions replacement driven by a sequence.

    Each call returns ``{"u1": position_sequence[i]}``, so the test can
    script exactly what each verify poll observes.
    """
    iterator = iter(position_sequence)

    def fake_extract(_payload):
        try:
            p = next(iterator)
        except StopIteration:
            return {"u1": None}
        return {"u1": p}

    return fake_extract


async def test_send_command_waits_between_consecutive_sends(coord, monkeypatch):
    """Two back-to-back sends must be spaced by at least COMMAND_SEND_GAP."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.05)
    coord.async_request_refresh = AsyncMock()  # type: ignore[assignment]

    # Verify always succeeds instantly so we measure only the send-side gap.
    async def fake_wait(*_args, **_kw):
        return True

    coord._wait_for_motor_evidence = fake_wait  # type: ignore[assignment]

    send_timestamps: list[float] = []
    loop = asyncio.get_event_loop()
    send_count = {"n": 0}

    async def runner(fn):
        # Inside the send lock we do (1) baseline get_plant then (2) the
        # actual do_call. The do_call is what we want to time-stamp.
        send_count["n"] += 1
        if send_count["n"] % 2 == 0:
            send_timestamps.append(loop.time())
            return await fn(AsyncMock())
        return b"plant"

    coord._run_or_reconnect = runner  # type: ignore[assignment]

    async def noop(c):
        return None

    await coord._send_command("u1", 0, noop)
    await coord._send_command("u1", 0, noop)
    assert len(send_timestamps) == 2
    assert send_timestamps[1] - send_timestamps[0] >= 0.05 - 0.005


async def test_send_command_serializes_concurrent_callers(coord, monkeypatch):
    """The lock must prevent overlap, not just enforce the time gap."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.05)
    # Every send sees a successful verify: baseline 100, first poll 50.
    monkeypatch.setattr(mod, "extract_shutter_positions", _position_stub([100, 50] * 10))
    monkeypatch.setattr(mod, "extract_shutter_motion", lambda _p: {"u1": 2})
    coord.async_request_refresh = AsyncMock()  # type: ignore[assignment]

    in_flight = 0
    max_in_flight = 0

    async def runner(fn):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.005)
        in_flight -= 1
        return await fn(AsyncMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]

    async def noop(c):
        return None

    await asyncio.gather(*(coord._send_command("u1", 0, noop) for _ in range(4)))
    # The lock only guards baseline-get + send, not the verify polls, so
    # the cap measures send-path concurrency. Verify polls fire outside
    # the lock and can run in parallel with subsequent sends.
    assert max_in_flight <= 2  # at most one send + one verify-poll mid-flight


async def test_send_command_raises_when_gateway_doesnt_act(coord, monkeypatch):
    """If both the first send-and-verify *and* the post-reconnect retry
    time out, the coordinator surfaces GatewayOfflineError so the cover
    translates it into a HomeAssistantError and HomeKit reverts."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.01)
    # Baseline 100, every poll returns 100 → position never moves.
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda _p: {"u1": 100})
    # Motion stays idle (2) — gateway never engaged the motor.
    monkeypatch.setattr(mod, "extract_shutter_motion", lambda _p: {"u1": 2})

    async def runner(fn):
        return await fn(AsyncMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    coord.async_request_refresh = AsyncMock()  # type: ignore[assignment]
    coord._drop_client = AsyncMock()  # type: ignore[assignment]

    async def noop(c):
        return None

    with pytest.raises(GatewayOfflineError):
        await coord._send_command("u1", 0, noop)
    # MAX_SEND_ATTEMPTS=3, so two drops between three attempts.
    assert coord._drop_client.await_count == mod.MAX_SEND_ATTEMPTS - 1


async def test_send_command_self_heals_on_first_failure(coord, monkeypatch):
    """On the first verify timeout the coordinator must drop the client
    (forcing a fresh OpenNotificationChannel handshake) and retry the
    command. If a retry succeeds, no error reaches HA."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.01)

    # Cycle 1: baseline 100, polls 100 → no movement → timeout.
    # drop_client flips the flag.
    # Cycle 2: baseline 100 (first call), then 50 (motor moved).
    state = {"dropped": False, "post_drop_calls": 0}

    def fake_extract(_payload):
        if not state["dropped"]:
            return {"u1": 100}
        state["post_drop_calls"] += 1
        return {"u1": 100} if state["post_drop_calls"] == 1 else {"u1": 50}

    monkeypatch.setattr(mod, "extract_shutter_positions", fake_extract)
    # Motion stays idle throughout — only position-change drives this test.
    monkeypatch.setattr(mod, "extract_shutter_motion", lambda _p: {"u1": 2})

    async def runner(fn):
        return await fn(AsyncMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    coord.async_request_refresh = AsyncMock()  # type: ignore[assignment]

    async def fake_drop():
        state["dropped"] = True

    coord._drop_client = AsyncMock(side_effect=fake_drop)  # type: ignore[assignment]

    async def noop(c):
        return None

    await coord._send_command("u1", 0, noop)
    # Self-heal: drop_client fired exactly once between the two attempts.
    coord._drop_client.assert_awaited_once()


async def test_send_command_succeeds_when_position_changes(coord, monkeypatch):
    """A normal successful command: baseline differs from a subsequent poll."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.1)
    monkeypatch.setattr(mod, "extract_shutter_positions", _position_stub([100, 50]))
    monkeypatch.setattr(mod, "extract_shutter_motion", lambda _p: {"u1": 2})

    async def runner(fn):
        return await fn(AsyncMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    refresh_calls = []

    async def fake_refresh():
        refresh_calls.append(True)

    coord.async_request_refresh = fake_refresh  # type: ignore[assignment]

    async def noop(c):
        return None

    await coord._send_command("u1", 0, noop)
    # On success the coordinator kicks an HA refresh.
    assert refresh_calls


async def test_send_command_succeeds_on_motion_signal(coord, monkeypatch):
    """When position can't be used (e.g. baseline already None) the motion
    flag transitioning to 3 must count as motor evidence."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.1)
    # Baseline None, every poll returns None → no position change ever.
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda _p: {"u1": None})
    # First two polls report idle, then the gateway flips to driving.
    motion_seq = iter([2, 2, 3, 3])

    def fake_motion(_payload):
        try:
            return {"u1": next(motion_seq)}
        except StopIteration:
            return {"u1": 3}

    monkeypatch.setattr(mod, "extract_shutter_motion", fake_motion)

    async def runner(fn):
        return await fn(AsyncMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    coord.async_request_refresh = AsyncMock()  # type: ignore[assignment]
    coord._drop_client = AsyncMock()  # type: ignore[assignment]

    async def noop(c):
        return None

    await coord._send_command("u1", 0, noop)
    coord._drop_client.assert_not_awaited()


async def test_send_command_skips_verify_when_already_at_target(coord, monkeypatch):
    """If the shutter's current position already matches the target the
    coordinator must short-circuit: no verify wait, no GatewayOfflineError
    even though position will never change."""
    monkeypatch.setattr(mod, "COMMAND_SEND_GAP", 0.0)
    # Set timeout small enough that a real verify would fail this test.
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.01)
    # Baseline reports 100 (open) and never changes; without the short-
    # circuit this would raise.
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda _p: {"u1": 100})
    monkeypatch.setattr(mod, "extract_shutter_motion", lambda _p: {"u1": 2})

    async def runner(fn):
        return await fn(AsyncMock())

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    coord.async_request_refresh = AsyncMock()  # type: ignore[assignment]
    coord._drop_client = AsyncMock()  # type: ignore[assignment]

    async def noop(c):
        return None

    # Target 100 = baseline 100 → must succeed without waiting.
    await coord._send_command("u1", 100, noop)
    coord._drop_client.assert_not_awaited()


async def test_wait_for_motor_evidence_skips_failed_polls(coord, monkeypatch):
    """A transient error during a verify poll must be logged but not abort
    the loop; subsequent polls keep trying."""
    monkeypatch.setattr(mod, "VERIFY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(mod, "VERIFY_TIMEOUT", 0.05)
    call = {"n": 0}

    async def runner(fn):
        call["n"] += 1
        if call["n"] == 1:
            raise RuntimeError("transient")
        return b"plant"

    coord._run_or_reconnect = runner  # type: ignore[assignment]
    monkeypatch.setattr(mod, "extract_shutter_positions", lambda _p: {"u1": 50})
    monkeypatch.setattr(mod, "extract_shutter_motion", lambda _p: {"u1": 2})
    ok = await coord._wait_for_motor_evidence("u1", baseline_position=100)
    assert ok is True
    assert call["n"] >= 2  # the first failed, then a second succeeded


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

    targets = []

    async def fake_send(shutter_uuid, target, do_call):
        targets.append((shutter_uuid, target))
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
    # Targets must reflect the semantic position: open=100, close=0, set=N.
    assert targets == [("u1", 100), ("u2", 0), ("u3", 33)]


# ---- async_shutdown -------------------------------------------------------


async def test_shutdown_closes_client(coord):
    coord._client = AsyncMock()
    await coord.async_shutdown()
    assert coord._client is None


async def test_shutdown_without_client_is_noop(coord):
    await coord.async_shutdown()
    assert coord._client is None
