"""Device tracker platform for Peplink Local integration."""
import logging
from typing import Any, Dict, Optional

from homeassistant.components.device_tracker.const import SourceType
from homeassistant.components.device_tracker import ScannerEntity, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import clients as client_util
from .const import (
    CONF_TRACKED_CLIENTS,
    DOMAIN,
    ATTR_CLIENT_NAME,
    ATTR_CLIENT_MAC,
    ATTR_CLIENT_IP,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    """Set up Peplink device tracker based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    _LOGGER.debug("Setting up Peplink device trackers for entry: %s", entry.entry_id)
    
    # Wait for coordinator to get data
    await coordinator.async_config_entry_first_refresh()
    
    entities = []
    
    # Add GPS device tracker for the router itself
    location_info = coordinator.data.get("location_info", {})
    has_gps = location_info.get("gps", False)
    _LOGGER.debug("GPS capability check for device tracker: %s (has_gps=%s)", location_info, has_gps)
    
    if has_gps:
        location_data = location_info.get("location", {})
        if location_data and "latitude" in location_data and "longitude" in location_data:
            _LOGGER.debug("Creating GPS device tracker for Peplink router")
            entities.append(
                PeplinkGPSTracker(
                    coordinator=coordinator, 
                    config_entry_id=entry.entry_id
                )
            )
        else:
            _LOGGER.debug("Router has GPS capability but no valid location data")
    else:
        _LOGGER.debug("Router does not have GPS capability - skipping GPS tracker")
    
    # Client trackers only for the MACs chosen in the integration options
    # (Configure -> Clients to track). Every connected client is listed in the
    # Connected Clients sensor regardless.
    tracked = {
        client_util.norm_mac(m) for m in entry.options.get(CONF_TRACKED_CLIENTS, [])
    }
    device_registry = dr.async_get(hass)
    _async_remove_untracked_clients(hass, entry, tracked)

    def _client_by_mac() -> dict[str, dict]:
        router_clients = (coordinator.data or {}).get("clients", {}).get("client", [])
        return {
            client_util.norm_mac(c["mac"]): c
            for c in router_clients
            if client_util.is_mac(c.get("mac"))
        }

    current = _client_by_mac()
    for mac in sorted(tracked):
        client = current.get(mac, {})
        # If another integration already has a device for this MAC
        # (ESPHome, WLED, a phone app...), join it without renaming it.
        existing = device_registry.async_get_device(
            connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(mac))}
        )
        entities.append(
            PeplinkClientTracker(
                coordinator,
                entry.entry_id,
                client_util.client_name(client),
                mac,
                name_device=existing is None,
            )
        )

    if entities:
        _LOGGER.debug("Adding %d Peplink device trackers", len(entities))
        async_add_entities(entities)


@callback
def _async_remove_untracked_clients(
    hass: HomeAssistant, entry: ConfigEntry, tracked: set[str]
) -> None:
    """Drop client trackers (and our link to their devices) no longer selected."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity_entry.domain != "device_tracker" or not client_util.is_mac(entity_entry.unique_id):
            continue
        if client_util.norm_mac(entity_entry.unique_id) in tracked:
            continue
        _LOGGER.debug("Removing untracked client tracker %s", entity_entry.entity_id)
        entity_registry.async_remove(entity_entry.entity_id)
        if not entity_entry.device_id:
            continue
        still_ours = [
            e for e in er.async_entries_for_device(entity_registry, entity_entry.device_id)
            if e.config_entry_id == entry.entry_id
        ]
        device = device_registry.async_get(entity_entry.device_id)
        if device and not still_ours and entry.entry_id in device.config_entries:
            device_registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )

class PeplinkGPSTracker(CoordinatorEntity, TrackerEntity):
    """Representation of the Peplink router GPS location."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
    ):
        """Initialize the GPS tracker."""
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self._attr_unique_id = f"{config_entry_id}_gps"
        self._attr_name = "GPS Location"
        self._latitude = None
        self._longitude = None
        self._attributes = {}
        
        # Update initial state
        self._update_gps_data()
    
    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Peplink router."""
        # Get the coordinator
        coordinator = self.coordinator
        
        # Use device name from API if available
        device_name = coordinator.device_name if hasattr(coordinator, "device_name") and coordinator.device_name else f"Peplink {coordinator.host}" if hasattr(coordinator, "host") else "Peplink"
        
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry_id)},
            manufacturer="Peplink",
            model=coordinator.model if hasattr(coordinator, "model") else "Router",
            name=device_name,
            sw_version=coordinator.firmware if hasattr(coordinator, "firmware") else None,
        )

    @property
    def source_type(self) -> str:
        """Return the source type of the device."""
        return SourceType.GPS
    
    @property
    def latitude(self) -> Optional[float]:
        """Return the latitude of the device."""
        return self._latitude
    
    @property
    def longitude(self) -> Optional[float]:
        """Return the longitude of the device."""
        return self._longitude
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return the device state attributes."""
        return self._attributes
    
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_gps_data()
        self.async_write_ha_state()
    
    def _update_gps_data(self) -> None:
        """Update GPS data from the coordinator."""
        self._latitude = None
        self._longitude = None
        self._attributes = {}
        
        if (
            self.coordinator.data
            and "location_info" in self.coordinator.data
            and "location" in self.coordinator.data["location_info"]
        ):
            location_data = self.coordinator.data["location_info"]["location"]
            self._latitude = location_data.get("latitude")
            self._longitude = location_data.get("longitude")
            
            # Add accuracy if available
            if "accuracy" in location_data:
                self._attributes["gps_accuracy"] = location_data["accuracy"]
                
            # Add other attributes that might be useful
            for key, value in location_data.items():
                if key not in ["latitude", "longitude", "accuracy"]:
                    self._attributes[key] = value


class PeplinkClientTracker(CoordinatorEntity, ScannerEntity):
    """Representation of a Peplink client device tracker.

    Naming follows Home Assistant's router-tracker convention: the entity has
    no name of its own and takes its device's name. The device is keyed by
    the MAC address and named after the client name the router reports. If
    another integration already has a device with this MAC (ESPHome, WLED, a
    phone...), the tracker joins that device and keeps its name. The MAC is
    the hidden unique ID, as for every router integration. Trackers exist
    only for the clients picked in the integration options.
    """

    _attr_has_entity_name = True
    _attr_name = None
    # Only clients picked in the options get a tracker, so enable them.
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        client_name: str | None,
        client_mac: str,
        name_device: bool = True,
    ):
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._client_name = client_name or client_mac
        self._client_mac = client_mac
        hostname = client_name if client_name and client_name != "Unknown Device" else None
        self._attr_hostname = hostname
        # Home Assistant 2027.11+ requires ScannerEntity device info carrying
        # the MAC. A device that already exists for this MAC keeps its name.
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(client_mac))}
        )
        if name_device:
            self._attr_device_info["name"] = hostname or client_mac
        self._is_connected = False
        self._ip_address = None
        self._mac_address = client_mac
        self._attributes = {}

        # Update initial state
        self._update_device_data()

    @property
    def source_type(self) -> str:
        """Return the source type of the device."""
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected to the network."""
        return self._is_connected

    @property
    def ip_address(self) -> Optional[str]:
        """Return the IP address of the device."""
        return self._ip_address

    @property
    def mac_address(self) -> Optional[str]:
        """Return the MAC address of the device."""
        return self._mac_address

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return the device state attributes."""
        return self._attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_device_data()
        self.async_write_ha_state()

    def _update_device_data(self) -> None:
        """Update device data from the coordinator."""
        self._is_connected = False
        self._ip_address = None
        self._attributes = {
            ATTR_CLIENT_NAME: self._client_name,
            ATTR_CLIENT_MAC: self._client_mac,
        }
        
        if (
            self.coordinator.data
            and "clients" in self.coordinator.data
            and "client" in self.coordinator.data["clients"]
        ):
            for client in self.coordinator.data["clients"]["client"]:
                if client_util.is_mac(client.get("mac")) and client_util.norm_mac(client["mac"]) == client_util.norm_mac(self._client_mac):
                    self._is_connected = client_util.is_active(client)
                    self._ip_address = client.get("ip")
                    
                    # Add all client attributes
                    for key, value in client.items():
                        if key not in ["mac", "name", "ip", "connected"]:
                            self._attributes[key] = value
