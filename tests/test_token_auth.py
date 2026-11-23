"""API-client (access token) sign-in: no admin password after setup."""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from test_api import BASE, BR1_MINI, HOST, load
from peplink_api import PeplinkAPI, PeplinkAuthFailed

TOKEN = "tok-1"


def any_query(path: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(BASE + path)}(\?.*)?$")


def cgi(func: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(BASE)}/cgi-bin/MANGA/api\.cgi\?(.*&)?func={re.escape(func)}(&.*)?$")


def grant(m: aioresponses, token: str = TOKEN, expires: int = 172800, repeat: bool = False) -> None:
    m.post(f"{BASE}/api/auth.token.grant",
           payload={"stat": "ok", "response": {"accessToken": token, "authorizationType": 3,
                                                "scope": "api", "expiresIn": expires}},
           repeat=repeat)


def sent(m: aioresponses):
    """(method, url) of every request made."""
    return [(k[0], str(k[1])) for k in m.requests]


@pytest.fixture
async def token_api():
    session = aiohttp.ClientSession()
    client = PeplinkAPI(host=HOST, session=session, verify_ssl=False,
                        client_id="cid", client_secret="secret")
    yield client
    await session.close()


async def test_requests_carry_token_not_cookie(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        grant(m)
        m.get(any_query("/api/status.wan"), payload=load(BR1_MINI, "status.wan.connection"))
        m.get(cgi("status.traffic"), payload=load(BR1_MINI, "cgi.status.traffic"))
        assert token_api.uses_token
        assert await token_api.connect()
        await token_api.get_wan_status()
        await token_api.get_traffic_stats()

        urls = sent(m)
        assert not any("/api/login" in u for _, u in urls)
        data_calls = [u for _, u in urls if "auth.token.grant" not in u]
        assert len(data_calls) == 2
        assert all(f"accessToken={TOKEN}" in u for u in data_calls)
        grant_call = next(v for k, v in m.requests.items() if "auth.token.grant" in str(k[1]))
        assert grant_call[0].kwargs["json"] == {"clientId": "cid", "clientSecret": "secret", "scope": "api"}
        for calls in m.requests.values():
            for call in calls:
                assert "Cookie" not in (call.kwargs.get("headers") or {})


async def test_post_commands_carry_token(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        grant(m)
        m.post(any_query("/api/status.cellularModule.temperature"),
               payload={"stat": "ok", "response": {"temperature": 42}})
        assert await token_api.get_cellular_module_temperature(2) == 42.0
        assert any(meth == "POST" and "cellularModule" in u and f"accessToken={TOKEN}" in u
                   for meth, u in sent(m))


async def test_expired_token_is_renewed_and_request_retried(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        grant(m, "old")
        grant(m, "new")
        m.get(any_query("/api/status.wan"), payload={"stat": "fail", "code": 401, "message": "Invalid access token"})
        m.get(any_query("/api/status.wan"), payload=load(BR1_MINI, "status.wan.connection"))
        result = await token_api.get_wan_status()
        assert result.get("connection")
        wan_urls = [u for _, u in sent(m) if "/api/status.wan" in u]
        assert any("accessToken=old" in u for u in wan_urls)
        assert any("accessToken=new" in u for u in wan_urls)


async def test_token_renewed_before_expiry(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        grant(m, "short", expires=0)
        grant(m, "fresh")
        m.get(any_query("/api/status.wan"), payload=load(BR1_MINI, "status.wan.connection"))
        assert await token_api.connect()
        await token_api.get_wan_status()
        wan_url = next(u for _, u in sent(m) if "/api/status.wan" in u)
        assert "accessToken=fresh" in wan_url


async def test_rejected_client_raises_auth_failed(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        m.post(f"{BASE}/api/auth.token.grant",
               payload={"stat": "fail", "code": 401, "message": "Invalid client"})
        with pytest.raises(PeplinkAuthFailed):
            await token_api.connect()


async def test_unreachable_router_is_not_an_auth_failure(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        m.post(f"{BASE}/api/auth.token.grant", exception=aiohttp.ClientConnectionError("down"))
        assert await token_api.connect() is False


async def test_create_api_client_with_admin_login() -> None:
    async with aiohttp.ClientSession() as session:
        admin = PeplinkAPI(host=HOST, username="admin", password="pw", session=session, verify_ssl=False)
        with aioresponses() as m:
            m.post(f"{BASE}/api/login", payload={"stat": "ok"},
                   headers={"Set-Cookie": "bauth=c; Path=/api"})
            m.get(f"{BASE}/api/status", payload={"stat": "ok"})
            m.post(f"{BASE}/api/auth.client",
                   payload={"stat": "ok", "response": {"clientId": "cid", "clientSecret": "sec",
                                                        "name": "Home Assistant", "scope": "api"}})
            m.post(f"{BASE}/api/logout", payload={"stat": "ok"})
            assert await admin.connect()
            assert await admin.create_api_client("Home Assistant") == ("cid", "sec")
            body = next(v for k, v in m.requests.items() if "auth.client" in str(k[1]))[0].kwargs["json"]
            assert body == {"action": "add", "name": "Home Assistant", "scope": "api"}
            await admin.logout()
            assert any("/api/logout" in u for _, u in sent(m))


async def test_token_is_redacted_from_errors(token_api: PeplinkAPI) -> None:
    with aioresponses() as m:
        grant(m)
        m.get(any_query("/api/status.wan"), status=500)
        assert await token_api.connect()
        with pytest.raises(Exception) as err:
            await token_api._make_api_request("status.wan", public_api=True)
        assert TOKEN not in str(err.value)


async def test_parallel_401s_renew_the_token_once(token_api: PeplinkAPI) -> None:
    import asyncio

    from aioresponses import CallbackResult

    def router(payload):
        def answer(url, **kwargs):
            if "accessToken=old" in str(url):
                return CallbackResult(payload={"stat": "fail", "code": 401, "message": "Invalid access token"})
            return CallbackResult(payload=payload)
        return answer

    with aioresponses() as m:
        grant(m, "old")
        grant(m, "new", repeat=True)
        m.get(any_query("/api/status.wan"), callback=router(load(BR1_MINI, "status.wan.connection")), repeat=True)
        m.get(any_query("/api/status.client"), callback=router(load(BR1_MINI, "status.client")), repeat=True)
        assert await token_api.connect()
        wan, clients = await asyncio.gather(token_api.get_wan_status(), token_api.get_clients())
        assert wan.get("connection") and clients.get("client") is not None
        grants = sum(len(v) for k, v in m.requests.items() if "auth.token.grant" in str(k[1]))
        assert grants == 2  # the first sign-in, then a single renewal for both requests
