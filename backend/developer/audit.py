"""Append-only audit events stored in a Redis Stream.

Application code only appends events (``XADD``) and trims the stream
approximately; Redis itself does not provide WORM storage, so "append-only"
here means the application layer never updates or deletes these records.
Writes are best-effort: a missing or failing Redis client must never block a
login, a chat request, or a worker task.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STREAM_KEY = 'developer:audit'
MAX_STREAM_LEN = 10_000
DETAIL_LIMIT = 200
SCAN_BATCH = 200
SCAN_CAP = 5_000
# Only these keys may ever reach the `detail` field of an audit event; anything
# else (message bodies, tool results, identifiers of unknown shape) is dropped.
DETAIL_FIELDS = {'kind', 'tool', 'success', 'latency_ms', 'attempts', 'reason_code', 'status'}

_SECRET_VALUE = re.compile(r'(?i)(sk-[A-Za-z0-9._\-]{4,}|whsec_[A-Za-z0-9_\-]{4,}|Bearer\s+[A-Za-z0-9._\-]+)')

# Set by the HTTP middleware so every event raised while handling one request
# carries the same id; worker and background paths leave it empty.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('devtrace_request_id', default='')
remote_var: contextvars.ContextVar[str] = contextvars.ContextVar('devtrace_remote', default='unknown')
# Endpoints and the worker set this before running the assistant so that tool
# events raised deep inside a run carry the owning workspace without threading
# the value through every signature.
workspace_var: contextvars.ContextVar[str] = contextvars.ContextVar('devtrace_workspace', default='')


def _clean(value: Any, limit: int = DETAIL_LIMIT) -> str:
    return _SECRET_VALUE.sub('<redacted>', str(value))[:limit]


def sanitize_detail(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key not in DETAIL_FIELDS:
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float)):
            clean[key] = round(value, 1) if isinstance(value, float) else value
        elif isinstance(value, str):
            clean[key] = _clean(value)
        # containers and other types could smuggle message bodies, so they are dropped
    return clean


async def record(redis: Any, event: str, *, actor: str = '', resource: str = '',
                 status: str = '', request_id: Optional[str] = None, remote: str = '',
                 workspace: Optional[str] = None,
                 detail: Optional[Dict[str, Any]] = None) -> None:
    """Append one audit event. Never raises: failures are logged and ignored."""
    xadd = getattr(redis, 'xadd', None)
    if xadd is None:
        return
    fields = {
        'ts': f'{time.time():.3f}',
        'event': _clean(event, 64),
        'actor': _clean(actor, 120),
        'resource': _clean(resource, 200),
        'status': _clean(status, 64),
        'request_id': _clean(request_id if request_id is not None else request_id_var.get(), 64),
        'remote': _clean(remote if remote else remote_var.get(), 64),
        'workspace': _clean(workspace if workspace is not None else workspace_var.get(), 40),
        'detail': json.dumps(sanitize_detail(detail), ensure_ascii=False, sort_keys=True),
    }
    try:
        await xadd(STREAM_KEY, fields, maxlen=MAX_STREAM_LEN, approximate=True)
    except Exception:  # noqa: BLE001 - audit must never break the main flow
        logger.warning('audit 写入失败（best-effort，已忽略）: event=%s', event, exc_info=True)


def _decode(value: Any) -> str:
    return value.decode('utf-8', 'replace') if isinstance(value, (bytes, bytearray)) else str(value)


def _entry(entry_id: Any, values: Dict[Any, Any]) -> Dict[str, Any]:
    row = {'id': _decode(entry_id)}
    for key, value in (values or {}).items():
        row[_decode(key)] = _decode(value)
    for key in ('ts', 'event', 'actor', 'resource', 'status', 'request_id', 'remote', 'workspace'):
        row.setdefault(key, '')
    # Events written before workspace tagging existed belong to the default workspace.
    if not row['workspace']:
        row['workspace'] = 'default'
    try:
        detail = json.loads(row.get('detail') or '{}')
        row['detail'] = detail if isinstance(detail, dict) else {}
    except (TypeError, ValueError):
        row['detail'] = {}
    return row


async def read(redis: Any, limit: int, user: str = '', event: str = '',
               workspace: str = '') -> List[Dict[str, Any]]:
    """Newest-first events, filtered server-side; scans at most SCAN_CAP records."""
    xrevrange = getattr(redis, 'xrevrange', None)
    if xrevrange is None or limit <= 0:
        return []
    events: List[Dict[str, Any]] = []
    scanned = 0
    high = '+'
    while len(events) < limit and scanned < SCAN_CAP:
        try:
            batch = await xrevrange(STREAM_KEY, max=high, min='-', count=SCAN_BATCH)
        except Exception:  # noqa: BLE001 - audit read degrades to what it has
            logger.warning('audit 读取失败', exc_info=True)
            break
        if not batch:
            break
        scanned += len(batch)
        for entry_id, values in batch:
            row = _entry(entry_id, values)
            high = f'({row["id"]}'
            if user and row.get('actor') != user:
                continue
            if event and row.get('event') != event:
                continue
            if workspace and row.get('workspace') != workspace:
                continue
            events.append(row)
            if len(events) >= limit:
                break
        if len(batch) < SCAN_BATCH or len(events) >= limit:
            break
    return events
