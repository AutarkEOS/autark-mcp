"""Locate the marketplace (mock-servivce) inside a checkout of the Autark repo.

Only the end-to-end tests need it: they start the real backend. Set AUTARK_SERVICE_ROOT,
or clone Autark next to this repo (../Autark/mock-servivce).
"""
import os
import sys
from pathlib import Path


def service_root() -> Path:
    override = os.environ.get("AUTARK_SERVICE_ROOT", "").strip()
    root = Path(override).expanduser() if override else Path(__file__).resolve().parents[2] / "Autark" / "mock-servivce"
    if not (root / "backend").is_dir():
        sys.exit(f"Marketplace not found at {root}. Clone AutarkEOS/Autark next to this repo or set AUTARK_SERVICE_ROOT.")
    return root.resolve()
