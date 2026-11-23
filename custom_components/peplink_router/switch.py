"""WAN enable/disable switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .devices import wan_device_info
from .entity_base import PeplinkControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    connections = coordinator.data.get("wan_status", {}).get("connection", [])
    async_add_entities(
        PeplinkWANEnableSwitch(coordinator, conn)
        for conn in connections
        if "enable" in conn
    )


class PeplinkWANEnableSwitch(PeplinkControlEntity, SwitchEntity):
    """Turns a WAN connection on or off (config.wan.connection + apply)."""

    _attr_name = "Enabled"
    _attr_icon = "mdi:wan"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, conn: dict[str, Any]) -> None:
        super().__init__(coordinator, str(conn.get("id")), "enabled")
        self._attr_device_info = wan_device_info(coordinator, conn)

    @property
    def is_on(self) -> bool | None:
        conn = self._connection()
        return None if conn is None else bool(conn.get("enable"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(
            lambda: self.coordinator.api.set_wan_enabled(self._wan_id, True),
            f"enable WAN {self._wan_id}",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(
            lambda: self.coordinator.api.set_wan_enabled(self._wan_id, False),
            f"disable WAN {self._wan_id}",
        )
