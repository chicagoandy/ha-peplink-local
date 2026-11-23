"""The Peplink Router integration."""
from __future__ import annotations

import asyncio
import logging
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import clients as client_util
from . import wan_data
from . import wifi as wifi_util
from .const import (
    DOMAIN,
    EVENT_NEW_CLIENT,
    CONF_POLL_FREQUENCY,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    API_CLIENT_NAME,
    DEFAULT_POLL_FREQUENCY,
    SCAN_INTERVAL,
)
from .peplink_api import PeplinkAPI, PeplinkAuthFailed, PeplinkCommandError

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
    """Set up the Peplink Router component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Peplink Router from a config entry."""
    host = entry.data[CONF_HOST]
    # Options (set via the options flow) override the values entered at setup.
    settings = {**entry.data, **entry.options}
    verify_ssl = settings.get(CONF_VERIFY_SSL, True)
    poll_frequency = settings.get(CONF_POLL_FREQUENCY, DEFAULT_POLL_FREQUENCY)

    session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    try:
        # Entries made before 1.0.0-beta7 stored the admin login: swap it for
        # an API client on the router, then delete the stored password.
        if not entry.data.get(CONF_CLIENT_ID) and entry.data.get(CONF_PASSWORD):
            await _async_replace_admin_login(hass, entry, session, verify_ssl)

        api = PeplinkAPI(
            host=host,
            session=session,
            verify_ssl=verify_ssl,
            client_id=entry.data.get(CONF_CLIENT_ID),
            client_secret=entry.data.get(CONF_CLIENT_SECRET),
        )
        if not api.uses_token:
            raise ConfigEntryAuthFailed("No API client stored; sign in again")

        if not await api.connect():
            raise ConfigEntryNotReady(f"Router at {host} is not reachable")

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

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Reload when options change so a new poll frequency / SSL setting applies.
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

        return True

    except (PeplinkAuthFailed, ConfigEntryAuthFailed) as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ConfigEntryNotReady:
        raise
    except Exception as err:
        _LOGGER.exception("Error setting up Peplink integration")
        raise ConfigEntryNotReady(f"Failed to connect: {err}") from err


async def _async_replace_admin_login(
    hass: HomeAssistant, entry: ConfigEntry, session, verify_ssl: bool
) -> None:
    """Create an API client with the stored admin login, then forget the login."""
    admin = PeplinkAPI(
        host=entry.data[CONF_HOST],
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        session=session,
        verify_ssl=verify_ssl,
    )
    if not await admin.connect():
        raise ConfigEntryNotReady("Router not reachable to create its API client")
    try:
        client_id, client_secret = await admin.create_api_client(API_CLIENT_NAME)
    except PeplinkCommandError as err:
        raise ConfigEntryAuthFailed(f"Could not create an API client: {err}") from err
    finally:
        await admin.logout()
    data = {
        k: v for k, v in entry.data.items() if k not in (CONF_USERNAME, CONF_PASSWORD)
    }
    data.update({CONF_CLIENT_ID: client_id, CONF_CLIENT_SECRET: client_secret})
    hass.config_entries.async_update_entry(entry, data=data)
    _LOGGER.info(
        "Peplink %s: created API client '%s'; the admin password is no longer stored",
        entry.data[CONF_HOST], API_CLIENT_NAME,
    )


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
        self._optional_failing: set[str] = set()
        self._firmware_at: float | None = None
        self._documented_firmware: str | None = None
        self._modem_temps: dict[str, float] = {}
        self._modem_temps_at: float | None = None
        self._system_info: dict[str, Any] = {}
        self._system_info_at: float | None = None
        # MACs seen before, persisted so restarts don't re-announce them.
        self._client_store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.known_clients"
        )
        self._known_macs: set[str] | None = None
        # Wi-Fi WAN scanning/joining (button, select and sensor platforms)
        self.wifi_networks: dict[str, list[dict[str, Any]]] = {}  # wan_id -> scan
        self.wifi_scanned_at: dict[str, Any] = {}
        self.wifi_selected: dict[str, str] = {}  # wan_id -> chosen SSID
        # wan_id -> (password, monotonic time); memory only, never state
        self.wifi_pending_password: dict[str, tuple[str, float]] = {}
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
        """Fire peplink_router_new_client for each MAC never seen before.

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

    def _optional(self, name: str, result: Any, empty: Any) -> Any:
        """Result of an optional call, or `empty` if it failed.

        Logs a warning the first time a source fails and a note when it
        recovers, instead of failing the whole update.
        """
        failed = isinstance(result, Exception) or not result
        if failed:
            if name not in self._optional_failing:
                self._optional_failing.add(name)
                _LOGGER.warning(
                    "Peplink %s unavailable (%s); only its sensors are affected",
                    name, result if isinstance(result, Exception) else "empty response",
                )
            return empty
        if name in self._optional_failing:
            self._optional_failing.discard(name)
            _LOGGER.info("Peplink %s available again", name)
        return result

    async def _async_add_modem_temperatures(self, wan_status: dict[str, Any]) -> dict[str, Any]:
        """Add each cellular WAN's modem temperature (documented, fw 8.4.1+).

        Read once a minute; temperature changes slowly. A WAN whose read fails
        (modem off, older firmware) just has no value.
        """
        connections = wan_status.get("connection", [])
        if self._modem_temps_at is None or time.monotonic() - self._modem_temps_at > 60:
            self._modem_temps_at = time.monotonic()
            temps: dict[str, float] = {}
            for conn in connections:
                if not wan_data.is_cellular(conn) or not conn.get("enable", True):
                    continue
                try:
                    value = await self.api.get_cellular_module_temperature(conn["id"])
                except Exception as err:
                    _LOGGER.debug("Modem temperature for WAN %s unavailable: %s", conn.get("id"), err)
                    continue
                if value is not None:
                    temps[str(conn["id"])] = value
            self._modem_temps = temps
        return {
            **wan_status,
            "connection": [
                {**conn, "modemTemperature": self._modem_temps[str(conn.get("id"))]}
                if str(conn.get("id")) in self._modem_temps else conn
                for conn in connections
            ],
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via API."""
        try:
            # Ensure we're connected
            if not await self.api.ensure_connected():
                raise UpdateFailed("Failed to connect to Peplink router")

            # Documented API (required): WAN status and clients. If either
            # fails, the update fails.
            # Optional: GPS (documented info.location; some models have no GPS)
            # and the undocumented web-UI calls for system info and traffic,
            # which have no documented equivalent. A failed optional call only
            # empties its own sensors.
            # System info (model, serial, name) changes
            # rarely: read it hourly, retry after 5 min if it fails.
            now = time.monotonic()
            system_info_due = (
                self._system_info_at is None or now - self._system_info_at > 3600
            )

            async def _cached_system_info() -> dict[str, Any]:
                return self._system_info

            results = await asyncio.gather(
                self.api.get_wan_status(),
                self.api.get_clients(),
                self.api.get_system_info() if system_info_due else _cached_system_info(),
                self.api.get_traffic_stats(),
                self.api.get_location(),
                return_exceptions=True,
            )
            wan_status, clients, system_info, traffic_stats, location_info = results

            for name, result in (("WAN status", wan_status), ("client information", clients)):
                if isinstance(result, PeplinkAuthFailed):
                    raise result
                if isinstance(result, Exception):
                    raise UpdateFailed(f"Failed to get {name}: {result}") from result
            if not wan_status:
                raise UpdateFailed("Failed to get WAN status")
            if not isinstance(clients, dict):
                clients = {"client": []}

            system_info = self._optional("system information", system_info, {})
            if system_info_due:
                if system_info:
                    self._system_info = system_info
                    self._system_info_at = now
                else:
                    self._system_info_at = now - 3600 + 300
            traffic_stats = self._optional("traffic statistics", traffic_stats, {"stats": []})
            location_info = self._optional(
                "location", location_info, {"gps": False, "type": "Unknown", "location": {}}
            )

            device_info = {"device_info": system_info.get("device_info", {})}
            system_time = system_info.get("system_time", {})

            # Firmware version: documented info.firmware only (hourly).
            if self._firmware_at is None or time.monotonic() - self._firmware_at > 3600:
                try:
                    version = await self.api.get_firmware_version()
                except Exception as err:
                    _LOGGER.debug("info.firmware unavailable: %s", err)
                    version = None
                self._firmware_at = time.monotonic()
                if version:
                    self._documented_firmware = version

            self._announce_new_clients(clients.get("client", []))
            if self._wifi_watch:
                await self._async_poll_wifi_results()
            # Saved networks change rarely: at start, every 10 min, and after
            # a join/forget (the platforms call async_refresh_wifi_saved).
            if self._wifi_saved_at is None or time.monotonic() - self._wifi_saved_at > 600:
                await self.async_refresh_wifi_saved()

            # Update model and firmware information if available
            device_info_data = device_info.get("device_info", {})
            for key, attr in (
                ("model", "model"),
                ("name", "device_name"),
                ("serial_number", "serial_number"),
                ("product_code", "product_code"),
                ("hardware_revision", "hardware_revision"),
            ):
                if device_info_data.get(key):
                    setattr(self, attr, device_info_data[key])
            if self._documented_firmware:
                self.firmware = self._documented_firmware
                device_info_data = {**device_info_data, "firmware_version": self._documented_firmware}

            wan_status = await self._async_add_modem_temperatures(wan_status)

            # Combine all data into a single data structure
            return {
                "wan_status": wan_status,
                "clients": clients,
                "traffic_stats": traffic_stats,
                "device_info": device_info_data,
                "system_time": system_time,
                "location_info": location_info
            }
                
        except PeplinkAuthFailed as err:
            # API client removed on the router (or its secret changed): ask
            # the user to sign in again (reauth flow).
            raise ConfigEntryAuthFailed(str(err)) from err
        except UpdateFailed:
            raise
        except Exception as e:
            _LOGGER.exception("Failed to update data: %s", e)
            raise UpdateFailed(f"Failed to update data: {e}") from e
