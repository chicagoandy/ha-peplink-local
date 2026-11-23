"""Router reboot, cellular modem and Starlink dish buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import wan_data
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
