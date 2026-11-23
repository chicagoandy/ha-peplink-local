"""'Join with Password' box on each Wi-Fi WAN's device page."""

from __future__ import annotations

from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import wan_data, wifi
from .const import DOMAIN
from .devices import wan_device_info
from .entity_base import PeplinkControlEntity


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
    """Type a password and press Enter to join the network chosen in Network.

    Write-only: the entity's value is always empty, so the password is never
    stored as entity state or in the recorder. It is sent to the router, which
    saves it in the network's profile for this WAN.
    """

    _attr_name = "Join with Password"
    _attr_icon = "mdi:wifi-lock-open"
    _attr_mode = TextMode.PASSWORD
    _attr_native_min = 0
    _attr_native_max = 63  # WPA passphrase limit
    _attr_native_value = ""
    _requires_enabled_wan = True

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "wifi_join_password")
        self._attr_device_info = wan_device_info(coordinator, conn)

    async def async_set_value(self, value: str) -> None:
        wan_id = self._wan_id
        if not value:
            return
        ssid = self.coordinator.wifi_selected.get(wan_id)
        if not ssid:
            raise HomeAssistantError("Pick a network in the Network list first")
        net = wifi.find(self.coordinator.wifi_networks.get(wan_id, []), ssid)

        async def join() -> None:
            await self.coordinator.api.wifi_connect(
                wan_id, ssid, value, net.get("security") if net else None
            )
            self.coordinator.watch_wifi_result(wan_id)
            await self.coordinator.async_refresh_wifi_saved()

        try:
            await self._run(join, f"join '{ssid}' on WAN {wan_id}")
        finally:
            # Never keep the password as state.
            self._attr_native_value = ""
            self.async_write_ha_state()
