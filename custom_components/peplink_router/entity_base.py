"""Shared base for control entities (switch, select, button)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import wifi
from .peplink_api import PeplinkCommandError

PENDING_PASSWORD_SECONDS = 300


class PeplinkControlEntity(CoordinatorEntity):
    """A control bound to one WAN (wan_id) or to the router (wan_id None)."""

    _attr_has_entity_name = True
    # Unavailable while this WAN is disabled (e.g. Wi-Fi scan/join controls).
    _requires_enabled_wan = False

    def __init__(self, coordinator: Any, wan_id: str | None, key: str) -> None:
        super().__init__(coordinator)
        self._wan_id = wan_id
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = (
            f"{entry_id}_wan{wan_id}_{key}" if wan_id is not None else f"{entry_id}_{key}"
        )

    def _connection(self) -> dict[str, Any] | None:
        for conn in (self.coordinator.data or {}).get("wan_status", {}).get("connection", []):
            if str(conn.get("id")) == self._wan_id:
                return conn
        return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._requires_enabled_wan:
            conn = self._connection()
            return bool(conn and conn.get("enable"))
        return True

    async def _run(self, action: Callable[[], Awaitable[None]], what: str) -> None:
        """Run a router command, surface failures in the UI, then refresh."""
        try:
            await action()
        except (PeplinkCommandError, ValueError) as err:
            raise HomeAssistantError(f"Peplink: could not {what}: {err}") from err
        except Exception as err:  # connection problems from the API client
            raise HomeAssistantError(f"Peplink: could not {what}: {err}") from err
        await self.coordinator.async_request_refresh()


async def async_join_chosen_network(
    entity: PeplinkControlEntity, password: str | None = None
) -> None:
    """The one Join: join the network chosen in this WAN's Network select.

    Uses, in order: the password passed in, a password typed in the WAN's
    Password box within the last 5 minutes (memory only), or none. Open and
    saved networks need none; a secured network that isn't saved on this WAN
    asks for the password in the Password box.
    """
    coordinator = entity.coordinator
    wan_id = entity._wan_id
    ssid = coordinator.wifi_selected.get(wan_id)
    if not ssid:
        raise HomeAssistantError("Pick a network in the Network list first")

    if not password:
        pending = coordinator.wifi_pending_password.get(wan_id)
        if pending and time.monotonic() - pending[1] < PENDING_PASSWORD_SECONDS:
            password = pending[0]

    net = wifi.find(coordinator.wifi_networks.get(wan_id, []), ssid)
    saved = wifi.find(coordinator.wifi_saved.get(wan_id, []), ssid)
    if not password and saved is None and wifi.needs_password(net):
        raise HomeAssistantError(
            f"'{ssid}' needs its password: type it in Password and press Enter."
        )

    async def join() -> None:
        await coordinator.api.wifi_connect(
            wan_id, ssid, password or None, net.get("security") if net else None
        )
        coordinator.wifi_pending_password.pop(wan_id, None)
        coordinator.watch_wifi_result(wan_id)
        await coordinator.async_refresh_wifi_saved()

    await entity._run(join, f"join '{ssid}' on WAN {wan_id}")
