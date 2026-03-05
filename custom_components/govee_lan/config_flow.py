import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class GoveeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # If you need uniqueness enforcement, add async_set_unique_id() here.
            return self.async_create_entry(title=DOMAIN, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_API_KEY): cv.string}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return GoveeOptionsFlowHandler(config_entry)


class GoveeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Govee LAN."""

    VERSION = 1

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # HA 2026.2: OptionsFlow doesn't accept (config_entry) in __init__
        # and config_entry property may be read-only, so keep our own reference.
        self._config_entry = config_entry
        self._options: dict[str, Any] = dict(config_entry.options)

    async def async_step_init(self, user_input=None) -> FlowResult:
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None) -> FlowResult:
        current_api_key = self._config_entry.options.get(
            CONF_API_KEY, self._config_entry.data.get(CONF_API_KEY)
        )

        errors: dict[str, str] = {}

        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title=DOMAIN, data=self._options)

        options_schema = vol.Schema(
            {vol.Optional(CONF_API_KEY, default=current_api_key): cv.string}
        )

        return self.async_show_form(
            step_id="user",
            data_schema=options_schema,
            errors=errors,
        )
