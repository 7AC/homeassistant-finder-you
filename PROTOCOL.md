# Finder YOU cloud protocol

Reverse-engineered notes on the `you-api.iot.findernet.com:443` gRPC
backend and the `accounts.iot.findernet.com` IdentityServer4 OAuth that
fronts it. Enough to write a client from scratch.

If you replay raw bytes from a "naïve" gRPC client (Python `grpc-python`,
plain `httpx`, `curl_cffi`) you will get the opaque server error
`{status: 2, code: 19}` on every device-touching call — even with a
valid `api.v1` JWT and byte-identical bootstrap responses. The cloud
demands a specific connection setup *and* a specific subscription
handshake before it will route gateway commands to you.

The pieces, in order:

1. [OAuth — Android-style, no PKCE](#1-oauth--android-style-no-pkce)
2. [Raw HTTP/2 — Android's exact connection setup](#2-raw-http2--androids-exact-connection-setup)
3. [The gRPC service](#3-the-grpc-service)
4. [OpenNotificationChannel — the 3-message subscription](#4-opennotificationchannel--the-3-message-subscription)
5. [One connection for everything + 30 s keepalive](#5-one-connection-for-everything--30-s-keepalive)

---

## 1. OAuth — Android-style (no PKCE)

The OAuth client is `com.findernet.You`. The iOS app uses PKCE; the
Android app does **not** — and the IdentityServer4 instance accepts
both. The Android flow is one fewer step, so we use it.

### Step 1 — `GET /connect/authorize`

Redirect discovery. Omit `code_challenge` so the server doesn't expect a
`code_verifier` later.

```http
GET /connect/authorize?
    client_id=com.findernet.You
    &response_type=code
    &scope=openid email profile offline_access api.v1 finder:role finder:language
    &redirect_uri=finderyou://auth
```

Response: `302` to `/access/signin?returnUrl=…`. Capture the `returnUrl`.

### Step 2 — `POST /_api/v1/auth/signin-oidc`

The Vue SPA at `/access/signin` posts JSON credentials here. **The path
suffix is `-oidc`** — the plain `/_api/v1/auth/signin` endpoint returns
`UNAUTHORIZED` for `api.v1`-scoped clients.

```http
POST /_api/v1/auth/signin-oidc
Content-Type: application/json;charset=UTF-8
Origin: https://accounts.iot.findernet.com
Referer: https://accounts.iot.findernet.com/access/signin
X-Requested-With: com.findernet.FinderYou
Accept: application/json, text/plain, */*
Sec-Fetch-Site: same-origin
Sec-Fetch-Mode: cors
Sec-Fetch-Dest: empty

{
  "returnUrl": "<from step 1>",
  "username": "<email>",
  "password": "<password>",
  "impersonateUsername": null
}
```

Response: `{"data": {"next": "/connect/authorize/callback?..."}, "result": "OK"}`
plus `Set-Cookie: idsrv.session=...; Set-Cookie: FINDER_AUTH=...`.

### Step 3 — follow the callback

`GET <base>/connect/authorize/callback?...` (the `next` URL) with the
cookies set. Server replies `302` to
`finderyou://auth?code=<code>&scope=…&session_state=…`. Extract `code`.

### Step 4 — exchange code for token

```http
POST /connect/token
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=authorization_code
&client_id=com.findernet.You
&code=<code>
&redirect_uri=finderyou://auth
```

No `code_verifier` (non-PKCE leg). Returns `access_token` +
`refresh_token` + `expires_in` JSON. The access token has
`aud: api.v1`, `iss: https://accounts.iot.findernet.com`, ~1 h life.

PKCE-issued and password-issued JWTs are **structurally identical** —
the differentiator is not in the token.

---

## 2. Raw HTTP/2 — Android's exact connection setup

This is the first half of what distinguishes accepted clients from
rejected ones. `grpc-python` opens a fresh TCP+TLS per call and emits
its own SETTINGS — the server-side router doesn't like that.

Open one TCP+TLS connection to `you-api.iot.findernet.com:443` (ALPN
`h2`), and send these three things, in order, with nothing else
between them:

| Frame | Payload | Notes |
|---|---|---|
| HTTP/2 preface | `PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n` | 24-byte literal |
| `SETTINGS` | `ENABLE_PUSH = 0`, `INITIAL_WINDOW_SIZE = 65535` | **Exactly these two.** No `HEADER_TABLE_SIZE`, no `MAX_CONCURRENT_STREAMS`. |
| `WINDOW_UPDATE` on stream 0 | increment `0x03FF0001` (~67 MB) | Connection-level window expansion |

Read the server's `SETTINGS` frame (it sets
`MAX_CONCURRENT_STREAMS = 100`) and `ACK` it. From this point you
multiplex any number of streams. **Client streams use odd IDs starting
at 1** — note 1, not 3 — because the first stream you open is
`OpenNotificationChannel` (see §4), and Android opens it as stream 1.

---

## 3. The gRPC service

Service: `finder_home.grpc.common.model.v1.FinderHome`.

Each RPC is a normal gRPC-over-HTTP/2 call on its own stream:

- **HEADERS** with these pseudo-headers:
  - `:authority: you-api.iot.findernet.com`
  - `:method: POST`
  - `:path: /finder_home.grpc.common.model.v1.FinderHome/<Method>`
  - `:scheme: https`
- Real headers (every call):
  - `user-agent: grpc-dotnet/2.66.0 (.NET 9.0.14; CLR 9.0.14; net8.0; arm64)`
  - `te: trailers`
  - `grpc-accept-encoding: identity,gzip,deflate`
  - `content-type: application/grpc`
  - `authorization: Bearer <jwt>` — see "Auth" column below
- **DATA** with gRPC framing (`0x00 + uint32 length BE`) + protobuf
  body. `END_STREAM` set for unary calls, **cleared** for
  OpenNotificationChannel.

Responses are DATA frame(s) followed by trailers HEADERS with
`grpc-status: 0` on success, non-zero on failure. Error responses carry
`field 1 = 2, field 2 = <code>` inside the gRPC body — `code 19` ("no
claim") is the central one this document exists to solve.

### Methods

| Method | Auth | Request body |
|---|---|---|
| `CheckUser` | none | `field 1 = ClientInfo` |
| `PlatformCheck` | none | `field 1 = ClientInfo` |
| `GetUserPlants` | Bearer | `field 1 = ClientInfo` |
| `OpenNotificationChannel` (bidi-streaming) | Bearer | see §4 |
| `GetPlant` | Bearer | `field 1 = ClientInfo`, `field 2 = plant_id` |
| `SetOpenPercent` | Bearer | `field 1 = ClientInfo`, `field 2 = plant_id`, `field 3 = shutter_id`, `field 4 = percent` |
| `OpenFull` | Bearer | same as SetOpenPercent without `field 4` |
| `CloseFull` | Bearer | same as OpenFull |
| `ActivateScenario` | Bearer | `field 1 = ClientInfo`, `field 2 = plant_id`, `field 3 = scenario_id` |

Successful responses contain `field 1 = varint 1` ("OK") plus
method-specific payload.

### The `ClientInfo` envelope

Every request wraps its arguments in a `ClientInfo` payload at field 1:

```
ClientInfo {
  #1 string client_id   UUID generated at first install (any UUID works)
  #2 varint version     always 143
  #3 string platform    "Finder You/Android" (or "Finder You/iOS")
  #4 string appVersion  "1.4.4"
  #5 string device      e.g. "Google/sdk_gphone64_arm64/14"
  #6 varint             0
}
```

Server accepts both Android and iOS-style platform/device strings.

### The `GetPlant` response

~22 KB of protobuf containing the full plant tree:

- Devices at the top with their UUIDs and AES-128 keys (used for local
  BLE-mesh signing if you ever go off-cloud).
- Rooms tree with display names and device-type markers
  `device_roller_shutter_50` (shutters), `device_light_bulb` (lights).
- Scenes/scenarios with their UUIDs and human-readable names.
- One per-device **state submessage** per shutter, repeated under
  field 12 of the plant body (top-level field 3 of the response
  wrapper). The state submessage carries:

| Field | Meaning |
|-------|---------|
| `#1`  | shutter UUID (36-char string) |
| `#2`  | plant UUID (same for every shutter) |
| `#3`  | device-family string (`13S2` for these shutters) |
| `#4`  | BLE MAC address (in `{ #1: "AA:BB:CC:DD:EE:FF" }`) |
| `#6`  | config JSON blob (channel mappings, names) |
| `#7`  | commissioning timestamps (`#1`, `#2` varints) |
| `#8`  | gateway UUID |
| `#9`  | RSSI / signal value |
| `#11` | reserved varint |
| `#12` | motion flag (`varint(2)` = idle, `varint(3)` = moving) |
| `#13` | open percentage (`{ #1: varint(0..100) }`) |

The integration walks this tree to enumerate shutters as
`{uuid, name, room_name}` and to extract per-shutter positions for
HomeKit state. See
[`api/plant.py`](custom_components/finder_you/api/plant.py).

**Caveat — plant-cache lag.** The position fields update *lazily*: a
successful `SetOpenPercent` is reflected in the plant payload anywhere
from a few seconds to ~90 s later, depending on gateway WiFi/MQTT
health and BLE-mesh load. During a 6-shutter scene we've seen every
slice update slip past a 60 s window even though four of the shutters
physically moved. After issuing a command we poll `GetPlant` until we
observe **motor evidence** for that shutter — either the position
field (#13) changes from baseline, or the motion flag (#12) reads 3
("driving") at any verify poll — or up to a 180 s timeout. Position
alone isn't enough: the gateway clears `#13` when a command is queued
and refills it from BLE-mesh telemetry, so a "100 → None" transition
looks like motion even when no motor ran. Motion=3 is observed only
when the gateway is actively driving the motor, so it's positive
proof the BLE-mesh hop landed.

**Caveat — BLE-mesh fan-out.** The cloud accepts commands over WiFi,
but the gateway then has to hop each command to the shutter via
BLE-mesh. Firing six commands back-to-back swamps the mesh — the
farthest hops (e.g. living-room and kitchen shutters in our setup) get
dropped on the floor even though the cloud ack came back fine. We
serialize sends with a 2 s inter-command gap so each BLE-mesh hop has
time to land before the next one fires.

**Caveat — stale subscriptions.** Even with the keepalive (re-sending
the subscribe-client message every 30 s — see §4), the
`OpenNotificationChannel` claim can go silently stale on the cloud
side after a server restart or claim expiry, and every subsequent
`SetOpenPercent` is dropped on the floor even though `GetPlant` still
works. The gateway only recovers when we tear down the HTTP/2
connection and re-run the full 3-message handshake. The integration
self-heals on a verify timeout: it drops the client, re-handshakes,
and retries the command — up to `MAX_SEND_ATTEMPTS = 3` total
attempts — before raising `GatewayOfflineError`. The dominant cost
of a wedge is the **first** 180 s verify timeout that detects it; the
retry then completes within seconds because the fresh handshake
re-establishes the puck's cloud-side claim.

**Why no time-based watchdog.** A scheduled forced rehandshake every
N minutes was tempting but is wasteful: the keepalive is fire-and-
forget (subscribe-client gets no response on subsequent fires; we
have no protocol-level "is the claim still good?" probe), and a
healthy claim can survive for hours of idle time, so a periodic
rehandshake churns the connection for no measurable benefit.

**Preemptive rehandshake on stale telemetry.** What we do instead:
a fresh user command that arrives while the gateway has been silent
(no per-shutter slice diff observed by polls) for more than
`PREEMPTIVE_HANDSHAKE_TELEMETRY_AGE` (default 10 min) drops the
cloud client *before* the first send. The implicit reconnect on the
next call re-runs the full 3-message OpenNotificationChannel
handshake, which clears whatever cloud-side claim drift accumulated
during the idle period. The reactive self-heal does the same thing
— but only after a 180 s verify timeout, which is the dominant cost
of "first scene of the morning takes three minutes." The preemptive
gate folds that cost into the user command itself, so the first try
lands directly. It only fires when there's actual evidence of drift
(silent telemetry), not on a clock. Skipped when another command
holds `_send_lock` so we don't yank the connection out from under
an in-flight verify.

**"Unknown baseline" fast-path.** The cloud cache periodically clears
the position field for individual shutters (the data structure shows
position field `#13` present but with no inner varint when this
happens). When a command targets such a shutter we can't verify by
position change — there's nothing to diff against. Treating that as
a verify failure makes "close all" silently get stuck on
`Closing…` for the full 3×180 s retry budget on any shutter that
was already at the target, because already-at-target sends produce
no motor evidence (the motor doesn't run if it's already there). To
avoid this, when the baseline position is unknown but the gateway
is still demonstrating it can push telemetry — `_telemetry_recent()`
returns true for fresh slice diffs on *other* shutters — we accept
the cloud-side send as success without waiting for motor evidence.
Wedge state is explicitly excluded: stale telemetry plus unknown
baseline means we have no signal of liveness at all, so we still
run the full verify and surface the failure honestly.

**Live-client takeover.** `OpenNotificationChannel` grants live-client
status to one app at a time. When the Finder YOU mobile app is
opened it silently demotes us: our cloud-side `SetOpenPercent` RPCs
keep getting accepted (the cloud answers with success) but they
never reach the puck — and there's no protocol-level signal that
tells us we've been demoted. Verify timeouts catch this eventually,
but the unknown-baseline fast-path masks it: it reports success
without observing motor activity. We work around this with a
two-level reclaim:

1. **In-line reclaim+retry within one command.** If the first
   attempt of a send returns via the fast-path (no motor evidence),
   we immediately drop the cloud client to force a fresh
   `OpenNotificationChannel` handshake and try the send once more.
   The user's *first* tap after the Finder app session — the one
   that would otherwise silently no-op — recovers within the same
   service call. Bounded to one reclaim-retry per command so a
   genuine no-op (close on already-closed shutter) doesn't loop.

2. **Cross-command reclaim via suspicion counter.** The fast-path
   also bumps `_unverified_send_count`; real motor evidence (or a
   `baseline == target` short-circuit, which proves cloud-cache →
   us is alive on the subscription) resets it. Once it reaches
   `PREEMPTIVE_HANDSHAKE_UNVERIFIED_SENDS` (default 1, single tap
   is enough), the *next* command's preemptive-rehandshake gate
   fires. Belt-and-suspenders with the in-line path above: handles
   the case where the in-line retry also goes via fast-path (true
   no-op) but the previous command was the one that lost the claim.

Cost of being wrong is one extra handshake (~3 s) on the first
fast-path of a session; cost of being right is no more "Apple Home
says it worked but the shutter didn't move" surprises after using
the Finder app.

**Caveat — observability is a separate concern.** We can't fix what
we can't see. The coordinator now diffs each per-shutter slice
against the prior poll and emits an `INFO` log line (`telemetry: N
shutter(s) updated: <uuid-prefixes>`) for any byte-level change.
Three diagnostic sensors expose freshness as seconds-ago:
`Gateway telemetry age`, `Gateway last command age`, and
`Gateway handshake age`. Watching `Gateway telemetry age` climb
unbounded while `Gateway handshake age` is fresh is the
fingerprint of a wedge — the cloud is talking to us, just not to
the puck — and is the data we need to characterize the wedge
cadence before designing a real preventive measure.

---

## 4. OpenNotificationChannel — the 3-message subscription

`OpenNotificationChannel` is **bidirectional-streaming**, not server-
streaming as you might guess from the name. Opening it and sending a
single `ClientInfo` *does* get a one-byte `10 01` reply — and historic
captures led us to believe that single ack was the "live client"
signal. **It is not.** That handshake is a three-message subscribe, and
without it every device-touching RPC returns `code: 19`.

The dance (all on stream 1, all with `END_STREAM` cleared on the DATA
frames):

### Message 1 — hello

```
DATA stream=1 (no END_STREAM)
   field 1 = ClientInfo
```

Server replies on the same stream:

```
DATA stream=1
   10 01      ← field 2 = varint 1 ("I see you")
```

### A PING in between

Android sends a PING frame (opaque data `FF FF FF FF FF FF FF FF`)
between messages 1 and 2. We replicate it. Skipping the PING sometimes
silently causes subscribe to not take.

```
PING stream=0
   FF FF FF FF FF FF FF FF
```

### Message 2 — subscribe-as-client

```
DATA stream=1 (no END_STREAM)
   field 1 = ClientInfo
   field 2 = varint 1
```

Server replies:

```
DATA stream=1
   10 01 40 01   ← field 2 = 1 (ack), field 8 = 1 (claim granted)
```

After this reply, the cloud regards this TCP connection as the
authoritative "live" client. **Unary calls on streams 3, 5, 7… will now
succeed for the bootstrap RPCs** (`CheckUser`, `PlatformCheck`,
`GetUserPlants`). Run those next — Android does.

### Message 3 — subscribe-plant

After `GetUserPlants` you know the plant UUID. Send:

```
DATA stream=1 (no END_STREAM)
   field 1 = ClientInfo
   field 2 = varint 2
   field 3 = nested {
       field 1 = string plant_id
   }
```

`field 3` is a length-delimited **nested message** whose inner `field 1`
carries the plant UUID. Wrap it; don't send `plant_id` flat at field 3.
(Sending it flat was the bug that wasted us an hour — flat field 3
times out with no reply.)

Server streams back a large (kilobytes) plant-state payload — initial
positions of all shutters, scenario state, etc. The gateway is now
paired with this TCP connection. `GetPlant` / `SetOpenPercent` /
`OpenFull` / `CloseFull` / `ActivateScenario` on subsequent unary
streams all succeed.

---

## 5. One connection for everything + 30 s keepalive

All RPCs in a session — bootstrap, the notification stream, every
control command — must travel over the **same** TCP+TLS+HTTP/2
connection. Opening a fresh channel per call (default in many gRPC
libraries) is fatal.

Practically:

- Use a raw HTTP/2 client
  ([`hpack`](https://pypi.org/project/hpack/) +
  `asyncio.open_connection(..., ssl=...)`), not `grpc-python`.
- **Re-send message 2 every 30 s** on stream 1. The cloud appears to
  expire the claim if the bidi stream goes quiet. Without the keepalive,
  device-touching calls start returning `code: 19` again after roughly a
  minute.
- Re-do the full handshake (preface → SETTINGS → WINDOW_UPDATE →
  3-message subscribe → bootstrap) when the server sends `GOAWAY`
  (typically every ~hour) or the TCP socket drops.
- Refresh the JWT a few minutes before its `expires_in` runs out. Send
  the new bearer on the next unary stream without closing the
  connection.

---

## How this was found

A pure-Python clone of the wire bytes succeeded for a single 30-minute
window on 2026-06-12 and then broke. We initially guessed cloud rate
limiting or live-session affinity to the iPhone. Neither held up — the
user logged out the iPhone entirely, rebooted the gateway, rotated
tokens, and the breakage persisted.

What actually worked: spawn Finder YOU under Frida on an Apple-Silicon
Mac running an `arm64-v8a` Pixel 6 AVD, with hooks on
`AndroidCryptoNative_SSLStreamWrite` / `…Read` in
`libSystem.Security.Cryptography.Native.Android.so` from process
spawn. Those exports give post-TLS plaintext for the .NET MAUI gRPC
stack. The capture revealed three OpenNotificationChannel DATA frames
where we'd only been sending one — once we replicated the PING and the
two extra subscribe messages, including the nested `field 3` for the
plant UUID, the cloud accepted unary RPCs and stayed accepting them as
long as message 2 was repeated every 30 s.

The "fresh tokens still fail" symptom was a red herring: we had been
debugging at the wrong layer the whole time. The JWT was fine; we just
weren't subscribed.
