from bleak import BleakClient, BleakError
import asyncio
from homeassistant.util import color
from homeassistant.components import bluetooth
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
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
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
_DEVICES = {}
_BLE_DEVICES = {}
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({})

SKU_NAMES = {
    "H610A": "Glide Lively",
    "H61A2": "Neon LED Strip",
    "H6072": "Lyra Floor Lamp",
    "H619A": "LED Strip",
}


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


async def async_setup_platform(
    hass: core.HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> bool:
    _LOGGER.error("async_setup_platform was called")
    await discover_devices(hass, add_entities)

    return True


async def try_ble(
    hass: core.HomeAssistant, service_info: bluetooth.BluetoothServiceInfoBleak
):
    async with BleakClient(service_info.device) as client:
        _LOGGER.error(
            "try_ble: %s has characteristics: %s",
            service_info,
            client.services.characteristics,
        )


async def async_setup_entry(
    hass: core.HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
):
    _LOGGER.error("async_setup_entry was called")
    await discover_devices(hass, add_entities)

    if False:

        @callback
        def _async_discovered_ble(
            service_info: bluetooth.BluetoothServiceInfoBleak,
            change: bluetooth.BluetoothChange,
        ) -> None:
            """Subscribe to bluetooth changes."""
            _LOGGER.warning("New service_info: %s %s", change, service_info)
            _BLE_DEVICES[service_info.device.address] = service_info
            hass.async_create_task(try_ble(hass, service_info))

        entry.async_on_unload(
            bluetooth.async_register_callback(
                hass,
                _async_discovered_ble,
                {"manufacturer_id": 34818},
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )


async def discover_devices(hass: core.HomeAssistant, add_entities: AddEntitiesCallback):
    interfaces = await async_get_interfaces(hass)
    _LOGGER.error("setup found interfaces: %r", interfaces)
    for interface in interfaces:
        hass.async_create_task(
            discover_devices_on_interface(interface, hass, add_entities)
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
    def __init__(
        self,
        hass: core.HomeAssistant,
        device_id,
        sku,
        hardware_version,
        software_version,
        addr,
    ):
        self.hass = hass
        self.device_id = device_id
        self.sku = sku
        self.hardware_version = hardware_version
        self.software_version = software_version
        self.addr = addr
        self.status = None
        self._attr_min_color_temp_kelvin = 2000
        self._attr_max_color_temp_kelvin = 9000
        self._govee_request_q = []
        self._govee_current_request = None

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

    @property
    def supported_features(self):
        return SUPPORT_BRIGHTNESS | SUPPORT_COLOR | SUPPORT_COLOR_TEMP

    async def async_request_govee_status(self):
        await self.async_send_govee_request(b'{"msg":{"cmd":"devStatus","data":{}}}')

    async def async_send_govee_request(self, data):
        if self._govee_current_request and self._govee_current_request.cancelled():
            _LOGGER.error("current req is cancelled; clear it out")
            self._govee_current_request = None
            if not self._govee_current_request:
                self._govee_schedule_next()

        fut = asyncio.get_running_loop().create_future()
        if self._govee_current_request or self._govee_request_q:
            _LOGGER.error(
                "queue up request %s to %s (%r, %r)",
                data,
                self.addr,
                self._govee_current_request,
                self._govee_request_q,
            )
            self._govee_request_q.append((data, fut))
            timeout = TimeoutManager()
            try:
                async with timeout.async_timeout(2):
                    await fut
                return
            finally:
                if (
                    self._govee_current_request
                    and self._govee_current_request.cancelled()
                ):
                    _LOGGER.error("current req is cancelled; clear it out")
                    self._govee_current_request = None
                if not self._govee_current_request:
                    self._govee_schedule_next()

        self._govee_current_request = fut
        self._govee_send_item(data)
        try:
            for _ in range(0, 2):
                try:
                    timeout = TimeoutManager()
                    async with timeout.async_timeout(1):
                        await fut
                        return
                except asyncio.TimeoutError:
                    _LOGGER.warning("timedout, will retry %s to %s", data, self.addr)
                    self._govee_send_item(data)

            _LOGGER.warning("final attempt to send %s to %s", data, self.addr)
            timeout = TimeoutManager()
            async with timeout.async_timeout(1):
                await fut
                return
        finally:
            self._govee_current_request = None
            self._govee_schedule_next()

    def _govee_send_item(self, data):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _LOGGER.debug("sending %s to %s %s", data, self.device_id, self.addr)
        loop = asyncio.get_event_loop()
        s.sendto(data, (self.addr, COMMAND_PORT))

    def _govee_schedule_next(self):
        while self._govee_request_q:
            data, fut = self._govee_request_q.pop(0)
            if fut.cancelled():
                continue
            _LOGGER.warning("schedule next for %s", self.addr)
            self._govee_current_request = fut
            self._govee_send_item(data)
            return

    def set_status(self, status: GoveeDevStatus):
        if self.status != status:
            _LOGGER.warning(
                "device status updated: %s from %r --> status %r",
                self.device_id,
                self.status,
                status,
            )
            self.status = status

            self._attr_color_temp_kelvin = status.color_temp_kelvin
            if status.color_temp_kelvin > 0:
                self._attr_color_temp = color.color_temperature_kelvin_to_mired(
                    status.color_temp_kelvin
                )
            else:
                self._attr_color_temp_kelvin = None
                self._attr_color_temp = None

            self._attr_brightness = max(
                min(int(255 * status.brightness_pct / 100), 100), 0
            )
            self._attr_is_on = status.turned_on
            self._attr_rgb_color = (
                status.color["r"],
                status.color["g"],
                status.color["b"],
            )
            self._attr_hs_color = color.color_RGB_to_hs(
                self._attr_rgb_color[0],
                self._attr_rgb_color[1],
                self._attr_rgb_color[2],
            )
            self._attr_xy_color = color.color_RGB_to_xy(
                self._attr_rgb_color[0],
                self._attr_rgb_color[1],
                self._attr_rgb_color[2],
            )

            if self.entity_id:
                self.async_write_ha_state()

        if self._govee_current_request:
            f = self._govee_current_request
            self._govee_current_request = None
            f.set_result(None)

        self._govee_schedule_next()

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.error("turn on %s with %s", self.device_id, kwargs)
        turn_on = True

        if ATTR_HS_COLOR in kwargs:
            hs_color = kwargs.pop(ATTR_HS_COLOR)
            r, g, b = color.color_hs_to_RGB(hs_color[0], hs_color[1])
            await self.async_send_govee_request(
                bytes(
                    json.dumps(
                        {
                            "msg": {
                                "cmd": "colorwc",
                                "data": {
                                    "color": {"r": r, "g": g, "b": b},
                                    "colorTemInKelvin": 0,
                                },
                            }
                        }
                    ),
                    "utf-8",
                )
            )
            turn_on = False

        if ATTR_BRIGHTNESS_PCT in kwargs:
            brightness = max(min(kwargs.pop(ATTR_BRIGHTNESS_PCT), 100), 0)
            await self.async_send_govee_request(
                bytes(
                    json.dumps(
                        {"msg": {"cmd": "brightness", "data": {"value": brightness}}}
                    ),
                    "utf-8",
                )
            )
            turn_on = False
        elif ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.pop(ATTR_BRIGHTNESS)
            await self.async_send_govee_request(
                bytes(
                    json.dumps(
                        {
                            "msg": {
                                "cmd": "brightness",
                                "data": {"value": int(brightness * 100 / 255)},
                            }
                        }
                    ),
                    "utf-8",
                )
            )
            turn_on = False

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp_kelvin = kwargs.pop(ATTR_COLOR_TEMP_KELVIN)
            color_temp_kelvin = max(
                min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                self._attr_min_color_temp_kelvin,
            )
            await self.async_send_govee_request(
                bytes(
                    json.dumps(
                        {
                            "msg": {
                                "cmd": "colorwc",
                                "data": {
                                    # "color": {"r": r, "g": g, "b": b},
                                    "colorTemInKelvin": color_temp_kelvin
                                },
                            }
                        }
                    ),
                    "utf-8",
                )
            )
            turn_on = False
        elif ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs.pop(ATTR_COLOR_TEMP)
            color_temp_kelvin = color.color_temperature_mired_to_kelvin(color_temp)
            color_temp_kelvin = max(
                min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                self._attr_min_color_temp_kelvin,
            )
            await self.async_send_govee_request(
                bytes(
                    json.dumps(
                        {
                            "msg": {
                                "cmd": "colorwc",
                                "data": {
                                    # "color": {"r": r, "g": g, "b": b},
                                    "colorTemInKelvin": color_temp_kelvin
                                },
                            }
                        }
                    ),
                    "utf-8",
                )
            )
            turn_on = False

        if turn_on:
            await self.async_send_govee_request(
                bytes(
                    json.dumps({"msg": {"cmd": "turn", "data": {"value": 1}}}), "utf-8"
                )
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.error("turn OFF %s with %s", self.device_id, kwargs)
        await self.async_send_govee_request(
            bytes(json.dumps({"msg": {"cmd": "turn", "data": {"value": 0}}}), "utf-8")
        )

    async def async_update(self):
        await self.async_request_govee_status()


class GoveeDiscoProtocol:
    def __init__(self, hass: core.HomeAssistant, add_entities: AddEntitiesCallback):
        self.add_entities = add_entities
        self.hass = hass

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        message = data.decode()
        msg = json.loads(message)["msg"]
        _LOGGER.debug("decoded: %r from %s", msg, addr)
        source_ip = addr[0]
        data = msg["data"]
        if msg["cmd"] == "scan":
            device = GoveeDevice(
                self.hass,
                data["device"],
                data["sku"],
                data["wifiVersionHard"],
                data["wifiVersionSoft"],
                data["ip"],
            )
            existing = _DEVICES.get(device.device_id, None)
            if existing:
                changed = (
                    existing.addr != device.addr
                    or existing.hardware_version != device.hardware_version
                    or existing.software_version != device.software_version
                )
                if changed:
                    existing.addr = device.addr
                    existing.hardware_version = device.hardware_version
                    existing.software_version = device.software_version
                    _LOGGER.error("Updated device %r", device)
                    existing.schedule_update_ha_state(force_refresh=True)
            else:
                _LOGGER.error("Found device %r", device)
                _DEVICES[device.device_id] = device
                self.add_entities([device], update_before_add=True)

            return

        if msg["cmd"] == "devStatus":
            status = GoveeDevStatus(
                data["onOff"],
                data["brightness"],
                data["color"],
                data["colorTemInKelvin"],
            )
            for device in _DEVICES.values():
                if device.addr == source_ip:
                    device.set_status(status)

            return

        _LOGGER.warning("unknown msg: %r from %s", msg, addr)


async def discover_devices_on_interface(
    interface: str, hass: core.HomeAssistant, add_entities: AddEntitiesCallback
):
    loop = asyncio.get_event_loop()

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: GoveeDiscoProtocol(hass, add_entities),
        local_addr=(interface, LISTEN_PORT),
    )

    mcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mcast.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    mcast.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
    mcast.setsockopt(
        socket.SOL_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(BROADCAST_ADDR) + socket.inet_aton(interface),
    )
    mcast.bind((interface, BROADCAST_PORT))

    while True:
        _LOGGER.debug("Performing disco on %s", interface)
        mcast.sendto(
            b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}',
            (BROADCAST_ADDR, BROADCAST_PORT),
        )
        await asyncio.sleep(10)
