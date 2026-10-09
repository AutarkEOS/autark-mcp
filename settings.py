"""Resolve mock-service API base URL, demo credentials, and timeouts."""

import importlib.util
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse

MOCK_SERVICE_ROOT = Path(__file__).resolve().parent.parent / "mock-servivce"
DEFAULT_TIMEOUT_SECONDS = 30.0
FUNDING_TIMEOUT_SECONDS = 65.0
COMPUTE_RUN_TIMEOUT_SECONDS = 90.0


class ConfigError(ValueError):
    pass


def _validate_loopback_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError("API URL must use http or https.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("API URL must not include credentials, query, or fragment.")
    host = parsed.hostname
    if not host:
        raise ConfigError("API URL must include a host.")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ConfigError(
                "API URL host must be loopback (127.0.0.1, ::1, or localhost)."
            ) from error
        if not address.is_loopback:
            raise ConfigError("API URL host must be loopback to avoid SSRF.")
    origin = url.rstrip("/")
    # Paths are joined as f"{base}/api/...", so a configured ".../api" suffix would double up.
    if origin.lower().endswith("/api"):
        origin = origin[: -len("/api")]
    if urlparse(origin).path not in ("", "/"):
        raise ConfigError("API URL must be an origin such as http://127.0.0.1:5000, without a path.")
    return origin.rstrip("/")


def _load_mock_service_settings() -> dict:
    runtime_path = MOCK_SERVICE_ROOT / "config" / "runtime.py"
    if not runtime_path.is_file():
        raise ConfigError(f"mock-servivce config not found at {runtime_path}")
    spec = importlib.util.spec_from_file_location("mock_service_runtime", runtime_path)
    if spec is None or spec.loader is None:
        raise ConfigError("Could not load mock-servivce config.runtime")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_config(MOCK_SERVICE_ROOT / "config")


def api_base_url() -> str:
    """Return mock-service origin, e.g. http://127.0.0.1:5000."""
    override = os.environ.get("MOCK_SERVICE_API_URL", "").strip()
    if override:
        return _validate_loopback_base_url(override)

    settings = _load_mock_service_settings()
    host = settings["backend"]["host"]
    port = settings["backend"]["port"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return _validate_loopback_base_url(f"http://{host}:{port}")


def env_credentials() -> tuple[str, str] | None:
    """Optional demo login from the environment. Never log the password."""
    username = os.environ.get("MOCK_SHOP_USERNAME", "").strip()
    password = os.environ.get("MOCK_SHOP_PASSWORD", "")
    if username and password:
        return username, password
    return None
