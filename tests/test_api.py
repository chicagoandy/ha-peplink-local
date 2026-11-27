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
sys.path.insert(0, str(ROOT / "custom_components" / "peplink_router"))
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
    for func in ("status.system.info", "status.traffic"):
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
    assert "firmware_version" not in device  # documented info.firmware owns it
    assert device["serial_number"]  # redacted placeholder, but present
    assert set(info) == {"device_info", "system_time"}  # no temperature/fans


async def test_traffic_stats(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        await api.connect()
        stats = (await api.get_traffic_stats())["stats"]

    assert stats, "expected per-WAN traffic stats"
    assert {str(s["wan_id"]) for s in stats} <= {"1", "2", "3", "4", "5"}


# ---------------------------------------------------------------------------
# Commands and configuration changes
# ---------------------------------------------------------------------------

from peplink_api import PeplinkCommandError  # noqa: E402


def posted(m: aioresponses, path: str) -> list[dict]:
    """JSON bodies POSTed to /api/<path>."""
    from yarl import URL

    calls = m.requests.get(("POST", URL(f"{BASE}/api/{path}")), [])
    return [call.kwargs.get("json") for call in calls]


async def test_set_wan_enabled_posts_and_applies(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.post(f"{BASE}/api/config.wan.connection", payload={"stat": "ok", "response": {}})
        m.post(f"{BASE}/api/cmd.config.apply", payload={"stat": "ok"})
        await api.connect()
        await api.set_wan_enabled("3", True)
        assert posted(m, "config.wan.connection") == [
            {"action": "update", "list": [{"id": 3, "enable": True}]}
        ]
        assert len(posted(m, "cmd.config.apply")) == 1


async def test_set_routing_mode_posts_and_applies(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.post(f"{BASE}/api/config.wan.connection", payload={"stat": "ok", "response": {}})
        m.post(f"{BASE}/api/cmd.config.apply", payload={"stat": "ok"})
        await api.connect()
        await api.set_wan_routing_mode(4, "NAT")
        assert posted(m, "config.wan.connection") == [
            {"action": "update", "list": [{"id": 4, "connection": {"routingMode": "NAT"}}]}
        ]
        assert len(posted(m, "cmd.config.apply")) == 1


async def test_set_wan_priorities_one_change_then_apply(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.post(f"{BASE}/api/config.wan.connection", payload={"stat": "ok", "response": {}})
        m.post(f"{BASE}/api/cmd.config.apply", payload={"stat": "ok"})
        await api.connect()
        await api.set_wan_priorities({"1": 4, 2: 3})
        assert posted(m, "config.wan.connection") == [
            {"action": "update", "list": [
                {"id": 1, "connection": {"priority": 4}},
                {"id": 2, "connection": {"priority": 3}},
            ]}
        ]
        assert len(posted(m, "cmd.config.apply")) == 1


@pytest.mark.parametrize("bad", [{}, {1: 0}, {1: 5}, {1: "3"}, {1: True}])
async def test_set_wan_priorities_rejects_bad_values(api: PeplinkAPI, bad) -> None:
    with pytest.raises(ValueError):
        await api.set_wan_priorities(bad)


async def test_routing_mode_rejects_unknown_value(api: PeplinkAPI) -> None:
    with pytest.raises(ValueError):
        await api.set_wan_routing_mode(4, "Bridge")


async def test_failed_change_raises_and_skips_apply(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.post(f"{BASE}/api/config.wan.connection",
               payload={"stat": "fail", "code": 400, "message": "Invalid id"})
        await api.connect()
        with pytest.raises(PeplinkCommandError, match="Invalid id"):
            await api.set_wan_enabled(9, False)
        assert posted(m, "cmd.config.apply") == []


async def test_one_shot_commands(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        for path in ("cmd.cellularModule.reset", "cmd.cellularModule.rescanNetwork",
                     "cmd.system.reboot", "cmd.starlink"):
            m.post(f"{BASE}/api/{path}", payload={"stat": "ok"}, repeat=True)
        await api.connect()
        await api.reset_cellular_module("2")
        await api.rescan_cellular_network(2)
        await api.reboot()
        await api.starlink_command(1, "stow")
        assert posted(m, "cmd.cellularModule.reset") == [{"connId": 2}]
        assert posted(m, "cmd.cellularModule.rescanNetwork") == [{"connId": 2}]
        assert posted(m, "cmd.system.reboot") == [{}]
        assert posted(m, "cmd.starlink") == [{"connId": 1, "action": "stow"}]
        with pytest.raises(ValueError):
            await api.starlink_command(1, "launch")


async def test_wifi_scan_connect_result_forget(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.get(re.compile(rf"^{re.escape(BASE)}/api/cmd\.wifi\.scan\?.*connId=4.*$"),
              payload={"stat": "ok", "response": [{"ssid": "Camp", "securityPolicy": "wpa-psk"}]})
        m.post(f"{BASE}/api/cmd.wifi.connect", payload={"stat": "ok"})
        m.get(re.compile(rf"^{re.escape(BASE)}/api/cmd\.wifi\.result\?connId=4$"),
              payload={"stat": "ok", "response": {"result": "PSK_AUTH_FAIL", "ssid": "Camp"}})
        m.post(f"{BASE}/api/cmd.wifi.forget", payload={"stat": "ok"})
        m.post(f"{BASE}/api/cmd.wifi.disconnect", payload={"stat": "ok"})
        await api.connect()
        assert (await api.wifi_scan(4))[0]["ssid"] == "Camp"
        await api.wifi_connect(4, "Camp", "pw", "wpa-psk")
        assert posted(m, "cmd.wifi.connect")[0]["security"]["policy"]["detail"]["key"] == "pw"
        assert (await api.wifi_result("4"))["result"] == "PSK_AUTH_FAIL"
        await api.wifi_forget(4, "Camp", "wpa-psk")
        assert posted(m, "cmd.wifi.forget") == [{"connId": 4, "ssid": "Camp", "securityPolicy": "wpa-psk"}]
        await api.wifi_disconnect(4)
        assert posted(m, "cmd.wifi.disconnect") == [{"connId": 4}]


async def test_saved_wifi_from_wan_config(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.get(f"{BASE}/api/config.wan.connection", payload=load(BR1_MINI, "config.wan.connection"))
        await api.connect()
        config = await api.get_wan_config()
        assert "4" in config and config["4"]["wifi"]["wifiConnectionProfile"]["order"] == [2, 1]


# ---------------------------------------------------------------------------
# Documented endpoints first
# ---------------------------------------------------------------------------


async def test_location_uses_documented_endpoint_only(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.get(f"{BASE}/api/info.location", payload=load(BR1_MINI, "info.location"))
        await api.connect()
        loc = await api.get_location()
        assert loc["gps"] is True
        assert loc["location"]["latitude"] == 40.0
        assert not [k for k in m.requests if "cgi-bin" in str(k[1])]


async def test_location_failure_means_no_gps(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.get(f"{BASE}/api/info.location", payload={"stat": "fail", "code": 404, "message": "Not found"})
        await api.connect()
        assert (await api.get_location())["gps"] is False


async def test_cellular_module_temperature(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.post(f"{BASE}/api/status.cellularModule.temperature",
               payload={"stat": "ok", "response": {"temperature": 41}})
        await api.connect()
        assert await api.get_cellular_module_temperature(2) == 41.0
        sent = next(v for k, v in m.requests.items() if "cellularModule" in str(k[1]))
        assert sent[0].kwargs["json"] == {"connId": 2}


async def test_cellular_module_temperature_failure(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.post(f"{BASE}/api/status.cellularModule.temperature",
               payload={"stat": "fail", "code": 400, "message": "Module not ready"})
        await api.connect()
        with pytest.raises(PeplinkCommandError):
            await api.get_cellular_module_temperature(2)


async def test_firmware_version_from_info_firmware(api: PeplinkAPI) -> None:
    with aioresponses() as m:
        mock_router(m, BR1_MINI)
        m.get(f"{BASE}/api/info.firmware", payload=load(BR1_MINI, "info.firmware"))
        await api.connect()
        assert await api.get_firmware_version() == "8.5.2s150 build 5332"
