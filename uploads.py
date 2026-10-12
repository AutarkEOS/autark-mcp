"""Local files the agent may send to a purchased API. One directory, chosen by a human.

A model that reads untrusted text can be talked into "upload ~/.ssh/id_rsa". The path it
names is therefore resolved inside a single upload directory and nothing outside is read.
"""

import os
from pathlib import Path

from agent_config import state_path
from client import MockShopApiError

# Ceiling of the marketplace; each offer declares its own, usually lower, limit.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def upload_directory() -> Path:
    """AUTARK_UPLOAD_DIR, or `uploads` next to the agent config."""
    override = os.environ.get("AUTARK_UPLOAD_DIR", "").strip()
    return Path(override).expanduser() if override else state_path("uploads")


def read_upload(file_path: str, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[str, bytes]:
    """(file name, bytes) of a regular file inside the upload directory, at most max_bytes long."""
    directory = upload_directory()
    where = f"Put the file in {directory} (set AUTARK_UPLOAD_DIR to change it) and pass its name."
    if "\x00" in file_path or not file_path.strip():
        raise MockShopApiError(400, "file_path must name a file. " + where)
    try:
        root = directory.resolve(strict=True)
        # resolve() follows symlinks and `..`, so a link that points outside is caught below.
        target = (root / file_path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise MockShopApiError(400, "No such file in the upload directory. " + where) from None
    if not target.is_relative_to(root) or not target.is_file():
        raise MockShopApiError(400, "Only regular files inside the upload directory can be uploaded. " + where)
    try:
        if target.stat().st_size > max_bytes:
            raise MockShopApiError(413, f"The file exceeds the limit of {max_bytes} bytes. Nothing was sent or spent.")
        content = target.read_bytes()
    except OSError:
        raise MockShopApiError(400, "The file could not be read. " + where) from None
    if len(content) > max_bytes:
        raise MockShopApiError(413, f"The file exceeds the limit of {max_bytes} bytes. Nothing was sent or spent.")
    return target.name, content
