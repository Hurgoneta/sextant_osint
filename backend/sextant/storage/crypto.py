"""Local encryption at rest.

* Master key: from ``SEXTANT_PASSPHRASE`` via scrypt (recommended), otherwise a random
  32-byte key file created with 0600 permissions in the data directory.
* Two sub-keys via HKDF: one for Fernet (AES-128-CBC + HMAC-SHA256) encryption of every
  sensitive column, one for HMAC "blind index" hashes used for IDs and dedupe, so raw
  identifiers never appear in the database in plaintext.

Threat model (stated honestly in the README): this protects a copied/backed-up/synced
database file. With the key-file mode, an attacker who can read your user account's files
can read the key too — use a passphrase if that matters to you.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_FILE = "master.key"
SALT_FILE = "passphrase.salt"


class VaultError(Exception):
    pass


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


class Vault:
    def __init__(self, master_key: bytes, mode: str) -> None:
        self.mode = mode
        self._fernet = Fernet(base64.urlsafe_b64encode(_hkdf(master_key, b"sextant/encryption/v1")))
        self._mac_key = _hkdf(master_key, b"sextant/blind-index/v1")

    @classmethod
    def open(cls, data_dir: Path, passphrase: str | None = None) -> "Vault":
        data_dir.mkdir(parents=True, exist_ok=True)
        if passphrase:
            salt_path = data_dir / SALT_FILE
            if not salt_path.exists():
                _write_private(salt_path, secrets.token_bytes(16))
            master = hashlib.scrypt(passphrase.encode(), salt=salt_path.read_bytes(), n=2 ** 15, r=8, p=1,
                                    maxmem=64 * 1024 * 1024, dklen=32)
            return cls(master, "passphrase")
        key_path = data_dir / KEY_FILE
        if not key_path.exists():
            _write_private(key_path, secrets.token_bytes(32))
        master = key_path.read_bytes()
        if len(master) != 32:
            raise VaultError(f"Key file {key_path} is corrupt")
        return cls(master, "keyfile")

    def encrypt(self, obj: Any) -> str:
        return self._fernet.encrypt(json.dumps(obj, separators=(",", ":"), default=str).encode()).decode()

    def decrypt(self, token: str | None) -> Any:
        if token is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken:
            raise VaultError("Could not decrypt local data — wrong passphrase or key file?") from None

    def blind(self, value: str, length: int = 32) -> str:
        return hmac.new(self._mac_key, value.encode(), hashlib.sha256).hexdigest()[:length]
