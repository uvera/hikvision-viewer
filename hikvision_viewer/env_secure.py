"""Encrypt/decrypt `.env.enc` (dotenv-format secrets) using Fernet; key is in the OS keyring."""

from __future__ import annotations

from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError

_SERVICE = "hikvision-viewer"
_KEY_ENTRY = "dotenv-fernet-v1"


def _fernet_for_encrypt() -> Fernet:
    stored = keyring.get_password(_SERVICE, _KEY_ENTRY)
    if stored:
        return Fernet(stored.encode("ascii"))
    key = Fernet.generate_key()
    keyring.set_password(_SERVICE, _KEY_ENTRY, key.decode("ascii"))
    return Fernet(key)


def _fernet_for_decrypt() -> Fernet:
    stored = keyring.get_password(_SERVICE, _KEY_ENTRY)
    if not stored:
        raise RuntimeError(
            "No encryption key in the OS keyring — cannot decrypt .env.enc. "
            "Restore a .env.enc backup from this machine or re-enter secrets in Settings."
        )
    return Fernet(stored.encode("ascii"))


def decrypt_env_file_to_str(enc_path: Path) -> str:
    blob = enc_path.read_bytes()
    f = _fernet_for_decrypt()
    try:
        return f.decrypt(blob).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Decryption failed — wrong key, corrupted .env.enc, or keyring data was reset."
        ) from e


def encrypt_plaintext_to_path(plaintext: str, enc_path: Path) -> None:
    f = _fernet_for_encrypt()
    enc_path.write_bytes(f.encrypt(plaintext.encode("utf-8")))


__all__ = [
    "KeyringError",
    "decrypt_env_file_to_str",
    "encrypt_plaintext_to_path",
]
