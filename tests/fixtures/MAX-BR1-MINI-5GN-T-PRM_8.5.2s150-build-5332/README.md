# Peplink MAX BR1 Mini 5G, firmware 8.5.2s150 build 5332

Captured 2026-11-23 with `tests/capture_fixtures.py` and anonymised
(IMEI/IMSI/ICCID, EID, serial, MACs, SSIDs, client names, cell IDs, GPS).

WANs: 1 Starlink (Ethernet), 2 Google Fi (cellular, 5G SA), 3 Wi-Fi WAN 2.4 GHz
(disabled, so no `priority`), 4 Wi-Fi WAN 5 GHz (connected), 5 VLAN WAN 1 (disabled).
No fans (fanless model). GPS present. Bandwidth allowance monitor off on all WANs.

| File | Notes |
|---|---|
| `status.wan.connection.json` | Starlink up in hot standby (CGNAT IP), cellular standby, 5 GHz Wi-Fi WAN active |
| `status.wan.connection.starlink_down.json` | Earlier capture: Starlink dish powered off ("No Cable Detected", red LED, no IP) |
| `status.wan.json` | `/api/status.wan`, an alias returning the same shape as `status.wan.connection` |
| `cgi.*.json` | Web-UI endpoints (`/cgi-bin/MANGA/api.cgi`) the integration uses for device info, temperature, traffic and GPS |
