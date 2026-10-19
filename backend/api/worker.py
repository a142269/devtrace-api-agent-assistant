"""Worker process for the durable streaming task queue.

Run with:  python -m api.worker

The worker owns execution of queued stream tasks. It records timeline events in
Redis so the API can relay them to a reconnecting client, retries transient
failures once, and moves exhausted tasks to a dead-letter list.
"""
import asyncio
import logging
import time

from api import main as api_main
from developer import audit
from developer import task_queue
from developer.events import event_sink

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')


async def _audit_finished(redis, task_id: str, user_id: str, status: str, attempts,
                          workspace: str = '') -> None:
    await audit.record(redis, 'task.finished', actor=user_id, resource=task_id, status=status,
                       workspace=workspace, detail={'status': status, 'attempts': attempts})


async def process_one(assistant, redis, task_id: str) -> None:
    record = await task_queue.get_task(redis, task_id)
    if not record:
        return
    payload = record.get('payload') or {}
    user_id = record.get('user_id') or 'anonymous'
    conversation_id = payload.get('conversation_id') or ''
    message = payload.get('message') or ''
    attempts = int(record.get('attempts') or 0)
    workspace = str(record.get('workspace') or 'default')
    # Legacy tasks carry no workspace; tool events then stay untagged and the
    # audit reader normalizes them to `default`.
    audit.workspace_var.set(workspace if record.get('workspace') else '')
    if record.get('cancel_requested'):
        await task_queue.update_task(redis, task_id, status='cancelled', finished_at=time.time())
        await task_queue.release_session(redis, user_id, conversation_id)
        await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace)
        return
    await task_queue.update_task(
        redis, task_id, status='running', worker_id=api_main.WORKER_ID, started_at=time.time())

    loop = asyncio.get_running_loop()
    token = event_sink.set(
        lambda event: loop.create_task(task_queue.append_event(redis, task_id, event)))
    terminal = False
    try:
        result = await asyncio.wait_for(assistant.run(message, user_id, conversation_id), timeout=150)
        record = await task_queue.get_task(redis, task_id) or record
        if record.get('cancel_requested'):
            await task_queue.update_task(redis, task_id, status='cancelled',
                                         finished_at=time.time())
            terminal = True
            await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace)
            return
        status = 'waiting' if (result.get('plan') or {}).get('status') == 'waiting' else 'completed'
        await api_main._stamp_run(redis, result.get('run_id'), workspace)
        await task_queue.update_task(
            redis, task_id, status=status, run_id=result.get('run_id'),
            finished_at=time.time(), error=None)
        terminal = True
        await _audit_finished(redis, task_id, user_id, status, attempts, workspace)
    except asyncio.CancelledError:
        await task_queue.update_task(redis, task_id, status='cancelled',
                                     finished_at=time.time())
        terminal = True
        await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace)
        raise
    except Exception as exc:  # noqa: BLE001 - worker boundary
        logger.exception('task %s failed', task_id)
        if (await task_queue.get_task(redis, task_id) or {}).get('cancel_requested'):
            await task_queue.update_task(redis, task_id, status='cancelled',
                                         finished_at=time.time())
            await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace)
        else:
            state = await task_queue.requeue_or_fail(redis, task_id, str(exc))
            terminal = state != 'queued'
            if terminal:
                await _audit_finished(redis, task_id, user_id, 'failed', attempts + 1, workspace)
    finally:
        event_sink.reset(token)
        if terminal:
            await task_queue.release_session(redis, user_id, conversation_id)


async def run() -> None:
    async with api_main.app.router.lifespan_context(api_main.app):
        redis = api_main._developer.redis
        recovered = await task_queue.recover_running_tasks(redis, api_main._worker_alive)
        if recovered:
            logger.warning('recovered %s task(s) from dead workers', recovered)
        logger.info('task worker started (worker_id=%s)', api_main.WORKER_ID)
        while True:
            task_id = await task_queue.claim(redis)
            if not task_id:
                continue
            await process_one(api_main._developer, redis, task_id)


if __name__ == '__main__':
    asyncio.run(run())
