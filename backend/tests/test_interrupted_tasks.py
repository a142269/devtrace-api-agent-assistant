"""P14 path two: an interrupted task must not leave a zombie session lock behind.

Two routes mark a stale `running` task `interrupted`: startup reconciliation over
the `developer:tasks:running` index, and the query-time repair inside
`GET /developer/tasks/{id}`. That second route also reaches queue tasks, which hold
`developer:session-task:<sha256(user\\0conversation)>` - the lock the departed
worker would have released. These tests pin the closure: owner-aware release, one
terminal audit, no double write on a repeat, and a safe skip for records that never
named both a user and a conversation.
"""
import json
import time
import unittest
from unittest.mock import patch

import httpx

from api import main
from api.auth import issue_token, password_hash
from developer import audit
from developer import task_queue

HASH = f'pbkdf2_sha256${"01" * 16}${password_hash("a-strong-test-password", "01" * 16)}'
SECRET = 'test-secret-with-at-least-32-bytes-long'
STALE = main._stale_after_seconds()
MESSAGE = '帮我看看这个报错'


class InterruptRedis:
    """Fake covering exactly the verbs the interrupted path touches."""

    def __init__(self):
        self.values, self.sets, self.lists = {}, {}, {}
        self.streams = {}
        self.eval_calls = []
        self._seq = 0

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
        return 1

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def eval(self, script, _numkeys, key, *args):
        """Faithful compare-and-delete: only the recorded owner can drop the lock."""
        self.eval_calls.append((script, key, args))
        if args and self.values.get(key) == args[0]:
            self.values.pop(key, None)
            return 1
        return 0

    async def xadd(self, key, fields, maxlen=None, approximate=False, id='*'):
        entry_id = f'{self._seq:013d}-0'
        self._seq += 1
        stream = self.streams.setdefault(key, [])
        stream.append((entry_id, {str(k).encode(): str(v).encode() for k, v in fields.items()}))
        if maxlen:
            del stream[:max(0, len(stream) - int(maxlen))]
        return entry_id

    def events(self, event=None):
        rows = []
        for _eid, fields in self.streams.get(audit.STREAM_KEY, []):
            row = {k.decode(): v.decode('utf-8', 'replace') for k, v in fields.items()}
            row['detail'] = json.loads(row.get('detail') or '{}')
            rows.append(row)
        if event:
            rows = [row for row in rows if row.get('event') == event]
        return rows

    def dumped(self):
        parts = []
        for _eid, fields in self.streams.get(audit.STREAM_KEY, []):
            parts.extend(value.decode('utf-8', 'replace') for value in fields.values())
        return ' '.join(parts)


class TinyAssistant:
    def __init__(self, redis):
        self.redis = redis


class InterruptedCaseMixin:
    """Shared seeding helpers; every test lives in a concrete TestCase below."""

    async def asyncSetUp(self):
        self.redis = InterruptRedis()

    async def _seed(self, tid='q1', user='alice', conversation='c1', *,
                    status='running', attempts=1, stale=True,
                    workspace='ws-a', trace_id='a' * 32, user_field=True,
                    owner='dead-worker'):
        """A queued task as the queue wrote it: session lock held, worker gone."""
        await task_queue.try_claim_session(self.redis, user, conversation, tid)
        await task_queue.enqueue(self.redis, tid, user, MESSAGE, conversation,
                                 workspace=workspace, trace_id=trace_id)
        record = await task_queue.get_task(self.redis, tid)
        record['status'] = status
        record['attempts'] = attempts
        record['worker_id'] = owner
        record['updated_at'] = time.time() - (STALE + 60 if stale else 5)
        if not user_field:
            record['user_id'] = ''
        await self.redis.setex(main._task_key(tid), 60, json.dumps(record))
        await self.redis.sadd(main.TASK_INDEX_KEY, tid)
        if owner != 'dead-worker':
            # Publish the owner's heartbeat so replicas see it as alive.
            await self.redis.setex(main._worker_key(owner), 90, json.dumps({'pid': 1}))
        return tid

    async def _seed_stream_record(self, tid='s1', user='alice', *, stale=True):
        """An in-process stream record: same key namespace, no queue conversation."""
        await main._write_task_record(self.redis, tid, user, 'running', None,
                                      workspace='ws-a', trace_id='b' * 32)
        record = json.loads(self.redis.values[main._task_key(tid)])
        record['updated_at'] = time.time() - (STALE + 60 if stale else 5)
        await self.redis.setex(main._task_key(tid), 60, json.dumps(record))
        return tid

    def lock(self, user='alice', conversation='c1'):
        return task_queue.session_key(user, conversation)


class InterruptedTaskTests(InterruptedCaseMixin, unittest.IsolatedAsyncioTestCase):
    """Route one: reconcile_stale_tasks over the running index."""

    async def test_stale_queue_task_releases_lock_and_writes_one_audit(self):
        tid = await self._seed()
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 1)

        record = await task_queue.get_task(self.redis, tid)
        self.assertEqual(record['status'], 'interrupted')
        self.assertEqual(record['attempts'], 1)
        self.assertNotIn(tid, self.redis.sets.get(main.TASK_INDEX_KEY, set()))
        # The zombie lock is gone, so the same conversation can be used again.
        self.assertIsNone(await self.redis.get(self.lock()))
        self.assertTrue(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'q2'))

        finished = self.redis.events('task.finished')
        self.assertEqual(len(finished), 1)
        self.assertEqual((finished[0]['status'], finished[0]['resource'], finished[0]['actor']),
                         ('interrupted', tid, 'alice'))
        self.assertEqual((finished[0]['workspace'], finished[0]['trace_id']), ('ws-a', 'a' * 32))
        detail = finished[0]['detail']
        self.assertEqual(detail, {'status': 'interrupted', 'attempts': 1,
                                  'reason_code': 'stale_heartbeat'})
        self.assertTrue(set(detail) <= audit.DETAIL_FIELDS)
        # The message body never reaches the audit stream.
        self.assertNotIn(MESSAGE, self.redis.dumped())

    async def test_repeated_reconcile_writes_no_second_audit_or_release(self):
        tid = await self._seed()
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 1)
        first_calls = len(self.redis.eval_calls)
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 0)

        record = await task_queue.get_task(self.redis, tid)
        # Called directly again (as the query path could) it must stay a no-op.
        again = await main._mark_interrupted(self.redis, tid, record)
        self.assertEqual(again['status'], 'interrupted')
        self.assertEqual(len(self.redis.events('task.finished')), 1)
        self.assertEqual(len(self.redis.eval_calls), first_calls)

    async def test_interrupted_release_never_drops_a_newer_tasks_lock(self):
        tid = await self._seed()
        # A newer request claimed the conversation before the repair caught up.
        await self.redis.delete(self.lock())
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'q-newer')
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 1)

        self.assertEqual((await task_queue.get_task(self.redis, tid))['status'], 'interrupted')
        self.assertEqual(await self.redis.get(self.lock()), 'q-newer')
        self.assertEqual(len(self.redis.events('task.finished')), 1)

    async def test_stream_record_skips_release_but_still_audits(self):
        tid = await self._seed_stream_record()
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 1)
        # No compare-and-delete may even be attempted: the record names no
        # conversation, so there is no lock this task can prove it owns.
        self.assertEqual(self.redis.eval_calls, [])
        record = json.loads(self.redis.values[main._task_key(tid)])
        self.assertEqual(record['status'], 'interrupted')
        finished = self.redis.events('task.finished')
        self.assertEqual([(row['status'], row['resource']) for row in finished],
                         [('interrupted', tid)])
        self.assertEqual(finished[0]['detail']['attempts'], 0)

    async def test_record_without_user_skips_release(self):
        tid = await self._seed(user_field=False)
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 1)
        self.assertEqual(self.redis.eval_calls, [])
        # The lock it can no longer prove it owns stays exactly where it is.
        self.assertEqual(await self.redis.get(self.lock()), tid)
        self.assertEqual(self.redis.events('task.finished')[0]['actor'], 'anonymous')

    async def test_running_task_owned_by_a_live_worker_is_left_untouched(self):
        tid = await self._seed(owner='live-worker')
        self.assertEqual(await main.reconcile_stale_tasks(self.redis), 0)
        self.assertEqual((await task_queue.get_task(self.redis, tid))['status'], 'running')
        self.assertEqual(await self.redis.get(self.lock()), tid)
        self.assertEqual(self.redis.events('task.finished'), [])
        self.assertEqual(self.redis.eval_calls, [])

    async def test_lock_fault_is_logged_and_the_audit_still_lands(self):
        from redis.exceptions import RedisError

        async def broken(*_args, **_kwargs):
            raise RedisError('connection reset')

        tid = await self._seed()
        with patch.object(task_queue, 'release_session_if_owner', broken), \
                self.assertLogs('developer.task_queue', level='WARNING') as captured:
            self.assertEqual(await main.reconcile_stale_tasks(self.redis), 1)
        self.assertTrue(any('会话锁释放失败' in line for line in captured.output))
        self.assertEqual((await task_queue.get_task(self.redis, tid))['status'], 'interrupted')
        self.assertEqual([(row['status'], row['resource'])
                          for row in self.redis.events('task.finished')],
                         [('interrupted', tid)])

    async def test_task_view_still_hides_queue_internals_after_repair(self):
        tid = await self._seed()
        view = await main._mark_interrupted(
            self.redis, tid, await task_queue.get_task(self.redis, tid))
        self.assertEqual(view['status'], 'interrupted')
        self.assertIn('reason', view)


class QueryRepairEndpointTests(InterruptedCaseMixin, unittest.IsolatedAsyncioTestCase):
    """Route two: the same invariants reached through GET /developer/tasks/{id}."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        main.app.state.jwt_secret = SECRET
        main.app.state.users = {'alice': HASH}
        main.app.state.user_workspaces = {'alice': 'ws-a'}
        previous = main._developer
        main._developer = TinyAssistant(self.redis)
        self.addCleanup(setattr, main, '_developer', previous)

    async def _get(self, tid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url='http://test') as client:
            return await client.get(
                f'/developer/tasks/{tid}',
                headers={'Authorization': 'Bearer ' + issue_token('alice', SECRET)})

    async def test_query_repair_finalises_a_stale_queue_task(self):
        tid = await self._seed()
        response = await self._get(tid)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'interrupted')
        self.assertIsNone(await self.redis.get(self.lock()))
        self.assertEqual([(row['status'], row['resource'])
                          for row in self.redis.events('task.finished')],
                         [('interrupted', tid)])
        # The conversation is immediately usable again.
        self.assertTrue(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 'q9'))

    async def test_second_query_repair_does_not_duplicate_the_audit(self):
        tid = await self._seed()
        first = await self._get(tid)
        second = await self._get(tid)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['status'], 'interrupted')
        self.assertEqual(len(self.redis.events('task.finished')), 1)
        self.assertEqual(len(self.redis.eval_calls), 1)

    async def test_query_repair_never_exposes_payload_or_leaks_message(self):
        tid = await self._seed()
        body = (await self._get(tid)).json()
        for hidden in ('payload', 'user_id', 'lease_id', 'lease_until'):
            self.assertNotIn(hidden, body)
        self.assertNotIn(MESSAGE, json.dumps(body, ensure_ascii=False))

    async def test_live_task_view_is_not_repaired_through_the_endpoint(self):
        tid = await self._seed(stale=False)
        response = await self._get(tid)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'running')
        self.assertEqual(await self.redis.get(self.lock()), tid)
        self.assertEqual(self.redis.events('task.finished'), [])


if __name__ == '__main__':
    unittest.main()
