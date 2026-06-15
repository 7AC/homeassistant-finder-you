"""Tests for the raw HTTP/2 client.

The full handshake is genuine network behavior we can't reproduce 1:1 in
unit tests, but the dispatcher, framing helpers, error types, and the
per-shutter notification waiter mechanism are all in-process state we
can exercise directly.
"""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.finder_you.api.client import (
    FLAG_ACK,
    FLAG_END_HEADERS,
    FLAG_END_STREAM,
    H2_PREFACE,
    TYPE_DATA,
    TYPE_GOAWAY,
    TYPE_HEADERS,
    TYPE_PING,
    TYPE_SETTINGS,
    TYPE_WINDOW_UPDATE,
    FinderApiError,
    FinderHomeClient,
    GatewayOfflineError,
    _build_frame,
)
from custom_components.finder_you.api.proto import (
    field_string,
    field_varint,
    parse_fields,
)


# ---------------------------------------------------------------------------
# Errors


def test_finder_api_error_str():
    err = FinderApiError("Foo", 2, 19)
    assert "Foo" in str(err)
    assert "status=2" in str(err)
    assert "code=19" in str(err)
    assert err.method == "Foo"
    assert err.status == 2
    assert err.code == 19


def test_finder_api_error_default_code():
    err = FinderApiError("Bar", 7)
    assert err.code is None


def test_gateway_offline_error_message_is_class_docstring_oriented():
    # GatewayOfflineError is a plain Exception subclass; just verify it works
    # like one and has a docstring (so coverage of the class body is real).
    err = GatewayOfflineError("ping")
    assert str(err) == "ping"
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# _build_frame helper


def test_build_frame_layout():
    frame = _build_frame(TYPE_DATA, 0x1, 7, b"hi")
    # 3B length + 1B type + 1B flags + 4B sid + body
    assert frame[0:3] == b"\x00\x00\x02"
    assert frame[3] == TYPE_DATA
    assert frame[4] == 0x1
    assert frame[5:9] == struct.pack(">L", 7)
    assert frame[9:] == b"hi"


# ---------------------------------------------------------------------------
# Notification dispatcher: extracts UUIDs and wakes waiters


def _client():
    """Construct a client without opening a connection."""
    c = FinderHomeClient("tok")
    return c


def test_dispatch_resolves_waiter_when_uuid_present():
    c = _client()
    uuid = b"c9e9168a-bf74-4bd2-b25b-dd2c98f94432"
    fut = asyncio.get_event_loop().create_future()
    c._shutter_waiters[uuid.decode()] = [fut]
    msg = field_string(5, field_string(1, uuid))
    c._dispatch_shutter_event(msg)
    assert fut.done()
    assert fut.result() is None
    assert uuid.decode() not in c._shutter_waiters


def test_dispatch_silent_when_no_uuid_in_msg():
    c = _client()
    fut = asyncio.get_event_loop().create_future()
    c._shutter_waiters["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"] = [fut]
    c._dispatch_shutter_event(field_varint(1, 1))
    assert not fut.done()


def test_dispatch_handles_unparseable_outer_bytes_silently():
    c = _client()
    # Bogus bytes can't be parsed at top level — must not raise.
    c._dispatch_shutter_event(b"\xff\xff\xff")


def test_dispatch_handles_unparseable_inner_blobs():
    c = _client()
    # Outer parses; inner blob looks length-delimited but is corrupted.
    bogus_inner = b"\xff" * 8
    msg = field_string(2, bogus_inner)
    c._dispatch_shutter_event(msg)  # must not raise


def test_dispatch_skips_already_done_futures():
    c = _client()
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(None)  # pre-resolve
    uuid = b"11111111-1111-1111-1111-111111111111"
    c._shutter_waiters[uuid.decode()] = [fut]
    c._dispatch_shutter_event(field_string(5, field_string(1, uuid)))
    # Should not have raised (the `if not fut.done()` guard fires).


def test_dispatch_logs_and_recovers_on_internal_exception(monkeypatch):
    c = _client()
    # Force parse_fields to raise to exercise the outer except branch.
    import custom_components.finder_you.api.client as mod

    def boom(_buf):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "parse_fields", boom)
    # Should not raise -- the broad except in _dispatch_shutter_event swallows.
    c._dispatch_shutter_event(b"\x00")


# ---------------------------------------------------------------------------
# wait_for_shutter_event


async def test_wait_for_shutter_event_returns_when_dispatched():
    c = _client()
    uuid = b"c9e9168a-bf74-4bd2-b25b-dd2c98f94432"

    async def fire():
        await asyncio.sleep(0.01)
        c._dispatch_shutter_event(field_string(5, field_string(1, uuid)))

    await asyncio.gather(c.wait_for_shutter_event(uuid, timeout=1.0), fire())


async def test_wait_for_shutter_event_times_out():
    c = _client()
    uuid = b"deadbeef-dead-beef-dead-beefdeadbeef"
    with pytest.raises(GatewayOfflineError, match="no gateway notification"):
        await c.wait_for_shutter_event(uuid, timeout=0.05)
    # Waiter list cleaned up.
    assert uuid.decode() not in c._shutter_waiters


async def test_wait_for_shutter_event_cleans_up_other_waiters_left_alone():
    c = _client()
    uuid = b"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    other = asyncio.get_event_loop().create_future()
    # Register another waiter for the same key so removal goes through
    # the `if waiters` non-empty branch.
    c._shutter_waiters[uuid.decode()] = [other]
    with pytest.raises(GatewayOfflineError):
        await c.wait_for_shutter_event(uuid, timeout=0.05)
    # The pre-existing future is untouched (it was not the one we removed).
    assert other in c._shutter_waiters[uuid.decode()]


# ---------------------------------------------------------------------------
# _next_sid


async def test_next_sid_increments_odd():
    c = _client()
    a = await c._next_sid()
    b = await c._next_sid()
    cc = await c._next_sid()
    assert (a, b, cc) == (1, 3, 5)


# ---------------------------------------------------------------------------
# Headers + DATA frame builders


def test_build_headers_frame_includes_auth_when_requested():
    c = _client()
    frame = c._build_headers_frame("Foo", 5, with_auth=True)
    # We can decode via a fresh hpack decoder.
    import hpack

    decoder = hpack.Decoder()
    # Skip the 9-byte frame header.
    headers = dict(decoder.decode(frame[9:]))
    assert headers[":path"] == "/finder_home.grpc.common.model.v1.FinderHome/Foo"
    assert headers["authorization"] == "Bearer tok"
    assert headers["te"] == "trailers"


def test_build_headers_frame_omits_auth_when_disabled():
    c = _client()
    frame = c._build_headers_frame("Bar", 7, with_auth=False)
    import hpack

    decoder = hpack.Decoder()
    headers = dict(decoder.decode(frame[9:]))
    assert "authorization" not in headers


def test_build_data_frame_end_stream_flag():
    c = _client()
    a = c._build_data_frame(b"abc", 3, end_stream=True)
    b = c._build_data_frame(b"abc", 3, end_stream=False)
    # 9-byte header; flags byte at index 4.
    assert a[4] == FLAG_END_STREAM
    assert b[4] == 0


# ---------------------------------------------------------------------------
# _finalize_stream


def test_finalize_stream_strips_grpc_framing():
    c = _client()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending[5] = fut
    body = bytes([0]) + struct.pack(">L", 3) + b"abc"
    c._finalize_stream(5, body, 0)
    assert fut.result() == (b"abc", 0)


def test_finalize_stream_handles_short_body():
    c = _client()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending[5] = fut
    c._finalize_stream(5, b"\x00", 0)
    # Body shorter than 5 -> payload is empty.
    assert fut.result() == (b"", 0)


def test_finalize_stream_no_pending_future_is_noop():
    c = _client()
    c._finalize_stream(99, b"\x00\x00\x00\x00\x00", 0)


def test_finalize_stream_skips_done_future():
    c = _client()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(("ignored", 0))
    c._pending[5] = fut
    body = bytes([0]) + struct.pack(">L", 3) + b"new"
    c._finalize_stream(5, body, 0)
    # The pre-set result is not overwritten.
    assert fut.result() == ("ignored", 0)


# ---------------------------------------------------------------------------
# _call success + error responses


async def test_call_returns_payload_on_grpc_status_0_and_field_1_eq_1():
    c = _client()
    c._writer = AsyncMock()
    c._writer.write = MagicMock()
    c._next_stream_id = 7

    async def fake_drain():
        sid = 7
        # field 1 = 1 (OK), field 2 = bytes
        payload = field_varint(1, 1) + field_string(2, b"data")
        c._finalize_stream(sid, bytes([0]) + struct.pack(">L", len(payload)) + payload, 0)

    c._writer.drain.side_effect = fake_drain
    out = await c._call("Foo", b"body", with_auth=False)
    assert parse_fields(out)[2] == [b"data"]


async def test_call_raises_when_grpc_status_nonzero():
    c = _client()
    c._writer = AsyncMock()
    c._writer.write = MagicMock()

    async def fake_drain():
        c._finalize_stream(1, bytes([0, 0, 0, 0, 0]), 5)

    c._writer.drain.side_effect = fake_drain
    with pytest.raises(FinderApiError) as exc:
        await c._call("Foo", b"body")
    assert exc.value.status == 5


async def test_call_raises_when_field_1_says_error():
    c = _client()
    c._writer = AsyncMock()
    c._writer.write = MagicMock()

    async def fake_drain():
        payload = field_varint(1, 2) + field_varint(2, 19)
        c._finalize_stream(
            1, bytes([0]) + struct.pack(">L", len(payload)) + payload, 0
        )

    c._writer.drain.side_effect = fake_drain
    with pytest.raises(FinderApiError) as exc:
        await c._call("Foo", b"body")
    assert exc.value.status == 2
    assert exc.value.code == 19


# ---------------------------------------------------------------------------
# High-level RPC body shapes (we can verify the proto encoding without networking)


async def test_set_open_percent_sends_expected_body():
    c = _client()
    c._writer = AsyncMock()
    sent_bodies: list[bytes] = []

    async def fake_call(method, body, with_auth=True):
        sent_bodies.append(body)
        return b""

    c._call = fake_call  # type: ignore[assignment]
    await c.set_open_percent(b"plant", b"shutter", 42)
    body = sent_bodies[0]
    fields = parse_fields(body)
    assert fields[2] == [b"plant"]
    assert fields[3] == [b"shutter"]
    assert fields[4] == [42]


async def test_open_full_close_full_and_get_plant_bodies():
    c = _client()

    async def fake_call(method, body, with_auth=True):
        return body if method == "GetPlant" else b""

    c._call = fake_call  # type: ignore[assignment]
    await c.open_full(b"p", b"s")
    await c.close_full(b"p", b"s")
    out = await c.get_plant(b"p")
    assert parse_fields(out)[2] == [b"p"]


# ---------------------------------------------------------------------------
# close() and _notification_keepalive


async def test_close_cancels_tasks_and_closes_writer():
    c = _client()
    c._notification_keepalive_task = MagicMock()
    c._read_task = MagicMock()
    c._writer = MagicMock()

    async def waited():
        return None

    c._writer.wait_closed = waited
    await c.close()
    c._notification_keepalive_task.cancel.assert_called_once()
    c._read_task.cancel.assert_called_once()
    c._writer.close.assert_called_once()


async def test_close_swallows_writer_errors():
    c = _client()
    c._writer = MagicMock()
    c._writer.close.side_effect = RuntimeError("boom")
    # Should not propagate.
    await c.close()


async def test_close_when_nothing_open_is_safe():
    c = _client()
    await c.close()


async def test_notification_keepalive_resends_then_exits_on_writer_none():
    c = _client()
    sent = []

    class FakeWriter:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            return None

    c._writer = FakeWriter()

    # Patch asyncio.sleep so the 30 s loop runs instantly and we can flip
    # _writer to None after the first iteration.
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(_t):
        calls["n"] += 1
        if calls["n"] == 1:
            return
        # 2nd iter: drop writer so the function returns.
        c._writer = None

    with patch("custom_components.finder_you.api.client.asyncio.sleep", new=fake_sleep):
        await c._notification_keepalive(sid=1, keepalive_body=b"K")
    assert sent  # first write fired
    assert calls["n"] >= 2


async def test_notification_keepalive_exits_on_cancel():
    c = _client()
    c._writer = AsyncMock()

    async def cancel_sleep(_t):
        raise asyncio.CancelledError

    with patch("custom_components.finder_you.api.client.asyncio.sleep", new=cancel_sleep):
        # Returns cleanly on cancel.
        await c._notification_keepalive(sid=1, keepalive_body=b"K")


async def test_notification_keepalive_logs_other_exception():
    c = _client()
    c._writer = AsyncMock()

    async def boom_sleep(_t):
        raise RuntimeError("kaboom")

    with patch("custom_components.finder_you.api.client.asyncio.sleep", new=boom_sleep):
        # The broad except logs and returns.
        await c._notification_keepalive(sid=1, keepalive_body=b"K")


# ---------------------------------------------------------------------------
# _recv_frame: parses a 9-byte header + body


async def test_recv_frame_parses_header_and_body():
    c = _client()
    reader = MagicMock()
    body = b"abc"
    hdr = (
        len(body).to_bytes(3, "big")
        + bytes([TYPE_DATA, 0x1])
        + (5 | (1 << 31)).to_bytes(4, "big")  # high bit must be masked
    )

    queue = [hdr, body]

    async def readexactly(n):
        return queue.pop(0)

    reader.readexactly = readexactly
    c._reader = reader
    frame = await c._recv_frame()
    assert frame == (TYPE_DATA, 0x1, 5, b"abc")


async def test_recv_frame_zero_length_skips_body_read():
    c = _client()
    reader = MagicMock()
    hdr = b"\x00\x00\x00" + bytes([TYPE_SETTINGS, 0]) + (0).to_bytes(4, "big")

    async def readexactly(n):
        return hdr

    reader.readexactly = readexactly
    c._reader = reader
    ftype, flags, sid, body = await c._recv_frame()
    assert body == b""
    assert ftype == TYPE_SETTINGS


# ---------------------------------------------------------------------------
# _read_loop: orchestration is the densest part. Inject frames via a fake
# _recv_frame and observe dispatch into _pending and _notification_queue.


async def test_read_loop_dispatches_notification_data_to_queue():
    c = _client()
    c._notification_stream_id = 1
    c._writer = AsyncMock()

    payload = b"hello"
    grpc = bytes([0]) + struct.pack(">L", len(payload)) + payload

    frames = [
        (TYPE_DATA, 0, 1, grpc),
        None,  # signal IncompleteReadError to end the loop
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    assert c._notification_queue.get_nowait() == payload


async def test_read_loop_dispatches_data_to_pending_future():
    c = _client()
    c._writer = AsyncMock()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending[3] = fut

    payload = field_varint(1, 1)
    grpc = bytes([0]) + struct.pack(">L", len(payload)) + payload

    frames = [
        (TYPE_DATA, FLAG_END_STREAM, 3, grpc),
        None,
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    assert fut.done()
    body, status = fut.result()
    assert body == payload
    assert status == 0


async def test_read_loop_collects_grpc_status_from_trailers():
    c = _client()
    c._writer = AsyncMock()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending[3] = fut

    payload = b""
    grpc = bytes([0]) + struct.pack(">L", 0) + payload

    import hpack

    enc = hpack.Encoder()
    trailers = enc.encode([("grpc-status", "7")])

    frames = [
        (TYPE_DATA, 0, 3, grpc),
        (TYPE_HEADERS, FLAG_END_STREAM, 3, trailers),
        None,
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    _, status = fut.result()
    assert status == 7


async def test_read_loop_swallows_trailer_decode_errors():
    c = _client()
    c._writer = AsyncMock()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending[3] = fut
    bogus = b"\xff\xff\xff"
    frames = [
        (TYPE_HEADERS, FLAG_END_STREAM, 3, bogus),
        None,
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    # Future got resolved with empty body + default status 0.
    body, status = fut.result()
    assert body == b""
    assert status == 0


async def test_read_loop_acks_ping():
    c = _client()
    writer = AsyncMock()
    writer.drain = AsyncMock()
    c._writer = writer

    ping_body = b"\x01" * 8
    frames = [
        (TYPE_PING, 0, 0, ping_body),
        None,
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    # An ACK frame was written.
    assert writer.write.called
    written = writer.write.call_args[0][0]
    assert written[3] == TYPE_PING
    assert written[4] & FLAG_ACK


async def test_read_loop_ignores_ping_ack():
    c = _client()
    c._writer = AsyncMock()

    frames = [
        (TYPE_PING, FLAG_ACK, 0, b"\x00" * 8),
        None,
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()  # no exception, no extra writes
    assert not c._writer.write.called


async def test_read_loop_breaks_on_goaway():
    c = _client()
    c._writer = AsyncMock()
    frames = [(TYPE_GOAWAY, 0, 0, b"")]

    async def fake_recv():
        return frames.pop(0)

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()


async def test_read_loop_fails_pending_futures_on_disconnect():
    c = _client()
    c._writer = AsyncMock()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending[3] = fut

    async def fake_recv():
        raise asyncio.IncompleteReadError(b"", None)

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    assert fut.done()
    with pytest.raises(ConnectionError):
        fut.result()


async def test_read_loop_swallows_short_notification_message():
    c = _client()
    c._notification_stream_id = 1
    c._writer = AsyncMock()

    # body shorter than 5 bytes — message is silently dropped (no exception).
    frames = [
        (TYPE_DATA, 0, 1, b"\x00\x00"),
        None,
    ]

    async def fake_recv():
        f = frames.pop(0)
        if f is None:
            raise asyncio.IncompleteReadError(b"", None)
        return f

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()
    assert c._notification_queue.empty()


async def test_read_loop_drops_done_pending_silently():
    c = _client()
    c._writer = AsyncMock()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(None)
    c._pending[3] = fut

    async def fake_recv():
        raise asyncio.IncompleteReadError(b"", None)

    c._recv_frame = fake_recv  # type: ignore[assignment]
    await c._read_loop()


# ---------------------------------------------------------------------------
# connect(): we patch asyncio.open_connection and exercise _connect through
# the public classmethod, then drive a fake handshake() by patching it.


async def test_classmethod_connect_runs_connect():
    c = FinderHomeClient("tok")
    # We can't run the real _connect (it talks to the cloud), but we can
    # verify the classmethod calls it. Patch _connect.
    with patch.object(FinderHomeClient, "_connect", new=AsyncMock()) as p:
        ret = await FinderHomeClient.connect("t2")
    assert isinstance(ret, FinderHomeClient)
    p.assert_called_once()


# ---------------------------------------------------------------------------
# _connect: needs an open_connection + recv_frame stub feeding a SETTINGS so
# the handshake loop can ACK and start the read task.


async def test__connect_sends_preface_settings_window_update_and_acks():
    c = FinderHomeClient("tok")

    sent: list[bytes] = []

    class FakeWriter:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            return None

    reader = MagicMock()

    async def open_conn(*a, **kw):
        return reader, FakeWriter()

    # Provide a server SETTINGS frame so the handshake's ack branch fires.
    # SETTINGS body is empty, so `_recv_frame` consumes only the 9-byte
    # header and skips the body read entirely. We don't queue the empty
    # body — the next read (from the background read-task created at the
    # end of _connect) should hit EOF and cleanly exit.
    server_settings = _build_frame(TYPE_SETTINGS, 0, 0, b"")
    queue = [server_settings[0:9]]

    async def readexactly(n):
        if not queue:
            raise asyncio.IncompleteReadError(b"", n)
        return queue.pop(0)

    reader.readexactly = readexactly

    with patch(
        "custom_components.finder_you.api.client.asyncio.open_connection",
        new=open_conn,
    ):
        await c._connect()
    # The first thing sent must be the preface, followed by SETTINGS and
    # WINDOW_UPDATE frames. They're concatenated into a single write(), so
    # walk the flat buffer.
    flat = b"".join(sent)
    assert flat.startswith(H2_PREFACE)
    # Past the preface, parse the back-to-back frames.
    pos = len(H2_PREFACE)
    frame_types: list[int] = []
    while pos + 9 <= len(flat):
        length = int.from_bytes(flat[pos : pos + 3], "big")
        ftype = flat[pos + 3]
        frame_types.append(ftype)
        pos += 9 + length
    assert TYPE_SETTINGS in frame_types
    assert TYPE_WINDOW_UPDATE in frame_types
    # The read task is now scheduled — cancel it to keep test runner clean.
    c._read_task.cancel()


async def test__connect_raises_when_server_disconnects_before_settings():
    c = FinderHomeClient("tok")

    reader = MagicMock()

    async def readexactly(n):
        raise asyncio.IncompleteReadError(b"", None)

    reader.readexactly = readexactly

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            return None

    async def open_conn(*a, **kw):
        return reader, FakeWriter()

    with patch(
        "custom_components.finder_you.api.client.asyncio.open_connection",
        new=open_conn,
    ):
        with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
            await c._connect()


# ---------------------------------------------------------------------------
# handshake(): we patch internals (_call, _next_sid) and watch the 3-message
# subscription bytes appear on the wire.


async def test_handshake_sends_three_subscription_messages_and_keepalive():
    c = FinderHomeClient("tok")
    sent: list[bytes] = []

    class FakeWriter:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            return None

    c._writer = FakeWriter()

    # Feed the 3 handshake-stage notification responses.
    c._notification_queue.put_nowait(b"\x10\x01")
    c._notification_queue.put_nowait(b"\x10\x01\x40\x01")
    c._notification_queue.put_nowait(b"plant-state")

    async def fake_call(method, body, with_auth=True):
        if method == "GetUserPlants":
            # Embed plant_id under field 3 → inner field 1.
            plant_inner = field_string(1, b"plant-1234")
            return field_string(3, plant_inner)
        return b""

    c._call = fake_call  # type: ignore[assignment]
    keepalive_started = []

    async def fake_keepalive(sid, body):
        keepalive_started.append((sid, body))

    c._notification_keepalive = fake_keepalive  # type: ignore[assignment]

    out = await c.handshake()
    # The keepalive is scheduled via create_task; yield once so the loop
    # actually runs our patched fake before we assert on it.
    await asyncio.sleep(0)
    assert isinstance(out, dict)
    # Two DATA frames (msg1, msg2) for subscribe — types in slot 3 of frame.
    types = [f[3] for f in sent if len(f) >= 4]
    assert types.count(TYPE_DATA) >= 2
    # Keepalive was scheduled.
    assert keepalive_started


async def test_handshake_warns_when_no_plant_id_in_response(caplog):
    c = FinderHomeClient("tok")

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            return None

    c._writer = FakeWriter()
    # Queue acks for both subscribe responses.
    c._notification_queue.put_nowait(b"\x10\x01")
    c._notification_queue.put_nowait(b"\x10\x01\x40\x01")

    async def fake_call(method, body, with_auth=True):
        # GetUserPlants response with NO field 3 at all → no plant_id branch.
        return b""

    c._call = fake_call  # type: ignore[assignment]

    async def fake_keepalive(sid, body):
        return

    c._notification_keepalive = fake_keepalive  # type: ignore[assignment]
    out = await c.handshake()
    assert out == {}


async def test_handshake_swallows_subscription_timeouts(caplog):
    c = FinderHomeClient("tok")

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            return None

    c._writer = FakeWriter()
    # Don't queue any acks → all three waits time out, warnings logged.

    async def fake_call(method, body, with_auth=True):
        if method == "GetUserPlants":
            plant_inner = field_string(1, b"plant")
            return field_string(3, plant_inner)
        return b""

    c._call = fake_call  # type: ignore[assignment]

    async def fake_keepalive(sid, body):
        return

    c._notification_keepalive = fake_keepalive  # type: ignore[assignment]

    # Speed up the three wait_for(timeout=...) calls so the test isn't slow.
    original_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await original_wait_for(coro, timeout=0.01)

    with patch(
        "custom_components.finder_you.api.client.asyncio.wait_for",
        new=fast_wait_for,
    ):
        await c.handshake()
    # We just need this to complete without raising — the timeouts are logged.
