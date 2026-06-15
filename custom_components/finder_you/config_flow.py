"""Config flow for Finder YOU: email + password."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import OAuthError, fetch_token
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class FinderYouConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            try:
                await fetch_token(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            except OAuthError as err:
                _LOGGER.warning("auth failed: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("unexpected auth error")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Finder YOU ({user_input[CONF_EMAIL]})",
                    data=user_input,
                )
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)
