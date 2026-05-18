import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_API_KEY
import logging
from typing import Any
from .const import DOMAIN
from homeassistant.core import callback
import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class GoveeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}

        if user_input is not None:
            return self.async_create_entry(title=DOMAIN, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_API_KEY): cv.string}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GoveeOptionsFlowHandler(config_entry)


class GoveeOptionsFlowHandler(config_entries.OptionsFlow):
    VERSION = 1

    def __init__(self, config_entry):
        # OptionsFlow.config_entry became a framework-managed property in HA
        # 2024.12 (setter removed shortly after). Don't reassign it — store
        # the entry under a private name and use that everywhere instead.
        self._entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        current_api_key = self._entry.options.get(
            CONF_API_KEY, self._entry.data.get(CONF_API_KEY, None)
        )

        errors = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY]
            self.options.update(user_input)
            return await self._update_options()

        options_schema = vol.Schema(
            {vol.Optional(CONF_API_KEY, default=current_api_key): cv.string}
        )

        return self.async_show_form(
            step_id="user", data_schema=options_schema, errors=errors
        )

    async def _update_options(self):
        return self.async_create_entry(title=DOMAIN, data=self.options)
