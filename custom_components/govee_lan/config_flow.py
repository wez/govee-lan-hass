import logging
from typing import Any
from .const import DOMAIN
from homeassistant.core import callback
import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.data_entry_flow import FlowResult
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)

_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class GoveeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        _LOGGER.error("bluetooth disco %r", discovery_info)
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        assert self._discovered_device is not None
        assert self._discovery_info is not None
        discovery_info = self._discovery_info

        title = discovery_info.name
        if user_input is not None:
            return self.async_create_entry(title=title, data={})

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_create_entry(title=DOMAIN, data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GoveeOptionsFlowHandler(config_entry)


class GoveeOptionsFlowHandler(config_entries.OptionsFlow):
    VERSION = 1

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        options_schema = vol.Schema({})

        return self.async_show_form(step_id="user", data_schema=options_schema)

    async def _update_options(self):
        return self.async_create_entry(title=DOMAIN, data=self.options)
