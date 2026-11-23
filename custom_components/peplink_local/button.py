"""Router reboot, cellular modem and Starlink dish buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import wan_data, wifi
from .const import DOMAIN
from .devices import wan_device_info
from .entity_base import PeplinkControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[ButtonEntity] = [PeplinkRebootButton(coordinator)]
    for conn in coordinator.data.get("wan_status", {}).get("connection", []):
        if wan_data.is_cellular(conn):
            entities.append(PeplinkWANButton(coordinator, conn, "cellular_reset"))
            entities.append(PeplinkWANButton(coordinator, conn, "cellular_rescan"))
        if wan_data.is_wifi(conn):
            entities.append(PeplinkWifiScanButton(coordinator, conn))
            entities.append(PeplinkWifiJoinButton(coordinator, conn))
            entities.append(PeplinkWifiForgetButton(coordinator, conn))
            entities.append(PeplinkWANButton(coordinator, conn, "wifi_disconnect"))
        if wan_data.is_starlink(conn):
            for action in ("starlink_reboot", "starlink_stow", "starlink_unstow"):
                entities.append(PeplinkWANButton(coordinator, conn, action))
    async_add_entities(entities)


class PeplinkRebootButton(PeplinkControlEntity, ButtonEntity):
    """Reboot the router."""

    _attr_name = "Reboot"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, None, "reboot")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)}
        )

    async def async_press(self) -> None:
        await self._run(self.coordinator.api.reboot, "reboot the router")


_WAN_BUTTONS: dict[str, dict[str, Any]] = {
    "cellular_reset": {
        "name": "Reset Cellular Module",
        "icon": "mdi:restart",
        "device_class": ButtonDeviceClass.RESTART,
        "call": lambda api, wan: api.reset_cellular_module(wan),
    },
    "cellular_rescan": {
        "name": "Rescan Cellular Network",
        "icon": "mdi:radar",
        "device_class": None,
        "call": lambda api, wan: api.rescan_cellular_network(wan),
    },
    "wifi_disconnect": {
        "name": "Disconnect Wi-Fi",
        "icon": "mdi:wifi-off",
        "device_class": None,
        "call": lambda api, wan: api.wifi_disconnect(wan),
    },
    "starlink_reboot": {
        "name": "Reboot Starlink",
        "icon": "mdi:satellite-uplink",
        "device_class": ButtonDeviceClass.RESTART,
        "call": lambda api, wan: api.starlink_command(wan, "reboot"),
    },
    "starlink_stow": {
        "name": "Stow Starlink",
        "icon": "mdi:satellite-variant",
        "device_class": None,
        "call": lambda api, wan: api.starlink_command(wan, "stow"),
    },
    "starlink_unstow": {
        "name": "Unstow Starlink",
        "icon": "mdi:satellite-uplink",
        "device_class": None,
        "call": lambda api, wan: api.starlink_command(wan, "unstow"),
    },
}


class PeplinkWANButton(PeplinkControlEntity, ButtonEntity):
    """A one-shot command for one WAN."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, conn: dict[str, Any], kind: str) -> None:
        super().__init__(coordinator, str(conn.get("id")), kind)
        spec = _WAN_BUTTONS[kind]
        self._kind = kind
        self._requires_enabled_wan = kind == "wifi_disconnect"
        if kind == "wifi_disconnect":
            # Everyday Wi-Fi action: list it under Controls with Scan/Join/Forget
            # rather than under Configuration.
            self._attr_entity_category = None
        self._attr_name = spec["name"]
        self._attr_icon = spec["icon"]
        self._attr_device_class = spec["device_class"]
        self._attr_device_info = wan_device_info(coordinator, conn)

    async def async_press(self) -> None:
        spec = _WAN_BUTTONS[self._kind]
        await self._run(
            lambda: spec["call"](self.coordinator.api, self._wan_id),
            f"{spec['name'].lower()} on WAN {self._wan_id}",
        )


class PeplinkWifiScanButton(PeplinkControlEntity, ButtonEntity):
    """Scan for nearby networks; results feed the Network select and sensor."""

    _attr_name = "Scan Networks"
    _attr_icon = "mdi:wifi-refresh"

    _requires_enabled_wan = True

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "wifi_scan")
        self._attr_device_info = wan_device_info(coordinator, conn)

    async def async_press(self) -> None:
        wan_id = self._wan_id

        async def scan() -> None:
            raw = await self.coordinator.api.wifi_scan(wan_id)
            self.coordinator.wifi_networks[wan_id] = wifi.networks(raw)
            self.coordinator.wifi_scanned_at[wan_id] = dt_util.utcnow()
            self.coordinator.async_update_listeners()

        await self._run(scan, f"scan for Wi-Fi networks on WAN {wan_id}")


class PeplinkWifiJoinButton(PeplinkControlEntity, ButtonEntity):
    """Join the network chosen in the Network select (open or saved networks)."""

    _attr_name = "Join"
    _attr_icon = "mdi:wifi-arrow-right"

    _requires_enabled_wan = True

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "wifi_join")
        self._attr_device_info = wan_device_info(coordinator, conn)

    async def async_press(self) -> None:
        wan_id = self._wan_id
        ssid = self.coordinator.wifi_selected.get(wan_id)
        if not ssid:
            raise HomeAssistantError("Pick a network in the Network list first")
        net = wifi.find(self.coordinator.wifi_networks.get(wan_id, []), ssid)
        saved = wifi.find(self.coordinator.wifi_saved.get(wan_id, []), ssid)
        if saved is None and wifi.needs_password(net):
            # Saved networks are per Wi-Fi WAN on Peplink routers.
            elsewhere = [
                conn.get("name") or f"WAN {conn.get('id')}"
                for conn in (self.coordinator.data or {}).get("wan_status", {}).get("connection", [])
                if str(conn.get("id")) != wan_id
                and wifi.find(self.coordinator.wifi_saved.get(str(conn.get("id")), []), ssid)
            ]
            hint = (
                f" It is saved on {', '.join(elsewhere)}, but each Wi-Fi WAN keeps its own"
                " saved networks, so this one needs the password once."
                if elsewhere else ""
            )
            raise HomeAssistantError(
                f"'{ssid}' is secured and not saved on this WAN: use Join with password.{hint}"
            )

        async def join() -> None:
            await self.coordinator.api.wifi_connect(
                wan_id, ssid, None, net.get("security") if net else None
            )
            self.coordinator.watch_wifi_result(wan_id)
            await self.coordinator.async_refresh_wifi_saved()

        await self._run(join, f"join '{ssid}' on WAN {wan_id}")


class PeplinkWifiForgetButton(PeplinkControlEntity, ButtonEntity):
    """Delete the network chosen in the Network select from the router.

    Disconnects if that network is in use. Add a confirmation on dashboards.
    """

    _attr_name = "Forget"
    _attr_icon = "mdi:wifi-remove"

    _requires_enabled_wan = True

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "wifi_forget")
        self._attr_device_info = wan_device_info(coordinator, conn)

    async def async_press(self) -> None:
        wan_id = self._wan_id
        ssid = self.coordinator.wifi_selected.get(wan_id)
        saved = wifi.find(self.coordinator.wifi_saved.get(wan_id, []), ssid) if ssid else None
        if not ssid or saved is None:
            raise HomeAssistantError("Pick a saved network in the Network list first")
        policy = wifi.policy_for(saved, wifi.find(self.coordinator.wifi_networks.get(wan_id, []), ssid))

        async def forget() -> None:
            await self.coordinator.api.wifi_forget(wan_id, ssid, policy)
            self.coordinator.wifi_selected.pop(wan_id, None)
            await self.coordinator.async_refresh_wifi_saved()

        await self._run(forget, f"forget '{ssid}' on WAN {wan_id}")
