from bleak import BleakClient, BleakError
import asyncio
import time
import aiohttp
import ssl
import certifi
from homeassistant.const import CONF_API_KEY
from homeassistant.util import color
from homeassistant.const import Platform
import logging
import socket
from homeassistant.util.timeout import TimeoutManager
import json
from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant import core
from homeassistant.components import network
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.light import (
    ColorMode,
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    SUPPORT_BRIGHTNESS,
    SUPPORT_COLOR,
    SUPPORT_COLOR_TEMP,
    LightEntity,
    PLATFORM_SCHEMA,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

BROADCAST_PORT = 4001
COMMAND_PORT = 4003
LISTEN_PORT = 4002
BROADCAST_ADDR = "239.255.255.250"

_LOGGER = logging.getLogger(__name__)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({})

SKU_NAMES = {
    "H610A": "Glide Lively",
    "H61A2": "Neon LED Strip",
    "H6072": "Lyra Floor Lamp",
    "H619A": "LED Strip",
}


class DeviceRegistry:
    def __init__(self):
        self.devices = {}


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


async def discover_api_devices(
    hass: core.HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
    registry: DeviceRegistry,
):
    api_key = entry.options.get(CONF_API_KEY, entry.data.get(CONF_API_KEY, None))
    if not api_key:
        return

    client = GoveeApiClient(api_key)
    devices = await client.get_devices()
    if not devices:
        return

    for dev in devices:
        device = GoveeDevice(hass, dev["device"], dev["model"], None)
        device._attr_name = dev["deviceName"]
        device._client = client

        existing = registry.devices.get(device.device_id, None)
        if not existing:
            _LOGGER.debug("HTTP API Found device %r", device)
            registry.devices[device.device_id] = device
            add_entities([device], update_before_add=False)


async def async_setup_entry(
    hass: core.HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
):
    _LOGGER.info("async_setup_entry was called")
    registry = DeviceRegistry()

    async def update_config(hass: core.HomeAssistant, entry: ConfigEntry):
        _LOGGER.info("config options were changed")
        await discover_api_devices(hass, entry, add_entities, registry)

    entry.async_on_unload(entry.add_update_listener(update_config))

    await discover_api_devices(hass, entry, add_entities, registry)
    await discover_devices(hass, add_entities, entry, registry)


async def discover_devices(
    hass: core.HomeAssistant,
    add_entities: AddEntitiesCallback,
    entry: ConfigEntry,
    registry: DeviceRegistry,
):
    interfaces = await async_get_interfaces(hass)
    _LOGGER.info("setup found interfaces: %r", interfaces)
    for interface in interfaces:
        hass.async_create_task(
            discover_devices_on_interface(
                interface, hass, add_entities, entry, registry
            )
        )


class GoveeDevStatus:
    def __init__(
        self, turned_on: bool, brightness_pct: int, color, color_temp_kelvin: int
    ):
        self.turned_on = turned_on
        self.brightness_pct = brightness_pct
        self.color = color
        self.color_temp_kelvin = color_temp_kelvin

    def __repr__(self):
        return str(self.__dict__)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if other is None:
            return False
        return self.__dict__ == other.__dict__


class GoveeDevice(LightEntity):
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000
    _attr_supported_color_modes = {
        ColorMode.BRIGHTNESS,
        ColorMode.COLOR_TEMP,
        ColorMode.RGB,
    }

    def __init__(self, hass: core.HomeAssistant, device_id, sku, addr):
        self.hass = hass
        self.device_id = device_id
        self.sku = sku
        self.addr = addr
        self.status = None
        self._govee_current_request = None
        self._last_http_poll = None

        ident = self.device_id.replace(":", "")
        self._attr_unique_id = f"{sku}_{ident}"
        if sku in SKU_NAMES:
            self._attr_name = f"{SKU_NAMES[sku]} {sku.upper()}_{ident[-4:].upper()}"
        else:
            self._attr_name = f"{sku.upper()}_{ident[-4:].upper()}"

    def __repr__(self):
        return str(self.__dict__)

    @property
    def entity_registry_enabled_default(self):
        """Return if the entity should be enabled when first added to the entity registry."""
        return True

    async def _async_send_govee_request_http(self, cmd, value, assumed_status):
        if cmd == "devStatus":
            now = time.monotonic()
            if self._last_http_poll is not None:
                if now - self._last_http_poll < 600:
                    # Skip this poll
                    _LOGGER.debug(
                        "skipping poll because only %s have elapsed",
                        now - self._last_http_poll,
                    )
                    return

            self._last_http_poll = now
            props = await self._client.get_state(self.device_id, self.sku)
            _LOGGER.debug("props is %r", props)
            if not props:
                return

            turned_on = False
            brightness_pct = 100
            color = None
            color_temp_kelvin = 0

            for prop in props:
                if "powerState" in prop:
                    turned_on = prop["powerState"] == "on"
                if "brightness" in prop:
                    brightness_pct = prop["brightness"]
                if "color" in prop:
                    color = prop["color"]
                if "colorTem" in prop:
                    color_temp_kelvin = prop["colorTem"]

            status = GoveeDevStatus(turned_on, brightness_pct, color, color_temp_kelvin)
            self.set_status(status)
            return

        put = None

        if cmd == "turn":
            put = {"name": "turn", "value": "on" if value["value"] else "off"}
        elif cmd == "brightness":
            put = {"name": "brightness", "value": value["value"]}
        elif cmd == "colorwc" and "color" in value:
            put = {"name": "color", "value": value["color"]}
        elif cmd == "colorwc":
            put = {"name": "colorTem", "value": value["colorTemInKelvin"]}
        else:
            _LOGGER.error("not sure how to translate %s %s to http api", cmd, value)
            return

        resp = await self._client.control(
            {"device": self.device_id, "model": self.sku, "cmd": put}
        )

        if assumed_status:
            self.set_status(assumed_status)

    async def async_send_govee_request(self, cmd, value, assumed_status=None):
        if not self.addr:
            return await self._async_send_govee_request_http(cmd, value, assumed_status)

        data = bytes(json.dumps({"msg": {"cmd": cmd, "data": value}}), "utf-8")
        dev_status = bytes(
            json.dumps({"msg": {"cmd": "devStatus", "data": {}}}), "utf-8"
        )

        _LOGGER.debug("will send %s to %s", data, self.addr)

        if self._govee_current_request:
            _LOGGER.error(
                "ignoring %s -> %s because we have a pending request", data, self.addr
            )
            return

        self._govee_current_request = asyncio.get_running_loop().create_future()
        try:
            self._govee_send_item(data)

            for attempt in range(0, 3):
                if data != dev_status or attempt > 0:
                    self._govee_send_item(dev_status)

                timeout = TimeoutManager()
                try:
                    async with timeout.async_timeout(3):
                        await self._govee_current_request
                        _LOGGER.debug("OK %s -> %s", data, self.addr)
                        return
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    _LOGGER.error("TIMEOUT %s -> %s", data, self.addr)
                    self._govee_current_request = (
                        asyncio.get_running_loop().create_future()
                    )

        finally:
            self._govee_current_request = None

    def _govee_send_item(self, data):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _LOGGER.debug("sending %s to %s %s", data, self.device_id, self.addr)
        loop = asyncio.get_event_loop()
        s.sendto(data, (self.addr, COMMAND_PORT))

    def set_status(self, status: GoveeDevStatus):
        if self.status != status:
            _LOGGER.debug(
                "device status updated: %s from %r --> status %r",
                self.device_id,
                self.status,
                status,
            )
            self.status = status

            self._attr_color_temp_kelvin = status.color_temp_kelvin
            if status.color_temp_kelvin and status.color_temp_kelvin > 0:
                self._attr_color_temp = color.color_temperature_kelvin_to_mired(
                    status.color_temp_kelvin
                )
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_rgb_color = None
            else:
                self._attr_color_temp_kelvin = None
                self._attr_color_temp = None
                self._attr_color_mode = ColorMode.RGB
                self._attr_rgb_color = (
                    status.color["r"],
                    status.color["g"],
                    status.color["b"],
                )

            self._attr_brightness = max(
                min(int(255 * status.brightness_pct / 100), 255), 0
            )
            self._attr_is_on = status.turned_on

        if self.entity_id:
            self.schedule_update_ha_state()

        if self._govee_current_request:
            self._govee_current_request.set_result(None)

    def _current_dev_state(self) -> GoveeDevStatus:
        rgb = self._attr_rgb_color or [255, 255, 255]
        return GoveeDevStatus(
            self._attr_is_on or False,
            (self._attr_brightness or 0) * 100 / 255,
            {"r": rgb[0], "g": rgb[1], "b": rgb[2]},
            self._attr_color_temp_kelvin or 0,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("turn on %s with %s", self.device_id, kwargs)
        turn_on = True

        orig_status = self.status
        status = self._current_dev_state()
        status.turned_on = True

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs.pop(ATTR_RGB_COLOR)
            rgb = {"r": r, "g": g, "b": b}
            status.color = rgb
            await self.async_send_govee_request(
                "colorwc", {"color": rgb, "colorTemInKelvin": 0}, assumed_status=status
            )
            turn_on = False

        if ATTR_BRIGHTNESS_PCT in kwargs:
            brightness = max(min(kwargs.pop(ATTR_BRIGHTNESS_PCT), 100), 0)
            status.brightness_pct = brightness
            await self.async_send_govee_request(
                "brightness", {"value": brightness}, assumed_status=status
            )
            turn_on = False
        elif ATTR_BRIGHTNESS in kwargs:
            brightness = int(kwargs.pop(ATTR_BRIGHTNESS) * 100 / 255)
            status.brightness_pct = brightness
            await self.async_send_govee_request(
                "brightness", {"value": brightness}, assumed_status=status
            )
            turn_on = False

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp_kelvin = kwargs.pop(ATTR_COLOR_TEMP_KELVIN)
            color_temp_kelvin = max(
                min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                self._attr_min_color_temp_kelvin,
            )
            status.color_temp_kelvin = color_temp_kelvin
            await self.async_send_govee_request(
                "colorwc",
                {"colorTemInKelvin": color_temp_kelvin},
                assumed_status=status,
            )
            turn_on = False
        elif ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs.pop(ATTR_COLOR_TEMP)
            color_temp_kelvin = color.color_temperature_mired_to_kelvin(color_temp)
            color_temp_kelvin = max(
                min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                self._attr_min_color_temp_kelvin,
            )
            status.color_temp_kelvin = color_temp_kelvin
            await self.async_send_govee_request(
                "colorwc",
                {"colorTemInKelvin": color_temp_kelvin},
                assumed_status=status,
            )
            turn_on = False

        if turn_on:
            await self.async_send_govee_request(
                "turn", {"value": 1}, assumed_status=status
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("turn OFF %s with %s", self.device_id, kwargs)
        status = self._current_dev_state()
        status.turned_on = False
        await self.async_send_govee_request("turn", {"value": 0}, assumed_status=status)

    async def async_update(self):
        _LOGGER.info("async_update was called for %s", self.device_id)
        await self.async_send_govee_request("devStatus", {})


class GoveeDiscoProtocol:
    def __init__(
        self,
        hass: core.HomeAssistant,
        add_entities: AddEntitiesCallback,
        registry: DeviceRegistry,
    ):
        self.add_entities = add_entities
        self.hass = hass
        self.registry = registry

    def connection_lost(self, exc):
        pass

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        message = data.decode()
        msg = json.loads(message)["msg"]
        _LOGGER.debug("decoded: %r from %s", msg, addr)
        source_ip = addr[0]
        data = msg["data"]
        if msg["cmd"] == "scan":
            device = GoveeDevice(self.hass, data["device"], data["sku"], data["ip"])
            existing = self.registry.devices.get(device.device_id, None)
            if existing:
                changed = existing.addr != device.addr
                if changed:
                    existing.addr = device.addr
                    _LOGGER.debug("Updated device %r", device)
                    existing.schedule_update_ha_state(force_refresh=True)
            else:
                _LOGGER.debug("LAN Found device %r", device)
                self.registry.devices[device.device_id] = device
                self.add_entities([device], update_before_add=True)

            return

        if msg["cmd"] == "devStatus":
            status = GoveeDevStatus(
                True if data["onOff"] == 1 else False,
                data["brightness"],
                data["color"],
                data["colorTemInKelvin"],
            )
            for device in self.registry.devices.values():
                if device.addr == source_ip:
                    device.set_status(status)
                    return

            _LOGGER.warning(
                "datagram_received: didn't find device for %r from %s %r",
                msg,
                addr,
                status,
            )
            return

        _LOGGER.warning("unknown msg: %r from %s", msg, addr)


async def spawn_disco_listener(
    interface: str,
    hass: core.HomeAssistant,
    add_entities: AddEntitiesCallback,
    entry: ConfigEntry,
    registry: DeviceRegistry,
):
    loop = asyncio.get_event_loop()

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: GoveeDiscoProtocol(hass, add_entities, registry),
        local_addr=(interface, LISTEN_PORT),
    )

    def unload():
        _LOGGER.warning("unloading; stop listener for %s", interface)
        transport.close()

    entry.async_on_unload(unload)


async def discover_devices_on_interface(
    interface: str,
    hass: core.HomeAssistant,
    add_entities: AddEntitiesCallback,
    entry: ConfigEntry,
    registry: DeviceRegistry,
):
    cancelled = False

    hass.async_create_task(
        spawn_disco_listener(interface, hass, add_entities, entry, registry)
    )

    def unload():
        _LOGGER.warning("unloading; cancel disco for %s", interface)
        cancelled = True

    entry.async_on_unload(unload)

    mcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mcast.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    mcast.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
    mcast.setsockopt(
        socket.SOL_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(BROADCAST_ADDR) + socket.inet_aton(interface),
    )
    mcast.bind((interface, 0))

    interval = 10
    while not cancelled:
        _LOGGER.debug("Performing disco on %s", interface)
        mcast.sendto(
            b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}',
            (BROADCAST_ADDR, BROADCAST_PORT),
        )
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 60)

        if cancelled:
            _LOGGER.error("disco for %s cancelled", interface)


# https://govee-public.s3.amazonaws.com/developer-docs/GoveeDeveloperAPIReference.pdf
class GoveeApiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def control(self, params):
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        conn = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.put(
                url="https://developer-api.govee.com/v1/devices/control",
                headers={"Govee-API-Key": self.api_key},
                json=params,
            ) as response:
                if response.status == 200:
                    resp = await response.json()
                    return resp
                else:
                    message = await response.text()
                    _LOGGER.error(
                        "failed to control: %s %r (api_key=%s)",
                        message,
                        params,
                        self.api_key,
                    )
                return None

    async def get_state(self, device_id, model):
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        conn = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(
                url="https://developer-api.govee.com/v1/devices/state",
                headers={"Govee-API-Key": self.api_key},
                params={"model": model, "device": device_id},
            ) as response:
                if response.status == 200:
                    resp = await response.json()
                    _LOGGER.debug("resp: %r", resp)
                    if "data" in resp and "properties" in resp["data"]:
                        return resp["data"]["properties"]
                else:
                    message = await response.text()
                    _LOGGER.error(
                        "failed to get state: %s (api_key=%s)", message, self.api_key
                    )
                return None

    async def get_devices(self):
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        conn = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(
                url="https://developer-api.govee.com/v1/devices",
                headers={"Govee-API-Key": self.api_key},
            ) as response:
                if response.status == 200:
                    devices = await response.json()
                    _LOGGER.debug("devices: %r", devices)
                    if "data" in devices and "devices" in devices["data"]:
                        return devices["data"]["devices"]
                else:
                    message = await response.text()
                    _LOGGER.error(
                        "failed to get devices: %s (api_key=%s)", message, self.api_key
                    )
                return None
