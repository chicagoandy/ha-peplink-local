"""WAN routing mode selects (NAT / IP Forwarding)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import wan_data
from .const import DOMAIN
from .devices import wan_device_info
from .entity_base import PeplinkControlEntity
from .peplink_api import ROUTING_MODES


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    connections = coordinator.data.get("wan_status", {}).get("connection", [])
    async_add_entities(
        PeplinkRoutingModeSelect(coordinator, conn)
        for conn in connections
        if wan_data.routing_mode(conn) is not None
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
