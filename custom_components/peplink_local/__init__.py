"""The Peplink Local integration."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import clients as client_util
from . import wifi as wifi_util
from .const import (
    DOMAIN,
    EVENT_NEW_CLIENT,
    CONF_POLL_FREQUENCY,
    DEFAULT_POLL_FREQUENCY,
    SCAN_INTERVAL,
)
from .peplink_api import PeplinkAPI, PeplinkAuthFailed

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Peplink Local component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Peplink Local from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    # Options (set via the options flow) override the values entered at setup.
    settings = {**entry.data, **entry.options}
    verify_ssl = settings.get(CONF_VERIFY_SSL, True)
    poll_frequency = settings.get(CONF_POLL_FREQUENCY, DEFAULT_POLL_FREQUENCY)

    session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    api = PeplinkAPI(
        host=host,
        username=username,
        password=password,
        session=session,
        verify_ssl=verify_ssl,
    )

    try:
        if not await api.connect():
            raise ConfigEntryAuthFailed("Failed to connect to Peplink router")

        coordinator = PeplinkDataUpdateCoordinator(
            hass=hass,
            logger=_LOGGER,
            name=f"Peplink {host}",
            update_interval=timedelta(seconds=poll_frequency),
            api=api,
            config_entry=entry,
        )

        await coordinator.async_load_known_clients()
        await coordinator.async_config_entry_first_refresh()

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "coordinator": coordinator,
            "api": api,
        }

        # Get device name from device info if available, otherwise use a generic name
        device_name = coordinator.device_name or f"Peplink Router ({host})"
        
        # Create model string with available info
        model_string = coordinator.model or "Router"
        if coordinator.product_code and coordinator.hardware_revision:
            model_string = f"{model_string} ({coordinator.product_code} HW {coordinator.hardware_revision})"
        elif coordinator.product_code:
            model_string = f"{model_string} ({coordinator.product_code})"
        elif coordinator.hardware_revision:
            model_string = f"{model_string} (HW {coordinator.hardware_revision})"
        
        device_registry = dr.async_get(hass)
        router_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer="Peplink",
            model=model_string,
            serial_number=coordinator.serial_number,
            sw_version=coordinator.firmware,
        )

        # WAN sub-devices link to the router through its registry ID.
        coordinator.router_device_id = router_device.id

        await _async_migrate_unique_ids(hass, entry, coordinator)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Reload when options change so a new poll frequency / SSL setting applies.
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

        return True

    except PeplinkAuthFailed as err:
        raise ConfigEntryAuthFailed from err
    except Exception as err:
        _LOGGER.exception("Error setting up Peplink integration")
        raise ConfigEntryNotReady(f"Failed to connect: {err}") from err


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        api = data["api"]
        await api.close()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored list of known clients when the integration is removed."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.known_clients").async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry after its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


class PeplinkDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta,
        api: PeplinkAPI,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
        )
        self.api = api
        self.config_entry = config_entry
        self.host = config_entry.data[CONF_HOST]
        self.model = "Router"  # Can be updated later if the API provides model info
        self.firmware = "Unknown"  # Can be updated later if the API provides firmware info
        self.device_name = None  # Can be updated later if the API provides device name
        self.serial_number = None  # Can be updated later if the API provides serial number
        self.product_code = None  # Can be updated later if the API provides product code
        self.hardware_revision = None  # Can be updated later if the API provides hardware revision
        self.router_device_id: str | None = None
        # MACs seen before, persisted so restarts don't re-announce them.
        self._client_store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.known_clients"
        )
        self._known_macs: set[str] | None = None
        # Wi-Fi WAN scanning/joining (button, select and sensor platforms)
        self.wifi_networks: dict[str, list[dict[str, Any]]] = {}  # wan_id -> scan
        self.wifi_scanned_at: dict[str, Any] = {}
        self.wifi_selected: dict[str, str] = {}  # wan_id -> chosen SSID
        self.wifi_results: dict[str, dict[str, Any]] = {}  # wan_id -> last result
        self._wifi_watch: dict[str, float] = {}  # wan_id -> monotonic deadline
        self.wifi_saved: dict[str, list[dict[str, Any]]] = {}  # wan_id -> profiles
        self._wifi_saved_at: float | None = None

    async def async_refresh_wifi_saved(self) -> None:
        """Re-read saved Wi-Fi networks (config.wan.connection)."""
        try:
            config = await self.api.get_wan_config()
        except Exception as err:
            _LOGGER.debug("Could not read saved Wi-Fi networks: %s", err)
            return
        self.wifi_saved = {
            str(wan_id): wifi_util.saved_networks(config.get(str(wan_id)))
            for wan_id in config.get("order", [])
            if (config.get(str(wan_id)) or {}).get("wifi") is not None
        }
        self._wifi_saved_at = time.monotonic()

    def watch_wifi_result(self, wan_id: str, seconds: float = 90) -> None:
        """Poll cmd.wifi.result for a while after a join (it takes 20-60 s)."""
        self._wifi_watch[wan_id] = time.monotonic() + seconds

    async def _async_poll_wifi_results(self) -> None:
        now = time.monotonic()
        for wan_id, deadline in list(self._wifi_watch.items()):
            if now > deadline:
                del self._wifi_watch[wan_id]
                continue
            try:
                self.wifi_results[wan_id] = await self.api.wifi_result(wan_id)
            except Exception as err:  # never fail the whole update for this
                _LOGGER.debug("Wi-Fi result for WAN %s unavailable: %s", wan_id, err)

    async def async_load_known_clients(self) -> None:
        """Load the MACs seen in earlier runs (None until the first save)."""
        data = await self._client_store.async_load()
        if data and isinstance(data.get("macs"), list):
            self._known_macs = set(data["macs"])

    def _announce_new_clients(self, client_list: list[dict[str, Any]]) -> None:
        """Fire peplink_local_new_client for each MAC never seen before.

        The very first poll after the integration is added only records the
        clients already on the network; nothing is announced for them.
        """
        current = client_util.macs(client_list)
        if self._known_macs is None:
            self._known_macs = current
        else:
            new = current - self._known_macs
            if not new:
                return
            for client in client_list:
                mac = client.get("mac")
                if client_util.is_mac(mac) and client_util.norm_mac(mac) in new:
                    self.hass.bus.async_fire(
                        EVENT_NEW_CLIENT,
                        {
                            "config_entry_id": self.config_entry.entry_id,
                            "mac": client_util.norm_mac(mac),
                            "name": client_util.client_name(client),
                            "ip": client.get("ip"),
                            "connection_type": client.get("connectionType"),
                            "ssid": client.get("essid"),
                        },
                    )
            self._known_macs |= new
        self._client_store.async_delay_save(
            lambda: {"macs": sorted(self._known_macs or [])}, 10
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via API."""
        try:
            # Ensure we're connected
            if not await self.api.ensure_connected():
                raise UpdateFailed("Failed to connect to Peplink router")

            # Parallelize API calls using asyncio.gather
            # Use the new combined system info API for device info, thermal sensors, and fan speeds
            results = await asyncio.gather(
                self.api.get_wan_status(),
                self.api.get_clients(),
                self.api.get_system_info(),  # Combined system info call
                self.api.get_traffic_stats(),
                self.api.get_location(),      # Get location data from GPS
                return_exceptions=True,
            )
            
            # Check results for exceptions
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    api_calls = ["WAN status", "client information", "system information", "traffic statistics", "location information"]
                    raise UpdateFailed(f"Failed to get {api_calls[i]}: {result}")
                
            # Unpack results
            wan_status, clients, system_info, traffic_stats, location_info = results
            
            # Extract components from the combined system_info call
            thermal_sensors = system_info.get("thermal_sensors", {"sensors": []})
            fan_speeds = system_info.get("fan_speeds", {"fans": []})
            device_info = {"device_info": system_info.get("device_info", {})} 
            system_time = system_info.get("system_time", {})
            
            # Validate results
            if not wan_status:
                raise UpdateFailed("Failed to get WAN status")
            if not clients:
                raise UpdateFailed("Failed to get client information")
            if not system_info:
                raise UpdateFailed("Failed to get system information")
            if not traffic_stats:
                raise UpdateFailed("Failed to get traffic statistics")
                
            self._announce_new_clients(clients.get("client", []))
            if self._wifi_watch:
                await self._async_poll_wifi_results()
            # Saved networks change rarely: at start, every 10 min, and after
            # a join/forget (the platforms call async_refresh_wifi_saved).
            if self._wifi_saved_at is None or time.monotonic() - self._wifi_saved_at > 600:
                await self.async_refresh_wifi_saved()

            # Update model and firmware information if available
            device_info_data = device_info.get("device_info", {})
            if device_info_data:
                if device_info_data.get("model"):
                    self.model = device_info_data.get("model")
                if device_info_data.get("firmware_version"):
                    self.firmware = device_info_data.get("firmware_version")
                if device_info_data.get("name"):
                    self.device_name = device_info_data.get("name")
                if device_info_data.get("serial_number"):
                    self.serial_number = device_info_data.get("serial_number") 
                if device_info_data.get("product_code"):
                    self.product_code = device_info_data.get("product_code")
                if device_info_data.get("hardware_revision"):
                    self.hardware_revision = device_info_data.get("hardware_revision")
                
            # Combine all data into a single data structure
            return {
                "wan_status": wan_status,
                "clients": clients,
                "thermal_sensors": thermal_sensors,
                "fan_speeds": fan_speeds,
                "traffic_stats": traffic_stats,
                "device_info": device_info_data,
                "system_time": system_time,
                "location_info": location_info
            }
                
        except Exception as e:
            _LOGGER.exception("Failed to update data: %s", e)
            raise UpdateFailed(f"Failed to update data: {e}") from e


_WAN_UNIQUE_ID = re.compile(r"^(?P<prefix>.+?)_wan(?P<wan>\d+)_(?P<key>.+)$")


async def _async_migrate_unique_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PeplinkDataUpdateCoordinator,
) -> None:
    """Move entities from host/name-based unique IDs to entry-ID-based ones.

    Up to 0.3.7 unique IDs started with the router's IP address (static and
    binary sensors, GPS tracker) or its device name (WAN sensors), so changing
    either orphaned every entity and created duplicates. They now start with
    the config entry ID. Entity IDs, names and history are kept.
    """
    entry_id = entry.entry_id
    host = entry.data[CONF_HOST]

    @callback
    def _migrate(entity_entry: er.RegistryEntry) -> dict[str, Any] | None:
        old = entity_entry.unique_id
        if old.startswith(f"{entry_id}_"):
            return None  # already migrated

        new: str | None = None
        if entity_entry.domain == "binary_sensor":
            suffix = f"_binary_{entry_id}"
            if old.endswith(suffix) and (m := _WAN_UNIQUE_ID.match(old[: -len(suffix)])):
                new = f"{entry_id}_wan{m['wan']}_{m['key']}_binary"
        elif entity_entry.domain == "sensor":
            # Only this entry's entities are visited, so any "<x>_wan<n>_<key>"
            # is ours, even if the router was renamed since it was created.
            if m := _WAN_UNIQUE_ID.match(old):
                new = f"{entry_id}_wan{m['wan']}_{m['key']}"
            elif old.startswith(f"{host}_"):
                new = f"{entry_id}_{old[len(host) + 1:]}"
        elif entity_entry.domain == "device_tracker" and old == f"{host}_gps":
            new = f"{entry_id}_gps"

        if new is None:
            return None
        _LOGGER.debug("Migrating unique_id %s -> %s", old, new)
        return {"new_unique_id": new}

    await er.async_migrate_entries(hass, entry_id, _migrate)

    # 0.4.0 beta: routing mode moved from a read-only sensor to a select.
    registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(registry, entry_id):
        if entity_entry.domain == "sensor" and entity_entry.unique_id.endswith("_routing_mode"):
            registry.async_remove(entity_entry.entity_id)
