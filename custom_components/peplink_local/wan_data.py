"""Pure helpers that read values out of a status.wan.connection WAN object.

Kept free of Home Assistant imports so they can be unit-tested against
recorded router responses (tests/test_wan_data.py).

Field names follow the Peplink Router API documentation for firmware 8.5.2
and what a MAX BR1 Mini 5G actually returns. Older firmware used ``gobi``
instead of ``cellular`` (before 8.0.1), ``mode`` instead of ``routingMode``
and a flat ``band`` list instead of ``rat[].band`` (before 8.1.2); both
spellings are accepted.
"""

from __future__ import annotations

from typing import Any

CELLULAR_TYPES = ("cellular", "gobi")
WIFI_TYPES = ("wifi", "wireless")

STATUS_LED_OPTIONS = ["gray", "red", "yellow", "green", "flash"]


def wan_type(conn: dict[str, Any]) -> str:
    return str(conn.get("type") or "").lower()


def is_cellular(conn: dict[str, Any]) -> bool:
    return wan_type(conn) in CELLULAR_TYPES or "cellular" in conn or "gobi" in conn


def is_wifi(conn: dict[str, Any]) -> bool:
    return wan_type(conn) in WIFI_TYPES or "wifi" in conn or "wireless" in conn


def cellular(conn: dict[str, Any]) -> dict[str, Any]:
    return conn.get("cellular") or conn.get("gobi") or {}


def wifi(conn: dict[str, Any]) -> dict[str, Any]:
    return conn.get("wifi") or conn.get("wireless") or {}


def bands(conn: dict[str, Any]) -> list[dict[str, Any]]:
    """All bands in use, primary first (rat[].band[], or legacy band[])."""
    cell = cellular(conn)
    found: list[dict[str, Any]] = []
    for rat in cell.get("rat") or []:
        found.extend(rat.get("band") or [])
    if not found:
        found.extend(cell.get("band") or [])
    return [b for b in found if isinstance(b, dict)]


def primary_band(conn: dict[str, Any]) -> dict[str, Any]:
    found = bands(conn)
    return found[0] if found else {}


def band_signal(conn: dict[str, Any], field: str) -> float | None:
    """rsrp / rsrq / sinr / rssi of the primary band."""
    value = (primary_band(conn).get("signal") or {}).get(field)
    return value if isinstance(value, (int, float)) else None


def signal_level(conn: dict[str, Any]) -> int | None:
    value = cellular(conn).get("signalLevel")
    return value if isinstance(value, int) else None


def carrier(conn: dict[str, Any]) -> str | None:
    return (cellular(conn).get("carrier") or {}).get("name") or None


def technology(conn: dict[str, Any]) -> str | None:
    """e.g. '5G SA', 'LTE-A'; falls back to mobileType ('5G') or network."""
    cell = cellular(conn)
    return cell.get("dataTechnology") or cell.get("mobileType") or cell.get("network") or None


def band_names(conn: dict[str, Any]) -> list[str]:
    return [b["name"] for b in bands(conn) if b.get("name")]


def active_sim(conn: dict[str, Any]) -> str | None:
    """Which SIM is carrying data: 'SIM 1', 'SIM 2', 'eSIM', 'SpeedFusion Connect 5G/LTE', ..."""
    cell = cellular(conn)
    sims = cell.get("sim") or {}
    for sim_id in sims.get("order") or [k for k in sims if k != "order"]:
        sim = sims.get(str(sim_id)) or {}
        if sim.get("active"):
            return f"SIM {sim_id}" if str(sim_id).isdigit() else str(sim_id)
    if (cell.get("speedfusionConnect5gLte") or {}).get("active"):
        return "SpeedFusion Connect 5G/LTE"
    if (cell.get("remoteSim") or {}).get("active"):
        return "RemoteSIM"
    return None


def roaming(conn: dict[str, Any]) -> str | None:
    return (cellular(conn).get("roamingStatus") or {}).get("message") or None


def status_led(conn: dict[str, Any]) -> str | None:
    value = str(conn.get("statusLed") or "").lower()
    return value if value in STATUS_LED_OPTIONS else None


def routing_mode(conn: dict[str, Any]) -> str | None:
    return conn.get("routingMode") or conn.get("mode") or None


def priority(conn: dict[str, Any]) -> int | None:
    """Priority group (1 = highest). The router omits it for disabled WANs."""
    value = conn.get("priority")
    return value if isinstance(value, int) else None


_TYPE_LABELS = {
    "modem": "Modem",
    "wireless": "WiFi",
    "wifi": "WiFi",
    "gobi": "Cellular",
    "cellular": "Cellular",
    "ipsec": "IPSec VPN",
    "adsl": "ADSL",
    "ethernet": "Ethernet",
    "wovlan": "VLAN",
}


def type_label(value: str | None) -> str | None:
    """Readable WAN type, e.g. 'cellular' -> 'Cellular'."""
    if not value:
        return None
    return _TYPE_LABELS.get(str(value).lower(), str(value))
