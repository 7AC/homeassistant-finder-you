"""Tests for the OAuth flow (mocked HTTP via respx)."""

from __future__ import annotations

import urllib.parse

import httpx
import pytest
import respx

from custom_components.finder_you.api import oauth
from custom_components.finder_you.api.oauth import (
    ACCOUNTS_BASE,
    OAuthError,
    _get_ssl_context,
    fetch_token,
    refresh_token,
)


@pytest.fixture(autouse=True)
def _reset_ssl_ctx_cache(monkeypatch):
    """Each test starts with a clean cached SSLContext."""
    monkeypatch.setattr(oauth, "_ssl_ctx", None)


def _mount_authorize_ok(mocker, return_url="/connect/authorize/callback?abc"):
    mocker.get(f"{ACCOUNTS_BASE}/connect/authorize").mock(
        return_value=httpx.Response(
            302,
            headers={
                "location": "https://x/access/signin?"
                + urllib.parse.urlencode({"returnUrl": return_url}),
            },
        )
    )


def _mount_signin_ok(mocker, next_url="/connect/authorize/callback?xyz"):
    mocker.post(f"{ACCOUNTS_BASE}/_api/v1/auth/signin-oidc").mock(
        return_value=httpx.Response(200, json={"result": "OK", "data": {"next": next_url}})
    )


def _mount_callback_ok(mocker, code="ABC123"):
    mocker.get(f"{ACCOUNTS_BASE}/connect/authorize/callback").mock(
        return_value=httpx.Response(
            302,
            headers={"location": f"finderyou://auth?{urllib.parse.urlencode({'code': code})}"},
        )
    )


def _mount_token_ok(mocker, body=None):
    mocker.post(f"{ACCOUNTS_BASE}/connect/token").mock(
        return_value=httpx.Response(
            200,
            json=body or {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600},
        )
    )


# ---- _get_ssl_context cache --------------------------------------------------


async def test_get_ssl_context_caches_result():
    first = await _get_ssl_context()
    second = await _get_ssl_context()
    assert first is second


# ---- fetch_token success path -----------------------------------------------


async def test_fetch_token_happy_path():
    with respx.mock(assert_all_called=True) as mocker:
        _mount_authorize_ok(mocker)
        _mount_signin_ok(mocker)
        _mount_callback_ok(mocker, code="C0DE")
        _mount_token_ok(mocker)
        tok = await fetch_token("u@example.com", "pw")
    assert tok["access_token"] == "tok"
    assert tok["refresh_token"] == "ref"


# ---- fetch_token error branches ---------------------------------------------


async def test_fetch_token_authorize_not_302():
    with respx.mock() as mocker:
        mocker.get(f"{ACCOUNTS_BASE}/connect/authorize").mock(return_value=httpx.Response(500))
        with pytest.raises(OAuthError, match="authorize step failed"):
            await fetch_token("u", "p")


async def test_fetch_token_signin_rejected_by_status():
    with respx.mock() as mocker:
        _mount_authorize_ok(mocker)
        mocker.post(f"{ACCOUNTS_BASE}/_api/v1/auth/signin-oidc").mock(
            return_value=httpx.Response(401, json={"result": "NO"})
        )
        with pytest.raises(OAuthError, match="signin-oidc rejected"):
            await fetch_token("u", "p")


async def test_fetch_token_signin_rejected_by_result_field():
    with respx.mock() as mocker:
        _mount_authorize_ok(mocker)
        mocker.post(f"{ACCOUNTS_BASE}/_api/v1/auth/signin-oidc").mock(
            return_value=httpx.Response(200, json={"result": "NO"})
        )
        with pytest.raises(OAuthError, match="signin-oidc rejected"):
            await fetch_token("u", "p")


async def test_fetch_token_callback_not_302():
    with respx.mock() as mocker:
        _mount_authorize_ok(mocker)
        _mount_signin_ok(mocker)
        mocker.get(f"{ACCOUNTS_BASE}/connect/authorize/callback").mock(
            return_value=httpx.Response(200, text="not a redirect")
        )
        with pytest.raises(OAuthError, match="callback didn't redirect"):
            await fetch_token("u", "p")


async def test_fetch_token_exchange_non_200():
    with respx.mock() as mocker:
        _mount_authorize_ok(mocker)
        _mount_signin_ok(mocker)
        _mount_callback_ok(mocker)
        mocker.post(f"{ACCOUNTS_BASE}/connect/token").mock(
            return_value=httpx.Response(400, json={"error": "nope"})
        )
        with pytest.raises(OAuthError, match="token exchange failed"):
            await fetch_token("u", "p")


async def test_fetch_token_missing_access_token_field():
    with respx.mock() as mocker:
        _mount_authorize_ok(mocker)
        _mount_signin_ok(mocker)
        _mount_callback_ok(mocker)
        mocker.post(f"{ACCOUNTS_BASE}/connect/token").mock(
            return_value=httpx.Response(200, json={"only": "junk"})
        )
        with pytest.raises(OAuthError, match="missing access_token"):
            await fetch_token("u", "p")


async def test_fetch_token_authorize_missing_location_header():
    with respx.mock() as mocker:
        mocker.get(f"{ACCOUNTS_BASE}/connect/authorize").mock(
            return_value=httpx.Response(302, headers={})
        )
        with pytest.raises(OAuthError, match="authorize step failed"):
            await fetch_token("u", "p")


# ---- refresh_token branches -------------------------------------------------


async def test_refresh_token_happy_path():
    with respx.mock() as mocker:
        _mount_token_ok(
            mocker, body={"access_token": "T2", "refresh_token": "R2", "expires_in": 60}
        )
        tok = await refresh_token("old")
    assert tok["access_token"] == "T2"


async def test_refresh_token_non_200():
    with respx.mock() as mocker:
        mocker.post(f"{ACCOUNTS_BASE}/connect/token").mock(
            return_value=httpx.Response(400, text="bad")
        )
        with pytest.raises(OAuthError, match="refresh failed"):
            await refresh_token("old")


async def test_refresh_token_missing_access_token_field():
    with respx.mock() as mocker:
        mocker.post(f"{ACCOUNTS_BASE}/connect/token").mock(
            return_value=httpx.Response(200, json={"nope": True})
        )
        with pytest.raises(OAuthError, match="refresh missing access_token"):
            await refresh_token("old")
