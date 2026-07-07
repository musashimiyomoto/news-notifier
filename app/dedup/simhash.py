"""Cheap pre-filter for near-duplicate titles within the same processing batch,
before paying for an embedding call. Not a replacement for the vector dedup —
just avoids wasting embedding calls on obvious title-level repeats."""

import hashlib
import re

_TOKEN_RE = re.compile(r"\w+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def simhash(text: str, bits: int = 64) -> int:
    tokens = _tokenize(text)
    if not tokens:
        return 0

    weights = [0] * bits
    for token in tokens:
        digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(bits):
            weights[i] += 1 if (digest >> i) & 1 else -1

    fingerprint = 0
    for i in range(bits):
        if weights[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_signed_64(fingerprint: int) -> int:
    """Reinterpret an unsigned 64-bit simhash as signed, for storage in a
    BigInteger column (Postgres int8 is signed; simhash's top bit can be
    set, which would otherwise overflow it)."""
    return fingerprint - (1 << 64) if fingerprint >= (1 << 63) else fingerprint


def from_signed_64(value: int) -> int:
    """Inverse of to_signed_64: reinterpret a signed int8 read back from the
    BigInteger column as the original unsigned 64-bit simhash. Required before
    hamming_distance, which XORs in Python's arbitrary-precision two's
    complement and would miscount bits on a negative operand."""
    return value & ((1 << 64) - 1)
