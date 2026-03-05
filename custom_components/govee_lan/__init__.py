"""Govee LAN Control integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration via YAML (we mainly use config entries)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee LAN from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug("Setting up config entry: %s", entry.entry_id)

    # Forward setup to the platforms (light.py, etc.)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading config entry: %s", entry.entry_id)
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Optional cleanup if you stored per-entry data
    if ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
