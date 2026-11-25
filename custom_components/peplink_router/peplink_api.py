#!/usr/bin/env python3
"""
Peplink API Client - Async API class for interacting with the Peplink router API

This module provides the PeplinkAPI class that connects to a Peplink router's local API,
authenticates, and retrieves information about WAN status and connected clients.
"""
from __future__ import annotations

import logging
import json
import ssl
import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin
from functools import partial
import time

import aiohttp

_LOGGER = logging.getLogger(__name__)


class PeplinkAuthFailed(Exception):
    """Authentication failed."""


class PeplinkSSLError(Exception):
    """SSL Certificate validation error."""


class PeplinkCommandError(Exception):
    """The router rejected a configuration change or command."""


ROUTING_MODES = ("NAT", "IP Forwarding")
STARLINK_ACTIONS = ("reboot", "stow", "unstow")


_TEMPORARY_MESSAGES = ("internal error", "busy", "try again", "timeout", "not ready")


def _is_temporary_failure(status: int, data: Any) -> bool:
    """True when a failed token request is the router's fault, not a bad client.

    A router that is busy or still starting answers auth.token.grant with
    {"stat": "fail", "message": "Internal Error"} or an HTTP 5xx. That must not
    make Home Assistant ask the user to sign in again (seen after an HA upgrade:
    the same client worked moments later).
    """
    if status >= 500:
        return True
    if not isinstance(data, dict):
        return True  # not a Peplink answer at all
    code = data.get("code")
    if isinstance(code, int) and code >= 500:
        return True
    message = str(data.get("message") or "").lower()
    return any(text in message for text in _TEMPORARY_MESSAGES)


def _create_insecure_ssl_context():
    """Create an insecure SSL context (non-blocking function)."""
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


class PeplinkAPI:
    """Async class for interacting with the Peplink router API."""

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        session: Optional[aiohttp.ClientSession] = None,
        verify_ssl: bool = True,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        """Initialize the Peplink API client.

        Two ways to sign in:
        - API client (client_id + client_secret): the normal mode. Access
          tokens are requested with auth.token.grant, kept in memory and
          renewed before they expire. No admin password is needed.
        - Admin login (username + password): used only during setup or
          re-authentication, to create the API client.
        """
        self.host = host  # Store the host for reference
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session
        self._verify_ssl = verify_ssl
        self._own_session = False
        self._connected = False
        self._auth_cookie = None
        self._access_token: str | None = None
        self._token_expires_at = 0.0  # time.monotonic()
        self._auth_lock = asyncio.Lock()  # one sign-in at a time

    @property
    def uses_token(self) -> bool:
        """True when signing in with an API client instead of the admin login."""
        return bool(self.client_id and self.client_secret)

    def _redact(self, text: Any) -> str:
        """Hide the access token in anything that may be logged."""
        text = str(text)
        if self._access_token:
            text = text.replace(self._access_token, "***")
        return text

    def _with_auth(self, url: str) -> tuple[str, dict[str, str]]:
        """Add the access token (query parameter) or session cookie to a request."""
        if self.uses_token and self._access_token:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}accessToken={quote(self._access_token, safe='')}", {}
        if self._auth_cookie:
            return url, {"Cookie": f"bauth={self._auth_cookie}"}
        return url, {}

    async def _grant_token(self) -> bool:
        """Get an access token for the API client (auth.token.grant).

        Raises PeplinkAuthFailed if the router rejects the client (removed on
        the router, or wrong secret); returns False if it can't be reached or
        answers with a temporary error ("Internal Error", HTTP 5xx), so Home
        Assistant retries setup instead of asking the user to sign in again.
        """
        session = await self._get_session()
        url = urljoin(self.base_url, "/api/auth.token.grant")
        body = {"clientId": self.client_id, "clientSecret": self.client_secret, "scope": "api"}
        try:
            async with session.post(url, json=body) as response:
                status = response.status
                data = await response.json(content_type=None)
        except aiohttp.ClientConnectorCertificateError as err:
            raise PeplinkSSLError(f"SSL Certificate validation failed: {err}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.error("Could not reach the router for an access token: %s", err)
            self._connected = False
            return False
        result = data.get("response") if isinstance(data, dict) else None
        if not (isinstance(data, dict) and data.get("stat") == "ok" and isinstance(result, dict)
                and result.get("accessToken")):
            self._connected = False
            message = data.get("message") if isinstance(data, dict) else data
            if _is_temporary_failure(status, data):
                _LOGGER.warning("The router could not issue an access token right now (%s); "
                                "will retry", message)
                return False
            raise PeplinkAuthFailed(f"The router rejected the API client: {message}")
        self._access_token = result["accessToken"]
        try:
            lifetime = int(result["expiresIn"]) if result.get("expiresIn") is not None else 172800
        except (TypeError, ValueError):
            lifetime = 172800
        # Renew 10 minutes early (or halfway, for very short lifetimes).
        self._token_expires_at = time.monotonic() + max(lifetime - 600, lifetime / 2)
        self._connected = True
        _LOGGER.debug("Got an access token valid for %s s", lifetime)
        return True

    async def list_api_clients(self) -> list[dict[str, Any]]:
        """API clients on the router (GET auth.client, needs the admin login).

        Each item has name, clientId, clientSecret and scope. The router's web
        UI does not show these, so this is the only way to see them.
        """
        result = await self._get("auth.client")

        def walk(node: Any):
            if isinstance(node, dict):
                if "clientId" in node:
                    yield node
                else:
                    for value in node.values():
                        yield from walk(value)
            elif isinstance(node, list):
                for value in node:
                    yield from walk(value)

        return list(walk(result))

    async def get_or_create_api_client(self, name: str = "Home Assistant") -> tuple[str, str]:
        """Reuse the read-write API client with this name, or create one.

        Reusing keeps setup, re-authentication and retries from piling up
        clients on the router, where they can't be seen or removed in the UI.
        Needs the admin login. Returns (client_id, client_secret).
        """
        try:
            existing = await self.list_api_clients()
        except PeplinkCommandError as err:
            _LOGGER.debug("Could not list API clients (%s); creating one", err)
            existing = []
        for client in existing:
            if (client.get("name") == name and client.get("scope") == "api"
                    and client.get("clientId") and client.get("clientSecret")):
                _LOGGER.debug("Reusing the existing '%s' API client", name)
                return client["clientId"], client["clientSecret"]
        return await self.create_api_client(name)

    async def create_api_client(self, name: str = "Home Assistant") -> tuple[str, str]:
        """Create a read-write API client on the router (needs the admin login).

        Prefer get_or_create_api_client. Returns (client_id, client_secret).
        """
        response = await self._post("auth.client", {"action": "add", "name": name, "scope": "api"})
        result = response.get("response") or {}
        client_id, client_secret = result.get("clientId"), result.get("clientSecret")
        if not client_id or not client_secret:
            raise PeplinkCommandError("auth.client returned no client ID or secret")
        return client_id, client_secret

    async def logout(self) -> None:
        """End an admin login session (best effort)."""
        if self.uses_token or not self._auth_cookie:
            return
        try:
            await self._make_api_request("logout", public_api=True, method="POST", data={})
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Logout failed: %s", err)
        self._auth_cookie = None
        self._connected = False

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a session with the appropriate SSL settings."""
        if not self._session:
            if self._verify_ssl:
                # Create a standard session
                self._session = aiohttp.ClientSession()
            else:
                # Create a session with SSL verification disabled
                ssl_context = _create_insecure_ssl_context()
                connector = aiohttp.TCPConnector(
                    ssl=ssl_context,
                    verify_ssl=False
                )
                self._session = aiohttp.ClientSession(connector=connector)
                
            self._own_session = True
        
        return self._session

    async def connect(self) -> bool:
        """Connect to the Peplink router and authenticate."""
        if self._connected:
            return True

        if self.uses_token:
            return await self._grant_token()

        # Create or get a session object
        session = await self._get_session()
        
        try:
            # Step 1: Login to get a cookie
            login_url = urljoin(self.base_url, "/api/login")
            
            login_data = {
                "username": self.username,
                "password": self.password,
                "challenge": "challenge"  # Required by Peplink API
            }
            
            _LOGGER.debug("Attempting to connect to %s", login_url)
            
            try:
                async with session.post(login_url, json=login_data) as response:
                    if response.status == 401:
                        _LOGGER.error("Failed to authenticate: 401 Unauthorized")
                        return False
                    
                    response.raise_for_status()
                    login_response = await response.json()
                    
                    # Check for successful login
                    if login_response.get("stat") != "ok":
                        _LOGGER.error("Failed to authenticate: %s", login_response.get("message", "Unknown error"))
                        return False
                    
                    # Parse cookie from response
                    cookies = response.cookies
                    _LOGGER.debug("Login response cookies: %s", list(cookies.keys()))
                    
                    # Extract the bauth cookie
                    if "bauth" in cookies:
                        self._auth_cookie = cookies["bauth"].value
                        _LOGGER.debug("Got auth cookie")
                    
                    if not self._auth_cookie:
                        # Try extracting from Set-Cookie header if available
                        if "Set-Cookie" in response.headers:
                            cookie_header = response.headers["Set-Cookie"]
                            _LOGGER.debug("Parsing Set-Cookie header for auth cookie")
                            
                            if "bauth=" in cookie_header:
                                # Extract the bauth value from cookie header
                                # Format is typically: bauth=VALUE; path=/; HttpOnly
                                cookie_parts = cookie_header.split(';')
                                for part in cookie_parts:
                                    if part.strip().startswith("bauth="):
                                        self._auth_cookie = part.strip().split('=', 1)[1]
                                        _LOGGER.debug("Extracted auth cookie from Set-Cookie header")
                                        break
                    
                    if not self._auth_cookie:
                        _LOGGER.error("No auth cookie received after login")
                        return False
            except aiohttp.ClientConnectorCertificateError as e:
                _LOGGER.error("SSL Certificate error: %s", e)
                self._connected = False
                raise PeplinkSSLError(f"SSL Certificate validation failed: {e}")
                
            # Step 3: Verify that we're authenticated by accessing a protected endpoint
            verify_url = urljoin(self.base_url, "/api/status")
            
            # Ensure cookie is set for verification request
            headers = {}
            if self._auth_cookie:
                headers["Cookie"] = f"bauth={self._auth_cookie}"
                _LOGGER.debug("Using stored auth cookie for verification request")
            
            async with session.get(verify_url, headers=headers) as response:
                if response.status == 401:
                    _LOGGER.error("Failed to authenticate: 401 Unauthorized")
                    return False
                
                response.raise_for_status()
                verify_data = await response.json()
                
                # Even if it returns an error code (other than 401), it's fine as long as we can access it
                _LOGGER.debug("Authentication verification successful: %s", verify_data)
            
            self._connected = True
            return True
            
        except PeplinkSSLError:
            # Re-raise SSL errors so they can be handled differently from auth errors
            raise
        except aiohttp.ClientConnectorError as e:
            _LOGGER.error("Connection error (host unreachable): %s", e)
            self._connected = False
            return False
        except aiohttp.ClientError as e:
            _LOGGER.error("Connection error: %s", e)
            self._connected = False
            return False

    async def ensure_connected(
        self, force_reconnect: bool = False, failed_token: str | None = None
    ) -> bool:
        """Ensure that a connection has been established.
        
        Args:
            force_reconnect: If True, will reconnect even if already connected
        
        Returns:
            bool: True if successfully connected, False otherwise
        """
        async with self._auth_lock:
            if self.uses_token and time.monotonic() >= self._token_expires_at:
                force_reconnect = True
            elif failed_token and self._access_token != failed_token:
                # Another request already signed in again while we waited.
                return True
            if not self._connected or force_reconnect:
                self._connected = False  # Reset connected state if forcing reconnect
                return await self.connect()
            return True
    
    async def _api_request(self, endpoint: str, method: str = "GET", data: Optional[Dict] = None) -> Dict:
        """Make an API request to the Peplink router."""
        if not await self.ensure_connected():
            raise Exception("Not connected to Peplink router")
        
        session = await self._get_session()
        base = urljoin(self.base_url, endpoint)

        async def _send() -> tuple[int, Any]:
            nonlocal used_token
            used_token = self._access_token
            url, headers = self._with_auth(base)
            async with session.request(method, url, json=data, headers=headers) as response:
                if response.status == 401:
                    return 401, None
                response.raise_for_status()
                return response.status, await response.json(content_type=None)

        used_token: str | None = None

        def _unauthorized(result: tuple[int, Any]) -> bool:
            status, body = result
            return status == 401 or (
                isinstance(body, dict) and body.get("stat") == "fail" and body.get("code") == 401
            )

        try:
            result = await _send()
            if _unauthorized(result):
                # Session or token expired: sign in again and retry once.
                _LOGGER.debug("Router answered 401; signing in again")
                if not await self.ensure_connected(force_reconnect=True, failed_token=used_token):
                    raise Exception("Failed to reconnect, unauthorized (code: 401)")
                result = await _send()
                if _unauthorized(result):
                    raise Exception("Unauthorized (code: 401) after signing in again")
            return result[1]
        except aiohttp.ClientError as e:
            _LOGGER.error("API request error: %s", self._redact(e))
            raise Exception(f"API request error: {self._redact(e)}") from None

    async def _format_api_url(self, func: str, public_api: bool = False, **kwargs) -> str:
        """Format an API URL based on the function pattern and additional parameters.
        
        Args:
            func: The API function to call (e.g., 'status.client' or 'status.traffic')
            public_api: If True, use the /api/ style endpoint, otherwise use the cgi-bin style
            **kwargs: Additional URL parameters to include
            
        Returns:
            str: Formatted API URL
        """
        if public_api:
            # Use "/api/..." style endpoint
            # Remove leading slash if present to ensure consistent formatting
            if func.startswith('/'):
                func = func[1:]
            endpoint = f"/api/{func}"
        else:
            # Use "/cgi-bin/MANGA/api.cgi?func=..." style endpoint
            # Add timestamp to prevent caching
            params = {
                "func": func,
                "_": str(int(time.time() * 1000))
            }
            
            # Add any additional parameters
            params.update(kwargs)
            
            # Build query string
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            endpoint = f"/cgi-bin/MANGA/api.cgi?{query_string}"
            
        return endpoint
    
    async def _make_api_request(self, func: str, public_api: bool = False, method: str = "GET", data: Optional[Dict] = None, **kwargs) -> Dict:
        """Make an API request with proper URL formatting, authentication, and logging.
        
        Args:
            func: The API function to call (e.g., 'status.client' or 'status.traffic')
            public_api: If True, use the /api/ style endpoint, otherwise use the cgi-bin style
            method: HTTP method to use (default: GET)
            data: Optional JSON data for POST requests
            **kwargs: Additional URL parameters to include
            
        Returns:
            Dict: The unmodified JSON response from the API
        """
        # Ensure we're authenticated
        if not await self.ensure_connected():
            _LOGGER.error("Failed to connect to Peplink router")
            raise Exception("Not connected to Peplink router")
            
        # Format the URL
        endpoint = await self._format_api_url(func, public_api=public_api, **kwargs)
        
        # Perform the API request
        _LOGGER.debug("Requesting data from %s", endpoint)
        response = await self._api_request(endpoint, method=method, data=data)
        
        # Log the response
        _LOGGER.debug("Raw data from router: %s", response)
        
        # Return the unmodified JSON response
        return response

    async def get_wan_status(self) -> Dict[str, Any]:
        """
        Retrieve WAN status for all WAN links.
        
        Returns:
            dict: Dictionary containing WAN status information
                 Format: {
                     "connection": [
                         {
                             "id": str,           # WAN interface ID
                             "name": str,         # WAN interface name
                             "status": str,       # Connection status (e.g., "connected", "disconnected")
                             "type": str,         # WAN connection type
                             "ipAddress": str,    # IP address if connected
                             "gateway": str,      # Gateway IP address
                             "dns": list,         # List of DNS servers
                             "mac": str,          # MAC address of the WAN interface
                             # Additional fields may be present depending on the router model
                         },
                         # Additional WAN connections...
                     ]
                 }
        """
        try:
            response = await self._make_api_request("status.wan", public_api=True)
            
            # Check for error response
            if "stat" in response and response["stat"] == "fail":
                _LOGGER.error("API error: %s (code: %s)", 
                             response.get("message", "Unknown error"), 
                             response.get("code", "Unknown"))
                return {"connection": []}
                
            # Handle different response formats
            if "stat" in response and response["stat"] == "ok":
                if "response" in response:
                    response_data = response["response"]
                    
                    # Extract WAN interfaces (numeric keys)
                    wans = []
                    for key, value in response_data.items():
                        # Skip non-WAN keys like 'order' or 'supportGatewayProxy'
                        if isinstance(key, str) and key.isdigit():
                            # Add the WAN ID to each WAN object
                            value["id"] = key
                            value["name"] = value.get("name", f"WAN {key}")
                            value["status"] = value.get("status", "unknown")
                            wans.append(value)
                    
                    _LOGGER.debug("Processed %d WAN interfaces", len(wans))
                    return {"connection": wans}
            
            # Handle alternative format where WAN data is directly in the response
            if "connection" in response:
                _LOGGER.debug("Found 'connection' key in WAN data")
                return response
            
            _LOGGER.warning("Unexpected WAN data format: %s", response)
            return {"connection": []}
                    
        except aiohttp.ClientError as e:
            _LOGGER.error("Error retrieving WAN status: %s", e)
            return {"connection": []}
        except json.JSONDecodeError:
            _LOGGER.error("Unexpected response format for WAN status")
            return {"connection": []}
    
    async def get_clients(self) -> Dict[str, Any]:
        """
        Retrieve information about all connected clients.
        
        Returns:
            dict: Dictionary containing client information
                 Format: {
                     "client": [
                         {
                             "mac": str,           # MAC address of the client
                             "name": str,          # Client name or hostname
                             "ip": str,            # IP address assigned to the client
                             "rssi": int,          # Signal strength (for wireless clients)
                             "connected": bool,    # Always True for connected clients
                             "interface": str,     # Network interface the client is connected to
                             "vlan": str,          # VLAN the client is connected to (if applicable)
                             "ssid": str,          # Wireless SSID (for wireless clients)
                             # Additional fields may be present depending on the router model
                         },
                         # Additional clients...
                     ]
                 }
        """
        try:
            response = await self._make_api_request("status.client", public_api=True)
            
            # Check for error response
            if "stat" in response and response["stat"] == "fail":
                _LOGGER.error("API error: %s (code: %s)", 
                             response.get("message", "Unknown error"), 
                             response.get("code", "Unknown"))
                return {"client": []}
                
            # Handle different response formats
            if "stat" in response and response["stat"] == "ok":
                if "response" in response:
                    if "list" in response["response"]:
                        clients = response["response"]["list"]
                        for client in clients:
                            # Ensure each client has the required fields
                            client["connected"] = True  # If it's in the list, it's connected
                            client["mac"] = client.get("mac", "unknown")
                            client["name"] = client.get("name", client.get("hostname", "Unknown Device"))
                        
                        _LOGGER.debug("Processed %d clients", len(clients))
                        return {"client": clients}
            
            # Handle alternative format where client data is directly in the response
            if "client" in response:
                _LOGGER.debug("Found 'client' key in client data")
                return response
            
            _LOGGER.warning("Unexpected client data format: %s", response)
            return {"client": []}
                    
        except aiohttp.ClientError as e:
            _LOGGER.error("Error retrieving client information: %s", e)
            return {"client": []}
        except json.JSONDecodeError:
            _LOGGER.error("Unexpected response format for client information")
            return {"client": []}

    async def get_system_info(self) -> Dict[str, Any]:
        """
        Retrieve device information and system time from the router (undocumented web-UI API).
        
        Returns:
            dict: Dictionary containing combined system information
                 Format: {
                     "device_info": {
                         "serial_number": str,      # Router serial number
                         "name": str,               # Router hostname
                         "model": str,              # Router model
                         "product_code": str,       # Product code
                         "hardware_revision": str,  # Hardware revision
                         "firmware_version": str,   # Firmware version
                         "host": str,               # Host address
                         "pepvpn_version": str      # PepVPN version
                     },
                     "system_time": {
                         "time_string": str,        # Formatted time string
                         "timestamp": int,          # UNIX timestamp
                         "timezone": str            # Timezone
                     }
                 }
        """
        try:
            # Combined info types in a single request
            response = await self._make_api_request("status.system.info", public_api=False, infoType="device%20systemTime")
                
            result = {
                "device_info": {},
                "system_time": {}
            }
                
            # Process the response
            if response.get("stat") == "ok" and "response" in response:
                # Process device information
                device_info = response["response"].get("device", {})
                if device_info:
                    result["device_info"] = {
                        "serial_number": device_info.get("serialNumber", ""),
                        "name": device_info.get("name", ""),
                        "model": device_info.get("model", ""),
                        "product_code": device_info.get("productCode", ""),
                        "hardware_revision": device_info.get("hardwareRevision", ""),
                        "host": device_info.get("host", ""),
                        "pepvpn_version": device_info.get("pepvpnVersion", "")
                    }
                
                # Process system time information
                system_time = response["response"].get("systemTime", {})
                if system_time:
                    result["system_time"] = {
                        "time_string": system_time.get("string", ""),
                        "timestamp": system_time.get("timestamp", 0),
                        "timezone": system_time.get("timezone", "")
                    }
                
                return result
            else:
                _LOGGER.warning("Unexpected combined system information format: %s", response)
                return result
                    
        except Exception as e:
            _LOGGER.error("Error fetching combined system information: %s", e)
            return {
                "device_info": {},
                "system_time": {}
            }

    async def get_device_info(self) -> Dict[str, Any]:
        """
        Retrieve device information from the router.
        
        Returns:
            dict: Dictionary containing device information
                 Format: {
                     "device_info": {
                         "serial_number": str,      # Router serial number
                         "name": str,               # Router hostname
                         "model": str,              # Router model
                         "product_code": str,       # Product code
                         "hardware_revision": str,  # Hardware revision
                         "firmware_version": str,   # Firmware version
                         "host": str,               # Host address
                         "pepvpn_version": str      # PepVPN version
                     }
                 }
        """
        # Try to get data from the combined system info call for efficiency
        try:
            system_info = await self.get_system_info()
            if system_info and system_info.get("device_info"):
                return {"device_info": system_info["device_info"]}
        except Exception as e:
            _LOGGER.warning("Error getting device info from combined call, falling back to dedicated call: %s", e)
        
        # Fallback to the original method if combined call fails
        try:
            response = await self._make_api_request("status.system.info", public_api=False, infoType="device")
                
            # Process the response
            if response.get("stat") == "ok" and "response" in response:
                device_info = response["response"].get("device", {})
                
                if device_info:
                    info = {
                        "serial_number": device_info.get("serialNumber", ""),
                        "name": device_info.get("name", ""),
                        "model": device_info.get("model", ""),
                        "product_code": device_info.get("productCode", ""),
                        "hardware_revision": device_info.get("hardwareRevision", ""),
                        "host": device_info.get("host", ""),
                        "pepvpn_version": device_info.get("pepvpnVersion", "")
                    }
                    return {"device_info": info}
            
            _LOGGER.warning("Unexpected device information format: %s", response)
            return {"device_info": {}}
                    
        except Exception as e:
            _LOGGER.error("Error fetching device information: %s", e)
            return {"device_info": {}}

    async def get_traffic_stats(self) -> Dict[str, Any]:
        """
        Retrieve traffic statistics from the router using undocumented API.
        
        Returns:
            dict: Dictionary containing traffic statistics for each WAN interface
                 Format: {
                     "stats": [
                         {
                             "wan_id": str,         # WAN interface ID
                             "name": str,           # WAN interface name
                             "rx_bytes": int,       # Total received bytes
                             "tx_bytes": int,       # Total transmitted bytes
                             "rx_rate": int,        # Current receive rate in bits/sec
                             "tx_rate": int,        # Current transmit rate in bits/sec
                             "unit": str            # Unit (bits/sec for rates, bytes for totals)
                         }
                     ]
                 }
        """
        try:
            # Make API request for bandwidth and traffic data
            response = await self._make_api_request("status.traffic", public_api=False)
            
            if response.get("stat") == "ok" and "response" in response:
                data = response["response"]
                _LOGGER.debug("Received traffic data: %s", data)
                
                wan_stats = []
                
                # Extract traffic data (total transferred) and bandwidth data (current rates)
                if "traffic" in data and "bandwidth" in data:
                    traffic_data = data["traffic"]
                    bandwidth_data = data["bandwidth"]
                    
                    # Get the ordered list of WAN interfaces if available
                    wan_order = traffic_data.get("order", [])
                    
                    # Process each WAN interface
                    for wan_id in wan_order:
                        wan_id_str = str(wan_id)
                        
                        # Get traffic data (total bytes transferred)
                        if wan_id_str in traffic_data:
                            wan_traffic = traffic_data[wan_id_str]
                            traffic_unit = traffic_data.get("unit", "MB")
                            
                            # Get bandwidth data (current rates)
                            wan_bandwidth = bandwidth_data.get(wan_id_str, {})
                            bandwidth_unit = bandwidth_data.get("unit", "kbps")
                            
                            # Extract values
                            name = wan_traffic.get("name", f"WAN {wan_id_str}")
                            
                            # Get total transferred data
                            overall_traffic = wan_traffic.get("overall", {})
                            rx_bytes = overall_traffic.get("download", 0)
                            tx_bytes = overall_traffic.get("upload", 0)
                            
                            # Convert to bytes based on unit
                            if traffic_unit == "MB":
                                rx_bytes = rx_bytes * 1024 * 1024  # Convert MB to bytes
                                tx_bytes = tx_bytes * 1024 * 1024  # Convert MB to bytes
                            elif traffic_unit == "KB":
                                rx_bytes = rx_bytes * 1024  # Convert KB to bytes
                                tx_bytes = tx_bytes * 1024  # Convert KB to bytes
                            
                            # Get current rates
                            overall_bandwidth = wan_bandwidth.get("overall", {})
                            rx_rate = overall_bandwidth.get("download", 0)
                            tx_rate = overall_bandwidth.get("upload", 0)
                            
                            # Convert to bits per second based on unit
                            if bandwidth_unit == "kbps":
                                rx_rate = rx_rate * 1000  # Convert kbps to bps
                                tx_rate = tx_rate * 1000  # Convert kbps to bps
                            elif bandwidth_unit == "Mbps":
                                rx_rate = rx_rate * 1000 * 1000  # Convert Mbps to bps
                            
                            stats_entry = {
                                "wan_id": wan_id_str,
                                "name": name,
                                "rx_bytes": rx_bytes,
                                "tx_bytes": tx_bytes,
                                "rx_rate": rx_rate,
                                "tx_rate": tx_rate,
                                "unit": "bits/sec" if rx_rate or tx_rate else "bytes"
                            }
                            wan_stats.append(stats_entry)
                
                if wan_stats:
                    _LOGGER.debug("Processed traffic stats for %d WAN interfaces", len(wan_stats))
                    return {"stats": wan_stats}
                else:
                    _LOGGER.warning("No WAN interfaces found in traffic data")
            else:
                _LOGGER.warning("Invalid response format from traffic API")
            
            # If processing fails, return empty stats
            return {"stats": []}
            
        except Exception as e:
            _LOGGER.error("Error retrieving traffic statistics: %s", e)
            return {"stats": []}

    async def get_location(self) -> Dict[str, Any]:
        """
        Retrieve location information from the router using GPS data.
        
        Returns:
            dict: Dictionary containing location information
                Format: {
                    "gps": bool,                  # Whether GPS is enabled
                    "type": str,                  # GPS type
                    "location": {
                        "timeElapsed": int,       # Time elapsed since last update
                        "timestamp": int,         # UNIX timestamp
                        "latitude": float,        # Latitude in degrees
                        "longitude": float,       # Longitude in degrees
                        "altitude": float,        # Altitude in meters
                        "speed": float,           # Speed in km/h
                        "heading": float,         # Heading in degrees
                        "accuracy": float,        # Accuracy in meters
                        "pdop": float,            # Position Dilution of Precision
                        "hdop": float,            # Horizontal Dilution of Precision
                        "vdop": float             # Vertical Dilution of Precision
                    }
                }
        """
        try:
            # Documented /api/info.location (fw 8.0.1+).
            response = await self._make_api_request("info.location", public_api=True)
            
            if "stat" in response and response["stat"] == "ok" and "response" in response:
                location_data = response["response"]
                _LOGGER.debug("Received location data: %s", location_data)
                
                # Check if GPS is explicitly set to False
                if location_data.get("gps") is False:
                    _LOGGER.debug("Router does not have GPS capability")
                    return {"gps": False, "type": "Unknown", "location": {}}
                
                # Process the location data to ensure consistent attribute names
                if "location" in location_data and location_data.get("gps", False):
                    loc = location_data["location"]
                    
                    # Calculate GPS accuracy from horizontal dilution of precision if available
                    if "hdop" in loc and loc.get("hdop") is not None:
                        # GPS accuracy is typically estimated as HDOP * base_precision
                        # Using 5 meters as the base precision
                        loc["accuracy"] = float(loc["hdop"]) * 5.0
                    
                    # Ensure heading is present if not available
                    if "heading" not in loc:
                        # Default heading if not available
                        loc["heading"] = None
                
                return location_data
            
            _LOGGER.warning("Unexpected location data format: %s", response)
            return {"gps": False, "type": "Unknown", "location": {}}
            
        except Exception as e:
            _LOGGER.error("Error retrieving location information: %s", e)
            return {"gps": False, "type": "Unknown", "location": {}}
            
    # ------------------------------------------------------------------
    # Commands and configuration changes (documented /api endpoints)
    # ------------------------------------------------------------------

    async def _post(self, func: str, body: Optional[Dict] = None) -> Dict:
        """POST to /api/<func>; raise PeplinkCommandError unless stat is ok."""
        response = await self._make_api_request(
            func, public_api=True, method="POST", data=body if body is not None else {}
        )
        if not isinstance(response, dict) or response.get("stat") != "ok":
            message = response.get("message") if isinstance(response, dict) else None
            raise PeplinkCommandError(f"{func} failed: {message or response}")
        if response.get("warning"):
            _LOGGER.warning("%s: %s", func, response["warning"])
        return response

    async def get_firmware_version(self) -> str | None:
        """Version of the firmware in use, from the documented info.firmware."""
        result = await self._get("info.firmware")
        if not isinstance(result, dict):
            return None
        for key in result.get("order") or [k for k in result if k != "order"]:
            image = result.get(str(key)) or {}
            if image.get("inUse"):
                return image.get("version")
        return None

    async def apply_config(self) -> None:
        """Apply pending configuration changes (cmd.config.apply, fw 7.1.1+)."""
        await self._post("cmd.config.apply")

    async def set_wan_enabled(self, conn_id: int | str, enabled: bool) -> None:
        """Enable or disable a WAN connection, then apply (fw 8.1.1+)."""
        await self._post(
            "config.wan.connection",
            {"action": "update", "list": [{"id": int(conn_id), "enable": bool(enabled)}]},
        )
        await self.apply_config()

    async def set_wan_routing_mode(self, conn_id: int | str, mode: str) -> None:
        """Set a WAN's routing mode to 'NAT' or 'IP Forwarding', then apply."""
        if mode not in ROUTING_MODES:
            raise ValueError(f"Routing mode must be one of {ROUTING_MODES}, not {mode!r}")
        await self._post(
            "config.wan.connection",
            {"action": "update", "list": [{"id": int(conn_id), "connection": {"routingMode": mode}}]},
        )
        await self.apply_config()

    async def reset_cellular_module(self, conn_id: int | str) -> None:
        """Power-cycle a cellular modem (cmd.cellularModule.reset, fw 8.0.0+)."""
        await self._post("cmd.cellularModule.reset", {"connId": int(conn_id)})

    async def rescan_cellular_network(self, conn_id: int | str) -> None:
        """Rescan for carriers (cmd.cellularModule.rescanNetwork, fw 8.0.0+)."""
        await self._post("cmd.cellularModule.rescanNetwork", {"connId": int(conn_id)})

    async def get_cellular_module_temperature(self, conn_id: int | str) -> float | None:
        """Cellular modem temperature in °C (status.cellularModule.temperature, fw 8.4.1+)."""
        response = await self._post("status.cellularModule.temperature", {"connId": int(conn_id)})
        value = (response.get("response") or {}).get("temperature")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    async def reboot(self) -> None:
        """Reboot the router (cmd.system.reboot, fw 8.2.0+)."""
        await self._post("cmd.system.reboot")

    async def starlink_command(self, conn_id: int | str, action: str) -> None:
        """Reboot, stow or unstow a Starlink dish on a WAN (cmd.starlink, fw 8.5.0+, Care Plan)."""
        if action not in STARLINK_ACTIONS:
            raise ValueError(f"Starlink action must be one of {STARLINK_ACTIONS}, not {action!r}")
        await self._post("cmd.starlink", {"connId": int(conn_id), "action": action})

    # ------------------------------------------------------------------
    # Wi-Fi WAN: scan, join, result, disconnect, forget (fw 7.1.1+)
    # ------------------------------------------------------------------

    async def _get(self, func_with_query: str) -> Any:
        """GET /api/<func>?<query>; return 'response' or raise PeplinkCommandError."""
        response = await self._make_api_request(func_with_query, public_api=True)
        if not isinstance(response, dict) or response.get("stat") != "ok":
            message = response.get("message") if isinstance(response, dict) else None
            raise PeplinkCommandError(f"{func_with_query.split('?')[0]} failed: {message or response}")
        return response.get("response")

    async def get_wan_config(self) -> dict[str, Any]:
        """config.wan.connection for all WANs (includes saved Wi-Fi profiles)."""
        result = await self._get("config.wan.connection")
        return result if isinstance(result, dict) else {}

    async def wifi_scan(self, conn_id: int | str) -> list[dict[str, Any]]:
        """Nearby access points seen by a Wi-Fi WAN, strongest first.

        infoType=config adds each AP's saved-profile info ("config.profileId").
        """
        result = await self._get(
            f"cmd.wifi.scan?connId={int(conn_id)}&infoType=config&sortBy=signal&sortOrder=desc"
        )
        return result if isinstance(result, list) else []

    async def wifi_connect(
        self,
        conn_id: int | str,
        ssid: str,
        password: str | None = None,
        security_policy: str | None = None,
    ) -> None:
        """Join a network on a Wi-Fi WAN (saved/open: SSID only; else with key)."""
        try:
            from .wifi import connect_body
        except ImportError:  # loaded standalone (tests, standalone_test.py)
            from wifi import connect_body

        await self._post("cmd.wifi.connect", connect_body(conn_id, ssid, password, security_policy))

    async def wifi_result(self, conn_id: int | str) -> dict[str, Any]:
        """Outcome of the last join attempt (CONNECTED, PSK_AUTH_FAIL, ...)."""
        result = await self._get(f"cmd.wifi.result?connId={int(conn_id)}")
        return result if isinstance(result, dict) else {}

    async def wifi_disconnect(self, conn_id: int | str) -> None:
        await self._post("cmd.wifi.disconnect", {"connId": int(conn_id)})

    async def wifi_forget(self, conn_id: int | str, ssid: str, security_policy: str) -> None:
        """Delete a saved network profile (disconnects if it is in use)."""
        await self._post(
            "cmd.wifi.forget",
            {"connId": int(conn_id), "ssid": ssid, "securityPolicy": security_policy},
        )

    async def close(self) -> None:
        """Close the session if we created it."""
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._own_session = False
            self._connected = False