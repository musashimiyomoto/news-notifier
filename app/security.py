"""Symmetric encryption for callback secrets.

We store the client-supplied `callback_secret` encrypted (not hashed) because
the delivery worker needs the raw value back to compute the HMAC signature on
each outgoing webhook. A one-way hash would make that impossible.
"""

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().secret_encryption_key.encode())


def encrypt_secret(raw: str) -> str:
    return _fernet().encrypt(raw.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
