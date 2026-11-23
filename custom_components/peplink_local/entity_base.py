"""Shared base for control entities (switch, select, button)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .peplink_api import PeplinkCommandError


class PeplinkControlEntity(CoordinatorEntity):
    """A control bound to one WAN (wan_id) or to the router (wan_id None)."""

    _attr_has_entity_name = True

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

    async def _run(self, action: Callable[[], Awaitable[None]], what: str) -> None:
        """Run a router command, surface failures in the UI, then refresh."""
        try:
            await action()
        except (PeplinkCommandError, ValueError) as err:
            raise HomeAssistantError(f"Peplink: could not {what}: {err}") from err
        except Exception as err:  # connection problems from the API client
            raise HomeAssistantError(f"Peplink: could not {what}: {err}") from err
        await self.coordinator.async_request_refresh()
