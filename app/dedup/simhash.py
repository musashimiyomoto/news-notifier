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
