"""Symmetric encryption for callback secrets.

We store the client-supplied `callback_secret` encrypted (not hashed) because
the delivery worker needs the raw value back to compute the HMAC signature on
each outgoing webhook. A one-way hash would make that impossible.
"""

import hmac

from cryptography.fernet import Fernet
from fastapi import Header, HTTPException, status

from app.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().secret_encryption_key.encode())


def encrypt_secret(raw: str) -> str:
    return _fernet().encrypt(raw.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


async def require_api_key(authorization: str | None = Header(None)) -> None:
    """FastAPI dependency gating every /markets/* and /scrape-failures route.

    subscribe isn't a cheap read — it kicks off a recurring search+scrape+LLM
    pipeline per market, so an open endpoint is a resource-amplification DoS
    vector. PATCH can rewrite an existing market's callback_url/callback_secret,
    which would let an attacker who merely knows a market_id (not a secret —
    market ids are routinely public) hijack its webhook stream. Left disabled
    (api_key=None) only for local dev / the /ui page.
    """
    api_key = get_settings().api_key
    if api_key is None:
        return

    expected = f"Bearer {api_key}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
