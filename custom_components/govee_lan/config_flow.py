from .const import DOMAIN
from homeassistant.core import callback
import voluptuous as vol
from homeassistant import config_entries, core, exceptions


@config_entries.HANDLERS.register(DOMAIN)
class GoveeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        return self.async_create_entry(title=DOMAIN, data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GoveeOptionsFlowHandler(config_entry)


class GoveeOptionsFlowHandler(config_entries.OptionsFlow):
    VERSION = 1

    async def async_step_init(self, user_input=None):
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        options_schema = vol.Schema({})

        return self.async_show_form(
            step_id="user", data_schema=options_schema, errors=errors
        )

    async def _update_options(self):
        return self.async_create_entry(title=DOMAIN, data=self.options)
