#!/usr/bin/env python3
"""Record Peplink API responses as anonymised test fixtures.

Run this on any computer on the router's LAN. It needs only Python 3.9+
(standard library, nothing to install):

    python3 tests/capture_fixtures.py --host 192.168.50.1 --username admin

You are prompted for the password (or set PEPLINK_PASSWORD). The script logs
in, makes read-only GET requests for every endpoint the integration uses or
will use, logs out, and writes one JSON file per request to
tests/fixtures/<model>_<firmware>/ plus a .zip of that folder.

Nothing is changed on the router. Identifying values are replaced before
anything is written: IMEI/IMSI/ICCID/MEID/ESN, serial numbers, MAC addresses,
BSSIDs, SSIDs, client names, cell tower IDs, GPS coordinates, public IP
addresses, and any username/password/key/token field. The same original value
always maps to the same placeholder, so relationships between files survive.
Please still skim the files before sharing them.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import ipaddress
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# (file name, path) — /api/... is the documented API, /cgi-bin/... is the
# web-UI API the integration also uses.
ENDPOINTS: list[tuple[str, str]] = [
    ("status.wan", "/api/status.wan"),
    ("status.wan.connection", "/api/status.wan.connection"),
    ("status.wan.connection.allowance", "/api/status.wan.connection.allowance"),
    ("status.client", "/api/status.client"),
    ("status.client.active", "/api/status.client?activeOnly=yes&infoType=signalStrength"),
    ("info.location", "/api/info.location"),
    ("info.firmware", "/api/info.firmware"),
    ("config.wan.connection", "/api/config.wan.connection"),
    ("cmd.ap", "/api/cmd.ap"),
    ("cgi.status.system.info",
     "/cgi-bin/MANGA/api.cgi?func=status.system.info"
     "&infoType=device%20systemTime"),
    ("cgi.status.traffic", "/cgi-bin/MANGA/api.cgi?func=status.traffic"),
    ("cgi.info.location", "/cgi-bin/MANGA/api.cgi?func=info.location"),
]

# Keys whose values are always replaced (compared case-insensitively).
SECRET_KEYS = {
    "password", "username", "psk", "passphrase", "secret", "key", "token",
    "accesstoken", "clientsecret", "apn_password", "wpakey", "sharedkey",
}
ID_KEYS = {
    "imei", "imsi", "iccid", "esn", "mtn", "serialnumber", "serial_number",
    "serial", "sn", "hex", "dec", "mac", "bssid", "macaddress",
    "cellid", "cellplmn", "cellutranid", "tac", "lac", "pci", "eci",
    "ssid", "essid", "dialnumber", "phonenumber", "eid", "simpin",
}
COORD_KEYS = {"latitude": 40.0, "longitude": -75.0, "altitude": 100.0}
CLIENT_NAME_PARENTS = {"list", "client"}  # "name" inside client lists

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


class Redactor:
    def __init__(self) -> None:
        self.maps: dict[str, dict[str, str]] = {}
        self.counts: dict[str, int] = {}

    def _placeholder(self, kind: str, value: object) -> object:
        table = self.maps.setdefault(kind, {})
        key = json.dumps(value, sort_keys=True)
        if key not in table:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            n = self.counts[kind]
            if isinstance(value, int) and not isinstance(value, bool):
                table[key] = 900000 + n  # keep numeric IDs numeric
            elif kind == "mac":
                table[key] = f"02:00:00:00:{n // 256:02x}:{n % 256:02x}"
            elif kind == "public_ip":
                table[key] = f"203.0.113.{n % 254 + 1}"
            else:
                table[key] = f"{kind}-{n}"
        return table[key]

    def value(self, key: str | None, value: object, parent: str | None) -> object:
        k = (key or "").lower()
        if isinstance(value, (dict, list)):
            return self.walk(value, key)
        if value is None or value == "":
            return value
        if k in COORD_KEYS:
            return COORD_KEYS[k]
        if k in SECRET_KEYS:
            return "REDACTED"
        if k in ID_KEYS or (isinstance(value, str) and MAC_RE.match(value)):
            kind = "mac" if isinstance(value, str) and MAC_RE.match(value) else k
            return self._placeholder(kind, value)
        if k in ("name", "hostname") and (parent or "").lower() in CLIENT_NAME_PARENTS:
            return self._placeholder("client", value)
        if isinstance(value, str):
            return self._ip(value)
        return value

    def _ip(self, text: str) -> str:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return text
        if ip.is_global:
            return self._placeholder("public_ip", text)
        return text

    def walk(self, obj: object, parent: str | None = None) -> object:
        if isinstance(obj, dict):
            return {k: self.value(k, v, parent) for k, v in obj.items()}
        if isinstance(obj, list):
            return [
                self.walk(v, parent) if isinstance(v, (dict, list))
                else self.value(parent, v, parent)
                for v in obj
            ]
        return obj


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, help="router LAN IP, e.g. 192.168.50.1")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--out", default=str(Path(__file__).parent / "fixtures"))
    args = ap.parse_args()
    password = os.environ.get("PEPLINK_PASSWORD") or getpass.getpass("Router password: ")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # routers use self-signed certificates
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )
    base = f"https://{args.host}"

    auth_cookie: dict[str, str] = {}

    def request(path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        # The login cookie is scoped to /api, so the cookie jar won't send it
        # to /cgi-bin. Send it explicitly, as the integration does.
        if "bauth" in auth_cookie:
            headers["Cookie"] = f"bauth={auth_cookie['bauth']}"
        if path.startswith("/cgi-bin/"):
            path += f"&_={int(time.time() * 1000)}"
        req = urllib.request.Request(base + path, data=data, headers=headers)
        with opener.open(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    login = request("/api/login", {"username": args.username, "password": password})
    if login.get("stat") != "ok":
        print(f"Login failed: {login.get('message') or login}", file=sys.stderr)
        return 1
    for cookie in jar:
        if cookie.name == "bauth":
            auth_cookie["bauth"] = cookie.value
    print("Logged in.")

    redactor = Redactor()
    results: dict[str, object] = {}
    try:
        for name, path in ENDPOINTS:
            try:
                results[name] = request(path)
                stat = results[name].get("stat") if isinstance(results[name], dict) else "?"
                print(f"  {name:36} {stat}")
            except Exception as err:  # keep going; record the failure
                results[name] = {"capture_error": str(err)}
                print(f"  {name:36} ERROR {err}")
    finally:
        try:
            request("/api/logout", {})
        except Exception:
            pass

    device = {}
    info = results.get("cgi.status.system.info")
    if isinstance(info, dict):
        device = (info.get("response") or {}).get("device") or {}
    model = re.sub(r"[^A-Za-z0-9]+", "-", device.get("productCode") or "router").strip("-")
    fw = re.sub(r"[^A-Za-z0-9.]+", "-", device.get("firmwareVersion") or "unknown").strip("-")

    folder = Path(args.out) / f"{model}_{fw}"
    folder.mkdir(parents=True, exist_ok=True)
    cleaned = {name: redactor.walk(payload) for name, payload in results.items()}
    # SSIDs also appear inside free text, e.g. "Connected to MyHomeWiFi".
    for kind in ("ssid", "essid"):
        for original, placeholder in redactor.maps.get(kind, {}).items():
            text = json.loads(original)
            if isinstance(text, str) and len(text) >= 3:
                cleaned = json.loads(json.dumps(cleaned).replace(text, placeholder))
    for name, clean in cleaned.items():
        (folder / f"{name}.json").write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n")

    archive = shutil.make_archive(str(folder), "zip", folder)
    print(f"\nWrote {len(results)} files to {folder}")
    print(f"Archive: {archive}")
    print("Replaced:", ", ".join(f"{k} x{v}" for k, v in sorted(redactor.counts.items())) or "nothing")
    print("Skim the files before sharing them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
