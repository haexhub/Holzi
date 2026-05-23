"""AES-256-GCM helpers for credentials at rest.

Plaintext is stored as utf-8 strings (raw API keys, or the JSON blob the
`claude` CLI writes to `.credentials.json`). Ciphertext is hex-encoded so
the schema stays text-only.

Master key resolution order: `HERMES_SECRET_KEY` env var → on-disk
keyfile → freshly generated keyfile (mode 0600). The generated-keyfile
fallback only makes sense for single-instance deployments — if you ever
run more than one Hermes pointing at a shared DB, set the env var.
"""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_LEN = 32
_IV_LEN = 12


class InvalidSecretKeyError(ValueError):
    """`HERMES_SECRET_KEY` was set but isn't 64 hex chars."""


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """Hex-encoded ciphertext + GCM auth tag + IV. Maps 1:1 to the three
    `*_iv` / `*_tag` / `*_data` columns in `llm_credentials`."""

    iv: str
    tag: str
    data: str


class Encryptor:
    """Stateless AES-256-GCM wrapper. Hold one instance per app — the
    `AESGCM` object internally caches the key schedule, so re-creating
    it per call is wasteful."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LEN:
            raise InvalidSecretKeyError(
                f"master key must be {_KEY_LEN} bytes, got {len(key)}"
            )
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str) -> EncryptedBlob:
        iv = secrets.token_bytes(_IV_LEN)
        ct_and_tag = self._aead.encrypt(iv, plaintext.encode("utf-8"), None)
        # AESGCM returns ciphertext || tag (last 16 bytes). We split for
        # storage so the columns mirror Specifyr's schema 1:1.
        ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
        return EncryptedBlob(iv=iv.hex(), tag=tag.hex(), data=ct.hex())

    def decrypt(self, blob: EncryptedBlob) -> str:
        iv = bytes.fromhex(blob.iv)
        ct_and_tag = bytes.fromhex(blob.data) + bytes.fromhex(blob.tag)
        return self._aead.decrypt(iv, ct_and_tag, None).decode("utf-8")


def resolve_master_key(
    *,
    secret_key_env: str | None,
    key_file_path: Path,
) -> bytes:
    """Resolve the master key from env or filesystem.

    Order:
      1. `HERMES_SECRET_KEY` env var (validated as 64 hex chars).
      2. Existing keyfile at `key_file_path`.
      3. Generate a fresh 32-byte key, write it to `key_file_path` (mode
         0600), return it.
    """
    if secret_key_env:
        return _parse_hex_key(secret_key_env)
    if key_file_path.exists():
        return _parse_hex_key(key_file_path.read_text(encoding="utf-8").strip())
    key = secrets.token_bytes(_KEY_LEN)
    key_file_path.parent.mkdir(parents=True, exist_ok=True)
    # Use os.open so we can set mode at create-time — Path.write_text
    # creates with the inherited umask first and chmod is a separate
    # syscall, leaving a tiny window where the keyfile is world-readable.
    fd = os.open(str(key_file_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key.hex().encode("utf-8"))
    finally:
        os.close(fd)
    return key


def _parse_hex_key(hex_str: str) -> bytes:
    if len(hex_str) != _KEY_LEN * 2:
        raise InvalidSecretKeyError(
            f"master key must be {_KEY_LEN * 2} hex chars, got {len(hex_str)}"
        )
    try:
        return bytes.fromhex(hex_str)
    except ValueError as exc:
        raise InvalidSecretKeyError(f"master key is not valid hex: {exc}") from exc
