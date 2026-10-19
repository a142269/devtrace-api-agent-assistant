"""Redis-backed durable task queue for the streaming developer assistant.

The API process only enqueues a task and relays its events; a separate worker
process executes it. This keeps streamed runs alive across API restarts and
allows retries / dead-letter handling for transient failures.
"""
import hashlib
import json
import time

QUEUE_KEY = 'developer:tasks:queue'
DEAD_KEY = 'developer:tasks:dead'
INDEX_KEY = 'developer:tasks:queue-index'
EVENTS_TTL_SECONDS = 86400
MAX_ATTEMPTS = 2


def task_key(task_id: str) -> str:
    return f'developer:task:{task_id}'


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
    await redis.setex(task_key(task_id), EVENTS_TTL_SECONDS, json.dumps(record, ensure_ascii=False))
    if record.get('status') in {'queued', 'running'}:
        await redis.sadd(INDEX_KEY, task_id)
    else:
        await redis.srem(INDEX_KEY, task_id)


async def update_task(redis, task_id: str, **fields) -> dict:
    record = await get_task(redis, task_id) or {'task_id': task_id}
    record.update(fields)
    await save_task(redis, task_id, record)
    return record


async def enqueue(redis, task_id: str, user_id: str, message: str,
                  conversation_id: str, workspace: str = 'default') -> dict:
    record = {
        'task_id': task_id, 'user_id': user_id, 'status': 'queued',
        'attempts': 0, 'created_at': time.time(), 'updated_at': time.time(),
        'run_id': None, 'cancel_requested': False,
        'workspace': workspace or 'default',
        'payload': {'message': message, 'conversation_id': conversation_id},
    }
    await save_task(redis, task_id, record)
    await redis.lpush(QUEUE_KEY, task_id)
    return record


async def claim(redis, timeout: int = 5):
    item = await redis.brpop(QUEUE_KEY, timeout=timeout)
    if not item:
        return None
    return _text(item[1])


async def append_event(redis, task_id: str, event) -> None:
    await redis.rpush(events_key(task_id), json.dumps(event, ensure_ascii=False))
    await redis.expire(events_key(task_id), EVENTS_TTL_SECONDS)


async def read_events(redis, task_id: str, start: int = 0):
    rows = await redis.lrange(events_key(task_id), start, -1)
    return [_text(row) for row in rows]


async def try_claim_session(redis, user_id: str, conversation_id: str,
                            task_id: str, ttl: int = 900) -> bool:
    return bool(await redis.set(session_key(user_id, conversation_id), task_id, nx=True, ex=ttl))


async def release_session(redis, user_id: str, conversation_id: str) -> None:
    await redis.delete(session_key(user_id, conversation_id))


async def request_cancel(redis, task_id: str) -> None:
    record = await get_task(redis, task_id)
    if record and record.get('status') in {'queued', 'running'}:
        record['cancel_requested'] = True
        await save_task(redis, task_id, record)


async def requeue_or_fail(redis, task_id: str, error: str) -> str:
    record = await get_task(redis, task_id) or {'task_id': task_id}
    attempts = int(record.get('attempts') or 0) + 1
    record['attempts'] = attempts
    record['error'] = error[:500]
    if attempts < MAX_ATTEMPTS:
        record['status'] = 'queued'
        await save_task(redis, task_id, record)
        await redis.lpush(QUEUE_KEY, task_id)
        return 'queued'
    record['status'] = 'failed'
    record['finished_at'] = time.time()
    await save_task(redis, task_id, record)
    await redis.rpush(DEAD_KEY, task_id)
    return 'failed'


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
            await save_task(redis, task_id, record)
            await redis.lpush(QUEUE_KEY, task_id)
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
