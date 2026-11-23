"""Config flow for Peplink Local integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN, CONF_POLL_FREQUENCY, CONF_TRACKED_CLIENTS, DEFAULT_POLL_FREQUENCY
from . import clients as client_util
from . import wan_data, wifi as wifi_util
from .peplink_api import PeplinkAPI, PeplinkAuthFailed, PeplinkSSLError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
        vol.Optional(CONF_POLL_FREQUENCY, default=DEFAULT_POLL_FREQUENCY): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=300)
        ),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    host = data[CONF_HOST]
    username = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]
    verify_ssl = data.get(CONF_VERIFY_SSL, True)

    # Get the session with appropriate SSL settings
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    try:
        # Test the connection
        client = PeplinkAPI(
            host=host,
            username=username,
            password=password,
            session=session,
            verify_ssl=verify_ssl
        )

        if not await client.connect():
            raise PeplinkAuthFailed
            
        # Attempt to get the device name
        device_info = await client.get_device_info()
        device_name = None
        if device_info and "device_info" in device_info:
            device_name = device_info["device_info"].get("name")
            
    except PeplinkAuthFailed as err:
        raise PeplinkAuthFailed from err
    except PeplinkSSLError as err:
        # Catch and re-raise SSL errors separately
        raise PeplinkSSLError from err
    except Exception as e:
        _LOGGER.exception("Unexpected exception: %s", e)
        raise

    # Return info to be stored in the config entry
    # Use the device name if available, otherwise use the IP address
    if device_name:
        return {"title": device_name}
    else:
        return {"title": f"Peplink Router ({data[CONF_HOST]})"}


class PeplinkLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Peplink Local."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        # Use user_input as defaults if provided, otherwise empty dict
        defaults = user_input or {}
        
        # Create a dynamic schema that uses the previous input as defaults
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): str,
            vol.Optional(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, False)): bool,
            vol.Optional(CONF_POLL_FREQUENCY, default=defaults.get(CONF_POLL_FREQUENCY, DEFAULT_POLL_FREQUENCY)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        })

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except PeplinkAuthFailed:
                errors["base"] = "invalid_auth"
            except PeplinkSSLError:
                errors["base"] = "ssl_error"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return PeplinkLocalOptionsFlowHandler(config_entry)


class PeplinkLocalOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Peplink Local options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        # Don't directly set self.config_entry as it's deprecated
        self.entry = config_entry

    async def async_step_init(self, user_input=None):
        """Configure opens a menu: settings, or join a Wi-Fi network."""
        if self._wifi_wans():
            return self.async_show_menu(step_id="init", menu_options=["settings", "join_wifi"])
        return await self.async_step_settings()

    # ---- Join a Wi-Fi network --------------------------------------------

    def _coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("coordinator")

    def _wifi_wans(self) -> dict[str, str]:
        """Enabled Wi-Fi WANs: id -> name."""
        coordinator = self._coordinator()
        if coordinator is None or not coordinator.data:
            return {}
        return {
            str(conn.get("id")): conn.get("name") or f"WAN {conn.get('id')}"
            for conn in coordinator.data.get("wan_status", {}).get("connection", [])
            if wan_data.is_wifi(conn) and conn.get("enable")
        }

    async def async_step_join_wifi(self, user_input=None):
        """Pick a network (scanned, saved, or typed) and give its password.

        The password goes straight to the router; nothing is stored in Home
        Assistant (options flows are neither saved nor traced).
        """
        coordinator = self._coordinator()
        wans = self._wifi_wans()
        errors: dict[str, str] = {}

        if user_input is not None:
            wan_id = user_input.get("wan") or next(iter(wans))
            ssid = (user_input.get("network") or "").strip()
            password = user_input.get("password") or None
            scanned = wifi_util.find(coordinator.wifi_networks.get(wan_id, []), ssid)
            saved = wifi_util.find(coordinator.wifi_saved.get(wan_id, []), ssid)
            if not ssid:
                errors["network"] = "network_required"
            elif not password and saved is None and wifi_util.needs_password(scanned):
                errors["password"] = "password_required"
            else:
                try:
                    await coordinator.api.wifi_connect(
                        wan_id, ssid, password, scanned.get("security") if scanned else None
                    )
                except ValueError:
                    errors["base"] = "enterprise_not_supported"
                except Exception as err:  # router rejected it / unreachable
                    _LOGGER.warning("Joining %s failed: %s", ssid, err)
                    errors["base"] = "join_failed"
                else:
                    coordinator.wifi_selected[wan_id] = ssid
                    coordinator.watch_wifi_result(wan_id)
                    await coordinator.async_refresh_wifi_saved()
                    await coordinator.async_request_refresh()
                    return self.async_abort(
                        reason="join_started",
                        description_placeholders={"ssid": ssid, "wan": wans.get(wan_id, wan_id)},
                    )

        # Network choices: scanned (strongest first, with signal), then saved.
        options: list[SelectOptionDict] = []
        seen: set[str] = set()
        for wan_id in wans:
            for net in coordinator.wifi_networks.get(wan_id, []):
                if net["ssid"] in seen:
                    continue
                seen.add(net["ssid"])
                bits = [f"{net['signal_dbm']} dBm" if net.get("signal_dbm") is not None else None,
                        "open" if net.get("security") == "open" else "secured",
                        "saved" if wifi_util.find(coordinator.wifi_saved.get(wan_id, []), net["ssid"]) else None]
                options.append(SelectOptionDict(
                    value=net["ssid"], label=f"{net['ssid']} ({', '.join(b for b in bits if b)})"))
            for net in coordinator.wifi_saved.get(wan_id, []):
                if net["ssid"] not in seen:
                    seen.add(net["ssid"])
                    options.append(SelectOptionDict(value=net["ssid"], label=f"{net['ssid']} (saved, not in range)"))

        schema: dict = {}
        if len(wans) > 1:
            schema[vol.Required("wan", default=next(iter(wans)))] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=k, label=v) for k, v in wans.items()],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        default_ssid = next(
            (coordinator.wifi_selected.get(w) for w in wans if coordinator.wifi_selected.get(w)), None
        )
        network_key = (
            vol.Required("network", default=default_ssid) if default_ssid else vol.Required("network")
        )
        schema[network_key] = SelectSelector(
            SelectSelectorConfig(options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
        )
        schema[vol.Optional("password")] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
        scanned_any = any(coordinator.wifi_networks.get(w) for w in wans)
        return self.async_show_form(
            step_id="join_wifi",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "scan_hint": "" if scanned_any else
                "No scan yet: press the WAN's Scan Networks button first to list nearby networks, or type a network name.",
            },
        )

    # ---- Settings ---------------------------------------------------------

    async def async_step_settings(self, user_input=None):
        """Polling, SSL and which clients to track."""
        if user_input is not None:
            # Update the options
            return self.async_create_entry(title="", data=user_input)

        # Fill the form with current values (options override setup data)
        current = {**self.entry.data, **self.entry.options}
        tracked = [client_util.norm_mac(m) for m in current.get(CONF_TRACKED_CLIENTS, [])]

        # Clients the router knows now, plus any tracked ones that are offline.
        choices: dict[str, str] = {}
        coordinator = (
            self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("coordinator")
        )
        router_clients = ((coordinator.data or {}).get("clients") or {}).get("client", []) if coordinator else []
        for client in sorted(router_clients, key=lambda c: (client_util.client_name(c) or "~").lower()):
            if client_util.is_mac(client.get("mac")):
                choices[client_util.norm_mac(client["mac"])] = client_util.label(client)
        for mac in tracked:
            choices.setdefault(mac, f"{mac} (not connected)")

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=current.get(CONF_VERIFY_SSL, True),
                    ): bool,
                    vol.Optional(
                        CONF_POLL_FREQUENCY,
                        default=current.get(CONF_POLL_FREQUENCY, DEFAULT_POLL_FREQUENCY),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
                    vol.Optional(
                        CONF_TRACKED_CLIENTS,
                        default=tracked,
                    ): cv.multi_select(choices),
                }
            ),
        )
