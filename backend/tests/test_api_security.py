import asyncio
import json
import os
import time
import unittest

import httpx
from api import main
from api.auth import issue_token, password_hash
from developer.events import publish_existing


class Lock:
    def __init__(self, redis, key):
        self.redis, self.key = redis, key

    async def acquire(self):
        if self.key in self.redis.held:
            return False
        self.redis.held.add(self.key)
        return True

    async def release(self):
        self.redis.held.remove(self.key)


class Redis:
    def __init__(self):
        self.values, self.held, self.sets = {}, set(), {}

    async def eval(self, *_args):
        return 1

    def lock(self, key, **_kwargs):
        return Lock(self, key)

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))


class Assistant:
    def __init__(self):
        self.redis = Redis()
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.calls = 0

    async def run(self, message, user, conversation):
        self.calls += 1
        timeline = publish_existing([{'type': 'plan', 'text': '测试计划'}])
        timeline.append({'type': 'action', 'text': '测试行动'})
        self.entered.set()
        await self.resume.wait()
        result = {'conversation_id': conversation, 'run_id': 'r1', 'answer': message}
        await self.redis.setex('developer:run:r1', 60, json.dumps({'user_id': user, 'response': result}))
        return result


class ApiSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        salt = '01' * 16
        encoded = f'pbkdf2_sha256${salt}${password_hash("a-strong-test-password", salt)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded, 'bob': encoded}
        self.assistant = Assistant()
        self.old = main._developer
        main._developer = self.assistant
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main._developer = self.old

    def headers(self, user='alice', **extra):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret), **extra}

    async def test_login_and_untrusted_user_id(self):
        login = await self.client.post('/auth/login', json={'username': 'alice', 'password': 'a-strong-test-password'})
        self.assertEqual(login.status_code, 200)
        self.assertEqual((await self.client.post('/developer/chat', json={'message': 'hello'})).status_code, 401)
        forged = await self.client.post('/developer/chat', headers=self.headers(), json={'message': 'hello', 'user_id': 'bob'})
        self.assertEqual(forged.status_code, 422)

    async def test_same_session_concurrency_idempotency_and_isolation(self):
        headers = self.headers(**{'Idempotency-Key': 'unique-request'})
        body = {'message': 'hello', 'conversation_id': 'c1'}
        first = asyncio.create_task(self.client.post('/developer/chat', headers=headers, json=body))
        await self.assistant.entered.wait()
        concurrent = await self.client.post('/developer/chat', headers=self.headers(), json=body)
        self.assertEqual(concurrent.status_code, 409)
        self.assistant.resume.set()
        result = await first
        self.assertEqual(result.status_code, 200)
        replay = await self.client.post('/developer/chat', headers=headers, json=body)
        self.assertEqual(replay.json(), result.json())
        self.assertEqual(self.assistant.calls, 1)
        conflict = await self.client.post('/developer/chat', headers=headers, json={'message': 'other', 'conversation_id': 'c1'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual((await self.client.get('/developer/runs/r1', headers=self.headers('bob'))).status_code, 404)
        self.assertEqual((await self.client.get('/developer/runs/r1', headers=self.headers())).status_code, 200)

    async def test_authenticated_stream_reports_events_and_result(self):
        self.assistant.resume.set()
        response = await self.client.post('/developer/chat/stream', headers=self.headers(), json={'message': 'hello'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('event: timeline', response.text)
        self.assertIn('测试计划', response.text)
        self.assertIn('event: complete', response.text)
        self.assertEqual((await self.client.post('/developer/chat/stream', json={'message': 'hello'})).status_code, 401)

    async def test_stream_disconnect_releases_session_lock(self):
        stream = await main.developer_chat_stream(main.DeveloperChatRequest(message='hello', conversation_id='c1'), 'alice', 'request-1')
        iterator = stream.body_iterator
        started = await anext(iterator)
        self.assertIn('event: started', started)
        task_id = json.loads(started.split('data: ', 1)[1])['task_id']
        self.assertIn('event: timeline', await anext(iterator))
        self.assertTrue(self.assistant.redis.held)
        await iterator.aclose()
        self.assertFalse(self.assistant.redis.held)
        own = await self.client.get(f'/developer/tasks/{task_id}', headers=self.headers())
        self.assertEqual(own.json()['status'], 'cancelled')
        self.assertEqual((await self.client.get(f'/developer/tasks/{task_id}', headers=self.headers('bob'))).status_code, 404)

    async def test_cancelled_stream_task_records_cancelled_and_releases_lock(self):
        # Approximates Starlette cancelling the response task on client disconnect:
        # the generator receives CancelledError while the worker is still running.
        stream = await main.developer_chat_stream(
            main.DeveloperChatRequest(message='hello', conversation_id='c2'), 'alice', 'request-cancel')
        iterator = stream.body_iterator
        started = await anext(iterator)
        task_id = json.loads(started.split('data: ', 1)[1])['task_id']

        async def consume():
            async for _ in iterator:
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        record = {}
        for _ in range(100):
            raw = self.assistant.redis.values.get(f'developer:task:{task_id}')
            record = json.loads(raw) if raw else {}
            if record.get('status') != 'running':
                break
            await asyncio.sleep(0.02)
        self.assertEqual(record.get('status'), 'cancelled')
        self.assertFalse(self.assistant.redis.held)

    async def test_running_task_is_repaired_after_its_worker_disappears(self):
        redis = self.assistant.redis
        record = await main._write_task_record(redis, 't1', 'alice', 'running')
        record['updated_at'] -= 3600
        await redis.setex('developer:task:t1', 86400, json.dumps(record))
        view = await main.developer_task('t1', 'alice')
        self.assertEqual(view['status'], 'interrupted')
        self.assertNotIn('user_id', view)
        self.assertIn('reason', view)
        self.assertNotIn('t1', redis.sets.get(main.TASK_INDEX_KEY, set()))
        with self.assertRaises(Exception):
            await main.developer_task('t1', 'bob')

    async def test_task_owned_by_a_live_replica_is_not_repaired(self):
        redis = self.assistant.redis
        record = await main._write_task_record(redis, 't8', 'alice', 'running')
        record['worker_id'] = 'other-worker'
        record['updated_at'] -= 3600
        await redis.setex('developer:task:t8', 86400, json.dumps(record))
        await redis.setex('developer:worker:other-worker', 90, 'alive')
        view = await main.developer_task('t8', 'alice')
        self.assertEqual(view['status'], 'running')
        self.assertEqual(await main.reconcile_stale_tasks(redis), 0)

    async def test_task_owned_by_a_dead_replica_is_repaired(self):
        redis = self.assistant.redis
        record = await main._write_task_record(redis, 't9', 'alice', 'running')
        record['worker_id'] = 'dead-worker'
        record['updated_at'] -= 3600
        await redis.setex('developer:task:t9', 86400, json.dumps(record))
        self.assertEqual(await main.reconcile_stale_tasks(redis), 1)
        self.assertEqual((await main.developer_task('t9', 'alice'))['status'], 'interrupted')

    async def test_worker_registration_is_visible_to_replicas(self):
        await main.register_worker(self.assistant.redis)
        self.assertTrue(await main._worker_alive(self.assistant.redis, main.WORKER_ID))
        self.assertFalse(await main._worker_alive(self.assistant.redis, 'missing-worker'))

    async def test_live_running_task_and_finished_task_are_not_interrupted(self):
        redis = self.assistant.redis
        await main._write_task_record(redis, 't2', 'alice', 'running')
        await main._write_task_record(redis, 't3', 'alice', 'completed')
        self.assertEqual((await main.developer_task('t2', 'alice'))['status'], 'running')
        self.assertEqual((await main.developer_task('t3', 'alice'))['status'], 'completed')

    async def test_startup_reconcile_marks_only_leftover_running_tasks(self):
        redis = self.assistant.redis
        await main._write_task_record(redis, 't4', 'alice', 'running')
        await main._write_task_record(redis, 't5', 'alice', 'waiting')
        marked = await main.reconcile_stale_tasks(redis)
        self.assertEqual(marked, 1)
        self.assertEqual((await main.developer_task('t4', 'alice'))['status'], 'interrupted')
        self.assertEqual((await main.developer_task('t5', 'alice'))['status'], 'waiting')

    async def test_heartbeat_keeps_a_long_running_task_alive(self):
        redis = self.assistant.redis
        record = await main._write_task_record(redis, 't6', 'alice', 'running')
        record['updated_at'] = time.time() - 1000
        await redis.setex('developer:task:t6', 86400, json.dumps(record))
        await main._touch_task(redis, 't6')
        refreshed = json.loads(redis.values['developer:task:t6'])
        self.assertLess(time.time() - refreshed['updated_at'], 5)
        self.assertEqual(refreshed['status'], 'running')

    async def test_stale_threshold_stays_above_the_run_and_lock_windows(self):
        previous = os.environ.get('DEVTRACE_TASK_STALE_SECONDS')
        try:
            os.environ['DEVTRACE_TASK_STALE_SECONDS'] = '30'
            self.assertGreaterEqual(main._stale_after_seconds(), 240)
            os.environ['DEVTRACE_TASK_STALE_SECONDS'] = 'not-a-number'
            self.assertGreaterEqual(main._stale_after_seconds(), 240)
        finally:
            if previous is None:
                os.environ.pop('DEVTRACE_TASK_STALE_SECONDS', None)
            else:
                os.environ['DEVTRACE_TASK_STALE_SECONDS'] = previous

    async def test_repair_does_not_overwrite_a_task_that_just_finished(self):
        redis = self.assistant.redis
        record = await main._write_task_record(redis, 't7', 'alice', 'running')
        stale = dict(record)
        stale['updated_at'] -= 3600
        finished = dict(record)
        finished['status'] = 'completed'
        finished['updated_at'] = record['updated_at'] + 5
        await redis.setex('developer:task:t7', 86400, json.dumps(finished))
        view = await main._mark_interrupted(redis, 't7', stale)
        self.assertEqual(view['status'], 'completed')

    async def test_reconcile_degrades_when_the_index_is_not_supported(self):
        class MinimalRedis:
            async def get(self, key):
                return None

        self.assertEqual(await main.reconcile_stale_tasks(MinimalRedis()), 0)
