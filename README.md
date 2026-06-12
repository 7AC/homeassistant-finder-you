# Finder YOU — Home Assistant integration

Control your **Finder YESLY roller shutters** from Home Assistant via the
Finder YOU cloud — no iPhone, no Android, no Alexa-routine hack.

> Status: v0.1.0. Shutters only. Position feedback and scenes land in v0.2.

## What it does

Adds a `cover.*` entity for every roller shutter on your Finder YOU plant.
Supports open, close, and set-position. The integration mints fresh tokens
from your email + password and maintains a long-lived HTTP/2 connection to
the Finder cloud so commands reach your gateway promptly.

## Install (via HACS)

1. HACS → ⋮ → Custom repositories
2. URL: `https://github.com/7AC/homeassistant-finder-you`
3. Category: Integration
4. Install. Restart Home Assistant.
5. Settings → Devices & Services → Add Integration → Finder YOU.
6. Enter the same email + password you use for the Finder YOU app.

## Compatibility

- Tested with Finder YOU app **v1.4.4** cloud.
- Account must **own** (not just share) the plant.
- YESLY roller shutters via a **1Y.GU** gateway only.
  Bliss thermostats use the separate
  [`condatek/finderblissha`](https://github.com/condatek/finderblissha).
- Single plant per account.

## Known limits in v0.1

- **No position feedback** — `cover.current_cover_position` reports
  `unknown`. Commands still work. State decode lands when we finish parsing
  the `OpenNotificationChannel` stream messages.
- **No scenarios** — control individual blinds; build "all open" /
  "all closed" macros with Apple Home or HA automations.

---

# Protocol

The Finder YOU app talks to two cloud endpoints:

| Host | What it does |
|---|---|
| `accounts.iot.findernet.com` | OAuth (IdentityServer4) — mints JWTs |
| `you-api.iot.findernet.com:443` | gRPC over HTTP/2 — device commands |

The gateway in your home holds a persistent MQTT connection to
`mqtt.iot.findernet.com:1883` and executes commands the cloud forwards.

If you replay raw bytes from a "naïve" gRPC client (Python `grpc-python`,
plain `httpx`, `curl_cffi`) you'll get the opaque server error
`{status:2, code:19}` on every device-touching call — even when your JWT
is valid for `api.v1` and bootstrap RPCs (`CheckUser`, `GetUserPlants`)
return byte-identical responses to the real app. **Five things have to
line up for the gateway to actually accept your commands.** They are
detailed below.

## 1. OAuth — Android-style (no PKCE)

The OAuth client is `com.findernet.You`. The iOS app uses PKCE; the
Android app does **not** — and the IdentityServer4 instance accepts both.
The Android flow is simpler so we use it.

### Step 1 — `GET /connect/authorize`

Pure redirect-discovery. We don't include `code_challenge` so the server
doesn't expect a `code_verifier` at the token-exchange step.

```http
GET /connect/authorize?
    client_id=com.findernet.You
    &response_type=code
    &scope=openid email profile offline_access api.v1 finder:role finder:language
    &redirect_uri=finderyou://auth
```

Response is a 302 to `/access/signin?returnUrl=…` (the IdentityServer4
session-cookie issuer). Capture the `returnUrl` query param.

### Step 2 — `POST /_api/v1/auth/signin-oidc`

The Vue SPA at `/access/signin` posts JSON credentials here. **The path
is `signin-oidc`, not `signin`** — the path without the `-oidc` suffix
returns `UNAUTHORIZED` for `api.v1`-scoped clients.

Required headers (the server rejects without them):

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

Response is `{"data": {"next": "/connect/authorize/callback?..."}, "result": "OK"}`
along with two `Set-Cookie` headers: `idsrv.session` and `FINDER_AUTH`.

### Step 3 — follow the callback

`GET <BASE>/connect/authorize/callback?...` (the `next` URL from step 2)
with the cookies set. Server replies 302 to
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

No `code_verifier` (this is the non-PKCE leg). Returns the usual
`access_token` + `refresh_token` + `expires_in` JSON.

The JWT has `aud: api.v1`, `iss: https://accounts.iot.findernet.com`,
`scope` matching the `api.v1` family. Nothing magical compared to a
PKCE-issued token — both flows produce structurally identical JWTs.

## 2. Raw HTTP/2 — Android's exact handshake

This is **the** thing that distinguishes accepted clients from rejected
ones. Python's `grpc-python` opens fresh connections per call and sends
its own SETTINGS — the server-side router doesn't like that.

Open a TCP+TLS connection to `you-api.iot.findernet.com:443` (ALPN
`h2`), and send these three things, in this order, with no other frames
in between:

| Frame | Bytes | Notes |
|---|---|---|
| HTTP/2 preface | `PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n` | 24-byte literal |
| `SETTINGS` | `ENABLE_PUSH=0`, `INITIAL_WINDOW_SIZE=65535` | **Exactly these two**. No `HEADER_TABLE_SIZE`, no `MAX_CONCURRENT_STREAMS`. |
| `WINDOW_UPDATE` on stream 0 | increment `0x03FF0001` (~67 MB) | The connection-level window expansion |

Then read the server's `SETTINGS` frame (it only sets
`MAX_CONCURRENT_STREAMS=100`) and send a `SETTINGS` `ACK`.

That's it. After this, you can multiplex any number of HEADERS+DATA
request streams. Stream IDs start at 3 and increment by 2 (odd) per the
HTTP/2 spec; the server uses even IDs for nothing in particular here.

## 3. gRPC service `finder_home.grpc.common.model.v1.FinderHome`

Each RPC is a standard gRPC-over-HTTP/2 unary call on its own stream:

- HEADERS frame with these pseudo-headers:
  - `:authority: you-api.iot.findernet.com`
  - `:method: POST`
  - `:path: /finder_home.grpc.common.model.v1.FinderHome/<Method>`
  - `:scheme: https`
- Real headers:
  - `user-agent: grpc-dotnet/2.66.0 (.NET 9.0.14; CLR 9.0.14; net8.0; arm64)`
  - `te: trailers`
  - `grpc-accept-encoding: identity,gzip,deflate`
  - `authorization: Bearer <jwt>` (omit for `PlatformCheck`)
  - `content-type: application/grpc`
- DATA frame: gRPC framing (`0x00 + uint32 length BE`) + protobuf body, `END_STREAM` set.

Responses come back as DATA frame(s) followed by trailers HEADERS frame
with `grpc-status: 0` on success, non-zero on failure.

### Methods we exercise

| Method | Auth | Request body shape |
|---|---|---|
| `PlatformCheck` | No | `field 1 = ClientInfo` |
| `GetUserPlants` | Bearer | `field 1 = ClientInfo` |
| `OpenNotificationChannel` (server-streaming) | Bearer | `field 1 = ClientInfo` |
| `GetPlant` | Bearer | `field 1 = ClientInfo`, `field 2 = plant_id` |
| `SetOpenPercent` | Bearer | `field 1 = ClientInfo`, `field 2 = plant_id`, `field 3 = shutter_id`, `field 4 = percent` |
| `OpenFull` | Bearer | same as SetOpenPercent without field 4 |
| `CloseFull` | Bearer | same as OpenFull |
| `ActivateScenario` | Bearer | `field 1 = ClientInfo`, `field 2 = plant_id`, `field 3 = scenario_id` |

Successful responses contain `field 1 = varint 1` ("OK"). Errors carry
`field 1 = 2, field 2 = <code>`. The infamous "error 19" is the code the
server returns when your connection state doesn't match what it expects.

### The `ClientInfo` envelope

Every request wraps its arguments in a `ClientInfo` payload at field 1:

```
ClientInfo {
  #1 string client_id     UUID generated at first install (any UUID works)
  #2 varint version       always 143
  #3 string platform      "Finder You/Android" (or "Finder You/iOS")
  #4 string appVersion    "1.4.4"
  #5 string device        e.g. "Google/sdk_gphone64_arm64/14"
  #6 varint               0
}
```

Server accepts both Android and iOS-style platform/device strings.

### The `GetPlant` response

\~27 KB of protobuf containing the plant tree:

- Device list at the top with their UUIDs and AES128 keys (used for local
  BLE-mesh signing if you ever go off-cloud).
- Rooms tree with display names and the device-type marker
  `device_roller_shutter_50` for shutters (also `device_light_bulb` for
  YESLY lights).
- Scenes/scenarios list with their UUIDs and human-readable names.

The integration walks this tree looking for shutter entries and pulls
out `{uuid, name}`. See [`api/plant.py`](custom_components/finder_you/api/plant.py).

## 4. Hold `OpenNotificationChannel` open

`OpenNotificationChannel` is a server-streaming RPC: client sends one
message (just the `ClientInfo`), server streams notification messages
back. Within 1–2 seconds of opening it the server pushes a single tiny
message (`10 01` = `field 2 = varint 1`) that we believe is the gateway
saying **"this h2 connection is now the live one"**.

If you don't open this stream — or if you open it on a different TCP
connection from the one you'll use for `SetOpenPercent` — every
device-touching call you make returns `code:19`.

The integration opens the channel as part of the initial handshake and
keeps the stream alive. Subsequent `GetPlant` / `SetOpenPercent` /
`ActivateScenario` calls flow over the same h2 connection on fresh
streams.

## 5. One connection for everything

All RPCs in a session — bootstrap, the notification stream, every
control command — must travel over the **same** TCP+TLS+HTTP/2
connection. Opening a fresh channel per call (the default behavior of
many gRPC client libraries) is fatal.

Practically:

- Use a raw HTTP/2 client (we use [`hpack`](https://pypi.org/project/hpack/)
  + `asyncio.open_connection(..., ssl=...)`), not `grpc-python`.
- Re-connect with the same Android-style handshake when the server sends
  `GOAWAY` (typically every ~hour) or the TCP socket drops.
- Refresh the JWT a few minutes before its `expires_in` runs out and
  send the new bearer on the next request without closing the
  connection.

---

## Reverse-engineering notes

Most of the above was recovered by Frida-hooking `SSL_StreamWrite` and
`SSL_StreamRead` in `libSystem.Security.Cryptography.Native.Android.so`
of the Android Finder YOU app running on an Apple-Silicon Mac emulator
(`pixel_6` AVD, `arm64-v8a`). The hooks let us see the post-TLS plaintext
the .NET MAUI runtime puts on the wire, including the SETTINGS bytes and
each gRPC body.

The breakthrough was realising that the JWT, body bytes, and headers we
sent from Python were byte-identical to the app's, yet the app
succeeded and we failed. Diffing the connection setup at the HTTP/2
framing level revealed Android's minimal SETTINGS + the 67 MB
WINDOW_UPDATE as the only meaningful difference.

## Credits

- [`hpack`](https://pypi.org/project/hpack/) for HPACK encode/decode.
- [`condatek/finderblissha`](https://github.com/condatek/finderblissha)
  for the Bliss-side reverse engineering that proved the broader Finder
  cloud was tractable.

## License

MIT.
