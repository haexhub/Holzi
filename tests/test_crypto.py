from pathlib import Path

import pytest

from hermes.crypto import (
    EncryptedBlob,
    Encryptor,
    InvalidSecretKeyError,
    resolve_master_key,
)


def test_encryptor_roundtrip() -> None:
    key = bytes.fromhex("00" * 31 + "01")
    enc = Encryptor(key)
    blob = enc.encrypt("sk-ant-secret")
    assert isinstance(blob, EncryptedBlob)
    assert blob.iv != ""
    assert blob.tag != ""
    assert blob.data != ""
    assert enc.decrypt(blob) == "sk-ant-secret"


def test_encryptor_decrypt_rejects_tampered_data() -> None:
    enc = Encryptor(bytes.fromhex("aa" * 32))
    blob = enc.encrypt("payload")
    # Flip a byte in the ciphertext — GCM auth tag must reject it.
    tampered = EncryptedBlob(
        iv=blob.iv, tag=blob.tag, data=("ff" + blob.data[2:])
    )
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        enc.decrypt(tampered)


def test_encryptor_each_call_uses_fresh_iv() -> None:
    enc = Encryptor(bytes.fromhex("11" * 32))
    a = enc.encrypt("same plaintext")
    b = enc.encrypt("same plaintext")
    # Different IV → different ciphertext for the same plaintext, otherwise
    # GCM key-IV reuse breaks confidentiality.
    assert a.iv != b.iv
    assert a.data != b.data


def test_resolve_master_key_prefers_env(monkeypatch, tmp_path: Path) -> None:
    keyfile = tmp_path / "master.key"
    keyfile.write_text("ff" * 32, encoding="utf-8")
    monkeypatch.setenv("HERMES_SECRET_KEY", "11" * 32)
    key = resolve_master_key(secret_key_env="11" * 32, key_file_path=keyfile)
    assert key == bytes.fromhex("11" * 32)


def test_resolve_master_key_reads_file_when_env_empty(tmp_path: Path) -> None:
    keyfile = tmp_path / "master.key"
    keyfile.write_text("ab" * 32, encoding="utf-8")
    key = resolve_master_key(secret_key_env=None, key_file_path=keyfile)
    assert key == bytes.fromhex("ab" * 32)


def test_resolve_master_key_generates_keyfile_when_missing(tmp_path: Path) -> None:
    keyfile = tmp_path / "subdir" / "master.key"
    key = resolve_master_key(secret_key_env=None, key_file_path=keyfile)
    assert len(key) == 32
    assert keyfile.exists()
    # Re-running picks up the generated file rather than rolling a new key,
    # otherwise every restart would orphan all previously-encrypted blobs.
    again = resolve_master_key(secret_key_env=None, key_file_path=keyfile)
    assert again == key
    # File permissions should be 0600 — credentials at rest.
    assert keyfile.stat().st_mode & 0o777 == 0o600


def test_resolve_master_key_rejects_short_env() -> None:
    with pytest.raises(InvalidSecretKeyError):
        resolve_master_key(secret_key_env="abcd", key_file_path=Path("/nope"))


def test_resolve_master_key_rejects_non_hex_env() -> None:
    with pytest.raises(InvalidSecretKeyError):
        resolve_master_key(secret_key_env="z" * 64, key_file_path=Path("/nope"))
