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

LEASE_RENEW_SECONDS = 30


def _log_ids(trace_id: str, task_id: str) -> str:
    return f'trace_id={trace_id or "-"} task_id={task_id}'


async def _audit_finished(redis, task_id: str, user_id: str, status: str, attempts,
                          workspace: str = '', trace_id: str = '') -> None:
    await audit.record(redis, 'task.finished', actor=user_id, resource=task_id, status=status,
                       workspace=workspace, trace_id=trace_id,
                       detail={'status': status, 'attempts': attempts})


async def _renew_lease(redis, task_id: str, trace_id: str) -> None:
    """Refresh the lease while the task is running; stops at the task boundary."""
    while True:
        try:
            await asyncio.sleep(LEASE_RENEW_SECONDS)
            record = await task_queue.get_task(redis, task_id)
            if (not record or record.get('status') != 'running'
                    or record.get('lease_id') != api_main.WORKER_ID):
                return
            await task_queue.update_task(
                redis, task_id,
                lease_until=time.time() + task_queue.lease_seconds())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('lease 续期失败 (%s)', _log_ids(trace_id, task_id), exc_info=True)
            return


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
    trace_id = str(record.get('trace_id') or '')
    audit.trace_id_var.set(trace_id)
    # Legacy tasks carry no workspace; tool events then stay untagged and the
    # audit reader normalizes them to `default`.
    audit.workspace_var.set(workspace if record.get('workspace') else '')
    if record.get('cancel_requested'):
        await task_queue.update_task(redis, task_id, status='cancelled', finished_at=time.time())
        await task_queue.release_session_if_owner(redis, user_id, conversation_id, task_id)
        await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace, trace_id)
        return
    await task_queue.update_task(
        redis, task_id, status='running', worker_id=api_main.WORKER_ID, started_at=time.time(),
        lease_id=api_main.WORKER_ID, lease_until=time.time() + task_queue.lease_seconds())

    loop = asyncio.get_running_loop()
    token = event_sink.set(
        lambda event: loop.create_task(task_queue.append_event(redis, task_id, event)))
    renew_task = asyncio.create_task(_renew_lease(redis, task_id, trace_id))
    terminal = False
    try:
        result = await asyncio.wait_for(assistant.run(message, user_id, conversation_id), timeout=150)
        record = await task_queue.get_task(redis, task_id) or record
        if record.get('cancel_requested'):
            await task_queue.update_task(redis, task_id, status='cancelled',
                                         finished_at=time.time())
            terminal = True
            await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace, trace_id)
            return
        status = 'waiting' if (result.get('plan') or {}).get('status') == 'waiting' else 'completed'
        await api_main._stamp_run(redis, result.get('run_id'), workspace)
        await task_queue.update_task(
            redis, task_id, status=status, run_id=result.get('run_id'),
            finished_at=time.time(), error=None)
        terminal = True
        logger.info('task %s (%s)', status, _log_ids(trace_id, task_id))
        await _audit_finished(redis, task_id, user_id, status, attempts, workspace, trace_id)
    except asyncio.CancelledError:
        await task_queue.update_task(redis, task_id, status='cancelled',
                                     finished_at=time.time())
        terminal = True
        await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace, trace_id)
        raise
    except Exception as exc:  # noqa: BLE001 - worker boundary
        logger.exception('task failed (%s)', _log_ids(trace_id, task_id))
        if (await task_queue.get_task(redis, task_id) or {}).get('cancel_requested'):
            await task_queue.update_task(redis, task_id, status='cancelled',
                                         finished_at=time.time())
            await _audit_finished(redis, task_id, user_id, 'cancelled', attempts, workspace, trace_id)
        else:
            state = await task_queue.requeue_or_fail(redis, task_id, str(exc))
            terminal = state != 'queued'
            if terminal:
                await _audit_finished(redis, task_id, user_id, 'failed', attempts + 1,
                                      workspace, trace_id)
    finally:
        renew_task.cancel()
        await asyncio.gather(renew_task, return_exceptions=True)
        event_sink.reset(token)
        if terminal:
            # Owner-aware: this worker may finish long after a newer task on the
            # same conversation already re-claimed the lock.
            await task_queue.release_session_if_owner(redis, user_id, conversation_id, task_id)


async def run() -> None:
    async with api_main.app.router.lifespan_context(api_main.app):
        redis = api_main._developer.redis
        recovered = await task_queue.recover_running_tasks(redis, api_main._worker_alive)
        if recovered:
            logger.warning('recovered %s task(s) from dead workers', recovered)
        logger.info('task worker started (worker_id=%s, lease=%ss)',
                    api_main.WORKER_ID, task_queue.lease_seconds())
        while True:
            try:
                await task_queue.promote_delayed(redis)
                reclaimed = await task_queue.reclaim_expired(redis)
                if reclaimed:
                    logger.warning('reclaimed %s task(s) with expired leases', reclaimed)
            except Exception:
                logger.warning('队列维护周期失败（延迟提升/租约回收）', exc_info=True)
            task_id = await task_queue.claim(redis)
            if not task_id:
                continue
            await process_one(api_main._developer, redis, task_id)


if __name__ == '__main__':
    asyncio.run(run())
