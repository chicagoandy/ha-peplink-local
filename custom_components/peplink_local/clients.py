"""Pure helpers for the router's client list (no Home Assistant imports)."""

from __future__ import annotations

import re
from typing import Any

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def is_mac(value: str | None) -> bool:
    return bool(value and _MAC_RE.match(value))


def norm_mac(mac: str) -> str:
    return mac.replace("-", ":").upper()


def is_active(client: dict[str, Any]) -> bool:
    """status.client lists inactive (lease-only) clients too; 'active' says which."""
    return client.get("active", True) is not False


def client_name(client: dict[str, Any]) -> str | None:
    name = client.get("name") or client.get("hostname")
    return None if not name or name == "Unknown Device" else str(name)


def label(client: dict[str, Any]) -> str:
    """Option label, e.g. 'cerbo (192.168.50.12, C0:61:9A:B8:8E:D0)'."""
    details = ", ".join(x for x in (client.get("ip"), client.get("mac")) if x)
    return f"{client_name(client) or 'Unknown'} ({details})"


def summary(client: dict[str, Any]) -> dict[str, Any]:
    """Compact per-client record for the Connected Clients attribute."""
    signal = client.get("signal") or {}
    legacy = client.get("signalStrength") or {}
    record = {
        "name": client_name(client),
        "ip": client.get("ip"),
        "mac": client.get("mac"),
        "connection_type": client.get("connectionType"),
        "ssid": client.get("essid"),
        "signal_dbm": signal.get("strength", legacy.get("value")),
        "active": is_active(client),
    }
    return {k: v for k, v in record.items() if v is not None}


def active_summaries(clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (summary(c) for c in clients if is_active(c)),
        key=lambda r: ((r.get("name") or "~").lower(), r.get("ip") or ""),
    )


def macs(clients: list[dict[str, Any]]) -> set[str]:
    return {norm_mac(c["mac"]) for c in clients if is_mac(c.get("mac"))}
