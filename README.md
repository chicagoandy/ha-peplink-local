# Peplink Router for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)

Monitor and control a Peplink / Pepwave router from Home Assistant over its local API: WAN status and cellular signal, traffic, connected clients, GPS, WAN and routing-mode controls, and Wi-Fi WAN scanning and joining.

> **Origin:** Peplink Router began as a fork of [Peplink Local](https://github.com/weirded/ha-peplink-local) by Stefan Zier (Apache 2.0, see [NOTICE](NOTICE)) and is now developed separately. It uses its own integration domain (`peplink_router`), so it can be installed alongside or instead of Peplink Local without clashing.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chicagoandy&repository=ha-peplink-router&category=integration)

## Features

- **Local Communication**: Communicates directly with your Peplink router over your local network
- **WAN Status Sensors**: 
  - Connection Status (connectivity sensor)
  - Download Rate (data rate sensor)
  - Upload Rate (data rate sensor)
  - WAN Type (text sensor)
  - WAN Name (text sensor)
  - WAN IP Address (text sensor)
  - WAN Up Since (timestamp sensor)
- **Router Sensors**:
  - Fan Speed Sensors (speed sensors, dynamically created for each fan)
  - System Temperature (temperature sensor)
  - System Temperature Threshold (temperature sensor) 
  - Serial Number (diagnostic sensor)
  - Device Name (diagnostic sensor)
  - Firmware Version (diagnostic sensor)
- **GPS Tracking** (for GPS-enabled Peplink routers):
  - Device Tracker Entity (for mapping and zone detection)
  - Speed Sensor (meters per second)
  - Altitude Sensor (meters)
  - Heading Sensor (degrees)
- **Connected Clients**: one sensor with the number of active clients and the full list (name, IP, MAC, connection type, SSID, signal) as its `clients` attribute
- **Client Tracking (opt-in)**: pick clients under **Configure → Clients to track** to get a device tracker for each, e.g. phones for presence or devices with no integration of their own
- **New Client Event**: `peplink_router_new_client` fires when a device that has never been seen before joins the network (event data: `mac`, `name`, `ip`, `connection_type`, `ssid`), handy for "unknown device" alerts
- **Traffic Statistics**:
  - WAN Download (data rate sensor)
  - WAN Upload (data rate sensor)
  - Total Download (data rate sensor)
  - Total Upload (data rate sensor)
- **Controls** (they change the router's configuration):
  - **Enabled** switch per WAN (`config.wan.connection`, then apply)
  - **Routing Mode** select per WAN: NAT or IP Forwarding (then apply)
  - **Reset Cellular Module** and **Rescan Cellular Network** buttons for cellular WANs
  - **Reboot** button for the router
  - **Reboot / Stow / Unstow Starlink** buttons for a WAN named "Starlink" (firmware 8.5.0+, needs a Care Plan)
- **Wi-Fi WAN networks** (for each Wi-Fi WAN; the controls are unavailable while that WAN is disabled):
  - **Scan Networks** button, **Nearby Networks** sensor (count; list with signal, channel, security, saved)
  - **Saved Networks** sensor (count; list with security, auto-connect and priority; passwords are never shown)
  - **Network** select: networks in range and saved networks. Choosing one does not join it
  - **Password** box and **Join** button: choose a network, type its password if it needs one (Enter joins right away), or just press Join for open and saved networks. The box is write-only: it always reads empty and the password is kept in memory only
  - **Forget** and **Disconnect Wi-Fi** buttons
  - **Last Join Result** sensor: Connected / Wrong password / Network not found / Timed out
  - **Configure → Join Wi-Fi network**: a form with the scanned and saved networks (or type a name) and a password box; nothing is stored in Home Assistant
  - Actions `peplink_router.join_wifi` (with password, for new networks) and `peplink_router.forget_wifi`, targeting the Network select
- **Configurable Polling**: Adjust how frequently the integration polls your router (default: 5 seconds)

## Requirements

- The router must be accessible on your local network
- Valid admin credentials for your Peplink router
- Peplink firmware version 8.1.1 or higher

## Installation

### Moving from Peplink Local

Peplink Router is a separate integration. Remove Peplink Local first (Settings → Devices & services → Peplink Local → Delete, then remove it in HACS), restart, then install Peplink Router below. Entity IDs come from the router and WAN names, so with Peplink Local's entities gone they come back the same.

### HACS Installation (Recommended)

1. Ensure [HACS](https://hacs.xyz/) is installed in your Home Assistant instance
2. Add this repository as a custom repository in HACS:
   - Go to HACS > Integrations > ⋮ > Custom repositories
   - Add `https://github.com/chicagoandy/ha-peplink-router` as a repository
   - Select "Integration" as the category
3. Click "Install" on the Peplink Router integration
4. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [GitHub repository](https://github.com/chicagoandy/ha-peplink-router)
2. Extract the `custom_components/peplink_router` directory into your Home Assistant's `custom_components` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "Peplink Router"
3. Enter the following information:
   - **IP Address**: The IP address of your Peplink router
   - **Username**: Admin username for your Peplink router
   - **Password**: Admin password for your Peplink router
   - **Verify SSL** (optional): Disable this if your router uses a self-signed certificate
   - **Polling Frequency** (optional): How often to poll the router for updates (in seconds, default: 5)
4. Click "Submit"

## GPS Features

The GPS tracking features are automatically enabled when your Peplink router has GPS capabilities. This is particularly useful for mobile routers like MAX Transit or MAX BR1 series with cellular connectivity.

- **Device Tracker**: The router's GPS location will appear on your Home Assistant map and can be used with zones
- **Sensors**: Speed, altitude and heading sensors provide additional data about the router's movement and orientation
- **Accuracy**: GPS accuracy is calculated based on horizontal dilution of precision (HDOP) when available

## Contributing

Contributions to improve the integration are welcome! Please feel free to submit a pull request or open an issue on the [GitHub repository](https://github.com/chicagoandy/ha-peplink-router).

## License

This project is licensed under the Apache License 2.0 - see the LICENSE file for details.

[releases-shield]: https://img.shields.io/github/v/release/chicagoandy/ha-peplink-router?include_prereleases&style=for-the-badge&cacheSeconds=3600
[releases]: https://github.com/chicagoandy/ha-peplink-router/releases
[license-shield]: https://img.shields.io/github/license/chicagoandy/ha-peplink-router?style=for-the-badge&cacheSeconds=3600
