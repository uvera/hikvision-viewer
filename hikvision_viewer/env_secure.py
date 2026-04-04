"""Encrypt/decrypt .env using Fernet; the symmetric key is stored in the OS keyring."""

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
            "Restore a plaintext .env backup or re-encrypt from plain text on this machine."
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


def encrypt_dotenv_move_plain(dotenv_path: Path) -> Path:
    """Read plaintext .env, write .env.enc beside it, remove .env. Returns path to .env.enc."""
    if dotenv_path.name != ".env":
        raise ValueError("expected a file named .env")
    text = dotenv_path.read_text(encoding="utf-8")
    enc_path = dotenv_path.parent / ".env.enc"
    encrypt_plaintext_to_path(text, enc_path)
    dotenv_path.unlink()
    return enc_path


__all__ = [
    "KeyringError",
    "decrypt_env_file_to_str",
    "encrypt_dotenv_move_plain",
    "encrypt_plaintext_to_path",
]
