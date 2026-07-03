import hashlib
import hmac
import json

import httpx


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_webhook(url: str, secret: str, payload: dict, timeout: float = 15.0) -> tuple[int | None, str | None]:
    """POST the payload with an HMAC signature header. Returns (status_code, error).
    Never raises — callers decide retry policy based on the returned status."""
    body = json.dumps(payload, default=str).encode()
    signature = sign_payload(secret, body)
    headers = {
        "Content-Type": "application/json",
        "X-Signature": f"sha256={signature}",
        "Idempotency-Key": str(payload.get("batch_id", "")),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, None
    except httpx.HTTPError as exc:
        return None, str(exc)
