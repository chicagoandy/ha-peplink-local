"""WAN routing mode selects (NAT / IP Forwarding)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
import voluptuous as vol

from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import wan_data, wifi
from .const import DOMAIN
from .devices import wan_device_info
from .entity_base import PeplinkControlEntity
from .peplink_api import ROUTING_MODES
from homeassistant.exceptions import HomeAssistantError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    connections = coordinator.data.get("wan_status", {}).get("connection", [])
    entities: list[SelectEntity] = [
        PeplinkRoutingModeSelect(coordinator, conn)
        for conn in connections
        if wan_data.routing_mode(conn) is not None
    ]
    entities += [
        PeplinkWifiNetworkSelect(coordinator, conn)
        for conn in connections
        if wan_data.is_wifi(conn)
    ]
    async_add_entities(entities)

    # Actions targeting a Wi-Fi WAN's Network select.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "join_wifi",
        {vol.Optional("ssid"): cv.string, vol.Optional("password"): cv.string},
        "async_join_wifi",
    )
    platform.async_register_entity_service(
        "forget_wifi",
        {vol.Optional("ssid"): cv.string},
        "async_forget_wifi",
    )


class PeplinkRoutingModeSelect(PeplinkControlEntity, SelectEntity):
    """NAT or IP Forwarding for one WAN (config.wan.connection + apply)."""

    _attr_name = "Routing Mode"
    _attr_icon = "mdi:swap-horizontal"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(ROUTING_MODES)

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "routing_mode")
        self._attr_device_info = wan_device_info(coordinator, conn)

    @property
    def current_option(self) -> str | None:
        conn = self._connection()
        mode = wan_data.routing_mode(conn) if conn else None
        return mode if mode in ROUTING_MODES else None

    async def async_select_option(self, option: str) -> None:
        await self._run(
            lambda: self.coordinator.api.set_wan_routing_mode(self._wan_id, option),
            f"set WAN {self._wan_id} routing mode to {option}",
        )


NO_SCAN_YET = "Press Scan Networks"


class PeplinkWifiNetworkSelect(PeplinkControlEntity, SelectEntity):
    """Choose a network found by the last scan. Choosing does not join.

    Join with the Join button (open/saved networks) or the join_wifi action
    (password), which acts on the network chosen here unless given an SSID.
    """

    _attr_name = "Network"
    _attr_icon = "mdi:wifi-star"

    _requires_enabled_wan = True

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "wifi_network")
        self._attr_device_info = wan_device_info(coordinator, conn)

    def _networks(self) -> list[dict[str, Any]]:
        return self.coordinator.wifi_networks.get(self._wan_id, [])

    def _connected_ssid(self) -> str | None:
        conn = self._connection()
        return wan_data.wifi(conn).get("ssid") if conn else None

    def _saved(self) -> list[dict[str, Any]]:
        return self.coordinator.wifi_saved.get(self._wan_id, [])

    @property
    def options(self) -> list[str]:
        # Scanned networks (strongest first), then saved ones not in range.
        ssids = [n["ssid"] for n in self._networks()]
        ssids += [n["ssid"] for n in self._saved() if n["ssid"] not in ssids]
        for extra in (self.coordinator.wifi_selected.get(self._wan_id), self._connected_ssid()):
            if extra and extra not in ssids:
                ssids.append(extra)
        return ssids or [NO_SCAN_YET]

    @property
    def current_option(self) -> str | None:
        choice = self.coordinator.wifi_selected.get(self._wan_id) or self._connected_ssid()
        return choice if choice in self.options else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        choice = self.current_option
        net = wifi.find(self._networks(), choice) if choice else None
        saved = wifi.find(self._saved(), choice) if choice else None
        if net is not None and saved is not None:
            net = {**net, "saved": True}
        return {
            "in_range": net is not None,
            "needs_password": wifi.needs_password(net) if saved is None else False,
            "security": net.get("security") if net else None,
            "signal_dbm": net.get("signal_dbm") if net else None,
            "saved": saved is not None or bool(net and net.get("saved")),
        }

    async def async_select_option(self, option: str) -> None:
        if option == NO_SCAN_YET:
            return
        if self.coordinator.wifi_selected.get(self._wan_id) != option:
            self.coordinator.wifi_pending_password.pop(self._wan_id, None)
        self.coordinator.wifi_selected[self._wan_id] = option
        self.async_write_ha_state()

    async def async_join_wifi(self, ssid: str | None = None, password: str | None = None) -> None:
        """Action peplink_local.join_wifi: join (with a password if given)."""
        wan_id = self._wan_id
        ssid = ssid or self.coordinator.wifi_selected.get(wan_id)
        if not ssid:
            raise HomeAssistantError("No network given and none chosen in the Network list")
        self.coordinator.wifi_selected[wan_id] = ssid
        net = wifi.find(self._networks(), ssid)

        async def join() -> None:
            await self.coordinator.api.wifi_connect(
                wan_id, ssid, password or None, net.get("security") if net else None
            )
            self.coordinator.watch_wifi_result(wan_id)
            await self.coordinator.async_refresh_wifi_saved()

        await self._run(join, f"join '{ssid}' on WAN {wan_id}")

    async def async_forget_wifi(self, ssid: str | None = None) -> None:
        """Action peplink_local.forget_wifi: delete a saved network."""
        wan_id = self._wan_id
        ssid = ssid or self.current_option
        if not ssid or ssid == NO_SCAN_YET:
            raise HomeAssistantError("No network given and none chosen in the Network list")
        policy = wifi.policy_for(wifi.find(self._saved(), ssid), wifi.find(self._networks(), ssid))

        async def forget() -> None:
            await self.coordinator.api.wifi_forget(wan_id, ssid, policy)
            if self.coordinator.wifi_selected.get(wan_id) == ssid:
                self.coordinator.wifi_selected.pop(wan_id)
            await self.coordinator.async_refresh_wifi_saved()

        await self._run(forget, f"forget '{ssid}' on WAN {wan_id}")
