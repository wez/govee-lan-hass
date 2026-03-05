import asyncio
import math
import logging
import time

from typing import Any, Dict

from homeassistant import core
from homeassistant.components import network
from homeassistant.components.light import (
    ColorMode,
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
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

# TODO: move to option flow
HTTP_POLL_INTERVAL = 600
LAN_POLL_INTERVAL = 10

SKU_NAMES = {
    "H610A": "Glide Lively",
    "H61A2": "Neon LED Strip",
    "H6072": "Lyra Floor Lamp",
    "H619A": "LED Strip",
}


class DeviceRegistry:
    def __init__(self, add_entities: AddEntitiesCallback):
        self.devices: Dict[str, GoveLightEntity] = {}
        self.add_entities = add_entities

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
            entity = GoveLightEntity(controller, device)
            self.devices[device.device_id] = entity
            entity._govee_device_updated()
            _LOGGER.info("Adding %s %s", device.device_id, entity._attr_name)
            self.add_entities([entity])


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

    registry = DeviceRegistry(add_entities)
    controller = GoveeController()
    controller.set_device_control_timeout(3)  # TODO: configurable
    controller.set_device_change_callback(
        lambda device: registry.handle_device_update(hass, entry, controller, device)
    )
    hass.data[DOMAIN]["controller"] = controller
    hass.data[DOMAIN]["registry"] = registry

    api_key = entry.options.get(CONF_API_KEY, entry.data.get(CONF_API_KEY, None))

    entry.async_on_unload(controller.stop)

    async def update_config(hass: core.HomeAssistant, entry: ConfigEntry):
        _LOGGER.info("config options were changed")
        # TODO: how to propagate?

    entry.async_on_unload(entry.add_update_listener(update_config))

    if api_key:
        controller.set_http_api_key(api_key)
        try:
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

    # Defaults (Kelvin); may be overridden by device properties.
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000

    def __init__(self, controller: GoveeController, device: GoveeDevice):
        self._attr_extra_state_attributes = {}
        self._govee_controller = controller
        self._govee_device = device
        self._last_poll = None
        # Determine supported color modes for this device.
        self._attr_supported_color_modes = self._compute_supported_color_modes(device)

        # Prefer a deterministic default mode for when the device is ON but hasn't
        # yet reported a specific mode.
        self._attr_color_mode = self._default_color_mode()

        # If the device definition reports a valid color temperature range (Kelvin),
        # override our defaults so HA shows correct min/max.
        self._apply_color_temp_range(device)


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


    @staticmethod
    def _collect_supported_commands(device: GoveeDevice) -> set[str]:
        """Collect supported command names from available device definitions."""
        cmds: set[str] = set()
        for definition in (
            getattr(device, "http_definition", None),
            getattr(device, "lan_definition", None),
        ):
            supported = getattr(definition, "supported_commands", None)
            if supported:
                cmds.update(supported)
        return cmds

    def _compute_supported_color_modes(self, device: GoveeDevice) -> set[ColorMode]:
        """Compute valid Home Assistant supported_color_modes for this device.

        Important: ColorMode.BRIGHTNESS must be the *only* supported mode if present.
        """
        cmds = self._collect_supported_commands(device)

        modes: set[ColorMode] = set()
        if "color" in cmds:
            modes.add(ColorMode.RGB)
        if "colorTem" in cmds or "colorTemp" in cmds or "color_temperature" in cmds:
            modes.add(ColorMode.COLOR_TEMP)

        if not modes:
            if "brightness" in cmds:
                modes.add(ColorMode.BRIGHTNESS)
            else:
                modes.add(ColorMode.ONOFF)

        return modes

    def _default_color_mode(self) -> ColorMode | None:
        """Pick a deterministic default color mode from supported_color_modes."""
        modes: set[ColorMode] = getattr(self, "_attr_supported_color_modes", set()) or set()
        for preferred in (
            ColorMode.RGB,
            ColorMode.COLOR_TEMP,
            ColorMode.BRIGHTNESS,
            ColorMode.ONOFF,
        ):
            if preferred in modes:
                return preferred
        return next(iter(modes), None)

    def _apply_color_temp_range(self, device: GoveeDevice) -> None:
        """Apply Kelvin min/max range if available from device properties."""
        # Only relevant if the device supports color temperature.
        if ColorMode.COLOR_TEMP not in getattr(self, "_attr_supported_color_modes", set()):
            return

        definition = getattr(device, "http_definition", None)
        props = getattr(definition, "properties", None) if definition else None
        if not isinstance(props, dict):
            return

        ct = props.get("colorTem") or props.get("colorTemp") or props.get("color_temperature")
        if not isinstance(ct, dict):
            return

        rng = ct.get("range")
        if not isinstance(rng, dict):
            return

        mn = rng.get("min")
        mx = rng.get("max")
        if isinstance(mn, (int, float)):
            self._attr_min_color_temp_kelvin = int(mn)
        if isinstance(mx, (int, float)):
            self._attr_max_color_temp_kelvin = int(mx)

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
            # Color/temperature: devices are typically in *either* RGB or COLOR_TEMP mode.
            if getattr(state, "color", None) is not None and ColorMode.RGB in self._attr_supported_color_modes:
                self._attr_rgb_color = state.color.as_tuple()
                self._attr_color_temp_kelvin = None
                self._attr_color_mode = ColorMode.RGB
            else:
                ct = getattr(state, "color_temperature", None)
                if (
                    ct is not None
                    and ct > 0
                    and ColorMode.COLOR_TEMP in self._attr_supported_color_modes
                ):
                    ct = max(
                        min(int(ct), self._attr_max_color_temp_kelvin),
                        self._attr_min_color_temp_kelvin,
                    )
                    self._attr_color_temp_kelvin = ct
                    self._attr_rgb_color = None
                    self._attr_color_mode = ColorMode.COLOR_TEMP

            # Brightness
            bri_pct = getattr(state, "brightness_pct", None)
            if bri_pct is not None:
                self._attr_brightness = max(min(int(255 * bri_pct / 100), 255), 0)

            # Power
            self._attr_is_on = bool(getattr(state, "turned_on", False))

            # HA 2026.3+ requires a color_mode when ON.
            if self._attr_is_on and self._attr_color_mode is None:
                self._attr_color_mode = self._default_color_mode()
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
            # If we execute any state-changing command other than plain power-on, we do not
            # send an additional explicit power-on (to avoid tripping rate limits).
            turn_on_only = True

            if ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs.pop(ATTR_RGB_COLOR)
                if ColorMode.RGB in self._attr_supported_color_modes:
                    await self._govee_controller.set_color(
                        self._govee_device, GoveeColor(red=r, green=g, blue=b)
                    )
                    self._attr_rgb_color = (r, g, b)
                    self._attr_color_temp_kelvin = None
                    self._attr_color_mode = ColorMode.RGB
                    turn_on_only = False
                else:
                    _LOGGER.debug(
                        "Ignoring rgb_color for %s (not supported)",
                        self._govee_device.device_id,
                    )

            if ATTR_BRIGHTNESS_PCT in kwargs:
                bri_pct = max(min(int(kwargs.pop(ATTR_BRIGHTNESS_PCT)), 100), 0)
                await self._govee_controller.set_brightness(self._govee_device, bri_pct)
                self._attr_brightness = max(min(int(255 * bri_pct / 100), 255), 0)
                turn_on_only = False
            elif ATTR_BRIGHTNESS in kwargs:
                bri = max(min(int(kwargs.pop(ATTR_BRIGHTNESS)), 255), 0)
                bri_pct = max(min(int(bri * 100 / 255), 100), 0)
                await self._govee_controller.set_brightness(self._govee_device, bri_pct)
                self._attr_brightness = bri
                turn_on_only = False

            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                ct = int(kwargs.pop(ATTR_COLOR_TEMP_KELVIN))
                if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
                    ct = max(
                        min(ct, self._attr_max_color_temp_kelvin),
                        self._attr_min_color_temp_kelvin,
                    )
                    await self._govee_controller.set_color_temperature(
                        self._govee_device, ct
                    )
                    self._attr_color_temp_kelvin = ct
                    self._attr_rgb_color = None
                    self._attr_color_mode = ColorMode.COLOR_TEMP
                    turn_on_only = False
                else:
                    _LOGGER.debug(
                        "Ignoring color_temp_kelvin for %s (not supported)",
                        self._govee_device.device_id,
                    )

            if turn_on_only:
                await self._govee_controller.set_power_state(self._govee_device, True)

            # Assume ON after a successful service call.
            self._attr_is_on = True

            # HA 2026.3+ requires a color_mode when ON.
            if self._attr_color_mode is None:
                self._attr_color_mode = self._default_color_mode()

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