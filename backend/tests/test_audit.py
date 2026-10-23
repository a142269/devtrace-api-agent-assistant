"""P1 RBAC-lite + append-only audit log."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from api import main
from api import worker as api_worker
from api.auth import issue_token, password_hash
from developer import audit
from developer import sandbox
from developer import task_queue
from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from mcp.tool_manager import MCPToolManager

SECRET_MARKER = 'hunter2-secret-value'


class StreamRedis:
    """Fake redis that stores streams the way redis-py does: bytes ids and values."""

    def __init__(self):
        self.values, self.held, self.sets, self.lists = {}, set(), {}, {}
        self.streams = {}
        self._seq = 0

    async def eval(self, _script, _numkeys, key, *args):
        """Emulate the session-lock compare-and-delete script (only it passes argv)."""
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

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def zadd(self, key, mapping):
        pass

    async def zremrangebyrank(self, key, _start, _end):
        pass

    async def expire(self, key, _ttl):
        pass

    async def xadd(self, key, fields, maxlen=None, approximate=False, id='*'):
        entry_id = f'{self._seq:013d}-0'
        self._seq += 1
        stream = self.streams.setdefault(key, [])
        stream.append((entry_id, {str(k).encode('utf-8'): str(v).encode('utf-8') for k, v in fields.items()}))
        if maxlen:
            del stream[:max(0, len(stream) - int(maxlen))]
        return entry_id

    async def xrevrange(self, key, min='-', max='+', count=None):
        items = list(reversed(self.streams.get(key, [])))
        if max != '+':
            inclusive = not max.startswith('(')
            cutoff = max if inclusive else max[1:]
            items = [item for item in items
                     if (item[0] <= cutoff if inclusive else item[0] < cutoff)]
        return items[:count] if count else items

    def dumped(self):
        """Every stored audit value as one searchable string."""
        parts = []
        for _entry_id, fields in self.streams.get(audit.STREAM_KEY, []):
            for key, value in fields.items():
                parts.append(key.decode() if isinstance(key, bytes) else key)
                parts.append(value.decode('utf-8', 'replace') if isinstance(value, bytes) else value)
        return ' '.join(parts)


class EchoAssistant:
    def __init__(self, redis):
        self.redis = redis
        self.calls = 0

    async def run(self, message, user, conversation):
        self.calls += 1
        return {'conversation_id': conversation, 'run_id': 'run-1', 'kind': 'auth',
                'answer': message, 'plan': {'status': 'complete'}, 'metrics': {'elapsed_ms': 12.3}}


class SandboxClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.pop('timeout', None)
        super().__init__(transport=httpx.ASGITransport(app=sandbox.app), base_url='http://sandbox', **kwargs)


class ApiAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_admins = os.environ.get('DEVTRACE_ADMIN_USERS')
        os.environ['DEVTRACE_ADMIN_USERS'] = 'alice,ghost'  # ghost is not a configured user

    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        salt = '01' * 16
        encoded = f'pbkdf2_sha256${salt}${password_hash("a-strong-test-password", salt)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded, 'bob': encoded}
        self.redis = StreamRedis()
        self.assistant = EchoAssistant(self.redis)
        self.old = main._developer
        main._developer = self.assistant
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main._developer = self.old
        if self._old_admins is None:
            os.environ.pop('DEVTRACE_ADMIN_USERS', None)
        else:
            os.environ['DEVTRACE_ADMIN_USERS'] = self._old_admins

    def headers(self, user='alice', **extra):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret), **extra}

    def events(self, event=None, actor=None, redis=None):
        rows = [dict(id=eid, **{k.decode(): v.decode('utf-8', 'replace') for k, v in fields.items()})
                for eid, fields in (redis or self.redis).streams.get(audit.STREAM_KEY, [])]
        if event:
            rows = [row for row in rows if row.get('event') == event]
        if actor:
            rows = [row for row in rows if row.get('actor') == actor]
        return rows

    async def test_admin_only_audit_endpoint(self):
        ok = await self.client.get('/developer/audit', headers=self.headers('alice'))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(set(ok.json()), {'events', 'count'})
        self.assertRegex(ok.headers.get('X-Request-ID', ''), r'^[0-9a-f]{32}$')
        self.assertEqual((await self.client.get('/developer/audit', headers=self.headers('bob'))).status_code, 403)
        self.assertEqual((await self.client.get('/developer/audit')).status_code, 401)
        forged = await self.client.get('/developer/audit', headers=self.headers('alice', **{'X-Role': 'admin'}))
        self.assertEqual(forged.status_code, 200)  # headers never grant the role
        self.assertTrue(self.events('audit.read', actor='alice'))
        self.assertFalse(self.events('audit.read', actor='bob'))
        with patch.dict(os.environ, {'DEVTRACE_ADMIN_USERS': ''}):
            self.assertEqual((await self.client.get('/developer/audit', headers=self.headers('alice'))).status_code, 403)

    async def test_login_events_never_contain_the_password(self):
        login = await self.client.post('/auth/login', json={'username': 'alice', 'password': 'a-strong-test-password'})
        self.assertEqual(login.status_code, 200)
        bad = await self.client.post('/auth/login', json={'username': 'alice', 'password': SECRET_MARKER})
        self.assertEqual(bad.status_code, 401)
        self.assertTrue(self.events('auth.login.success', actor='alice'))
        self.assertTrue(self.events('auth.login.failure', actor='alice'))
        self.assertNotIn(SECRET_MARKER, self.redis.dumped())
        self.assertNotIn('sk-', self.redis.dumped())

    async def test_chat_event_has_no_message_body(self):
        response = await self.client.post('/developer/chat', headers=self.headers(),
                                          json={'message': f'为什么 req_auth_01 返回 401 密码是 {SECRET_MARKER}'})
        self.assertEqual(response.status_code, 200)
        chats = self.events('chat.sync')
        self.assertTrue(chats)
        detail = json.loads(chats[0]['detail'])
        self.assertEqual(detail.get('kind'), 'auth')
        self.assertIn('latency_ms', detail)
        self.assertNotIn(SECRET_MARKER, self.redis.dumped())

    async def test_audit_write_failures_never_block_the_main_flow(self):
        class BrokenRedis(StreamRedis):
            async def xadd(self, *_args, **_kwargs):
                raise ConnectionError('redis is down')

        main._developer.redis = BrokenRedis()
        login = await self.client.post('/auth/login', json={'username': 'alice', 'password': 'a-strong-test-password'})
        self.assertEqual(login.status_code, 200)
        chat = await self.client.post('/developer/chat', headers=self.headers(), json={'message': 'hello'})
        self.assertEqual(chat.status_code, 200)

        class NoStreams(StreamRedis):
            xadd = None  # a redis client without stream support must degrade silently

        main._developer.redis = NoStreams()
        chat = await self.client.post('/developer/chat', headers=self.headers(), json={'message': 'hello again'})
        self.assertEqual(chat.status_code, 200)

    async def test_read_applies_filters_decodes_bytes_and_validates_limit(self):
        for actor, event in [('alice', 'chat.sync'), ('bob', 'tool.call'), ('alice', 'tool.call')]:
            await audit.record(self.redis, event, actor=actor, status='success',
                               detail={'tool': 'get_request_trace'})
        admin = self.headers('alice')
        filtered = await self.client.get('/developer/audit?event=tool.call&user=alice', headers=admin)
        self.assertEqual(filtered.status_code, 200)
        rows = filtered.json()['events']
        self.assertEqual([row['event'] for row in rows], ['tool.call'])
        self.assertEqual(rows[0]['actor'], 'alice')
        self.assertIsInstance(rows[0]['detail'], dict)
        self.assertEqual(rows[0]['detail'].get('tool'), 'get_request_trace')
        newest = await self.client.get('/developer/audit', headers=admin)
        ids = [row['id'] for row in newest.json()['events']]
        self.assertEqual(ids, sorted(ids, reverse=True))  # newest first
        for bad in ('limit=0', 'limit=201', 'limit=abc'):
            self.assertEqual((await self.client.get(f'/developer/audit?{bad}', headers=admin)).status_code, 422)

    async def test_detail_whitelist_truncation_and_secret_redaction(self):
        await audit.record(self.redis, 'tool.call', actor='alice', resource='run-1', status='success',
                           detail={'tool': 'get_request_trace', 'success': True, 'latency_ms': 12.34,
                                   'attempts': 2, 'reason_code': 'ok', 'kind': 'auth', 'status': 'ok',
                                   'password': SECRET_MARKER, 'api_key': 'sk-live-abcdefghijklmnop',
                                   'body': 'the full user message must never be stored',
                                   'result': {'anything': 'nested'},
                                   'note': 'Authorization: Bearer abc.def-123 and sk-anothersecret99'})
        row = self.events('tool.call')[-1]
        detail = json.loads(row['detail'])
        self.assertNotIn('password', detail)
        self.assertNotIn('api_key', detail)
        self.assertNotIn('body', detail)
        self.assertNotIn('result', detail)
        self.assertNotIn('note', detail)
        self.assertEqual(detail['success'], True)
        self.assertEqual(detail['attempts'], 2)
        self.assertNotIn('sk-live-abcdefghijklmnop', row['detail'])
        self.assertNotIn(SECRET_MARKER, self.redis.dumped())
        await audit.record(self.redis, 'chat.sync', actor='alice', status='success',
                           detail={'kind': 'x' * 500})
        self.assertEqual(len(json.loads(self.events('chat.sync')[-1]['detail'])['kind']), audit.DETAIL_LIMIT)
        await audit.record(self.redis, 'chat.sync', actor='alice', resource='sk-abcdefghijklmnop1234', status='ok')
        self.assertNotIn('sk-abcdefghijklmnop1234', self.redis.dumped())

    async def test_real_assistant_records_tool_calls_without_results(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        original_db = sandbox.DB_PATH
        sandbox.DB_PATH = Path(temp.name) / 'tickets.sqlite'
        self.addCleanup(setattr, sandbox, 'DB_PATH', original_db)

        class Context:
            recent_messages, relevant_history, summary, user_profile = [], [], '', {}

            def to_prompt_text(self):
                return ''

        class Memory:
            async def get_context(self, *_a, **_k):
                return Context()

            async def add_message(self, *_a, **_k):
                pass

        class Knowledge:
            async def search_async(self, query, top_k=3):
                return [{'title': doc['title'], 'content': doc['content'], 'score': 1.0}
                        for doc in DOCS[:top_k]]

        assistant = DeveloperAssistant(memory=Memory(), knowledge=Knowledge(),
                                       redis_client=self.redis, model=RuleDecisionModel(),
                                       tool_manager=MCPToolManager(api_key='test-key'),
                                       sandbox_url='http://sandbox')
        with patch('developer.assistant.httpx.AsyncClient', SandboxClient):
            result = await assistant.run('为什么 req_auth_01 返回 401？', 'alice', 'conv-audit-1')
        tool_events = self.events('tool.call')
        self.assertTrue(tool_events)
        self.assertEqual(len(tool_events), result['metrics']['tool_calls'])
        self.assertTrue([row for row in tool_events if row['status'] == 'success'])
        for row in tool_events:
            self.assertEqual(set(json.loads(row['detail'])), {'tool', 'success', 'latency_ms'})
            self.assertEqual(row['actor'], 'alice')
            self.assertEqual(row['resource'], result['run_id'])
        self.assertNotIn('error_summary', self.redis.dumped())

    async def test_worker_records_task_finished(self):
        class WorkerAssistant:
            async def run(self, message, user, conversation):
                return {'run_id': 'run-9', 'plan': {'status': 'complete'}}

        await task_queue.enqueue(self.redis, 'task-1', 'alice', '请排查 req_auth_01', 'conv-1')
        await api_worker.process_one(WorkerAssistant(), self.redis, 'task-1')
        finished = self.events('task.finished')
        self.assertTrue(finished)
        self.assertEqual(finished[-1]['status'], 'completed')
        self.assertEqual(finished[-1]['actor'], 'alice')
        detail = json.loads(finished[-1]['detail'])
        self.assertEqual(detail['status'], 'completed')
        self.assertEqual(detail['attempts'], 0)


if __name__ == '__main__':
    unittest.main()
