"""Replay recorded router responses through PeplinkAPI.

These tests need only aiohttp, aioresponses, pytest and pytest-asyncio (no
Home Assistant). They load the anonymised fixtures in tests/fixtures/ and
check that the API layer parses them into the shapes the coordinator and
entities expect.

    pip install pytest pytest-asyncio aiohttp aioresponses
    pytest tests/test_api.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "peplink_local"))
from peplink_api import PeplinkAPI  # noqa: E402  isort:skip

FIXTURES = ROOT / "tests" / "fixtures"
BR1_MINI = FIXTURES / "MAX-BR1-MINI-5GN-T-PRM_8.5.2s150-build-5332"
HOST = "192.168.50.1"
BASE = f"https://{HOST}"


def load(folder: Path, name: str) -> dict:
    return json.loads((folder / f"{name}.json").read_text())


def mock_router(m: aioresponses, folder: Path, wan_file: str = "status.wan.connection") -> None:
    """Register every endpoint PeplinkAPI calls, answered from fixtures."""
    m.post(
        f"{BASE}/api/login",
        payload={"stat": "ok", "response": {"permission": {"GET": 1, "POST": 1}}},
        headers={"Set-Cookie": "bauth=test-cookie; Path=/api; Secure; HttpOnly"},
        repeat=True,
    )
    m.get(f"{BASE}/api/status", payload={"stat": "ok"}, repeat=True)
    m.get(f"{BASE}/api/status.wan", payload=load(folder, wan_file), repeat=True)
    m.get(
        re.compile(rf"^{re.escape(BASE)}/api/status\.client(\?.*)?$"),
        payload=load(folder, "status.client"),
        repeat=True,
    )
    # Query parameters may arrive in any order (aiohttp normalises them).
    cgi = re.escape(f"{BASE}/cgi-bin/MANGA/api.cgi?")
    for func in ("status.system.info", "status.traffic", "info.location"):
        m.get(re.compile(rf"^{cgi}(.*&)?func={re.escape(func)}(&.*)?$"),
              payload=load(folder, f"cgi.{func}"), repeat=True)


@pytest.fixture
async def api():
    session = aiohttp.ClientSession()
    client = PeplinkAPI(host=HOST, username="admin", password="pw",
                        session=session, verify_ssl=False)
    yield client
    await session.close()


async def test_login_sets_cookie(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        assert await api.connect()
        assert api._auth_cookie == "test-cookie"


async def test_wan_status_br1_mini(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        await api.connect()
        wans = {w["id"]: w for w in (await api.get_wan_status())["connection"]}

    assert set(wans) == {"1", "2", "3", "4", "5"}
    assert wans["1"]["name"] == "Starlink" and wans["1"]["type"] == "ethernet"
    assert wans["2"]["type"] == "cellular"
    assert wans["2"]["cellular"]["signalLevel"] == 4
    band = wans["2"]["cellular"]["rat"][0]["band"][0]
    assert band["name"].startswith("5G Band")
    assert isinstance(band["signal"]["rsrp"], int)
    assert wans["4"]["message"].startswith("Connected")
    assert wans["4"]["wifi"]["signal"]["strength"] < 0
    # Disabled WANs have no priority field at all.
    assert wans["3"]["enable"] is False and "priority" not in wans["3"]
    assert wans["5"]["enable"] is False and "priority" not in wans["5"]
    assert wans["4"]["priority"] == 1


async def test_wan_status_starlink_down(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI, wan_file="status.wan.connection.starlink_down")
        await api.connect()
        wans = {w["id"]: w for w in (await api.get_wan_status())["connection"]}

    assert wans["1"]["statusLed"] == "red"
    assert wans["1"]["message"] == "No Cable Detected"
    assert not wans["1"].get("ip")


async def test_clients(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        await api.connect()
        clients = (await api.get_clients())["client"]

    raw = load(BR1_MINI, "status.client")["response"]["list"]
    assert len(clients) == len(raw) > 0
    for client in clients:
        assert re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", client["mac"], re.I)
        assert client["connected"] is True
        assert client["name"]


async def test_system_info(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        await api.connect()
        info = await api.get_system_info()

    device = info["device_info"]
    assert device["model"] == "Peplink MAX BR1 Mini 5G"
    assert device["product_code"] == "MAX-BR1-MINI-5GN-T-PRM"
    assert device["firmware_version"] == "8.5.2s150 build 5332"
    assert device["serial_number"]  # redacted placeholder, but present
    # Fanless model: no fans reported.
    assert info["fan_speeds"].get("fans", []) == []


async def test_traffic_stats(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        await api.connect()
        stats = (await api.get_traffic_stats())["stats"]

    assert stats, "expected per-WAN traffic stats"
    assert {str(s["wan_id"]) for s in stats} <= {"1", "2", "3", "4", "5"}


async def test_location(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        await api.connect()
        loc = await api.get_location()

    assert loc["gps"] is True
    assert loc["location"]["latitude"] == 40.0
    assert loc["location"]["longitude"] == -75.0
