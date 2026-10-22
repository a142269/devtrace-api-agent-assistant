"""P7: every transient Redis write reads one TTL knob (DEVTRACE_REDIS_TTL_SECONDS).

Each assertion names the production call site it covers, so a future literal 86400
slipping back into an API, assistant or queue path shows up here.
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from api import main
from api.auth import issue_token, password_hash
from developer import task_queue
from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.ttl import (DEFAULT_REDIS_TTL_SECONDS, MIN_REDIS_TTL_SECONDS,
                           parse_redis_ttl_seconds, redis_ttl_seconds)
from memory.conversation_memory import parse_memory_ttl_seconds
from mcp.tool_manager import MCPToolManager
from test_developer_assistant import (FakeKnowledge, FakeMemory, FakeRedis, SandboxClient,
                                      sandbox)

CUSTOM_TTL = 120


class TtlRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.ttls = {}

    async def setex(self, key, ttl, value):
        self.ttls[key] = ttl
        await super().setex(key, ttl, value)

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True


class QueueRedis:
    """Only the verbs save_task/append_event touch."""

    def __init__(self):
        self.values, self.ttls, self.lists, self.sets = {}, {}, {}, {}

    async def setex(self, key, ttl, value):
        self.ttls[key] = ttl
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:end if end != -1 else None]

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))


class CountingTtlRedis(QueueRedis):
    """Adds the limiter, lock and audit-stream verbs the API path needs."""

    def __init__(self):
        super().__init__()
        self.held = set()
        self.events = []

    async def eval(self, _script, _numkeys, key, *_args):
        count = int(self.values.get(key, 0)) + 1
        self.values[key] = str(count)
        return count

    def lock(self, key, **_kwargs):
        outer = self

        class _Lock:
            def __init__(self):
                self.key = key

            async def acquire(self):
                if self.key in outer.held:
                    return False
                outer.held.add(self.key)
                return True

            async def release(self):
                outer.held.discard(self.key)

        return _Lock()

    async def xadd(self, key, fields, **_kwargs):
        self.events.append(dict(fields))
        return '1-1'


class TtlHelperTests(unittest.TestCase):
    def test_unset_or_illegal_values_fall_back_to_the_default(self):
        for raw in (None, '', '   ', 'not-a-number', '86400s', '3600.5'):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(parse_redis_ttl_seconds(raw), DEFAULT_REDIS_TTL_SECONDS)

    def test_legal_values_are_clamped_to_the_floor(self):
        for raw, expected in (('120', 120), ('86400', 86400), ('60', 60), ('59', MIN_REDIS_TTL_SECONDS),
                              ('0', MIN_REDIS_TTL_SECONDS), ('-1', MIN_REDIS_TTL_SECONDS)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_redis_ttl_seconds(raw), expected)

    def test_environment_is_read_at_call_time(self):
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': '120'}):
            self.assertEqual(redis_ttl_seconds(), CUSTOM_TTL)
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': ''}):
            self.assertEqual(redis_ttl_seconds(), DEFAULT_REDIS_TTL_SECONDS)

    def test_memory_ttl_knob_stays_separate(self):
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': '120',
                                     'DEVTRACE_MEMORY_TTL_SECONDS': '7200'}):
            self.assertEqual(redis_ttl_seconds(), CUSTOM_TTL)
            self.assertEqual(parse_memory_ttl_seconds(), 7200)
        with patch.dict(os.environ, {'DEVTRACE_MEMORY_TTL_SECONDS': '7200'}):
            self.assertEqual(redis_ttl_seconds(), DEFAULT_REDIS_TTL_SECONDS)
            self.assertEqual(parse_memory_ttl_seconds(), 7200)


class ApiTtlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        encoded = f'pbkdf2_sha256${"01" * 16}${password_hash("a-strong-test-password", "01" * 16)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded}
        self.redis = CountingTtlRedis()
        self.assistant = TinyAssistant(self.redis)
        self.old = main._developer
        main._developer = self.assistant
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                        base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(setattr, main, '_developer', self.old)

    def headers(self):
        return {'Authorization': 'Bearer ' + issue_token('alice', self.secret)}

    async def test_task_records_use_the_configured_ttl(self):
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': str(CUSTOM_TTL)}):
            await main._write_task_record(self.redis, 't1', 'alice', 'running')
            self.assertEqual(self.redis.ttls['developer:task:t1'], CUSTOM_TTL)
            await main._touch_task(self.redis, 't1')
            self.assertEqual(self.redis.ttls['developer:task:t1'], CUSTOM_TTL)
            record = json.loads(self.redis.values['developer:task:t1'])
            await main._mark_interrupted(self.redis, 't1', record)
            self.assertEqual(self.redis.ttls['developer:task:t1'], CUSTOM_TTL)
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': ''}):
            await main._write_task_record(self.redis, 't2', 'alice', 'running')
            self.assertEqual(self.redis.ttls['developer:task:t2'], DEFAULT_REDIS_TTL_SECONDS)

    async def test_run_stamp_and_idempotency_cache_use_the_configured_ttl(self):
        digest = hashlib.sha256('alice\0idem-1'.encode()).hexdigest()
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': str(CUSTOM_TTL)}):
            response = await self.client.post('/developer/chat', headers={**self.headers(),
                                                       'Idempotency-Key': 'idem-1'},
                                              json={'message': 'hello', 'conversation_id': 'c1'})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.redis.ttls[f'developer:request:{digest}'], CUSTOM_TTL)
        self.assertEqual(self.redis.ttls['developer:run:r1'], CUSTOM_TTL)

    async def test_task_queue_records_and_events_use_the_configured_ttl(self):
        redis = QueueRedis()
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': str(CUSTOM_TTL)}):
            await task_queue.enqueue(redis, 't9', 'alice', 'hello', 'c1')
            await task_queue.append_event(redis, 't9', {'type': 'started', 'task_id': 't9'})
        self.assertEqual(redis.ttls[task_queue.task_key('t9')], CUSTOM_TTL)
        self.assertEqual(redis.ttls[task_queue.events_key('t9')], CUSTOM_TTL)


class AssistantTtlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = sandbox.DB_PATH
        sandbox.DB_PATH = Path(self.temp.name) / 'tickets.sqlite'
        self.redis = TtlRedis()
        self.assistant = DeveloperAssistant(
            memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=self.redis,
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key='test-key'),
            sandbox_url='http://sandbox')
        self.patcher = patch('developer.assistant.httpx.AsyncClient', SandboxClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(lambda: setattr(sandbox, 'DB_PATH', self.original_db))
        self.addCleanup(self.temp.cleanup)

    async def test_checkpoints_run_records_and_history_share_one_ttl(self):
        with patch.dict(os.environ, {'DEVTRACE_REDIS_TTL_SECONDS': str(CUSTOM_TTL)}):
            result = await self.assistant.run('请求 req_auth_01 返回 401', 'u1')
        run_key = f"developer:run:{result['run_id']}"
        history_key = 'developer:run-index:' + hashlib.sha256(b'u1').hexdigest()
        state_keys = [key for key in self.redis.ttls if key.startswith('developer:plan:')]
        self.assertTrue(state_keys, 'plan checkpoint writes must exist')
        self.assertIn(run_key, self.redis.ttls)
        self.assertIn(history_key, self.redis.ttls)
        self.assertEqual(set(self.redis.ttls.values()), {CUSTOM_TTL})


class TinyAssistant:
    def __init__(self, redis):
        self.redis, self.calls = redis, 0

    async def run(self, message, user, conversation):
        self.calls += 1
        result = {'conversation_id': conversation, 'run_id': 'r1', 'answer': '诊断完成',
                  'kind': 'api_error', 'plan': {'status': 'complete'}}
        await self.redis.setex(f'developer:run:{result["run_id"]}', 60,
                               json.dumps({'user_id': user, 'response': result}))
        return result


if __name__ == '__main__':
    unittest.main()
