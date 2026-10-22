"""P10: the limiter fails open when Redis itself is unavailable.

A Redis/Lua fault on the quota path must not turn into HTTP 500 for every request.
Availability wins over enforcement here (demo-grade trade-off), while a real quota
breach still returns 429 with Retry-After for login, sync chat and streaming alike.
Only infrastructure faults are swallowed: an unexpected error keeps propagating.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from api import main
from api.auth import issue_token, password_hash
from developer.events import publish_existing
from redis.exceptions import ConnectionError as RedisConnectionError

SECRET_MESSAGE = '我的密钥是 sk-abcdefghijkl12345，正文不能出现在限流降级里'


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


class QuotaRedis:
    """Working quota store that can be told to fail on the limiter's EVAL only."""

    def __init__(self):
        self.values, self.held, self.sets = {}, set(), {}
        self.ttls, self.events = {}, []
        self.eval_error = None

    async def eval(self, _script, _numkeys, key, *_args):
        if self.eval_error is not None:
            raise self.eval_error
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

    def event_names(self):
        return [event['event'] for event in self.events]

    def events_named(self, name):
        return [event for event in self.events if event['event'] == name]

    def counts(self):
        return {name: int(value) for name, value in self.values.items()
                if 'limit' in name}


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


class RateLimitDegradationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        encoded = f'pbkdf2_sha256${"01" * 16}${password_hash("a-strong-test-password", "01" * 16)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded}
        self.redis = QuotaRedis()
        self.assistant = FakeAssistant(self.redis)
        self.old = main._developer
        main._developer = self.assistant
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                        base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(setattr, main, '_developer', self.old)

    def headers(self, user='alice'):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret)}

    async def post_login(self, password='a-strong-test-password'):
        return await self.client.post('/auth/login',
                                      json={'username': 'alice', 'password': password})

    async def post_sync(self, message='hello'):
        return await self.client.post('/developer/chat', headers=self.headers(),
                                      json={'message': message})

    async def post_stream(self, message='hello'):
        return await self.client.post('/developer/chat/stream', headers=self.headers(),
                                      json={'message': message})

    # ── fail-open: Redis 故障不再 500 ─────────────────────────────────────────

    async def test_login_succeeds_when_limiter_redis_is_down(self):
        self.redis.eval_error = RedisConnectionError('Connection refused')
        response = await self.post_login()
        self.assertEqual(response.status_code, 200)
        self.assertIn('access_token', response.json())
        self.assertEqual(self.redis.event_names(),
                         ['login.rate_limit_unavailable', 'auth.login.success'])

    async def test_sync_chat_runs_when_limiter_redis_is_down(self):
        self.redis.eval_error = RedisConnectionError('Connection refused')
        with self.assertLogs('api.main', level='WARNING') as logs:
            response = await self.post_sync()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.assistant.calls, 1)
        self.assertTrue(any('fail-open' in line for line in logs.output))

    async def test_stream_runs_when_limiter_redis_is_down(self):
        self.redis.eval_error = OSError('Connection reset by peer')
        response = await self.post_stream()
        self.assertEqual(response.status_code, 200)
        self.assertIn('plan', response.text)
        self.assertEqual(self.assistant.calls, 1)
        self.assertIn('chat.rate_limit_unavailable', self.redis.event_names())

    async def test_blocking_timeout_also_fails_open(self):
        self.redis.eval_error = asyncio.TimeoutError()
        response = await self.post_sync()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.assistant.calls, 1)

    async def test_audit_detail_stays_inside_the_whitelist(self):
        self.redis.eval_error = RedisConnectionError('Connection refused')
        await self.post_sync(message=SECRET_MESSAGE)
        await self.post_login()
        events = self.redis.events_named('chat.rate_limit_unavailable') + \
            self.redis.events_named('login.rate_limit_unavailable')
        self.assertTrue(events)
        for event in events:
            detail = json.loads(event['detail'])
            self.assertEqual(set(detail) - {'kind', 'status', 'reason_code'}, set())
            self.assertEqual(detail['reason_code'], 'limiter_unavailable')
            self.assertNotIn('正文', event['detail'])
            self.assertNotIn('sk-abcdefghijkl', event['detail'])
        self.assertTrue({'chat', 'login'} <=
                        {json.loads(e['detail'])['kind'] for e in events})

    async def test_quota_breach_is_never_swallowed_by_fail_open(self):
        # Redis is healthy for the window check; only the audit write is broken.
        self.redis.eval_error = None
        for _ in range(main.CHAT_LIMIT_MAXIMUM):
            self.assertEqual((await self.post_sync()).status_code, 200)

        async def broken_xadd(*_args, **_kwargs):
            raise RedisConnectionError('audit stream unavailable')

        self.redis.xadd = broken_xadd
        rejected = await self.post_sync()
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.headers['Retry-After'], str(main.CHAT_LIMIT_WINDOW_SECONDS))

    async def test_unexpected_limiter_error_is_not_swallowed(self):
        # Fail-open is scoped to infrastructure faults: a programming error must not
        # be turned into a silent pass.
        self.redis.eval_error = ValueError('bad limiter script')
        with self.assertRaises(ValueError):
            await main.enforce_limit('chat', main._chat_limit_key('alice'),
                                     main.CHAT_LIMIT_MAXIMUM, retry_after=60)

    # ── 一致性：登录 429 也带 Retry-After ─────────────────────────────────────

    async def test_login_429_carries_retry_after(self):
        codes = [(await self.post_login()).status_code for _ in range(main.LOGIN_LIMIT_MAXIMUM)]
        self.assertEqual(set(codes), {200})
        rejected = await self.post_login()
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.headers['Retry-After'], str(main.LOGIN_LIMIT_RETRY_AFTER))
        self.assertEqual(rejected.json()['detail'], '请求过于频繁，请稍后重试')
        # A real breach is enforcement, not degradation: no unavailable event is written.
        self.assertNotIn('login.rate_limit_unavailable', self.redis.event_names())
        self.assertEqual(self.redis.counts()[main._login_limit_key('127.0.0.1')],
                         main.LOGIN_LIMIT_MAXIMUM + 1)

    async def test_fixed_window_still_counts_rejected_requests(self):
        # Documented, unchanged semantics: INCR happens before the comparison, so the
        # refusals extend the window instead of being free.
        for _ in range(main.CHAT_LIMIT_MAXIMUM + 3):
            await self.post_sync()
        key = main._chat_limit_key('alice')
        self.assertEqual(int(self.redis.values[key]), main.CHAT_LIMIT_MAXIMUM + 3)
        self.assertEqual(self.assistant.calls, main.CHAT_LIMIT_MAXIMUM)

    async def test_normal_path_spends_quota_unchanged(self):
        with patch.object(main, 'limit') as spy:
            await main.enforce_limit('chat', main._chat_limit_key('alice'),
                                     main.CHAT_LIMIT_MAXIMUM, retry_after=60)
        spy.assert_awaited_once_with(main._chat_limit_key('alice'), main.CHAT_LIMIT_MAXIMUM,
                                     retry_after=60)
        self.assertEqual(self.redis.event_names(), [])


if __name__ == '__main__':
    unittest.main()
