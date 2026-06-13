"""OAuth flow against accounts.iot.findernet.com — Android-style, no PKCE."""
from __future__ import annotations

import asyncio
import ssl
import urllib.parse

import httpx

ACCOUNTS_BASE = "https://accounts.iot.findernet.com"
CLIENT_ID = "com.findernet.You"
SCOPE = (
    "openid email profile offline_access api.v1 finder:role finder:language"
)
REDIRECT_URI = "finderyou://auth"
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; sdk_gphone64_arm64 Build/UE1A.230829.050; wv) "
    "AppleWebKit/537.36"
)


class OAuthError(Exception):
    """Raised when the OAuth flow fails."""


# Build the SSL context once in a thread executor. ssl.create_default_context()
# calls set_default_verify_paths() which loads CAs from disk — a blocking I/O
# operation that HA forbids on the event loop. Cache the result for reuse.
_ssl_ctx: ssl.SSLContext | None = None
_ssl_ctx_lock = asyncio.Lock()


async def _get_ssl_context() -> ssl.SSLContext:
    global _ssl_ctx
    async with _ssl_ctx_lock:
        if _ssl_ctx is None:
            loop = asyncio.get_running_loop()
            _ssl_ctx = await loop.run_in_executor(None, ssl.create_default_context)
        return _ssl_ctx


async def fetch_token(username: str, password: str) -> dict:
    """Mint a fresh api.v1 access_token + refresh_token via Android signin-oidc flow.

    Returns the JSON dict from /connect/token: access_token, refresh_token,
    expires_in, token_type, scope, id_token.
    """
    ssl_ctx = await _get_ssl_context()
    async with httpx.AsyncClient(
        timeout=10,
        verify=ssl_ctx,
        headers={"User-Agent": ANDROID_UA},
        follow_redirects=False,
    ) as client:
        # 1. /connect/authorize → 302 to /access/signin (carries returnUrl)
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
        }
        r = await client.get(
            f"{ACCOUNTS_BASE}/connect/authorize?" + urllib.parse.urlencode(params)
        )
        if r.status_code != 302 or "location" not in r.headers:
            raise OAuthError(f"authorize step failed: {r.status_code}")
        return_url = urllib.parse.parse_qs(
            urllib.parse.urlparse(r.headers["location"]).query
        )["returnUrl"][0]

        # 2. /_api/v1/auth/signin-oidc with creds. Cookies issued.
        r = await client.post(
            f"{ACCOUNTS_BASE}/_api/v1/auth/signin-oidc",
            json={
                "returnUrl": return_url,
                "username": username,
                "password": password,
                "impersonateUsername": None,
            },
            headers={
                "content-type": "application/json;charset=UTF-8",
                "origin": ACCOUNTS_BASE,
                "x-requested-with": "com.findernet.FinderYou",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "referer": f"{ACCOUNTS_BASE}/access/signin",
                "accept": "application/json, text/plain, */*",
            },
        )
        if r.status_code != 200 or r.json().get("result") != "OK":
            raise OAuthError(f"signin-oidc rejected: {r.status_code} {r.text[:200]}")

        # 3. Follow the OAuth callback (cookies carry the session) → 302 to
        #    finderyou://auth?code=… — we extract the code from the redirect.
        cb_url = urllib.parse.urljoin(ACCOUNTS_BASE, return_url)
        r = await client.get(cb_url, follow_redirects=False)
        if r.status_code != 302:
            raise OAuthError(f"callback didn't redirect: {r.status_code}")
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(r.headers["location"]).query
        )["code"][0]

        # 4. Exchange code for access_token. No code_verifier — Android client
        #    is configured without PKCE.
        r = await client.post(
            f"{ACCOUNTS_BASE}/connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        if r.status_code != 200:
            raise OAuthError(f"token exchange failed: {r.status_code} {r.text[:200]}")
        token = r.json()
        if "access_token" not in token:
            raise OAuthError(f"token response missing access_token: {token}")
        return token


async def refresh_token(refresh: str) -> dict:
    """Exchange a refresh_token for a new access_token."""
    async with httpx.AsyncClient(timeout=10, verify=True) as client:
        r = await client.post(
            f"{ACCOUNTS_BASE}/connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        if r.status_code != 200:
            raise OAuthError(
                f"refresh failed: {r.status_code} {r.text[:200]}"
            )
        token = r.json()
        if "access_token" not in token:
            raise OAuthError(f"refresh missing access_token: {token}")
        return token
