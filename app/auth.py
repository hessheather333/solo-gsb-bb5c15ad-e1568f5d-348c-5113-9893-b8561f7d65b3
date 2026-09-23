"""API-key authentication.

Keys carry a public identifier (``key_id``) and a high-entropy secret. The
plaintext token issued to clients has the shape ``dpk_<key_id>.<secret>``;
only the SHA-256 hash of the full token is stored. Authentication compares
the presented token against the stored hash in constant time (both the HMAC
comparison and a fixed dummy digest for unknown key ids), and rejects revoked
or expired keys.

An admin token (env ``DP_ADMIN_API_KEY``) guards key management. When the env
var is unset, a random token is generated once and persisted (mode 0600) next
to the database, mirroring the server-key bootstrap in ``security.py``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

logger = logging.getLogger("dp-api.auth")

# dpk_ + 32 hex key id + "." + 43 url-safe secret chars (32 bytes).
_TOKEN_RE = re.compile(r"^dpk_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")

_ADMIN_PREFIX = "admin_"

# Compared against when no row matches the presented key id, so that the
# "unknown key" path performs the same hash work as the real comparison.
_DUMMY_HASH = hashlib.sha256(b"dp-api-missing-key-dummy").hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Current UTC time as a second-precision ISO-8601 string (with 'Z')."""
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hash_token(token: str) -> str:
    """SHA-256 hex digest of the full plaintext token.

    The token itself contains 256 bits of CSPRNG entropy, so a fast digest
    without a server-side pepper is sufficient (same approach as GitHub PAT
    storage); brute force is infeasible without the token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(key_id, token, key_prefix)`` for a freshly minted key."""
    key_id = secrets.token_hex(16)
    secret = secrets.token_urlsafe(32)  # 43 url-safe characters
    token = f"dpk_{key_id}.{secret}"
    key_prefix = token[: len("dpk_") + 8]
    return key_id, token, key_prefix


def _split_token(token: str) -> tuple[str, str] | None:
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        return None
    return match.group(1), token


@dataclass(frozen=True)
class Principal:
    """An authenticated API key."""

    key_id: str
    dataset_ids: frozenset[str]

    def allows(self, dataset_id: str) -> bool:
        return dataset_id in self.dataset_ids


@dataclass(frozen=True)
class AdminPrincipal:
    """The bootstrap admin token; manages keys and the dataset catalog."""


def authenticate_key(token: str, conn: sqlite3.Connection) -> Principal | None:
    """Resolve and validate a presented API key.

    Returns ``None`` for malformed, unknown, revoked or expired tokens. A
    dummy constant-time comparison runs when the key id is unknown so the
    response path does not leak which ids exist via timing.
    """
    parsed = _split_token(token)
    if parsed is None:
        return None
    key_id, full_token = parsed

    row = conn.execute(
        "SELECT key_hash, expires_at, revoked FROM api_keys WHERE key_id = ?",
        (key_id,),
    ).fetchone()
    if row is None:
        hmac.compare_digest(hash_token(token), _DUMMY_HASH)
        return None

    presented_hash = hash_token(full_token)
    if not hmac.compare_digest(presented_hash, row["key_hash"]):
        return None
    if int(row["revoked"]) != 0:
        return None
    expires = parse_iso(row["expires_at"])
    if expires is not None and expires <= _now():
        return None

    scope_rows = conn.execute(
        "SELECT dataset_id FROM api_key_scopes WHERE key_id = ?",
        (key_id,),
    ).fetchall()
    return Principal(
        key_id=key_id,
        dataset_ids=frozenset(r["dataset_id"] for r in scope_rows),
    )


def authenticate_admin(token: str, admin_token: str) -> bool:
    """True iff ``token`` is the configured admin token.

    Data-plane API keys never manage keys: only the env/persisted admin
    token is accepted on /auth/keys.
    """
    if not token:
        return False
    return hmac.compare_digest(token, admin_token)


def resolve_admin_key() -> str:
    """Return the admin token from the environment, persisting a random one.

    When ``DP_ADMIN_API_KEY`` is unset a random token is generated once and
    stored (mode 0600) next to the database so restarts keep accepting it.
    """
    if settings.admin_api_key:
        return settings.admin_api_key

    key_path = Path(settings.database_path).with_name("admin.key")
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()

    token = _ADMIN_PREFIX + secrets.token_urlsafe(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process/worker generated the token first.
        return key_path.read_text(encoding="utf-8").strip()
    try:
        os.write(fd, token.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    logger.warning(
        "DP_ADMIN_API_KEY not set; generated and persisted an admin token "
        "(mode 0600) to %s — read it from that file to manage API keys. "
        "Set DP_ADMIN_API_KEY explicitly for production deployments.",
        key_path,
    )
    return token
