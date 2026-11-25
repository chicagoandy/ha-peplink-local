"""WAN enable/disable switches."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import entity_platform
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .devices import wan_device_info
from .entity_base import PeplinkControlEntity

_LOGGER = logging.getLogger(__name__)


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

    # Action targeting a WAN's Enabled switch.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "set_health_check",
        {
            vol.Optional("retries"): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional("timeout"): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
            vol.Optional("interval"): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
            vol.Optional("recovery_retries"): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
        },
        "async_set_health_check",
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

    async def async_set_health_check(
        self,
        retries: int | None = None,
        timeout: float | None = None,
        interval: int | None = None,
        recovery_retries: int | None = None,
    ) -> None:
        """Make this WAN's health check more or less tolerant (timeout in seconds)."""

        async def _apply() -> None:
            sent = await self.coordinator.api.set_wan_health_check(
                self._wan_id,
                retry=retries,
                timeout_ms=round(timeout * 1000) if timeout is not None else None,
                interval=interval,
                recovery=recovery_retries,
            )
            _LOGGER.info("WAN %s health check set to %s", self._wan_id, sent)

        await self._run(_apply, f"change WAN {self._wan_id} health check")
