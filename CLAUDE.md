# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a Home Assistant custom integration (`peplink_router`) for monitoring Peplink routers via their local API. It is distributed via HACS and requires Home Assistant 2025.1.0+.

## Dependencies

All `homeassistant.*` imports can be assumed to exist at runtime — do not attempt to install or verify them locally. The integration runs inside Home Assistant which provides these packages.

## Development Commands

**Run the standalone API test against a real router:**
```bash
# Copy and configure .env first
cp .env.example .env  # or let standalone_test.py generate it
# Then run:
bash tests/run_api_test.sh
# Or directly:
python3 tests/standalone_test.py
```

The test script requires a `.env` file in the project root with:
```
PEPLINK_ROUTER_IP=192.168.1.1
PEPLINK_USERNAME=admin
PEPLINK_PASSWORD=your_password
PEPLINK_VERIFY_SSL=false
```

Test output (raw JSON responses from each API endpoint) is saved to `tests/output/`.

## Architecture

### Data Flow

`PeplinkAPI` (peplink_api.py) → `PeplinkDataUpdateCoordinator` (__init__.py) → platform entities (sensor.py, binary_sensor.py, device_tracker.py)

The coordinator calls five API methods in parallel via `asyncio.gather`:
1. `get_wan_status()` → WAN connection info
2. `get_clients()` → connected client devices
3. `get_system_info()` → combined call returning device info, thermal sensors, fan speeds, system time
4. `get_traffic_stats()` → per-WAN bandwidth rates
5. `get_location()` → GPS data from documented `/api/info.location` (returns `gps: false` if the router has no GPS)

All coordinator data is stored under `hass.data[DOMAIN][entry_id]["coordinator"].data` as a flat dict with keys: `wan_status`, `clients`, `thermal_sensors`, `fan_speeds`, `traffic_stats`, `device_info`, `system_time`, `location_info`.

### API Layer (peplink_api.py)

Two API bases are used. Prefer the documented one wherever it has the data:
- **Documented API**: `https://{host}/api/` — authentication (`/api/login`), WAN status, clients, GPS (`/api/info.location`), firmware version (`/api/info.firmware`). Where a documented endpoint exists, the web-UI API is not used for that data, not even as a fallback, Wi-Fi WAN and configuration commands. WAN status and clients are required: if they fail, the update fails.
- **Undocumented web-UI API**: `https://{host}/cgi-bin/MANGA/api.cgi?func=<function>&_=<timestamp>` — only for data with no documented endpoint: traffic rates (`status.traffic`) and model/serial/temperature/fans (`status.system.info`). Peplink's API docs (through firmware 8.6.0) have no equivalent. These calls are optional: a failure empties only their sensors and logs one warning (`PeplinkDataUpdateCoordinator._optional`).

Authentication uses cookie-based sessions (`bauth` cookie). The `_` parameter in CGI requests is the current Unix timestamp in milliseconds.

### Entity Model

Each WAN connection creates a sub-device (via `via_device`) linked to the main router device. WAN entities use `identifiers={(DOMAIN, f"{entry_id}_wan{wan_id}")}`.

- **sensor.py**: Defines `SENSOR_TYPES` tuple of `PeplinkSensorEntityDescription` (with `value_fn` lambdas). Static sensors (temperature, device info, GPS, fans) use `PeplinkSensor`. Per-WAN sensors use `PeplinkWANSensor`, which routes traffic data from `traffic_stats` and connection data from `wan_status`.
- **binary_sensor.py**: One `connection_status` binary sensor per enabled WAN, checking if `message.startswith("Connected")`.
- **device_tracker.py**: `PeplinkClientTracker` (one per client MAC) + optional `PeplinkGPSTracker` if GPS is available.

### Key Data Structure Notes

- `wan_status["connection"]` is a list of dicts, each with `"id"` field (integer)
- `traffic_stats["stats"]` is a list with `"wan_id"` field — matched to WAN connections by string comparison
- `system_info` from `get_system_info()` combines four separate CGI calls into one dict
- GPS sensors/tracker are only created if `location_info["gps"] == True` and valid lat/lon exist
- Fan sensors are created dynamically (numbered fan_1, fan_2, etc.) based on what the router reports
