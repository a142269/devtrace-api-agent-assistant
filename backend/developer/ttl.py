"""One TTL knob for the transient Redis data of the developer assistant.

Task records, plan checkpoints, run records/history and the idempotency cache all
live for the same window, so they read it here instead of repeating a literal.
Conversation memory has its own knob (DEVTRACE_MEMORY_TTL_SECONDS) and is not
covered by this module.
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_REDIS_TTL_SECONDS = 86400
MIN_REDIS_TTL_SECONDS = 60
REDIS_TTL_ENV = "DEVTRACE_REDIS_TTL_SECONDS"


def parse_redis_ttl_seconds(raw: Optional[str] = None) -> int:
    """Read DEVTRACE_REDIS_TTL_SECONDS: unset or non-numeric falls back to 24h."""
    if raw is None:
        raw = os.getenv(REDIS_TTL_ENV, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_REDIS_TTL_SECONDS
    return max(MIN_REDIS_TTL_SECONDS, value)


def redis_ttl_seconds() -> int:
    """Read the env at call time so a container restarted with a new value applies it."""
    return parse_redis_ttl_seconds()
