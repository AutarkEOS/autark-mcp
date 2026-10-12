"""Resolve mock-service API base URL, demo credentials, and timeouts."""

import ipaddress
import os
from urllib.parse import urlparse

# Host port of the backend started by the Autark repo's stack scripts (mock-servivce/config/backend.config.json).
DEFAULT_API_URL = "http://127.0.0.1:500"
DEFAULT_TIMEOUT_SECONDS = 30.0
FUNDING_TIMEOUT_SECONDS = 65.0
COMPUTE_RUN_TIMEOUT_SECONDS = 90.0
# A file-input API call (OCR, transcription, image analysis) waits on a model upstream; the marketplace gives up after 45 s.
API_FILE_TIMEOUT_SECONDS = 75.0


class ConfigError(ValueError):
    pass


def trusted_hosts() -> set[str]:
    """Remote marketplace hosts a human listed in MOCK_SERVICE_TRUSTED_HOSTS (comma separated)."""
    return {host.strip().lower() for host in os.environ.get("MOCK_SERVICE_TRUSTED_HOSTS", "").split(",") if host.strip()}


def _validate_loopback_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError("API URL must use http or https.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("API URL must not include credentials, query, or fragment.")
    host = parsed.hostname
    if not host:
        raise ConfigError("API URL must include a host.")
    if host.lower() in trusted_hosts():
        # The session token and payments leave this machine, so a remote host needs TLS.
        if parsed.scheme != "https":
            raise ConfigError("A trusted remote API URL must use https.")
    elif host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ConfigError(
                "API URL host must be loopback (127.0.0.1, ::1, or localhost) or listed in MOCK_SERVICE_TRUSTED_HOSTS."
            ) from error
        if not address.is_loopback:
            raise ConfigError("API URL host must be loopback or listed in MOCK_SERVICE_TRUSTED_HOSTS, to avoid SSRF.")
    origin = url.rstrip("/")
    # Paths are joined as f"{base}/api/...", so a configured ".../api" suffix would double up.
    if origin.lower().endswith("/api"):
        origin = origin[: -len("/api")]
    if urlparse(origin).path not in ("", "/"):
        raise ConfigError("API URL must be an origin such as http://127.0.0.1:5000, without a path.")
    return origin.rstrip("/")


def api_base_url() -> str:
    """Return mock-service origin, e.g. http://127.0.0.1:5000."""
    override = os.environ.get("MOCK_SERVICE_API_URL", "").strip()
    return _validate_loopback_base_url(override or DEFAULT_API_URL)


def env_credentials() -> tuple[str, str] | None:
    """Optional demo login from the environment. Never log the password."""
    username = os.environ.get("MOCK_SHOP_USERNAME", "").strip()
    password = os.environ.get("MOCK_SHOP_PASSWORD", "")
    if username and password:
        return username, password
    return None
