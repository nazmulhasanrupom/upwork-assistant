"""Keys derived from SECRET_KEY with HKDF. One key per purpose."""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_key(secret_key: str, purpose: str) -> bytes:
    """32 bytes, different for every `purpose` string."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"upwork-assistant/{purpose}/v1".encode(),
    ).derive(secret_key.encode())


def fernet(secret_key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key(secret_key, "fernet")))


def encrypt(secret_key: str, text: str) -> str:
    return fernet(secret_key).encrypt(text.encode()).decode()


def decrypt(secret_key: str, token: str) -> str:
    return fernet(secret_key).decrypt(token.encode()).decode()
