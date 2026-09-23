"""Server-side key management.

The HMAC key used for deterministic noise comes from the ``DP_SERVER_KEY``
environment variable. When it is not provided, a random 32-byte key is
generated once and persisted (mode 0600) next to the database so that
restarts keep producing identical noise. Volatile storage (e.g. an
ephemeral container without a mounted volume) means the key rotates on
restart, so production deployments should set ``DP_SERVER_KEY`` explicitly.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import settings


def resolve_server_key() -> bytes:
    if settings.server_key:
        return settings.server_key.encode("utf-8")

    key_path = Path(settings.database_path).with_name("server.key")
    if key_path.exists():
        return key_path.read_bytes()

    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process/worker generated the key first.
        return key_path.read_bytes()
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key
