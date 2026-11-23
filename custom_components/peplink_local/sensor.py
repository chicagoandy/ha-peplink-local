"""Support for Peplink sensors."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable
import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    REVOLUTIONS_PER_MINUTE,
    UnitOfTemperature,
    UnitOfDataRate,
    UnitOfTime,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.variance import ignore_variance
from homeassistant.config_entries import ConfigEntry

from . import PeplinkDataUpdateCoordinator
from .const import DOMAIN
from . import wan_data

_LOGGER = logging.getLogger(__name__)


@dataclass
class PeplinkSensorEntityDescription(SensorEntityDescription):
    """Class describing Peplink sensor entities."""

    value_fn: Callable[[dict], Any] | None = None
    icon: str | None = None
    # Per-WAN sensors: only create for WANs this returns True for.
    wan_filter: Callable[[dict], bool] | None = None
    # Per-WAN sensors: skip if the value is missing when the WAN is set up.
    require_value: bool = False
    attrs_fn: Callable[[dict], dict[str, Any]] | None = None


@dataclass
class PeplinkBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class describing Peplink binary sensor entities."""

    value_fn: Callable[[dict], Any] | None = None
    icon: str | None = None


uptime_to_stable_datetime = ignore_variance(
    lambda value: dt_util.utcnow() - datetime.timedelta(seconds=value),
    datetime.timedelta(minutes=1),
)

SENSOR_TYPES: tuple[PeplinkSensorEntityDescription, ...] = (
    # System sensors
    PeplinkSensorEntityDescription(
        key="system_temperature",
        translation_key=None,
        name="System Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda x: x.get("temperature"),
        icon="mdi:thermometer",
    ),
    PeplinkSensorEntityDescription(
        key="system_temperature_threshold",
        translation_key=None,
        name="System Temperature Threshold",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda x: x.get("threshold"),
        icon="mdi:thermometer-alert",
    ),
    # System-level bandwidth sensors
    PeplinkSensorEntityDescription(
        key="system_download_rate",
        translation_key=None,
        name="System Download",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: round(x.get("rx_rate") / 1_000_000, 2) if x.get("rx_rate") is not None else None,
        icon="mdi:download-network",
    ),
    PeplinkSensorEntityDescription(
        key="system_upload_rate",
        translation_key=None,
        name="System Upload",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: round(x.get("tx_rate") / 1_000_000, 2) if x.get("tx_rate") is not None else None,
        icon="mdi:upload-network",
    ),
    # Device info sensors
    PeplinkSensorEntityDescription(
        key="device_serial_number",
        translation_key=None,
        name="Serial Number",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda x: x.get("serial_number"),
        icon="mdi:barcode",
    ),
    PeplinkSensorEntityDescription(
        key="device_name",
        translation_key=None,
        name="Device Name",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda x: x.get("name"),
        icon="mdi:label",
    ),
    PeplinkSensorEntityDescription(
        key="device_firmware_version",
        translation_key=None,
        name="Firmware Version",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda x: x.get("firmware_version"),
        icon="mdi:package-variant-closed",
    ),
    # WAN traffic sensors - these will be created dynamically per WAN
    PeplinkSensorEntityDescription(
        key="wan_download_rate",
        translation_key=None,
        name="Download",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: round(x.get("rx_rate") / 1_000_000, 2) if x.get("rx_rate") is not None else None,
        icon="mdi:download-network",
    ),
    PeplinkSensorEntityDescription(
        key="wan_upload_rate",
        translation_key=None,
        name="Upload",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: round(x.get("tx_rate") / 1_000_000, 2) if x.get("tx_rate") is not None else None,
        icon="mdi:upload-network",
    ),
    PeplinkSensorEntityDescription(
        key="wan_type",
        translation_key=None,
        name="Type",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda x: _translate_wan_type(x.get("type")),
        icon="mdi:lan",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="wan_name",
        translation_key=None,
        name="Name",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda x: x.get("name"),
        icon="mdi:tag",
    ),
    PeplinkSensorEntityDescription(
        key="wan_ip",
        translation_key=None,
        name="IP Address",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda x: x.get("ip"),
        icon="mdi:ip-network",
    ),
    PeplinkSensorEntityDescription(
        key="wan_up_since",
        translation_key=None,
        name="Up Since",
        native_unit_of_measurement=None,
        device_class=SensorDeviceClass.TIMESTAMP,
        state_class=None,
        # Calculate the "up since" timestamp by subtracting uptime from current time
        value_fn=lambda x: (
            uptime_to_stable_datetime(x.get("uptime"))
            if x.get("uptime") is not None
            else None
        ),
        icon="mdi:clock-start",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # WiFi specific sensors for WiFi WAN connections
    PeplinkSensorEntityDescription(
        key="wifi_signal_strength",
        translation_key=None,
        name="WiFi Signal Strength",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: (wan_data.wifi(x).get("signal") or {}).get("strength"),
        icon="mdi:wifi-strength-4",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="wifi_ssid",
        translation_key=None,
        name="WiFi SSID",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda x: wan_data.wifi(x).get("ssid"),
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="wifi_bssid",
        translation_key=None,
        name="WiFi BSSID",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda x: wan_data.wifi(x).get("bssid"),
        icon="mdi:wifi-marker",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="wifi_channel",
        translation_key=None,
        name="WiFi Channel",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: wan_data.wifi(x).get("channel"),
        icon="mdi:wifi-settings",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # GPS Location sensors
    PeplinkSensorEntityDescription(
        key="heading",
        translation_key=None,
        name="Heading",
        native_unit_of_measurement="°",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: x.get('heading'),
        icon="mdi:compass",
    ),
    PeplinkSensorEntityDescription(
        key="speed",
        translation_key=None,
        name="Speed",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: x.get('speed'),
        icon="mdi:speedometer",
    ),
    PeplinkSensorEntityDescription(
        key="altitude",
        translation_key=None,
        name="Altitude",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda x: x.get('altitude'),
        icon="mdi:arrow-up-bold",
    ),
)


# Per-WAN status and cellular sensors (read from status.wan.connection).
WAN_STATUS_SENSOR_TYPES: tuple[PeplinkSensorEntityDescription, ...] = (
    PeplinkSensorEntityDescription(
        key="status_led",
        name="Status",
        device_class=SensorDeviceClass.ENUM,
        options=wan_data.STATUS_LED_OPTIONS,
        value_fn=wan_data.status_led,
        icon="mdi:led-on",
    ),
    PeplinkSensorEntityDescription(
        key="priority",
        name="Priority",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=wan_data.priority,  # None (unknown) while the WAN is disabled
        icon="mdi:sort-numeric-ascending",
    ),
    PeplinkSensorEntityDescription(
        key="routing_mode",
        name="Routing Mode",
        value_fn=wan_data.routing_mode,
        icon="mdi:swap-horizontal",
        entity_category=EntityCategory.DIAGNOSTIC,
        require_value=True,
    ),
    # WiFi WAN sensors (descriptions above, were never created before)
    *(d for d in SENSOR_TYPES if d.key.startswith("wifi_")),
    # Cellular
    PeplinkSensorEntityDescription(
        key="cellular_signal_level",
        name="Signal Level",
        native_unit_of_measurement="bars",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=wan_data.signal_level,
        wan_filter=wan_data.is_cellular,
        icon="mdi:signal-cellular-3",
    ),
    PeplinkSensorEntityDescription(
        key="cellular_rsrp",
        name="RSRP",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: wan_data.band_signal(c, "rsrp"),
        wan_filter=wan_data.is_cellular,
        require_value=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="cellular_rsrq",
        name="RSRQ",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: wan_data.band_signal(c, "rsrq"),
        wan_filter=wan_data.is_cellular,
        require_value=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="cellular_sinr",
        name="SINR",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: wan_data.band_signal(c, "sinr"),
        wan_filter=wan_data.is_cellular,
        require_value=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="cellular_rssi",
        name="RSSI",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: wan_data.band_signal(c, "rssi"),
        wan_filter=wan_data.is_cellular,
        require_value=True,  # 5G NR bands don't report RSSI
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="cellular_carrier",
        name="Carrier",
        value_fn=wan_data.carrier,
        wan_filter=wan_data.is_cellular,
        icon="mdi:radio-tower",
    ),
    PeplinkSensorEntityDescription(
        key="cellular_technology",
        name="Network Type",
        value_fn=wan_data.technology,
        wan_filter=wan_data.is_cellular,
        icon="mdi:signal-5g",
    ),
    PeplinkSensorEntityDescription(
        key="cellular_band",
        name="Band",
        value_fn=lambda c: (wan_data.band_names(c) or [None])[0],
        attrs_fn=lambda c: {
            "bands": wan_data.band_names(c),
            "carrier_aggregation": wan_data.cellular(c).get("carrierAggregation"),
        },
        wan_filter=wan_data.is_cellular,
        icon="mdi:antenna",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="cellular_active_sim",
        name="Active SIM",
        value_fn=wan_data.active_sim,
        wan_filter=wan_data.is_cellular,
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PeplinkSensorEntityDescription(
        key="cellular_roaming",
        name="Roaming",
        value_fn=wan_data.roaming,
        wan_filter=wan_data.is_cellular,
        require_value=True,
        icon="mdi:earth",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Peplink sensors."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []

    # Add system sensors
    thermal_sensors = coordinator.data.get("thermal_sensors", {})
    if thermal_sensors and thermal_sensors.get("sensors"):
        sensor = thermal_sensors["sensors"][0]  # Only one sensor
        for description in SENSOR_TYPES:
            if description.key in ["system_temperature", "system_temperature_threshold"]:
                entities.append(
                    PeplinkSensor(
                        coordinator=coordinator,
                        description=description,
                        sensor_data=sensor,
                    )
                )

    # Add system-level bandwidth sensors
    if coordinator.data.get("traffic_stats", {}).get("stats"):
        raw_traffic_data = coordinator.data["traffic_stats"]["stats"]
        # Create system download/upload sensors
        for description in SENSOR_TYPES:
            if description.key in ["system_download_rate", "system_upload_rate"]:
                # Prepare system-level traffic data
                system_traffic_data = {
                    "rx_rate": 0,
                    "tx_rate": 0,
                }
                # Sum up all WAN interfaces data
                for wan in raw_traffic_data:
                    system_traffic_data["rx_rate"] += wan.get("rx_rate", 0)
                    system_traffic_data["tx_rate"] += wan.get("tx_rate", 0)
                
                entities.append(
                    PeplinkSensor(
                        coordinator=coordinator,
                        description=description,
                        sensor_data=system_traffic_data,
                    )
                )

    # Add device info sensors
    device_info = coordinator.data.get("device_info", {}).get("device_info", {})
    if device_info:
        for description in SENSOR_TYPES:
            if description.key.startswith("device_"):
                entities.append(
                    PeplinkSensor(
                        coordinator=coordinator,
                        description=description,
                        sensor_data=device_info,
                    )
                )

    # Add fan sensors
    if coordinator.data.get("fan_speeds", {}).get("fans"):
        fans = coordinator.data["fan_speeds"]["fans"]
        for fan_num, fan in enumerate(fans, 1):
            # Dynamically create a fan speed sensor for each fan
            fan_speed_description = PeplinkSensorEntityDescription(
                key=f"fan_{fan_num}_speed",
                translation_key=None,
                name=f"Fan {fan_num} Speed",
                native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
                device_class=None,
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
                icon="mdi:fan",
                value_fn=lambda x: x.get("speed"),
            )
            entities.append(
                PeplinkSensor(
                    coordinator=coordinator,
                    description=fan_speed_description,
                    sensor_data=fan,
                )
            )

    # Per-WAN sensors: one sub-device per WAN reported by status.wan.connection
    # (disabled WANs included), traffic sensors where traffic stats exist.
    wan_connections = coordinator.data.get("wan_status", {}).get("connection", [])
    wan_stats = {
        str(stat.get("wan_id")): stat
        for stat in coordinator.data.get("traffic_stats", {}).get("stats", [])
    }
    router_name = coordinator.device_name or "Peplink"

    def _wan_copy(description: PeplinkSensorEntityDescription, key: str) -> PeplinkSensorEntityDescription:
        return PeplinkSensorEntityDescription(
            key=key,
            translation_key=description.translation_key,
            name=description.name,
            native_unit_of_measurement=description.native_unit_of_measurement,
            device_class=description.device_class,
            state_class=description.state_class,
            entity_category=description.entity_category,
            options=description.options,
            icon=description.icon,
            value_fn=description.value_fn,
            attrs_fn=description.attrs_fn,
        )

    for wan_connection in wan_connections:
        wan_id = str(wan_connection.get("id"))
        wan_name = wan_connection.get("name") or f"WAN{wan_id}"
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
            manufacturer="Peplink",
            model=f"WAN {wan_id} ({_translate_wan_type(wan_connection.get('type')) or 'WAN'})",
            name=f"{router_name} {wan_name}",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

        def _add(description: PeplinkSensorEntityDescription, data: dict) -> None:
            entities.append(
                PeplinkWANSensor(
                    coordinator=coordinator,
                    description=description,
                    sensor_data=data,
                    device_info=device_info,
                    wan_id=wan_id,
                )
            )

        # Traffic rates
        if wan_id in wan_stats:
            for description in SENSOR_TYPES:
                if description.key in ("wan_download_rate", "wan_upload_rate"):
                    _add(_wan_copy(description, description.key.replace("wan_", "")), wan_stats[wan_id])

        # Message, name, type, IP, up since
        _add(
            PeplinkSensorEntityDescription(
                key="message",
                name="Message",
                value_fn=lambda x: x.get("message"),
                icon="mdi:network",
            ),
            wan_connection,
        )
        for description in SENSOR_TYPES:
            if not description.key.startswith("wan_") or description.key in (
                "wan_download_rate", "wan_upload_rate", "wan_up_since",
            ):
                continue
            _add(_wan_copy(description, description.key.replace("wan_", "")), wan_connection)
        if "uptime" in wan_connection:
            up_since = next(d for d in SENSOR_TYPES if d.key == "wan_up_since")
            _add(_wan_copy(up_since, "up_since"), wan_connection)

        # Status, priority, routing mode, Wi-Fi and cellular
        for description in WAN_STATUS_SENSOR_TYPES:
            if description.key.startswith("wifi_") and not wan_data.is_wifi(wan_connection):
                continue
            if description.wan_filter and not description.wan_filter(wan_connection):
                continue
            if description.require_value and description.value_fn(wan_connection) is None:
                continue
            _add(_wan_copy(description, description.key), wan_connection)

    # Add GPS location sensors
    location_info = coordinator.data.get("location_info", {})
    has_gps = location_info.get("gps", False)
    _LOGGER.debug("GPS capability check: %s (has_gps=%s)", location_info, has_gps)
    
    if has_gps:
        location_data = location_info.get("location", {})
        if location_data:
            for description in SENSOR_TYPES:
                if description.key in ["heading", "speed", "altitude"]:
                    entities.append(
                        PeplinkSensor(
                            coordinator=coordinator,
                            description=description,
                            sensor_data=location_data,
                        )
                    )
            _LOGGER.debug("Added GPS sensors for heading, speed, and altitude")
        else:
            _LOGGER.debug("Router has GPS capability but no valid location data")
    else:
        _LOGGER.debug("Router does not have GPS capability - skipping GPS sensors")

    # Add all entities
    async_add_entities(entities)


class PeplinkSensor(CoordinatorEntity, SensorEntity):
    """Implementation of a Peplink sensor."""

    entity_description: PeplinkSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PeplinkDataUpdateCoordinator,
        description: PeplinkSensorEntityDescription,
        sensor_data: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self.entity_description = description
        self._sensor_data_key = None
        self._sensor_data_id = None
        
        # Store identifiers to find data in coordinator updates
        if description.key.startswith("system_temperature"):
            self._sensor_data_key = "thermal_sensors"
            self._sensor_data_id = "System"  # System temperature sensor name
        elif description.key.startswith("fan_"):
            self._sensor_data_key = "fan_speeds"
            # Extract fan number from key (e.g., fan_1_speed -> 1)
            try:
                self._sensor_data_id = description.key.split("_")[1]
            except (IndexError, ValueError):
                self._sensor_data_id = None
        elif description.key.startswith("device_"):
            self._sensor_data_key = "device_info"
        elif description.key in ["heading", "speed", "altitude"]:
            self._sensor_data_key = "location_info"
            
        # Keep a reference to the initial data as fallback
        self._initial_sensor_data = sensor_data
        
        # Config entry ID prefix: stable across IP and router-name changes
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        
        # Use the device name from API if available, otherwise fallback to IP
        device_name = coordinator.device_name or f"Peplink {coordinator.host}"
        
        # Create model string with available info
        model_string = coordinator.model or "Router"
        if coordinator.product_code and coordinator.hardware_revision:
            model_string = f"{model_string} ({coordinator.product_code} HW {coordinator.hardware_revision})"
        elif coordinator.product_code:
            model_string = f"{model_string} ({coordinator.product_code})"
        elif coordinator.hardware_revision:
            model_string = f"{model_string} (HW {coordinator.hardware_revision})"
        
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            manufacturer="Peplink",
            model=model_string,
            name=device_name,
            sw_version=coordinator.firmware,
        )
        # Set custom icon if provided
        if description.icon:
            self._attr_icon = description.icon

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        value = None
        
        if self.coordinator.data:
            # Handle location data differently
            if self.entity_description.key in ["heading", "speed", "altitude"]:
                location_data = self.coordinator.data.get("location_info", {}).get("location", {})
                if location_data and self.entity_description.value_fn:
                    value = self.entity_description.value_fn(location_data)
            # Handle system-level bandwidth sensors
            elif self.entity_description.key.startswith("system_") and self.entity_description.key in ["system_download_rate", "system_upload_rate"]:
                raw_traffic_data = self.coordinator.data.get("traffic_stats", {}).get("stats", [])
                if raw_traffic_data and self.entity_description.value_fn:
                    # Prepare system-level traffic data
                    system_traffic_data = {
                        "rx_rate": 0,
                        "tx_rate": 0,
                    }
                    # Sum up all WAN interfaces data
                    for wan in raw_traffic_data:
                        system_traffic_data["rx_rate"] += wan.get("rx_rate", 0)
                        system_traffic_data["tx_rate"] += wan.get("tx_rate", 0)

                    value = self.entity_description.value_fn(system_traffic_data)
            # Check if we need to extract from a specific data category
            elif self._sensor_data_key:
                # Handle thermal sensors
                if self._sensor_data_key == "thermal_sensors":
                    sensors = self.coordinator.data.get("thermal_sensors", {}).get("sensors", [])
                    for sensor in sensors:
                        if sensor.get("name") == self._sensor_data_id:
                            if self.entity_description.value_fn:
                                value = self.entity_description.value_fn(sensor)
                            break
                # Handle fan speeds
                elif self._sensor_data_key == "fan_speeds":
                    fans = self.coordinator.data.get("fan_speeds", {}).get("fans", [])
                    for i, fan in enumerate(fans, 1):
                        if str(i) == str(self._sensor_data_id):
                            if self.entity_description.value_fn:
                                value = self.entity_description.value_fn(fan)
                            break
                # Handle device info
                elif self._sensor_data_key == "device_info":
                    device_info = self.coordinator.data.get("device_info", {})
                    if device_info and self.entity_description.value_fn:
                        value = self.entity_description.value_fn(device_info)
            # Handle sensors with no specific data category
            elif self.entity_description.value_fn:
                value = self.entity_description.value_fn(self._initial_sensor_data)
        
        # Fallback to initial data if no value found
        if value is None and self.entity_description.value_fn:
            value = self.entity_description.value_fn(self._initial_sensor_data)
            
        return value


class PeplinkWANSensor(CoordinatorEntity, SensorEntity):
    """Implementation of a Peplink WAN sensor."""

    entity_description: PeplinkSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PeplinkDataUpdateCoordinator,
        description: PeplinkSensorEntityDescription,
        sensor_data: dict[str, Any],
        device_info: DeviceInfo,
        wan_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self.entity_description = description
        self._wan_id = str(wan_id)
        # Keep reference to initial data
        self._initial_sensor_data = sensor_data
        
        # Config entry ID prefix: stable across IP and router-name changes
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_wan{wan_id}_{description.key}"
        
        # Use the device name from API if available, otherwise fallback to IP
        device_name = coordinator.device_name or f"Peplink {coordinator.host}"
        
        self._attr_device_info = device_info
        
        # Add extra attributes for IP sensor
        self._extra_attrs = {}
        if description.key == "ip" and sensor_data:
            self._extra_attrs = {
                "gateway": sensor_data.get("gateway"),
                "dns": sensor_data.get("dns", []),
                "mask": sensor_data.get("mask"),
            }
            
        # Set custom icon if provided
        if description.icon:
            self._attr_icon = description.icon

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.entity_description.value_fn is None:
            return None

        # Check if we're dealing with traffic rate sensors
        is_traffic_sensor = self.entity_description.key in ["download_rate", "upload_rate"]
        
        # Try to get fresh data from coordinator
        if self.coordinator.data:
            try:
                if is_traffic_sensor:
                    # For traffic sensors, get data from traffic_stats
                    stats = self.coordinator.data.get("traffic_stats", {}).get("stats", [])
                    for stat in stats:
                        if str(stat.get("wan_id", "")) == self._wan_id:
                            return self.entity_description.value_fn(stat)
                else:
                    # For other WAN sensors, get data from wan_status
                    wan_status = self.coordinator.data.get("wan_status", {})
                    connections = wan_status.get("connection", [])  # Use the correct key based on setup_entry
                    
                    for connection in connections:
                        if str(connection.get("id", "")) == self._wan_id:
                            # Update extra attributes if needed
                            if self.entity_description.key == "ip":
                                self._extra_attrs = {
                                    "gateway": connection.get("gateway"),
                                    "dns": connection.get("dns", []),
                                    "mask": connection.get("mask"),
                                }
                            return self.entity_description.value_fn(connection)
            except Exception:
                # If anything goes wrong, fall back to initial data
                pass
                
        # Fall back to initial sensor data
        return self.entity_description.value_fn(self._initial_sensor_data)
        
    def _connection(self) -> dict[str, Any] | None:
        """This WAN's current status.wan.connection object."""
        for connection in (self.coordinator.data or {}).get("wan_status", {}).get("connection", []):
            if str(connection.get("id", "")) == self._wan_id:
                return connection
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        if self.entity_description.attrs_fn:
            connection = self._connection() or self._initial_sensor_data
            return self.entity_description.attrs_fn(connection)
        return self._extra_attrs


def _translate_wan_type(wan_type: str) -> str:
    """Translate WAN type to more user-friendly format."""
    if not wan_type:
        return None
        
    type_map = {
        "modem": "Modem",
        "wireless": "Wireless",
        "gobi": "Cellular",
        "cellular": "Cellular",
        "ipsec": "IPSec VPN",
        "adsl": "ADSL",
        "wifi": "WiFi",
        "ethernet": "Ethernet"
    }
    
    return type_map.get(wan_type.lower(), wan_type)
