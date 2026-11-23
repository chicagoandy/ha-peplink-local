"""Wi-Fi scan/join/saved-network helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "wifi", ROOT / "custom_components" / "peplink_router" / "wifi.py"
)
wifi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wifi)

BR1_MINI = ROOT / "tests" / "fixtures" / "MAX-BR1-MINI-5GN-T-PRM_8.5.2s150-build-5332"

# Shaped like the API doc's cmd.wifi.scan example (fw 8.1.0+ fields).
SCAN = [
    {"ssid": "Camp", "bssid": "02:00:00:00:01:01", "channel": 6, "signalStrength": -71,
     "signalLevel": 3, "securityPolicy": "wpa-psk", "config": {}},
    {"ssid": "Camp", "bssid": "02:00:00:00:01:02", "channel": 36, "signalStrength": -58,
     "signalLevel": 4, "securityPolicy": "wpa-psk", "config": {}},
    {"ssid": "Home", "bssid": "02:00:00:00:02:01", "channel": 36, "signalStrength": -65,
     "signalLevel": 4, "securityPolicy": "wpa-psk", "config": {"profileId": 1, "automatic": True}},
    {"ssid": "Free", "bssid": "02:00:00:00:03:01", "channel": 1, "signal": -80,
     "securityPolicy": "open"},
    {"ssid": "", "bssid": "02:00:00:00:04:01", "signalStrength": -50, "securityPolicy": "wpa-psk"},
]


def test_networks_dedupes_sorts_and_drops_hidden():
    nets = wifi.networks({"stat": "ok", "response": SCAN})
    assert [n["ssid"] for n in nets] == ["Camp", "Home", "Free"]
    camp = wifi.find(nets, "Camp")
    assert camp["signal_dbm"] == -58 and camp["channel"] == 36  # strongest AP kept
    assert wifi.find(nets, "Home")["saved"] is True
    assert wifi.find(nets, "Free")["signal_dbm"] == -80  # pre-8.1.0 "signal"


def test_needs_password():
    nets = wifi.networks(SCAN)
    assert wifi.needs_password(wifi.find(nets, "Camp")) is True
    assert wifi.needs_password(wifi.find(nets, "Home")) is False  # saved
    assert wifi.needs_password(wifi.find(nets, "Free")) is False  # open
    assert wifi.needs_password(None) is False


def test_connect_body():
    assert wifi.connect_body("4", "Home") == {"connId": 4, "ssid": "Home"}
    assert wifi.connect_body(4, "Camp", "secret", "wpa-psk") == {
        "connId": 4, "ssid": "Camp",
        "security": {"policy": {"type": "WPA2/WPA3-Personal", "detail": {"key": "secret"}}},
    }
    assert wifi.connect_body(4, "Free", None, "open") == {
        "connId": 4, "ssid": "Free", "security": {"policy": {"type": "Open"}},
    }
    with pytest.raises(ValueError):
        wifi.connect_body(4, "Corp", "x", "wpa-eap")


def test_saved_networks_from_fixture():
    config = json.loads((BR1_MINI / "config.wan.connection.json").read_text())["response"]
    saved = wifi.saved_networks(config["4"])
    assert [n["priority"] for n in saved] == [1, 2]
    assert [n["security"] for n in saved] == ["Open", "WPA2/WPA3-Personal"]  # order [2, 1]
    assert all("key" not in json.dumps(n) for n in saved)  # password never exposed
    assert wifi.saved_networks(config["3"]) == []
    assert wifi.saved_networks(None) == []


def test_policy_for_and_result_text():
    assert wifi.policy_for({"security": "WPA2/WPA3-Personal"}, None) == "wpa-psk"
    assert wifi.policy_for({"security": "Open"}, None) == "open"
    assert wifi.policy_for(None, {"security": "wep"}) == "wep"
    assert wifi.result_text({"result": "PSK_AUTH_FAIL"}) == "Wrong password"
    assert wifi.result_text({"result": "SOMETHING_NEW"}) == "SOMETHING_NEW"
    assert wifi.result_text(None) is None


def test_undocumented_security_values_seen_on_8_5_2():
    # Real scan values from a MAX BR1 Mini on 8.5.2s150.
    assert wifi.connect_body(4, "A", "pw", "wpa23-psk")["security"]["policy"]["type"] == "WPA2/WPA3-Personal"
    assert wifi.connect_body(4, "B", "pw", "wpa3-psk")["security"]["policy"]["type"] == "WPA3-Personal"
    assert wifi.connect_body(4, "C", "pw", "wpa")["security"]["policy"]["type"] == "WPA/WPA2-Personal"
    for policy in ("wpa23-psk", "wpa3-psk", "wpa", "wpa-psk"):
        assert wifi.policy_for(None, {"security": policy}) == "wpa-psk"
    assert wifi.policy_for(None, {"security": "open"}) == "open"


def test_mark_connected():
    nets = wifi.networks(SCAN)
    marked = wifi.mark_connected(nets, "Home")
    assert [n["connected"] for n in marked] == [False, True, False]
    assert all(not n["connected"] for n in wifi.mark_connected(nets, None))
