"""Check the per-WAN value helpers against recorded router responses."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "wan_data", ROOT / "custom_components" / "peplink_local" / "wan_data.py"
)
wan_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wan_data)

BR1_MINI = ROOT / "tests" / "fixtures" / "MAX-BR1-MINI-5GN-T-PRM_8.5.2s150-build-5332"


def wans(name: str = "status.wan.connection") -> dict[str, dict]:
    response = json.loads((BR1_MINI / f"{name}.json").read_text())["response"]
    return {str(k): response[str(k)] for k in response["order"]}


@pytest.fixture
def br1() -> dict[str, dict]:
    return wans()


def test_types(br1):
    assert [wan_data.is_cellular(w) for w in br1.values()] == [False, True, False, False, False]
    assert [wan_data.is_wifi(w) for w in br1.values()] == [False, False, True, True, False]


def test_cellular_values(br1):
    cell = br1["2"]
    assert wan_data.signal_level(cell) == 4
    assert wan_data.carrier(cell) == "Google Fi"
    assert wan_data.technology(cell) == "5G SA"
    assert wan_data.band_names(cell)[0].startswith("5G Band n")
    for field in ("rsrp", "rsrq", "sinr"):
        assert isinstance(wan_data.band_signal(cell, field), (int, float)), field
    assert wan_data.band_signal(cell, "rsrp") < 0
    # 5G NR bands report no RSSI: sensor is skipped, not shown as unknown.
    assert wan_data.band_signal(cell, "rssi") is None
    assert wan_data.active_sim(cell) == "SIM 1"


def test_non_cellular_returns_none(br1):
    starlink = br1["1"]
    assert wan_data.signal_level(starlink) is None
    assert wan_data.carrier(starlink) is None
    assert wan_data.band_signal(starlink, "rsrp") is None
    assert wan_data.active_sim(starlink) is None


def test_wifi_values(br1):
    wifi = wan_data.wifi(br1["4"])
    assert wifi["signal"]["strength"] < 0
    assert wifi["channel"] == 36
    assert wifi["ssid"]  # placeholder


def test_status_priority_routing(br1):
    assert wan_data.status_led(br1["4"]) == "green"
    assert wan_data.status_led(br1["3"]) == "gray"
    assert wan_data.priority(br1["4"]) == 1
    assert wan_data.priority(br1["2"]) == 4
    # Disabled WANs: the router omits priority entirely.
    assert wan_data.priority(br1["3"]) is None
    assert wan_data.priority(br1["5"]) is None
    assert wan_data.routing_mode(br1["1"]) == "NAT"


def test_starlink_down():
    down = wans("status.wan.connection.starlink_down")["1"]
    assert wan_data.status_led(down) == "red"
    assert wan_data.priority(down) == 3


def test_legacy_field_names():
    legacy = {
        "type": "gobi",
        "mode": "IP Forwarding",
        "gobi": {
            "signalLevel": 3,
            "carrier": {"name": "Carrier"},
            "mobileType": "LTE",
            "band": [{"name": "LTE Band 2", "signal": {"rsrp": -100, "rssi": -70}}],
        },
    }
    assert wan_data.is_cellular(legacy)
    assert wan_data.routing_mode(legacy) == "IP Forwarding"
    assert wan_data.technology(legacy) == "LTE"
    assert wan_data.band_signal(legacy, "rssi") == -70
    assert wan_data.band_names(legacy) == ["LTE Band 2"]


def test_empty_values():
    assert wan_data.status_led({"statusLed": "empty"}) is None
    assert wan_data.status_led({}) is None
    assert wan_data.priority({"priority": None}) is None
    assert wan_data.primary_band({}) == {}


def test_type_label(br1):
    assert [wan_data.type_label(w.get("type")) for w in br1.values()] == [
        "Ethernet", "Cellular", "WiFi", "WiFi", "VLAN",
    ]
    assert wan_data.type_label(None) is None
    assert wan_data.type_label("somethingnew") == "somethingnew"


def test_is_starlink(br1):
    assert [wan_data.is_starlink(w) for w in br1.values()] == [True, False, False, False, False]
    assert wan_data.is_starlink({"type": "cellular", "name": "Starlink backup"}) is False
    assert wan_data.is_starlink({"type": "ethernet", "name": "WAN", "starlink": {"x": 1}}) is True
