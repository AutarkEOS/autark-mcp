"""The worker's secrets live in the operating system's credential store, never in a file.

Windows Credential Manager, the macOS Keychain or the Secret Service on Linux (GNOME Keyring,
KWallet). Without one of them the worker refuses to start: there is no plaintext fallback.

It also holds the vendor's payout key. The worker creates it here, so the marketplace only
learns the address and the sales are paid to a key nobody else has.
"""

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import keyring
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from keyring.errors import KeyringError

SERVICE = "autark-worker"
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class VaultError(RuntimeError):
    pass


def _backend() -> Any:
    backend = keyring.get_keyring()
    module = type(backend).__module__
    # The fail and null backends store nothing; keyrings.alt keeps secrets in files, which is what this module avoids.
    if module.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")) or backend.priority <= 0:
        raise VaultError("No system credential store is available (Windows Credential Manager, macOS Keychain, or a Secret Service "
                         "such as GNOME Keyring or KWallet on Linux). The worker does not keep its secrets in plain files, so it "
                         "cannot start. On Linux, start a desktop session or `gnome-keyring-daemon` with D-Bus.")
    return backend


def store_name() -> str:
    return type(_backend()).__name__


def load(account: str) -> dict[str, Any] | None:
    try:
        raw = _backend().get_password(SERVICE, account)
    except KeyringError as error:
        raise VaultError(f"The credential store refused to read the worker secret: {error}") from None
    if raw is None:
        return None
    try:
        secret = json.loads(raw)
    except ValueError:
        raise VaultError(f"The worker secret for {account} in the credential store is not readable.") from None
    return secret if isinstance(secret, dict) else None


def save(account: str, secret: dict[str, Any]) -> None:
    try:
        _backend().set_password(SERVICE, account, json.dumps(secret, separators=(",", ":")))
    except KeyringError as error:
        raise VaultError(f"The credential store refused to keep the worker secret: {error}") from None


def delete(account: str) -> None:
    try:
        _backend().delete_password(SERVICE, account)
    except KeyringError:
        pass


# --- payout key -------------------------------------------------------------

def base58(data: bytes) -> str:
    number, text = int.from_bytes(data, "big"), ""
    while number:
        number, digit = divmod(number, 58)
        text = _ALPHABET[digit] + text
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + text


def new_payout_seed() -> str:
    seed = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return base64.b64encode(seed).decode()


def _key(seed: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed))


def payout_address(seed: str) -> str:
    return base58(_key(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def sign(seed: str, message: bytes) -> str:
    return base58(_key(seed).sign(message))


def solana_keypair(seed: str) -> list[int]:
    """The 64-byte secret key in the JSON form `solana-keygen` and wallets import."""
    key = _key(seed)
    return list(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()) + key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


# --- files ------------------------------------------------------------------

_owner_sid: str | None = None


def restrict_to_owner(path: Path) -> None:
    """Only the current user may read or change the file. POSIX mode 600; on Windows an ACL without inherited entries."""
    if os.name != "nt":
        path.chmod(0o600)
        return
    global _owner_sid
    if _owner_sid is None:
        # "DOMAIN\\user","S-1-5-21-..."
        answer = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=15)
        _owner_sid = answer.stdout.strip().split(",")[-1].strip('"')
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{_owner_sid}:(F)"],
                   capture_output=True, text=True, check=True, timeout=15)
