"""Check the client-list helpers against recorded router responses."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "clients", ROOT / "custom_components" / "peplink_local" / "clients.py"
)
clients = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clients)

BR1_MINI = ROOT / "tests" / "fixtures" / "MAX-BR1-MINI-5GN-T-PRM_8.5.2s150-build-5332"


def client_list(name: str = "status.client") -> list[dict]:
    return json.loads((BR1_MINI / f"{name}.json").read_text())["response"]["list"]


def test_active_summaries():
    raw = client_list()
    summary = clients.active_summaries(raw)
    assert len(summary) == sum(1 for c in raw if c.get("active", True) is not False)
    assert all(r["active"] for r in summary)
    names = [r.get("name") or "~" for r in summary]
    assert names == sorted(names, key=str.lower)
    for record in summary:
        assert clients.is_mac(record["mac"])
        assert record["ip"].startswith("192.168.")


def test_macs_and_labels():
    raw = client_list()
    found = clients.macs(raw)
    assert len(found) == len({c["mac"].upper() for c in raw})
    assert all(m == m.upper() for m in found)
    assert "(" in clients.label(raw[0]) and raw[0]["mac"] in clients.label(raw[0])


def test_helpers():
    assert clients.norm_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert not clients.is_mac("01ENTRY_gps")
    assert clients.client_name({"name": "Unknown Device"}) is None
    assert clients.client_name({"hostname": "pve"}) == "pve"
    assert clients.is_active({}) is True
    assert clients.is_active({"active": False}) is False
    rec = clients.summary({"name": "x", "mac": "AA:BB:CC:DD:EE:FF", "signal": {"strength": -60}})
    assert rec["signal_dbm"] == -60
    legacy = clients.summary({"name": "y", "signalStrength": {"value": -70}})
    assert legacy["signal_dbm"] == -70
