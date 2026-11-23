"""Binary sensor platform for Peplink Router integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PeplinkDataUpdateCoordinator
from .const import DOMAIN
from .devices import wan_device_info

_LOGGER = logging.getLogger(__name__)


@dataclass
class PeplinkBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class describing Peplink binary sensor entities."""

    value_fn: Callable[[dict], Any] | None = None
    icon: str | None = None


BINARY_SENSOR_TYPES: tuple[PeplinkBinarySensorEntityDescription, ...] = (
    PeplinkBinarySensorEntityDescription(
        key="connection_status",
        translation_key=None,
        name="Connection Status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda x: x.get("message", "").startswith("Connected"),
        icon="mdi:network",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Peplink binary sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    entities = []
    
    # Get the WAN status data
    wan_status = coordinator.data.get("wan_status", {})
    wan_connections = wan_status.get("connection", [])
    
    # Add binary sensors for each WAN connection
    for connection in wan_connections:
        wan_id = str(connection.get("id", ""))
        wan_name = connection.get("name", f"WAN {wan_id}")
        
        # Skip disabled WANs
        if connection.get("enable") is False:
            continue
            
        device_info = wan_device_info(coordinator, connection)

        # Add all binary sensors
        for description in BINARY_SENSOR_TYPES:
            entities.append(
                PeplinkWANBinarySensor(
                    coordinator=coordinator,
                    description=description,
                    sensor_data=connection,
                    device_info=device_info,
                    wan_id=wan_id,
                )
            )
    
    async_add_entities(entities)


class PeplinkWANBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Implementation of a Peplink WAN binary sensor."""

    entity_description: PeplinkBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PeplinkDataUpdateCoordinator,
        description: PeplinkBinarySensorEntityDescription,
        sensor_data: dict[str, Any],
        device_info: DeviceInfo,
        wan_id: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)

        self.entity_description = description
        self._initial_sensor_data = sensor_data  # Keep reference to initial data as fallback
        self._wan_id = wan_id  # Store WAN ID to find the right data in updates
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_wan{wan_id}_{description.key}_binary"
        self._attr_device_info = device_info
        
        # Set custom icon if provided
        if description.icon:
            self._attr_icon = description.icon

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        if self.entity_description.value_fn is None:
            return None

        # Try to get fresh data from coordinator
        if self.coordinator.data:
            try:
                # For WAN sensors, get data from wan_status
                wan_status = self.coordinator.data.get("wan_status", {})
                connections = wan_status.get("connection", [])
                
                for connection in connections:
                    if str(connection.get("id", "")) == self._wan_id:
                        return self.entity_description.value_fn(connection)
            except Exception:
                # If anything goes wrong, fall back to initial data
                pass
                
        # Fall back to initial sensor data
        return self.entity_description.value_fn(self._initial_sensor_data)
