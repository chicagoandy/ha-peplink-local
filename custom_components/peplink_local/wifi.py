"""Pure helpers for Wi-Fi WAN scanning and joining (no Home Assistant imports).

API: cmd.wifi.scan / connect / result / disconnect / forget (fw 7.1.1+;
signalStrength/signalLevel from 8.1.0; the connect "security" object from
8.2.0).
"""

from __future__ import annotations

from typing import Any

# Scan "securityPolicy" -> connect "security.policy.type".
# wpa-psk maps to the WPA2/WPA3 transition type, which joins WPA2-only and
# WPA3 access points alike (it is what the router uses for its own profiles).
SECURITY_TYPES = {
    "open": "Open",
    "wep": "WEP",
    "wpa-psk": "WPA2/WPA3-Personal",
    "wpapsk": "WPA2/WPA3-Personal",
}
ENTERPRISE_POLICIES = {"wpa-eap", "8021x"}

RESULT_TEXT = {
    "CONNECTED": "Connected",
    "TIMEOUT": "Timed out",
    "PSK_AUTH_FAIL": "Wrong password",
    "EAP_AUTH_FAIL": "Authentication failed",
    "AP_NOT_FOUND": "Network not found",
    "UNKNOWN_FAIL": "Failed",
}


def _signal(ap: dict[str, Any]) -> int | None:
    value = ap.get("signalStrength", ap.get("signal"))
    return value if isinstance(value, (int, float)) else None


def networks(scan: Any) -> list[dict[str, Any]]:
    """Scan response -> one entry per SSID (strongest AP), strongest first.

    Hidden networks (empty SSID) are dropped; they can't be picked by name.
    """
    if isinstance(scan, dict):
        scan = scan.get("response", scan.get("list", []))
    best: dict[str, dict[str, Any]] = {}
    for ap in scan or []:
        if not isinstance(ap, dict) or not ap.get("ssid"):
            continue
        entry = {
            "ssid": ap["ssid"],
            "signal_dbm": _signal(ap),
            "bars": ap.get("signalLevel"),
            "channel": ap.get("channel"),
            "security": ap.get("securityPolicy"),
            "saved": bool((ap.get("config") or {}).get("profileId")),
            "connected": bool((ap.get("status") or {}).get("connected")),
            "bssid": ap.get("bssid"),
        }
        current = best.get(entry["ssid"])
        if current is None or (entry["signal_dbm"] or -999) > (current["signal_dbm"] or -999):
            # keep "saved"/"connected" if any AP of this SSID had them
            if current:
                entry["saved"] = entry["saved"] or current["saved"]
                entry["connected"] = entry["connected"] or current["connected"]
            best[entry["ssid"]] = entry
        else:
            current["saved"] = current["saved"] or entry["saved"]
            current["connected"] = current["connected"] or entry["connected"]
    return sorted(best.values(), key=lambda n: -(n["signal_dbm"] or -999))


def find(nets: list[dict[str, Any]], ssid: str) -> dict[str, Any] | None:
    return next((n for n in nets if n["ssid"] == ssid), None)


def connect_body(
    conn_id: int | str,
    ssid: str,
    password: str | None = None,
    security_policy: str | None = None,
) -> dict[str, Any]:
    """Request body for cmd.wifi.connect.

    Without a password only connId and ssid are sent, which joins an open
    network or one the router already has a saved profile for.
    """
    body: dict[str, Any] = {"connId": int(conn_id), "ssid": ssid}
    policy = (security_policy or "").lower()
    if policy in ENTERPRISE_POLICIES:
        raise ValueError("Enterprise (802.1x / WPA-EAP) networks need the router's web page")
    if password:
        sec_type = SECURITY_TYPES.get(policy, "WPA2/WPA3-Personal")
        if sec_type == "Open":
            sec_type = "WPA2/WPA3-Personal"  # a password was given, so it isn't open
        body["security"] = {"policy": {"type": sec_type, "detail": {"key": password}}}
    elif policy == "open":
        body["security"] = {"policy": {"type": "Open"}}
    return body


def needs_password(net: dict[str, Any] | None) -> bool:
    """True for a secured network the router has no saved profile for."""
    if net is None:
        return False  # unknown: let the router decide
    return (net.get("security") or "open") != "open" and not net.get("saved")


def result_text(result: dict[str, Any] | None) -> str | None:
    if not result:
        return None
    code = result.get("result")
    return RESULT_TEXT.get(code, code)


# Saved-profile "security.policy.type" -> scan/forget "securityPolicy".
POLICY_FOR_TYPE = {
    "open": "open",
    "wep": "wep",
    "wpa3-personal": "wpa-psk",
    "wpa2/wpa3-personal": "wpa-psk",
    "wpa/wpa2-personal": "wpa-psk",
    "wpa/wpa2-enterprise": "wpa-eap",
    "802.1x with dynamic wep key": "8021x",
}


def saved_networks(wan_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Saved Wi-Fi profiles of one WAN from config.wan.connection, in the
    router's priority order. The stored password is never included.
    """
    profiles = ((wan_config or {}).get("wifi") or {}).get("wifiConnectionProfile") or {}
    order = profiles.get("order") or [k for k in profiles if k != "order"]
    result = []
    for rank, profile_id in enumerate(order, 1):
        profile = profiles.get(str(profile_id)) or {}
        if not profile.get("ssid"):
            continue
        sec_type = ((profile.get("security") or {}).get("policy") or {}).get("type")
        result.append(
            {
                "ssid": profile["ssid"],
                "security": sec_type,
                "auto_connect": profile.get("enable", True),
                "priority": rank,
            }
        )
    return result


def policy_for(saved: dict[str, Any] | None, scanned: dict[str, Any] | None) -> str:
    """securityPolicy to use for cmd.wifi.forget."""
    if scanned and scanned.get("security"):
        return scanned["security"]
    if saved and saved.get("security"):
        return POLICY_FOR_TYPE.get(str(saved["security"]).lower(), "wpa-psk")
    return "wpa-psk"
