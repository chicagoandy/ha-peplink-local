"""'Join with Password' box on each Wi-Fi WAN's device page."""

from __future__ import annotations

from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import wan_data
from .const import DOMAIN
from .devices import wan_device_info
import time

from .entity_base import PeplinkControlEntity, async_join_chosen_network


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        PeplinkWifiPasswordText(coordinator, conn)
        for conn in coordinator.data.get("wan_status", {}).get("connection", [])
        if wan_data.is_wifi(conn)
    )


class PeplinkWifiPasswordText(PeplinkControlEntity, TextEntity):
    """Password for the network chosen in Network.

    Press Enter to join right away, or press Join (within 5 minutes). The
    entity is write-only: its value always reads empty and the password is
    kept in memory only, so it never becomes entity state or a recorder row.
    The router saves it in the network's profile for this WAN.
    """

    _attr_name = "Password"
    _attr_icon = "mdi:form-textbox-password"
    _attr_mode = TextMode.PASSWORD
    _attr_native_min = 0
    _attr_native_max = 63  # WPA passphrase limit
    _attr_native_value = ""
    _requires_enabled_wan = True

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        # unique_id kept from 0.4.0-beta14 ("Join with Password")
        super().__init__(coordinator, str(conn.get("id")), "wifi_join_password")
        self._attr_device_info = wan_device_info(coordinator, conn)

    async def async_set_value(self, value: str) -> None:
        try:
            if not value:
                self.coordinator.wifi_pending_password.pop(self._wan_id, None)
                return
            self.coordinator.wifi_pending_password[self._wan_id] = (value, time.monotonic())
            if self.coordinator.wifi_selected.get(self._wan_id):
                await async_join_chosen_network(self, value)
        finally:
            self._attr_native_value = ""
            self.async_write_ha_state()
