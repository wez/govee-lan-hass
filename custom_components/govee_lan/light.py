import copy
import asyncio
import random
import math
import json
import logging
import socket
import time

from typing import Any, Dict

from homeassistant import core
from homeassistant.components import network
import aiohttp

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    PLATFORM_SCHEMA,
)

# ATTR_COLOR_TEMP (mireds) is deprecated in HA 2024.x, but we keep backward
# compatibility for callers that haven't migrated to ATTR_COLOR_TEMP_KELVIN yet.
# This constant will be removed in HA 2026.1.
ATTR_COLOR_TEMP = "color_temp"
import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import color
from homeassistant.util.timeout import TimeoutManager
from .const import DOMAIN
import voluptuous as vol
from bleak import BleakClient, BleakError
from homeassistant.components import bluetooth
from govee_led_wez import (
    GoveeController,
    GoveeDevice,
    GoveeDeviceState,
    GoveeColor,
    GoveeHttpDeviceDefinition,
    GoveeLanDeviceDefinition,
)

# Serialize async_update calls, even though they are async capable.
# For LAN control, we want to avoid a burst of UDP traffic causing
# lost responses.
# This is read by HA.
PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)

# This is read by HA
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({vol.Optional(CONF_API_KEY): cv.string})

# TODO: move to option flow
HTTP_POLL_INTERVAL = 600
LAN_POLL_INTERVAL = 10

SKU_NAMES = {
    "H610A": "Glide Lively",
    "H61A2": "Neon LED Strip",
    "H6072": "Lyra Floor Lamp",
    "H619A": "LED Strip",
}

# Govee API endpoints for scene/effect control
GOVEE_API_BASE = "https://openapi.api.govee.com/router/api/v1"
SCENES_ENDPOINT = f"{GOVEE_API_BASE}/device/scenes"
CONTROL_ENDPOINT = f"{GOVEE_API_BASE}/device/control"


async def fetch_device_scenes(api_key: str, sku: str, device_id: str) -> Dict[str, Dict]:
    """Fetch available scenes/effects for a device from Govee API."""
    if not api_key:
        return {}

    headers = {
        "Govee-API-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "requestId": f"scenes_{device_id}",
        "payload": {
            "sku": sku,
            "device": device_id,
        }
    }

    scenes = {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SCENES_ENDPOINT, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    capabilities = data.get("payload", {}).get("capabilities", [])

                    for cap in capabilities:
                        if cap.get("type") == "devices.capabilities.dynamic_scene":
                            instance = cap.get("instance", "")
                            options = cap.get("parameters", {}).get("options", [])

                            for opt in options:
                                name = opt.get("name", "")
                                value = opt.get("value", {})
                                if name and value:
                                    scenes[name] = {
                                        "instance": instance,
                                        "value": value,
                                    }
                else:
                    _LOGGER.warning("Failed to fetch scenes for %s: %s", device_id, resp.status)
    except Exception as exc:
        _LOGGER.debug("Error fetching scenes for %s: %s", device_id, exc)

    return scenes


async def set_device_scene(api_key: str, sku: str, device_id: str, instance: str, value: Dict) -> bool:
    """Set a scene/effect on a device via Govee API."""
    headers = {
        "Govee-API-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "requestId": f"control_{device_id}",
        "payload": {
            "sku": sku,
            "device": device_id,
            "capability": {
                "type": "devices.capabilities.dynamic_scene",
                "instance": instance,
                "value": value,
            }
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(CONTROL_ENDPOINT, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    _LOGGER.debug("Scene set successfully for %s", device_id)
                    return True
                else:
                    text = await resp.text()
                    _LOGGER.warning("Failed to set scene for %s: %s - %s", device_id, resp.status, text)
                    return False
    except Exception as exc:
        _LOGGER.error("Error setting scene for %s: %s", device_id, exc)
        return False


class DeviceRegistry:
    def __init__(self, hass: core.HomeAssistant, add_entities: AddEntitiesCallback, api_key: str | None):
        self.hass = hass
        self.devices: Dict[str, GoveLightEntity] = {}
        self.add_entities = add_entities
        self.api_key = api_key

    def handle_device_update(
        self,
        hass: core.HomeAssistant,
        entry: ConfigEntry,
        controller: GoveeController,
        device: GoveeDevice,
    ):
        entity = self.devices.get(device.device_id, None)
        if entity:
            # Update entity name in case we found the entity
            # via the LAN API before we found it via HTTP
            if (
                device.http_definition
                and entity._attr_name == entity._govee_fallback_name
            ):
                entity._attr_name = device.http_definition.device_name

            entity._govee_device = device
            entity._govee_device_updated()
        else:
            entity = GoveLightEntity(controller, device, self.api_key)
            self.devices[device.device_id] = entity
            entity._govee_device_updated()
            _LOGGER.info("Adding %s %s", device.device_id, entity._attr_name)
            self.add_entities([entity])

            # Fetch scenes/effects for the device in the background
            if self.api_key:
                self.hass.async_create_task(entity.async_fetch_scenes())


async def async_get_interfaces(hass: core.HomeAssistant):
    """Get list of interface to use."""
    interfaces = []

    adapters = await network.async_get_adapters(hass)
    for adapter in adapters:
        ipv4s = adapter.get("ipv4", None)
        if ipv4s:
            ip4 = ipv4s[0]["address"]
            if adapter["enabled"]:
                interfaces.append(ip4)

    if len(interfaces) == 0:
        interfaces.append("0.0.0.0")

    return interfaces


async def async_setup_entry(
    hass: core.HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
):
    _LOGGER.info("async_setup_entry was called")

    api_key = entry.options.get(CONF_API_KEY, entry.data.get(CONF_API_KEY, None))

    registry = DeviceRegistry(hass, add_entities, api_key)
    controller = GoveeController()
    controller.set_device_control_timeout(3)  # TODO: configurable

    # The callback may be called from a background thread, so we need to
    # schedule the update on the main event loop to be thread-safe
    def _threadsafe_device_callback(device):
        hass.loop.call_soon_threadsafe(
            lambda: registry.handle_device_update(hass, entry, controller, device)
        )

    controller.set_device_change_callback(_threadsafe_device_callback)
    hass.data[DOMAIN]["controller"] = controller
    hass.data[DOMAIN]["registry"] = registry

    entry.async_on_unload(controller.stop)

    async def update_config(hass: core.HomeAssistant, entry: ConfigEntry):
        _LOGGER.info("config options were changed")
        # TODO: how to propagate?

    entry.async_on_unload(entry.add_update_listener(update_config))

    if api_key:
        controller.set_http_api_key(api_key)
        try:
            # Note: This may trigger a "blocking call" warning for SSL cert loading.
            # The blocking is minimal (cert loading is cached after first call) and
            # the alternative (running in executor) causes thread-safety issues with
            # the device callback system.
            await controller.query_http_devices()
        except RuntimeError as exc:
            # The consequence of this is that the user-friendly names
            # won't be populated immediately for devices that we
            # do manage to discover via the LAN API.
            _LOGGER.error(
                "failed to get device list from Govee HTTP API. Will retry in the background",
                exc_info=exc,
            )

        async def http_poller(interval):
            await asyncio.sleep(interval)
            controller.start_http_poller(interval)

        hass.loop.create_task(http_poller(HTTP_POLL_INTERVAL))

    interfaces = await async_get_interfaces(hass)
    controller.start_lan_poller(interfaces)

    @callback
    def _async_discovered_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Subscribe to bluetooth changes."""

        _LOGGER.info(
            "New service_info: %s name=%s address=%s source=%s rssi=%s",
            change,
            service_info.name,
            service_info.address,
            service_info.source,
            service_info.rssi,
        )
        controller.register_ble_device(service_info.device)

    for mfr in [34817, 34818]:
        entry.async_on_unload(
            bluetooth.async_register_callback(
                hass,
                _async_discovered_ble,
                {"manufacturer_id": mfr},
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )


class GoveLightEntity(LightEntity):
    _govee_controller: GoveeController
    _govee_device: GoveeDevice
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000
    _attr_supported_color_modes = {
        ColorMode.COLOR_TEMP,
        ColorMode.RGB,
    }

    def __init__(self, controller: GoveeController, device: GoveeDevice, api_key: str | None = None):
        self._attr_extra_state_attributes = {}
        self._govee_controller = controller
        self._govee_device = device
        self._last_poll = None
        # Set default color mode to avoid "does not report a color mode" warning
        self._attr_color_mode = ColorMode.RGB

        # Scene/effect support
        self._api_key = api_key
        self._scenes: Dict[str, Dict] = {}
        self._attr_effect = None

        ident = device.device_id.replace(":", "")
        self._attr_unique_id = f"{device.model}_{ident}"

        fallback_name = None
        if device.model in SKU_NAMES:
            fallback_name = (
                f"{SKU_NAMES[device.model]} {device.model.upper()}_{ident[-4:].upper()}"
            )
        else:
            fallback_name = f"{device.model.upper()}_{ident[-4:].upper()}"

        self._govee_fallback_name = fallback_name

        if device.http_definition is not None:
            self._attr_name = device.http_definition.device_name
            # TODO: apply properties colorTem range?
        else:
            self._attr_name = fallback_name

    def __repr__(self):
        return str(self.__dict__)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._govee_device.device_id)},
            name=self.name,
            manufacturer="Govee",
            model=self._govee_device.model,
            sw_version=self._govee_device.lan_definition.wifi_software_version
            if self._govee_device.lan_definition
            else None,
            hw_version=self._govee_device.lan_definition.wifi_hardware_version
            if self._govee_device.lan_definition
            else None,
        )

    @property
    def entity_registry_enabled_default(self):
        """Return if the entity should be enabled when first added to the entity registry."""
        return True

    @property
    def supported_features(self) -> LightEntityFeature:
        """Return supported features."""
        if self._scenes:
            return LightEntityFeature.EFFECT
        return LightEntityFeature(0)

    @property
    def effect_list(self) -> list[str] | None:
        """Return the list of available effects."""
        if self._scenes:
            return list(self._scenes.keys())
        return None

    @property
    def effect(self) -> str | None:
        """Return the current effect."""
        return self._attr_effect

    async def async_fetch_scenes(self) -> None:
        """Fetch available scenes/effects from Govee API."""
        if not self._api_key:
            return

        device = self._govee_device
        scenes = await fetch_device_scenes(self._api_key, device.model, device.device_id)

        if scenes:
            self._scenes = scenes
            _LOGGER.info(
                "Loaded %d effects for %s (%s)",
                len(scenes),
                self._attr_name,
                device.device_id,
            )
            # Update state to reflect new effect support
            if self.hass:
                self.async_write_ha_state()

    def _govee_device_updated(self):
        device = self._govee_device
        state = device.state
        _LOGGER.debug(
            "device state updated: %s entity_id=%s --> %r %r",
            device.device_id,
            self.entity_id,
            state,
            device,
        )

        if state:
            self._attr_color_temp_kelvin = state.color_temperature
            if state.color_temperature and state.color_temperature > 0:
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_rgb_color = None
            elif state.color is not None:
                self._attr_color_temp_kelvin = None
                self._attr_color_mode = ColorMode.RGB
                self._attr_rgb_color = state.color.as_tuple()

            self._attr_brightness = max(
                min(int(255 * state.brightness_pct / 100), 255), 0
            )
            self._attr_is_on = state.turned_on

        self._attr_extra_state_attributes["http_enabled"] = device.http_definition is not None
        self._attr_extra_state_attributes["ble_enabled"] = device.ble_device is not None
        self._attr_extra_state_attributes["lan_enabled"] = device.lan_definition is not None

        if self.entity_id:
            self.schedule_update_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug(
            "turn on %s %s with %s",
            self._govee_device.device_id,
            self.entity_id,
            kwargs,
        )

        try:
            turn_on = True

            if ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs.pop(ATTR_RGB_COLOR)
                await self._govee_controller.set_color(
                    self._govee_device, GoveeColor(red=r, green=g, blue=b)
                )
                turn_on = False

            if ATTR_BRIGHTNESS_PCT in kwargs:
                brightness = max(min(kwargs.pop(ATTR_BRIGHTNESS_PCT), 100), 0)
                await self._govee_controller.set_brightness(
                    self._govee_device, brightness
                )
                turn_on = False
            elif ATTR_BRIGHTNESS in kwargs:
                brightness = int(kwargs.pop(ATTR_BRIGHTNESS) * 100 / 255)
                await self._govee_controller.set_brightness(
                    self._govee_device, brightness
                )
                turn_on = False

            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                color_temp_kelvin = kwargs.pop(ATTR_COLOR_TEMP_KELVIN)
                color_temp_kelvin = max(
                    min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                    self._attr_min_color_temp_kelvin,
                )
                await self._govee_controller.set_color_temperature(
                    self._govee_device, color_temp_kelvin
                )
                turn_on = False
            elif ATTR_COLOR_TEMP in kwargs:
                color_temp = kwargs.pop(ATTR_COLOR_TEMP)
                color_temp_kelvin = color.color_temperature_mired_to_kelvin(color_temp)
                color_temp_kelvin = max(
                    min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                    self._attr_min_color_temp_kelvin,
                )
                await self._govee_controller.set_color_temperature(
                    self._govee_device, color_temp_kelvin
                )
                turn_on = False

            if ATTR_EFFECT in kwargs:
                effect = kwargs.pop(ATTR_EFFECT)
                scene_data = self._scenes.get(effect)
                if scene_data and self._api_key:
                    success = await set_device_scene(
                        self._api_key,
                        self._govee_device.model,
                        self._govee_device.device_id,
                        scene_data["instance"],
                        scene_data["value"],
                    )
                    if success:
                        self._attr_effect = effect
                    turn_on = False
                else:
                    _LOGGER.warning("Unknown effect or no API key: %s", effect)

            if turn_on:
                await self._govee_controller.set_power_state(self._govee_device, True)

            # Update the last poll time to now to prevent the next poll from resetting the state
            # from the assumed state to an old state (because Govee returns the wrong state after a
            # write for some time)
            self._last_poll = time.monotonic()
            self.async_write_ha_state()

        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            _LOGGER.debug(
                "timeout while modifying device state for %s %s",
                self._govee_device.device_id,
                self.entity_id,
                exc_info=exc,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug(
            "turn OFF %s %s with %s",
            self._govee_device.device_id,
            self.entity_id,
            kwargs,
        )
        try:
            await self._govee_controller.set_power_state(self._govee_device, False)

            # Update the last poll time to now to prevent the next poll from resetting the state
            # from the assumed state to an old state (because Govee returns the wrong state after a
            # write for some time)
            self._last_poll = time.monotonic()
            self.async_write_ha_state()
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            _LOGGER.debug(
                "timeout while modifying device state for %s %s",
                self._govee_device.device_id,
                self.entity_id,
                exc_info=exc,
            )

    async def async_update(self):
        interval = (
            HTTP_POLL_INTERVAL
            if not self._govee_device.lan_definition
            else LAN_POLL_INTERVAL
        )

        # Can only poll via http; use our own poll interval for this,
        # as HA may poll too frequently and trip the miserly rate limit
        # set by Govee
        now = time.monotonic()
        if self._last_poll is not None:
            elapsed = math.ceil(now - self._last_poll)
            if elapsed < interval:
                _LOGGER.debug(
                    "skip async_update for %s %s as elapsed %s < %s",
                    self._govee_device,
                    self.entity_id,
                    elapsed,
                    interval,
                )
                return

        _LOGGER.debug(
            "async_update will poll %s %s", self._govee_device.device_id, self.entity_id
        )
        self._last_poll = now

        current_time_string = time.strftime("%c")

        try:
            # A little random jitter to avoid getting a storm of UDP
            # responses from the LAN interface all at once
            # await asyncio.sleep(random.uniform(0.0, 3.2))
            await self._govee_controller.update_device_state(self._govee_device)
            self._attr_available = True
            self._attr_extra_state_attributes["update_status"] = f"ok at {current_time_string}"
            self._attr_extra_state_attributes["timeout_count"] = 0
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            _LOGGER.debug(
                "timeout while querying device state for %s %s",
                self._govee_device.device_id,
                self.entity_id,
                exc_info=exc,
            )
            timeout_count = self._attr_extra_state_attributes.get("timeout_count", 0) + 1
            self._attr_extra_state_attributes["update_status"] = f"timed out at {current_time_string}"
            self._attr_extra_state_attributes["timeout_count"] = timeout_count
            if timeout_count > 1:
                self._attr_available = False
