"""Redis-backed durable task queue for the streaming developer assistant.

The API process only enqueues a task and relays its events; a separate worker
process executes it. This keeps streamed runs alive across API restarts and
allows retries / dead-letter handling for transient failures.
"""
import hashlib
import json
import os
import re
import time
import uuid

from developer.ttl import redis_ttl_seconds

QUEUE_KEY = 'developer:tasks:queue'          # legacy queue, drained last
DEAD_KEY = 'developer:tasks:dead'
INDEX_KEY = 'developer:tasks:queue-index'
DELAY_KEY = 'developer:tasks:delayed'        # sorted set, score = due timestamp
RECLAIM_PREFIX = 'developer:task-reclaim:'  # short mutex guarding lease reclaim
PRIORITY_QUEUES = ('high', 'normal', 'low')
DEFAULT_PRIORITY = 'normal'
MAX_ATTEMPTS = 2
DEFAULT_LEASE_SECONDS = 90
MIN_LEASE_SECONDS = 30
MAX_BACKOFF_SECONDS = 30
# Same secret shapes the audit layer masks; dead-letter summaries reuse them.
_SECRET_VALUE = re.compile(
    r'(?i)(sk-[A-Za-z0-9._\-]{4,}|whsec_[A-Za-z0-9_\-]{4,}|Bearer\s+[A-Za-z0-9._\-]+)')


def task_key(task_id: str) -> str:
    return f'developer:task:{task_id}'


def priority_queue_key(priority: str) -> str:
    return f'{QUEUE_KEY}:{priority}'


# Consumption order for the worker: strict priority first, legacy last so that
# tasks enqueued before the priority upgrade are never stranded.
QUEUE_ORDER = tuple(priority_queue_key(p) for p in PRIORITY_QUEUES) + (QUEUE_KEY,)


def lease_seconds() -> int:
    """Configurable per-task lease; never shorter than MIN_LEASE_SECONDS."""
    try:
        value = int(os.getenv('DEVTRACE_TASK_LEASE_SECONDS', str(DEFAULT_LEASE_SECONDS)))
    except ValueError:
        return DEFAULT_LEASE_SECONDS
    return max(MIN_LEASE_SECONDS, value)


def backoff_seconds(attempts: int) -> int:
    """Exponential retry delay (1s, 2s, 4s, ...) capped at MAX_BACKOFF_SECONDS."""
    return min(2 ** max(0, int(attempts) - 1), MAX_BACKOFF_SECONDS)


def events_key(task_id: str) -> str:
    return f'developer:task-events:{task_id}'


def session_key(user_id: str, conversation_id: str) -> str:
    digest = hashlib.sha256(f'{user_id}\0{conversation_id}'.encode()).hexdigest()
    return f'developer:session-task:{digest}'


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


async def get_task(redis, task_id: str):
    raw = await redis.get(task_key(task_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def save_task(redis, task_id: str, record: dict) -> None:
    record['updated_at'] = time.time()
    await redis.setex(task_key(task_id), redis_ttl_seconds(), json.dumps(record, ensure_ascii=False))
    if record.get('status') in {'queued', 'running'}:
        await redis.sadd(INDEX_KEY, task_id)
    else:
        await redis.srem(INDEX_KEY, task_id)


async def update_task(redis, task_id: str, **fields) -> dict:
    record = await get_task(redis, task_id) or {'task_id': task_id}
    record.update(fields)
    await save_task(redis, task_id, record)
    return record


async def _push_queue(redis, task_id: str, priority: str = DEFAULT_PRIORITY) -> None:
    await redis.lpush(priority_queue_key(priority), task_id)


async def _requeue_push(redis, task_id: str, record: dict) -> None:
    """Route a re-queued task to its priority queue; legacy tasks keep the old key."""
    priority = (record or {}).get('priority')
    if priority in PRIORITY_QUEUES:
        await _push_queue(redis, task_id, priority)
    else:
        await redis.lpush(QUEUE_KEY, task_id)


async def enqueue(redis, task_id: str, user_id: str, message: str,
                  conversation_id: str, workspace: str = 'default',
                  priority: str = DEFAULT_PRIORITY, trace_id: str = '') -> dict:
    if priority not in PRIORITY_QUEUES:
        raise ValueError(f'Unknown task priority: {priority}')
    record = {
        'task_id': task_id, 'user_id': user_id, 'status': 'queued',
        'attempts': 0, 'created_at': time.time(), 'updated_at': time.time(),
        'run_id': None, 'cancel_requested': False,
        'workspace': workspace or 'default',
        'priority': priority, 'trace_id': trace_id or uuid.uuid4().hex,
        'payload': {'message': message, 'conversation_id': conversation_id},
    }
    await save_task(redis, task_id, record)
    await _push_queue(redis, task_id, priority)
    return record


async def claim(redis, timeout: int = 5):
    item = await redis.brpop(list(QUEUE_ORDER), timeout=timeout)
    if not item:
        return None
    return _text(item[1])


async def append_event(redis, task_id: str, event) -> None:
    await redis.rpush(events_key(task_id), json.dumps(event, ensure_ascii=False))
    await redis.expire(events_key(task_id), redis_ttl_seconds())


async def read_events(redis, task_id: str, start: int = 0):
    rows = await redis.lrange(events_key(task_id), start, -1)
    return [_text(row) for row in rows]


async def try_claim_session(redis, user_id: str, conversation_id: str,
                            task_id: str, ttl: int = 900) -> bool:
    return bool(await redis.set(session_key(user_id, conversation_id), task_id, nx=True, ex=ttl))


# Compare-and-delete, run atomically by Redis: the lock value is the task id
# written by try_claim_session, so a late cleanup can never drop the lock that a
# newer task already claimed for the same conversation.
RELEASE_IF_OWNER_LUA = ("if redis.call('GET',KEYS[1]) == ARGV[1] "
                        "then return redis.call('DEL',KEYS[1]) else return 0 end")


async def release_session(redis, user_id: str, conversation_id: str) -> None:
    """Drop the session lock without checking who holds it.

    The unconditional primitive, kept for callers that carry no task id (operator
    cleanup, tests). Every task terminal path must use `release_session_if_owner`
    instead, otherwise a delayed release can delete a live task's lock.
    """
    await redis.delete(session_key(user_id, conversation_id))


async def release_session_if_owner(redis, user_id: str, conversation_id: str,
                                   task_id: str) -> int:
    """Release the session lock only while `task_id` still owns it.

    Returns 1 when this task deleted its own lock and 0 when the lock is already
    gone or held by another task; another task's lock is never touched.
    """
    deleted = await redis.eval(RELEASE_IF_OWNER_LUA, 1,
                               session_key(user_id, conversation_id), task_id)
    return int(deleted or 0)


async def request_cancel(redis, task_id: str) -> None:
    record = await get_task(redis, task_id)
    if record and record.get('status') in {'queued', 'running'}:
        record['cancel_requested'] = True
        await save_task(redis, task_id, record)


async def requeue_or_fail(redis, task_id: str, error: str) -> str:
    """Retry with exponential backoff via the delayed ZSET, or dead-letter.

    Returns 'queued' while a retry is pending (the task is only in the ZSET,
    never in a ready queue until `promote_delayed` moves it) and 'failed' once
    the attempt budget is exhausted.
    """
    record = await get_task(redis, task_id) or {'task_id': task_id}
    attempts = int(record.get('attempts') or 0) + 1
    record['attempts'] = attempts
    record['error'] = error[:500]
    record['lease_id'] = None
    record['lease_until'] = None
    if attempts < MAX_ATTEMPTS:
        record['status'] = 'queued'
        await save_task(redis, task_id, record)
        await redis.zadd(DELAY_KEY, {task_id: time.time() + backoff_seconds(attempts)})
        return 'queued'
    record['status'] = 'failed'
    record['finished_at'] = time.time()
    await save_task(redis, task_id, record)
    await redis.zrem(DELAY_KEY, task_id)
    await redis.rpush(DEAD_KEY, task_id)
    return 'failed'


async def promote_delayed(redis, now: float | None = None) -> int:
    """Move due delayed tasks back into their priority queue.

    ``ZREM`` is the atomic claim: when two workers promote the same task, only
    the one that removes it from the ZSET re-queues it.
    """
    due = await redis.zrangebyscore(DELAY_KEY, '-inf', time.time() if now is None else now)
    promoted = 0
    for raw in due:
        task_id = _text(raw)
        if not await redis.zrem(DELAY_KEY, task_id):
            continue
        record = await get_task(redis, task_id)
        if not record or record.get('status') != 'queued':
            continue
        await _requeue_push(redis, task_id, record)
        promoted += 1
    return promoted


async def reclaim_expired(redis, now: float | None = None) -> int:
    """Requeue running tasks whose lease lapsed (the owning worker crashed).

    Tasks without a lease field keep the pre-P3 startup-recovery semantics, and
    a short ``SET NX`` mutex guarantees only one worker requeues a given task.
    """
    current = time.time() if now is None else now
    reclaimed = 0
    for raw in list(await redis.smembers(INDEX_KEY)):
        task_id = _text(raw)
        record = await get_task(redis, task_id)
        if not record or record.get('status') != 'running':
            continue
        lease_until = record.get('lease_until')
        if not isinstance(lease_until, (int, float)) or lease_until > current:
            continue
        mutex = f'{RECLAIM_PREFIX}{task_id}'
        if not await redis.set(mutex, task_id, nx=True, ex=30):
            continue
        record = await get_task(redis, task_id)
        lease_until = (record or {}).get('lease_until')
        if (not record or record.get('status') != 'running'
                or not isinstance(lease_until, (int, float)) or lease_until > current):
            await redis.delete(mutex)
            continue
        await requeue_or_fail(redis, task_id, '租约过期，任务已被其他 worker 回收')
        reclaimed += 1
    return reclaimed


async def replay(redis, task_id: str):
    """Re-arm a dead-lettered task. Returns (result, record).

    'ok' - reset and re-queued; 'not_found' - dead entry with no live record;
    'not_failed' - the record is not a failed task.
    """
    record = await get_task(redis, task_id)
    if not record or record.get('status') != 'failed':
        return ('not_found' if not record else 'not_failed'), record
    if not await redis.lrem(DEAD_KEY, 1, task_id):
        return 'not_in_dead', record
    record['attempts'] = 0
    record['error'] = None
    record['status'] = 'queued'
    record['lease_id'] = None
    record['lease_until'] = None
    record['finished_at'] = None
    await save_task(redis, task_id, record)
    await _requeue_push(redis, task_id, record)
    return 'ok', record


async def read_dead(redis, limit: int):
    """Dead-letter summaries, oldest first; missing records stay visible."""
    rows = []
    for raw in await redis.lrange(DEAD_KEY, 0, max(0, limit) - 1):
        task_id = _text(raw)
        record = await get_task(redis, task_id) or {}
        rows.append({'task_id': task_id,
                     'status': record.get('status') or 'failed',
                     'attempts': record.get('attempts'),
                     'error': _SECRET_VALUE.sub('<redacted>', str(record.get('error') or ''))[:200],
                     'workspace': str(record.get('workspace') or 'default'),
                     'priority': record.get('priority') or '',
                     'trace_id': record.get('trace_id') or ''})
    return rows


async def queue_metrics(redis, status_cap: int = 500) -> dict:
    """Bounded queue snapshot: list lengths plus per-status counts of indexed tasks."""
    queues = {p: await redis.llen(priority_queue_key(p)) for p in PRIORITY_QUEUES}
    members = list(await redis.smembers(INDEX_KEY))[:status_cap]
    statuses: dict[str, int] = {}
    for raw in members:
        record = await get_task(redis, _text(raw))
        status = (record or {}).get('status') or 'missing'
        statuses[status] = statuses.get(status, 0) + 1
    return {'queues': queues,
            'legacy': await redis.llen(QUEUE_KEY),
            'delayed': await redis.zcard(DELAY_KEY),
            'dead': await redis.llen(DEAD_KEY),
            'indexed': len(members),
            'statuses': statuses}


async def recover_running_tasks(redis, is_worker_alive) -> int:
    """On worker startup, requeue tasks whose worker died mid-run.

    A running task owned by another live worker is left untouched. Exhausted
    attempts go to the dead-letter list instead of looping forever.
    """
    recovered = 0
    for task_id in list(await redis.smembers(INDEX_KEY)):
        task_id = _text(task_id)
        record = await get_task(redis, task_id)
        if not record:
            await redis.srem(INDEX_KEY, task_id)
            continue
        status = record.get('status')
        if status == 'queued':
            continue
        if status != 'running':
            await redis.srem(INDEX_KEY, task_id)
            continue
        owner = record.get('worker_id') or ''
        if owner and await is_worker_alive(redis, owner):
            continue
        attempts = int(record.get('attempts') or 0) + 1
        record['attempts'] = attempts
        record['error'] = 'worker 在执行中退出，任务已重新入队'
        if attempts < MAX_ATTEMPTS:
            record['status'] = 'queued'
            record['lease_id'] = None
            record['lease_until'] = None
            await save_task(redis, task_id, record)
            await _requeue_push(redis, task_id, record)
        else:
            record['status'] = 'failed'
            record['finished_at'] = time.time()
            await save_task(redis, task_id, record)
            await redis.rpush(DEAD_KEY, task_id)
        recovered += 1
    return recovered


async def dead_letter(redis, task_id: str, error: str) -> None:
    await update_task(redis, task_id, status='failed', error=error[:500],
                      finished_at=time.time())
    await redis.rpush(DEAD_KEY, task_id)
