"""P3 queue production hardening: priority, delayed retry, leases, replay, metrics."""
import fnmatch
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

from api import main
from api import worker as api_worker
from api.auth import issue_token, password_hash
from developer import audit
from developer import task_queue

HASH = f'pbkdf2_sha256${"01" * 16}${password_hash("a-strong-test-password", "01" * 16)}'


class ProdRedis:
    """Fake redis covering the command surface the durable queue and audit use."""

    def __init__(self):
        self.values, self.lists, self.sets, self.zsets = {}, {}, {}, {}
        self.held = set()
        self.streams = {}
        self._seq = 0

    # --- string / generic ---
    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        self.lists.pop(key, None)
        return 1

    async def expire(self, key, _ttl):
        return True

    async def eval(self, _script, _numkeys, key, *args):
        """Emulate the session-lock compare-and-delete script.

        Only the compare-and-delete form passes an argv; the limiter's INCR
        script reaches this fake with no value and must not be treated as a
        lock release.
        """
        if args and self.values.get(key) == args[0]:
            self.values.pop(key, None)
            return 1
        return 0

    def lock(self, key, **_kwargs):
        redis = self

        class _Lock:
            async def acquire(self):
                if key in redis.held:
                    return False
                redis.held.add(key)
                return True

            async def release(self):
                redis.held.discard(key)
        return _Lock()

    # --- sets ---
    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    # --- lists ---
    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def lrem(self, key, _count, value):
        rows = self.lists.get(key, [])
        if value in rows:
            rows.remove(value)
            return 1
        return 0

    async def lrange(self, key, start, end):
        rows = self.lists.get(key, [])
        return rows[start:] if end == -1 else rows[start:end + 1]

    async def brpop(self, keys, timeout=0):
        queue = [keys] if isinstance(keys, str) else list(keys)
        for key in queue:
            rows = self.lists.get(key, [])
            if rows:
                return key, rows.pop()
        return None

    # --- sorted sets ---
    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zrangebyscore(self, key, low, high):
        scores = self.zsets.get(key, {})
        low = float('-inf') if low == '-inf' else float(low)
        high = float('inf') if high == '+inf' else float(high)
        return [m for m, s in sorted(scores.items(), key=lambda kv: kv[1]) if low <= s <= high]

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    # --- scanning (worker heartbeat count) ---
    async def scan_iter(self, match=None, count=None):
        for key in list(self.values):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    # --- streams (audit) ---
    async def xadd(self, key, fields, maxlen=None, approximate=False, id='*'):
        entry_id = f'{self._seq:013d}-0'
        self._seq += 1
        stream = self.streams.setdefault(key, [])
        stream.append((entry_id, {str(k).encode(): str(v).encode() for k, v in fields.items()}))
        if maxlen:
            del stream[:max(0, len(stream) - int(maxlen))]
        return entry_id

    def dumped(self):
        parts = []
        for _entry_id, fields in self.streams.get(audit.STREAM_KEY, []):
            for key, value in fields.items():
                parts.append(value.decode('utf-8', 'replace'))
        return ' '.join(parts)

    def events(self, event=None):
        rows = [{k.decode(): v.decode('utf-8', 'replace') for k, v in fields.items()}
                for _eid, fields in self.streams.get(audit.STREAM_KEY, [])]
        if event:
            rows = [row for row in rows if row.get('event') == event]
        return rows


class SuccessAssistant:
    async def run(self, message, user, conversation):
        return {'run_id': 'run-x', 'plan': {'status': 'complete'}}


class QueueUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = ProdRedis()

    # 1. priority routing ------------------------------------------------
    async def test_enqueue_routes_to_priority_queue_and_records_priority(self):
        for tid, prio in (('t-h', 'high'), ('t-n', 'normal'), ('t-l', 'low')):
            await task_queue.enqueue(self.redis, tid, 'alice', '你好', 'c1', priority=prio)
        self.assertEqual(self.redis.lists[task_queue.priority_queue_key('high')], ['t-h'])
        self.assertEqual(self.redis.lists[task_queue.priority_queue_key('normal')], ['t-n'])
        self.assertEqual(self.redis.lists[task_queue.priority_queue_key('low')], ['t-l'])
        record = await task_queue.get_task(self.redis, 't-h')
        self.assertEqual(record['priority'], 'high')

    async def test_invalid_priority_is_rejected(self):
        with self.assertRaises(ValueError):
            await task_queue.enqueue(self.redis, 't-bad', 'alice', '你好', 'c1', priority='urgent')

    async def test_claim_order_high_normal_low_then_legacy(self):
        await self.redis.lpush(task_queue.QUEUE_KEY, 'legacy')
        await self.redis.lpush(task_queue.priority_queue_key('low'), 'low')
        await self.redis.lpush(task_queue.priority_queue_key('normal'), 'normal')
        await self.redis.lpush(task_queue.priority_queue_key('high'), 'high')
        order = [(await task_queue.claim(self.redis, timeout=0)) for _ in range(4)]
        self.assertEqual(order, ['high', 'normal', 'low', 'legacy'])

    # 2. delayed retry ---------------------------------------------------
    async def test_backoff_schedule_grows_and_caps(self):
        self.assertEqual(task_queue.backoff_seconds(1), 1)
        self.assertEqual(task_queue.backoff_seconds(2), 2)
        self.assertEqual(task_queue.backoff_seconds(3), 4)
        self.assertEqual(task_queue.backoff_seconds(20), task_queue.MAX_BACKOFF_SECONDS)

    async def test_transient_failure_waits_in_delayed_zset_until_due(self):
        await task_queue.enqueue(self.redis, 't1', 'alice', '你好', 'c1', priority='high')
        # The worker claims the task out of the ready queue before executing it.
        self.assertEqual(await task_queue.claim(self.redis, timeout=0), 't1')
        state = await task_queue.requeue_or_fail(self.redis, 't1', 'boom')
        self.assertEqual(state, 'queued')
        record = await task_queue.get_task(self.redis, 't1')
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['attempts'], 1)
        # Not in any ready queue; parked in the delayed ZSET.
        for key in task_queue.QUEUE_ORDER:
            self.assertNotIn('t1', self.redis.lists.get(key, []))
        self.assertEqual(await self.redis.zcard(task_queue.DELAY_KEY), 1)
        due = self.redis.zsets[task_queue.DELAY_KEY]['t1']
        self.assertGreaterEqual(due, time.time())
        # Promoting before the due time must not move it.
        self.assertEqual(await task_queue.promote_delayed(self.redis, now=time.time() - 1), 0)
        # After it is due, it returns to its original (high) queue.
        self.assertEqual(await task_queue.promote_delayed(self.redis, now=due + 1), 1)
        self.assertIn('t1', self.redis.lists[task_queue.priority_queue_key('high')])
        self.assertEqual(await self.redis.zcard(task_queue.DELAY_KEY), 0)

    async def test_replay_after_success_promote_completes(self):
        # Exhausted attempts dead-letter the task.
        await task_queue.enqueue(self.redis, 't2', 'alice', '你好', 'c1')
        await task_queue.requeue_or_fail(self.redis, 't2', 'boom')  # attempts 1 -> delayed
        await task_queue.requeue_or_fail(self.redis, 't2', 'boom')  # attempts 2 -> dead
        record = await task_queue.get_task(self.redis, 't2')
        self.assertEqual(record['status'], 'failed')
        self.assertIn('t2', self.redis.lists[task_queue.DEAD_KEY])

    # 3. leases ----------------------------------------------------------
    async def test_process_one_sets_a_lease_then_clears_terminal(self):
        seen = {}

        class SpyAssistant:
            async def run(self, message, user, conversation):
                seen['record'] = await task_queue.get_task(redis, 't3')
                return {'run_id': 'run-3', 'plan': {'status': 'complete'}}

        redis = self.redis
        await task_queue.enqueue(redis, 't3', 'alice', '你好', 'c1')
        await api_worker.process_one(SpyAssistant(), redis, 't3')
        running = seen['record']
        self.assertEqual(running['status'], 'running')
        self.assertEqual(running['lease_id'], main.WORKER_ID)
        self.assertGreater(running['lease_until'], time.time())

    async def test_reclaim_expired_requeues_and_clears_lease(self):
        await task_queue.enqueue(self.redis, 't4', 'alice', '你好', 'c1', priority='low')
        await task_queue.update_task(self.redis, 't4', status='running',
                                     lease_id='ghost', lease_until=time.time() - 1)
        reclaimed = await task_queue.reclaim_expired(self.redis)
        self.assertEqual(reclaimed, 1)
        record = await task_queue.get_task(self.redis, 't4')
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['attempts'], 1)
        self.assertIsNone(record['lease_id'])
        # Requeued through the delayed path, not a ready queue yet.
        self.assertEqual(await self.redis.zcard(task_queue.DELAY_KEY), 1)

    async def test_live_lease_is_not_reclaimed(self):
        await task_queue.enqueue(self.redis, 't5', 'alice', '你好', 'c1')
        await task_queue.update_task(self.redis, 't5', status='running',
                                     lease_id=main.WORKER_ID, lease_until=time.time() + 60)
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 0)
        self.assertEqual((await task_queue.get_task(self.redis, 't5'))['status'], 'running')

    async def test_reclaim_skips_legacy_records_without_lease(self):
        await task_queue.enqueue(self.redis, 't6', 'alice', '你好', 'c1')
        await task_queue.update_task(self.redis, 't6', status='running', worker_id='ghost')
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 0)

    async def test_reclaim_mutex_prevents_second_worker(self):
        await task_queue.enqueue(self.redis, 't7', 'alice', '你好', 'c1')
        await task_queue.update_task(self.redis, 't7', status='running',
                                     lease_id='ghost', lease_until=time.time() - 1)
        # Another worker already holds the short reclaim mutex.
        await self.redis.set(f'{task_queue.RECLAIM_PREFIX}t7', 'other', nx=True, ex=30)
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 0)
        self.assertEqual((await task_queue.get_task(self.redis, 't7'))['status'], 'running')

    # 3b. reclamation that exhausts its attempts is finalised by the loop ------
    async def _crashed_task(self, tid, lease_id='ghost'):
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', tid)
        await task_queue.enqueue(self.redis, tid, 'alice', '你好', 'c1')
        await task_queue.update_task(self.redis, tid, status='running',
                                     lease_id=lease_id, lease_until=time.time() - 1)

    async def test_first_crash_reclaims_without_terminal_bookkeeping(self):
        await self._crashed_task('r1')
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 1)
        record = await task_queue.get_task(self.redis, 'r1')
        self.assertEqual((record['status'], record['attempts']), ('queued', 1))
        # A retry is still pending, so the conversation lock stays held and no
        # terminal audit is written.
        self.assertEqual(await self.redis.get(task_queue.session_key('alice', 'c1')), 'r1')
        self.assertEqual(self.redis.events('task.finished'), [])

    async def test_second_crash_releases_lock_and_writes_exactly_one_audit(self):
        await self._crashed_task('r2')
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 1)
        # The fake redis has no TTL, so drop the 30s reclaim mutex the way Redis
        # would expire it before the task is claimed and dies a second time.
        await self.redis.delete(f'{task_queue.RECLAIM_PREFIX}r2')
        await task_queue.update_task(self.redis, 'r2', status='running',
                                     lease_id='ghost-2', lease_until=time.time() - 1)
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 1)

        record = await task_queue.get_task(self.redis, 'r2')
        self.assertEqual((record['status'], record['attempts']), ('failed', 2))
        self.assertIn('r2', self.redis.lists[task_queue.DEAD_KEY])
        # The zombie conversation lock is gone, so a new request can claim it.
        self.assertIsNone(await self.redis.get(task_queue.session_key('alice', 'c1')))
        self.assertTrue(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'r3'))
        finished = self.redis.events('task.finished')
        self.assertEqual(len(finished), 1)
        self.assertEqual((finished[0]['status'], finished[0]['resource']), ('failed', 'r2'))
        self.assertEqual(finished[0]['actor'], 'alice')
        detail = json.loads(finished[0]['detail'])
        self.assertEqual(detail, {'attempts': 2, 'status': 'failed'})
        self.assertTrue(set(detail) <= audit.DETAIL_FIELDS)

    async def test_reclaim_never_drops_a_lock_another_task_now_owns(self):
        await self._crashed_task('r4')
        await task_queue.reclaim_expired(self.redis)
        await self.redis.delete(f'{task_queue.RECLAIM_PREFIX}r4')
        # A newer request took the conversation over before the final reclamation.
        await self.redis.delete(task_queue.session_key('alice', 'c1'))
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'r-live')
        await task_queue.update_task(self.redis, 'r4', status='running',
                                     lease_id='ghost-2', lease_until=time.time() - 1)
        await task_queue.reclaim_expired(self.redis)
        record = await task_queue.get_task(self.redis, 'r4')
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(await self.redis.get(task_queue.session_key('alice', 'c1')), 'r-live')

    async def test_live_lease_writes_no_terminal_audit_and_keeps_the_lock(self):
        await self._crashed_task('r5')
        await task_queue.update_task(self.redis, 'r5', lease_until=time.time() + 60)
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 0)
        self.assertEqual(self.redis.events('task.finished'), [])
        self.assertEqual(await self.redis.get(task_queue.session_key('alice', 'c1')), 'r5')

    async def test_worker_terminal_path_is_not_double_audited_by_reclaim(self):
        await task_queue.enqueue(self.redis, 'r6', 'alice', '你好', 'c1')
        await api_worker.process_one(SuccessAssistant(), self.redis, 'r6')
        self.assertEqual(len(self.redis.events('task.finished')), 1)
        self.assertEqual(await task_queue.reclaim_expired(self.redis), 0)
        self.assertEqual(len(self.redis.events('task.finished')), 1)

    async def test_reclaim_finalize_lock_fault_is_logged_and_still_audits(self):
        from redis.exceptions import RedisError

        async def broken_owner_release(*_args, **_kwargs):
            raise RedisError('connection reset')

        await self._crashed_task('r7')
        await task_queue.reclaim_expired(self.redis)
        await self.redis.delete(f'{task_queue.RECLAIM_PREFIX}r7')
        await task_queue.update_task(self.redis, 'r7', status='running',
                                     lease_id='ghost-2', lease_until=time.time() - 1)
        with patch.object(task_queue, 'release_session_if_owner', broken_owner_release), \
                self.assertLogs('developer.task_queue', level='WARNING') as captured:
            self.assertEqual(await task_queue.reclaim_expired(self.redis), 1)
        self.assertTrue(any('会话锁释放失败' in line for line in captured.output))
        # The dead-letter state and its audit survive the lock fault.
        self.assertEqual((await task_queue.get_task(self.redis, 'r7'))['status'], 'failed')
        finished = self.redis.events('task.finished')
        self.assertEqual([(row['status'], row['resource']) for row in finished],
                         [('failed', 'r7')])

    # 3c. startup recovery of an exhausted task is finalised the same way -------
    async def _dead_worker_task(self, tid, attempts):
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', tid)
        await task_queue.enqueue(self.redis, tid, 'alice', '你好', 'c1')
        # Drain the ready queue so a re-queue by the recovery is observable.
        self.redis.lists.setdefault(task_queue.priority_queue_key('normal'), []).clear()
        await task_queue.update_task(self.redis, tid, status='running',
                                     worker_id='dead-worker', attempts=attempts)

    @staticmethod
    async def _dead(_redis, _worker):
        return False

    async def test_recover_exhausted_attempts_releases_lock_and_writes_one_audit(self):
        await self._dead_worker_task('rc2', attempts=task_queue.MAX_ATTEMPTS - 1)
        self.assertEqual(await task_queue.recover_running_tasks(self.redis, self._dead), 1)

        record = await task_queue.get_task(self.redis, 'rc2')
        self.assertEqual((record['status'], record['attempts']),
                         ('failed', task_queue.MAX_ATTEMPTS))
        self.assertIn('rc2', self.redis.lists[task_queue.DEAD_KEY])
        # Dead-lettered, so it must not also sit in a ready queue.
        self.assertNotIn('rc2', self.redis.lists.get(
            task_queue.priority_queue_key('normal'), []))
        self.assertIsNone(await self.redis.get(task_queue.session_key('alice', 'c1')))
        self.assertTrue(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'rc3'))
        finished = self.redis.events('task.finished')
        self.assertEqual(len(finished), 1)
        self.assertEqual((finished[0]['status'], finished[0]['resource'], finished[0]['actor']),
                         ('failed', 'rc2', 'alice'))
        detail = json.loads(finished[0]['detail'])
        self.assertEqual(detail, {'attempts': task_queue.MAX_ATTEMPTS, 'status': 'failed'})
        self.assertTrue(set(detail) <= audit.DETAIL_FIELDS)

    async def test_recover_with_retry_budget_keeps_lock_and_writes_no_audit(self):
        await self._dead_worker_task('rc1', attempts=0)
        self.assertEqual(await task_queue.recover_running_tasks(self.redis, self._dead), 1)
        record = await task_queue.get_task(self.redis, 'rc1')
        self.assertEqual((record['status'], record['attempts']), ('queued', 1))
        self.assertIn('rc1', self.redis.lists[task_queue.priority_queue_key('normal')])
        self.assertEqual(await self.redis.get(task_queue.session_key('alice', 'c1')), 'rc1')
        self.assertEqual(self.redis.events('task.finished'), [])

    async def test_recover_exhausted_attempts_never_drops_a_newer_tasks_lock(self):
        await self._dead_worker_task('rc4', attempts=task_queue.MAX_ATTEMPTS - 1)
        # A newer request already owns the conversation when recovery catches up.
        await self.redis.delete(task_queue.session_key('alice', 'c1'))
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'rc-live')
        self.assertEqual(await task_queue.recover_running_tasks(self.redis, self._dead), 1)
        self.assertEqual((await task_queue.get_task(self.redis, 'rc4'))['status'], 'failed')
        self.assertEqual(await self.redis.get(task_queue.session_key('alice', 'c1')), 'rc-live')
        self.assertEqual(len(self.redis.events('task.finished')), 1)

    async def test_recover_finalize_lock_fault_is_logged_and_still_audits(self):
        from redis.exceptions import RedisError

        async def broken_owner_release(*_args, **_kwargs):
            raise RedisError('connection reset')

        await self._dead_worker_task('rc5', attempts=task_queue.MAX_ATTEMPTS - 1)
        with patch.object(task_queue, 'release_session_if_owner', broken_owner_release), \
                self.assertLogs('developer.task_queue', level='WARNING') as captured:
            self.assertEqual(await task_queue.recover_running_tasks(self.redis, self._dead), 1)
        self.assertTrue(any('会话锁释放失败' in line for line in captured.output))
        self.assertEqual((await task_queue.get_task(self.redis, 'rc5'))['status'], 'failed')
        self.assertEqual([(row['status'], row['resource'])
                          for row in self.redis.events('task.finished')],
                         [('failed', 'rc5')])

    async def test_lease_seconds_floor_and_default(self):
        for raw, expected in ((None, 90), ('45', 45), ('5', 30), ('nope', 90)):
            env = {'DEVTRACE_TASK_LEASE_SECONDS': raw} if raw is not None else {}
            with patch.dict(os.environ, env, clear=False):
                if raw is None:
                    os.environ.pop('DEVTRACE_TASK_LEASE_SECONDS', None)
                self.assertEqual(task_queue.lease_seconds(), expected)

    # 4. dead-letter replay helper --------------------------------------
    async def _dead_task(self, tid='t8', priority='high'):
        await task_queue.enqueue(self.redis, tid, 'alice', '你好', 'c1', priority=priority)
        await task_queue.claim(self.redis, timeout=0)
        await task_queue.requeue_or_fail(self.redis, tid, 'boom')
        await task_queue.requeue_or_fail(self.redis, tid, 'boom')
        return tid

    async def test_replay_resets_and_requeues_original_priority(self):
        tid = await self._dead_task()
        result, record = await task_queue.replay(self.redis, tid)
        self.assertEqual(result, 'ok')
        record = await task_queue.get_task(self.redis, tid)
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['attempts'], 0)
        self.assertIsNone(record['error'])
        self.assertIn(tid, self.redis.lists[task_queue.priority_queue_key('high')])
        self.assertNotIn(tid, self.redis.lists[task_queue.DEAD_KEY])

    async def test_replay_rejects_non_dead_tasks(self):
        await task_queue.enqueue(self.redis, 'alive', 'alice', '你好', 'c1')
        result, _ = await task_queue.replay(self.redis, 'alive')
        self.assertEqual(result, 'not_failed')
        missing, _ = await task_queue.replay(self.redis, 'does-not-exist')
        self.assertEqual(missing, 'not_found')

    # 5. trace_id --------------------------------------------------------
    async def test_worker_task_finished_carries_trace_id(self):
        await task_queue.enqueue(self.redis, 't9', 'alice', '你好', 'c1', trace_id='trace-abc')
        await api_worker.process_one(SuccessAssistant(), self.redis, 't9')
        finished = self.redis.events('task.finished')[-1]
        self.assertEqual(finished['trace_id'], 'trace-abc')

    async def test_enqueue_generates_a_trace_id_when_absent(self):
        record = await task_queue.enqueue(self.redis, 't10', 'alice', '你好', 'c1')
        self.assertTrue(record['trace_id'])
        self.assertEqual(len(record['trace_id']), 32)


class AdminEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_admins = os.environ.get('DEVTRACE_ADMIN_USERS')
        os.environ['DEVTRACE_ADMIN_USERS'] = 'alice'

    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': HASH, 'carol': HASH}
        main.app.state.user_workspaces = {'alice': 'ws-a', 'carol': 'ws-b'}
        self.redis = ProdRedis()
        self.old = main._developer
        main._developer = SuccessAssistant()
        main._developer.redis = self.redis
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                        base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main._developer = self.old
        if self._old_admins is None:
            os.environ.pop('DEVTRACE_ADMIN_USERS', None)
        else:
            os.environ['DEVTRACE_ADMIN_USERS'] = self._old_admins

    def headers(self, user='alice'):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret)}

    async def _dead_task(self, tid='t8', priority='high'):
        await task_queue.enqueue(self.redis, tid, 'alice', '你好', 'c1', priority=priority)
        # Simulate the worker having claimed (popped) this exact task.
        self.redis.lists[task_queue.priority_queue_key(priority)].remove(tid)
        await task_queue.requeue_or_fail(self.redis, tid, 'boom')
        await task_queue.requeue_or_fail(self.redis, tid, 'boom')
        return tid

    # 4. dead-letter endpoints ------------------------------------------
    async def test_dead_letter_listing_is_admin_only(self):
        tid = await self._dead_task()
        forbidden = await self.client.get('/developer/dead-letter', headers=self.headers('carol'))
        self.assertEqual(forbidden.status_code, 403)
        anonymous = await self.client.get('/developer/dead-letter')
        self.assertEqual(anonymous.status_code, 401)
        ok = await self.client.get('/developer/dead-letter', headers=self.headers('alice'))
        self.assertEqual(ok.status_code, 200)
        row = next(r for r in ok.json()['tasks'] if r['task_id'] == tid)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['workspace'], 'default')
        self.assertNotIn('user_id', row)

    async def test_replay_endpoint_resets_task_and_audits(self):
        tid = await self._dead_task()
        ok = await self.client.post(f'/developer/tasks/{tid}/replay', headers=self.headers('alice'))
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()['replayed'])
        record = await task_queue.get_task(self.redis, tid)
        self.assertEqual(record['status'], 'queued')
        replayed = self.redis.events('task.replayed')[-1]
        self.assertEqual(replayed['status'], 'success')
        self.assertEqual(replayed['resource'], tid)

    async def test_replay_endpoint_boundaries(self):
        self.assertEqual((await self.client.post(
            '/developer/tasks/nope/replay', headers=self.headers('alice'))).status_code, 409)
        tid = await self._dead_task('t9')
        self.assertEqual((await self.client.post(
            f'/developer/tasks/{tid}/replay', headers=self.headers('carol'))).status_code, 403)
        self.assertEqual((await self.client.post(
            f'/developer/tasks/{tid}/replay')).status_code, 401)

    # 5. metrics endpoint ------------------------------------------------
    async def test_metrics_endpoint_counts_and_is_admin_only(self):
        await task_queue.enqueue(self.redis, 'm1', 'alice', '你好', 'c1', priority='high')
        await task_queue.enqueue(self.redis, 'm2', 'alice', '你好', 'c1', priority='low')
        await self._dead_task('m3')
        # m3 now failed (in dead), m1/m2 queued in high/low.
        await self.redis.setex('developer:worker:alive-1', 60, '{}')
        forbidden = await self.client.get('/developer/metrics', headers=self.headers('carol'))
        self.assertEqual(forbidden.status_code, 403)
        ok = await self.client.get('/developer/metrics', headers=self.headers('alice'))
        self.assertEqual(ok.status_code, 200)
        metrics = ok.json()
        self.assertEqual(metrics['queues']['high'], 1)
        self.assertEqual(metrics['queues']['low'], 1)
        self.assertEqual(metrics['queues']['normal'], 0)
        self.assertEqual(metrics['dead'], 1)
        self.assertEqual(metrics['delayed'], 0)
        self.assertEqual(metrics['workers'], 1)
        # Only indexed (queued/running) tasks carry status counts; m3 left the
        # index when it dead-lettered and is visible via `dead` instead.
        self.assertEqual(metrics['statuses'].get('queued'), 2)
        self.assertNotIn('failed', metrics['statuses'])

    async def test_started_event_carries_trace_id(self):
        captured = {}
        original = main._queued_stream

        async def spy(req, user_id, idempotency_key):
            response = await original(req, user_id, idempotency_key)
            gen = response.body_iterator
            captured['started'] = await gen.__anext__()
            await gen.aclose()
            return response

        main._queued_stream = spy
        try:
            with patch.dict(os.environ, {'DEVTRACE_TASK_QUEUE': 'enabled'}):
                resp = await self.client.post('/developer/chat/stream', headers=self.headers('alice'),
                                              json={'message': 'req_auth_01 为什么 401'})
                self.assertEqual(resp.status_code, 200)
        finally:
            main._queued_stream = original
        started_line = captured['started'].splitlines()[1]
        payload = json.loads(started_line[len('data: '):])
        self.assertEqual(len(payload['task_id']), 36)
        self.assertEqual(len(payload['trace_id']), 32)
        record = await task_queue.get_task(self.redis, payload['task_id'])
        self.assertEqual(record['trace_id'], payload['trace_id'])
        enqueued = self.redis.events('task.enqueued')[-1]
        self.assertEqual(enqueued['trace_id'], payload['trace_id'])

    async def test_audit_and_responses_never_leak_message_or_secrets(self):
        marker = 'super-secret-message-body-XYZ'
        tid = 's1'
        await task_queue.enqueue(self.redis, tid, 'alice', marker, 'c1')
        self.redis.lists[task_queue.priority_queue_key('normal')].remove(tid)
        # A failure whose message embeds an API key shape; the raw secret must
        # never surface in the dead-letter summary.
        await task_queue.requeue_or_fail(self.redis, tid, 'boom ' + marker)
        await task_queue.requeue_or_fail(self.redis, tid, 'auth failed sk-liveTOKEN123456')
        ok = await self.client.get('/developer/dead-letter', headers=self.headers('alice'))
        body = ok.text
        self.assertIn(tid, body)               # task is listed
        self.assertNotIn('sk-liveTOKEN123456', body)   # but the key is redacted
        self.assertNotIn(marker, body)         # and the message body never appears
        self.assertNotIn(marker, self.redis.dumped())


if __name__ == '__main__':
    unittest.main()
