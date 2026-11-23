"""Device registry helpers shared by the entity platforms."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .wan_data import type_label

# Home Assistant 2026.x replaced DeviceInfo["via_device"] (identifier tuple)
# with "via_device_id" (registry ID); via_device stops working in 2027.8.
# Older releases (this integration supports 2025.1+) only know via_device.
_HAS_VIA_DEVICE_ID = "via_device_id" in getattr(DeviceInfo, "__annotations__", {})


def wan_device_info(coordinator: Any, connection: dict[str, Any]) -> DeviceInfo:
    """One sub-device per WAN, e.g. 'MAX-BR1-3499 Starlink', under the router.

    Used by every platform so they all describe the WAN device identically
    (otherwise the last platform to register overwrites the name).
    """
    wan_id = str(connection.get("id"))
    entry_id = coordinator.config_entry.entry_id
    router_name = coordinator.device_name or f"Peplink {coordinator.host}"
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_wan{wan_id}")},
        manufacturer="Peplink",
        model=f"WAN {wan_id} ({type_label(connection.get('type')) or 'WAN'})",
        name=f"{router_name} {connection.get('name') or f'WAN{wan_id}'}",
    )
    router_device_id = getattr(coordinator, "router_device_id", None)
    if _HAS_VIA_DEVICE_ID and router_device_id:
        info["via_device_id"] = router_device_id
    else:
        info["via_device"] = (DOMAIN, entry_id)
    return info
