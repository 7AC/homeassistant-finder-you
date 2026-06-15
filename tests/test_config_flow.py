"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.finder_you.api import OAuthError
from custom_components.finder_you.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)

USER_INPUT = {CONF_EMAIL: "u@example.com", CONF_PASSWORD: "pw"}


async def test_form_is_shown_first(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_invalid_auth_shows_error(hass):
    with patch(
        "custom_components.finder_you.config_flow.fetch_token",
        new=AsyncMock(side_effect=OAuthError("bad creds")),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unknown_error_shows_unknown(hass):
    with patch(
        "custom_components.finder_you.config_flow.fetch_token",
        new=AsyncMock(side_effect=RuntimeError("kaboom")),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown"}


async def test_success_creates_entry(hass):
    fake_token = {"access_token": "T", "refresh_token": "R", "expires_in": 3600}
    with (
        patch(
            "custom_components.finder_you.config_flow.fetch_token",
            new=AsyncMock(return_value=fake_token),
        ),
        patch(
            "custom_components.finder_you.FinderYouCoordinator",
            autospec=True,
        ) as cls,
    ):
        cls.return_value.async_config_entry_first_refresh = AsyncMock()
        cls.return_value.shutters = []
        cls.return_value.data = {}
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] == "create_entry"
    assert result["title"].startswith("Finder YOU")
    assert result["data"] == USER_INPUT


async def test_duplicate_email_aborts(hass):
    """If the same email is already set up, the flow should abort instead of
    spawning a second entry."""
    fake_token = {"access_token": "T"}
    # First setup creates the entry.
    with (
        patch(
            "custom_components.finder_you.config_flow.fetch_token",
            new=AsyncMock(return_value=fake_token),
        ),
        patch(
            "custom_components.finder_you.FinderYouCoordinator",
            autospec=True,
        ) as cls,
    ):
        cls.return_value.async_config_entry_first_refresh = AsyncMock()
        cls.return_value.shutters = []
        cls.return_value.data = {}
        first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        await hass.config_entries.flow.async_configure(first["flow_id"], USER_INPUT)

        second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        second = await hass.config_entries.flow.async_configure(second["flow_id"], USER_INPUT)
    assert second["type"] == "abort"
