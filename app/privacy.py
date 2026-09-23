"""Deterministic, reproducible Laplace mechanism.

Noise is derived with HMAC-SHA256 keyed by the server secret. The same
(server key, dataset, query, epsilon, sensitivity, idempotency key) tuple
always produces exactly the same float noise, which is what makes retries
return byte-identical releases.
"""
from __future__ import annotations

import hashlib
import hmac
import math

# 56 bits give sub-ulp-ish resolution compared to a float64 unit interval;
# the tiny mass beyond that granularity is negligible.
_UNIT_DIVISOR = float(1 << 56)


def _uniform_unit_sample(key: bytes, message: bytes) -> float:
    """Return a deterministic pseudo-sample in the open interval (0, 1)."""
    digest = hmac.new(key, message, hashlib.sha256).digest()
    integer = int.from_bytes(digest[:7], "big")  # 56 bits
    return (integer + 1) / (_UNIT_DIVISOR + 1.0)


def _canonical_message(
    *,
    version: str,
    dataset_id: str,
    query_id: str,
    epsilon: float,
    sensitivity: float,
    idempotency_key: str,
) -> bytes:
    # ASCII unit separator keeps the encoding unambiguous regardless of
    # the characters that appear in the validated fields.
    sep = "\x1f"
    parts = [
        version,
        dataset_id,
        query_id,
        repr(float(epsilon)),
        repr(float(sensitivity)),
        idempotency_key,
    ]
    return sep.join(parts).encode("utf-8")


def derive_laplace_noise(
    *,
    key: bytes,
    dataset_id: str,
    query_id: str,
    epsilon: float,
    sensitivity: float,
    idempotency_key: str,
) -> float:
    """Deterministically draw Laplace(0, sensitivity / epsilon) noise."""
    message = _canonical_message(
        version="dp-release-v1",
        dataset_id=dataset_id,
        query_id=query_id,
        epsilon=epsilon,
        sensitivity=sensitivity,
        idempotency_key=idempotency_key,
    )
    u = _uniform_unit_sample(key, message) - 0.5  # (-0.5, 0.5)
    scale = sensitivity / epsilon
    return -scale * math.copysign(math.log1p(-2.0 * abs(u)), u)
