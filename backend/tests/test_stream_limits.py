"""P7: the streaming entry point spends the same user quota as the synchronous chat.

The limiter runs before any task record, queue entry or SSE byte exists, so a
rejected request never leaves a half-finished task behind.
"""
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi.responses import JSONResponse
from api import main
from api.auth import issue_token, password_hash
from developer.events import publish_existing

SECRET_MESSAGE = '我的密钥是 sk-abcdefghijkl12345，正文不能出现在限流响应里'


class Lock:
    def __init__(self, redis, key):
        self.redis, self.key = redis, key

    async def acquire(self):
        if self.key in self.redis.held:
            return False
        self.redis.held.add(self.key)
        return True

    async def release(self):
        self.redis.held.discard(self.key)


class CountingRedis:
    """`eval` mirrors the limiter script: INCR plus EXPIRE on the first hit."""

    def __init__(self):
        self.values, self.held, self.sets = {}, set(), {}
        self.ttls, self.events = {}, []

    async def eval(self, _script, _numkeys, key, *_args):
        count = int(self.values.get(key, 0)) + 1
        self.values[key] = str(count)
        self.ttls.setdefault(key, 60)
        return count

    def lock(self, key, **_kwargs):
        return Lock(self, key)

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.ttls[key] = ttl
        self.values[key] = value

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def xadd(self, key, fields, **_kwargs):
        self.events.append({k: v for k, v in fields.items()})
        return '1-1'

    def names(self):
        return set(self.values) | set(self.sets)

    def task_ids(self):
        return {name.split(':', 2)[2] for name in self.values if name.startswith('developer:task:')}

    def event_names(self):
        return [event['event'] for event in self.events]


class FakeAssistant:
    def __init__(self, redis):
        self.redis, self.calls = redis, 0

    async def run(self, message, user, conversation):
        self.calls += 1
        publish_existing([{'type': 'plan', 'text': '测试计划'}])
        result = {'conversation_id': conversation, 'run_id': f'r{self.calls}',
                  'answer': '诊断完成', 'kind': 'api_error', 'plan': {'status': 'complete'}}
        await self.redis.setex(f'developer:run:{result["run_id"]}', 60,
                               json.dumps({'user_id': user, 'response': result}))
        return result


class StreamLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        encoded = f'pbkdf2_sha256${"01" * 16}${password_hash("a-strong-test-password", "01" * 16)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded, 'bob': encoded}
        self.redis = CountingRedis()
        self.assistant = FakeAssistant(self.redis)
        self.old = main._developer
        main._developer = self.assistant
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                        base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(setattr, main, '_developer', self.old)

    def headers(self, user='alice'):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret)}

    async def post_stream(self, user='alice', message='hello'):
        return await self.client.post('/developer/chat/stream', headers=self.headers(user),
                                      json={'message': message})

    async def post_sync(self, user='alice', message='hello'):
        return await self.client.post('/developer/chat', headers=self.headers(user),
                                      json={'message': message})

    async def test_stream_first_request_spends_exactly_one_quota(self):
        self.assertEqual((await self.post_stream()).status_code, 200)
        self.assertEqual(int(self.redis.values[main._chat_limit_key('alice')]), 1)
        self.assertEqual(self.assistant.calls, 1)

    async def test_stream_thirty_requests_all_succeed(self):
        codes = [(await self.post_stream()).status_code for _ in range(main.CHAT_LIMIT_MAXIMUM)]
        self.assertEqual(set(codes), {200})
        self.assertEqual(self.assistant.calls, main.CHAT_LIMIT_MAXIMUM)

    async def test_stream_31st_request_is_429_with_retry_after_and_no_side_effects(self):
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            self.assertEqual((await self.post_stream()).status_code, 200)
        tasks_before, calls_before = self.redis.task_ids(), self.assistant.calls
        events_before = len(self.redis.events)
        rejected = await self.post_stream()
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.headers['Retry-After'], str(main.CHAT_LIMIT_WINDOW_SECONDS))
        self.assertEqual(rejected.json()['detail'], '请求过于频繁，请稍后重试')
        self.assertEqual(self.redis.task_ids(), tasks_before)
        self.assertEqual(self.assistant.calls, calls_before)
        self.assertEqual(self.redis.event_names()[events_before:], ['chat.rate_limited'])

    async def test_sync_and_stream_share_one_quota(self):
        codes = []
        for index in range(main.CHAT_LIMIT_MAXIMUM):
            codes.append((await self.post_sync() if index % 2 else await self.post_stream()).status_code)
        self.assertEqual(set(codes), {200})
        self.assertEqual(int(self.redis.values[main._chat_limit_key('alice')]),
                         main.CHAT_LIMIT_MAXIMUM)
        self.assertEqual((await self.post_stream()).status_code, 429)
        self.assertEqual((await self.post_sync()).status_code, 429)

    async def test_sync_429_also_carries_retry_after(self):
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            self.assertEqual((await self.post_sync()).status_code, 200)
        rejected = await self.post_sync()
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.headers['Retry-After'], str(main.CHAT_LIMIT_WINDOW_SECONDS))

    async def test_quota_is_isolated_per_user(self):
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            self.assertEqual((await self.post_stream()).status_code, 200)
        self.assertEqual((await self.post_stream()).status_code, 429)
        self.assertEqual((await self.post_stream(user='bob')).status_code, 200)
        self.assertEqual(int(self.redis.values[main._chat_limit_key('bob')]), 1)

    async def test_queue_path_is_limited_before_enqueue(self):
        # The queue branch is replaced by a spy: reaching it at all means the quota
        # was spent, so a rejected request must never get here.
        calls = []

        async def spy(req, user_id, idempotency_key):
            calls.append(user_id)
            return JSONResponse({'queued': True})

        with patch.dict(os.environ, {'DEVTRACE_TASK_QUEUE': 'enabled'}), \
                patch.object(main, '_queued_stream', spy):
            for _ in range(main.CHAT_LIMIT_MAXIMUM):
                self.assertEqual((await self.post_stream()).status_code, 200)
            self.assertEqual(len(calls), main.CHAT_LIMIT_MAXIMUM)
            rejected = await self.post_stream()
            self.assertEqual(rejected.status_code, 429)
        self.assertEqual(len(calls), main.CHAT_LIMIT_MAXIMUM)
        self.assertEqual(self.redis.task_ids(), set())
        self.assertNotIn('task.enqueued', self.redis.event_names())
        self.assertEqual(self.assistant.calls, 0)

    async def test_rejection_leaks_neither_message_nor_plaintext_user(self):
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            await self.post_stream(message=SECRET_MESSAGE)
        rejected = await self.post_stream(message=SECRET_MESSAGE)
        self.assertEqual(rejected.status_code, 429)
        self.assertNotIn('sk-abcdefghijkl', rejected.text)
        self.assertNotIn('正文', rejected.text)
        limit_key = main._chat_limit_key('alice')
        self.assertNotIn('alice', limit_key)
        self.assertNotIn('alice', json.dumps(sorted(self.redis.names())))
        rate_events = [event for event in self.redis.events if event['event'] == 'chat.rate_limited']
        self.assertTrue(rate_events)
        for event in rate_events:
            self.assertEqual(set(json.loads(event['detail'])) - {'kind', 'status', 'reason_code'}, set())
            self.assertNotIn('正文', event['detail'])

    async def test_enforce_limit_is_not_reachable_as_a_query_parameter(self):
        import inspect
        self.assertNotIn('enforce_limit', inspect.signature(main.developer_chat).parameters)
        self.assertNotIn('enforce_limit', inspect.signature(main.developer_chat_stream).parameters)
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            await self.post_stream()
        bypass = await self.client.post('/developer/chat/stream?enforce_limit=false',
                                        headers=self.headers(), json={'message': 'hello'})
        self.assertEqual(bypass.status_code, 429)
        bypass_sync = await self.client.post('/developer/chat?enforce_limit=false',
                                             headers=self.headers(), json={'message': 'hello'})
        self.assertEqual(bypass_sync.status_code, 429)

    async def test_rejected_stream_request_keeps_locks_untouched(self):
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            await self.post_stream()
        self.redis.held.clear()
        self.assertEqual((await self.post_stream()).status_code, 429)
        self.assertEqual(self.redis.held, set())


if __name__ == '__main__':
    unittest.main()
