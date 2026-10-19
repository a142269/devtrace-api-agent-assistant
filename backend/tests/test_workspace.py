"""P2 workspace isolation: config parsing, run/task 404 boundaries, audit tagging."""
import json
import os
import unittest
from unittest.mock import patch

import httpx

from api import main
from api import worker as api_worker
from api.auth import issue_token, parse_user_config, password_hash, settings
from developer import audit
from developer import task_queue

SECRET_MARKER = 'wsp4ss-secret-value'
HASH_A = f'pbkdf2_sha256${"01" * 16}${password_hash("a-strong-test-password", "01" * 16)}'


class WorkspaceRedis:
    """Fake redis storing stream values as bytes the way redis-py does."""

    def __init__(self):
        self.values, self.held, self.sets, self.lists, self.zsets = {}, set(), {}, {}, {}
        self.streams = {}
        self._seq = 0

    async def eval(self, *_args):
        return 1

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
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key, start, stop):
        ids = sorted(self.zsets.get(key, {}), reverse=True)[start:stop + 1]
        return list(ids)

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
        parts = []
        for _entry_id, fields in self.streams.get(audit.STREAM_KEY, []):
            for key, value in fields.items():
                parts.append(key.decode() if isinstance(key, bytes) else key)
                parts.append(value.decode('utf-8', 'replace') if isinstance(value, bytes) else value)
        return ' '.join(parts)


class RunWritingAssistant:
    """Mimics DeveloperAssistant: persists developer:run:{run_id} before returning."""

    def __init__(self, redis):
        self.redis = redis

    async def run(self, message, user, conversation):
        run_id = 'run-1'
        response = {'conversation_id': conversation, 'run_id': run_id, 'kind': 'auth',
                    'answer': message, 'plan': {'status': 'complete', 'goal': 'g', 'steps': []}}
        await self.redis.setex(f'developer:run:{run_id}', 60,
                               json.dumps({'user_id': user, 'response': response}))
        # A tool event raised without an explicit workspace must inherit the contextvar.
        await audit.record(self.redis, 'tool.call', actor=user, status='success',
                           detail={'tool': 'get_request_trace', 'success': True})
        return response


class WorkspaceApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_admins = os.environ.get('DEVTRACE_ADMIN_USERS')
        os.environ['DEVTRACE_ADMIN_USERS'] = 'alice'

    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': HASH_A, 'carol': HASH_A}
        main.app.state.user_workspaces = {'alice': 'ws-a', 'carol': 'ws-b'}
        self.redis = WorkspaceRedis()
        self.old = main._developer
        main._developer = RunWritingAssistant(self.redis)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main._developer = self.old
        if self._old_admins is None:
            os.environ.pop('DEVTRACE_ADMIN_USERS', None)
        else:
            os.environ['DEVTRACE_ADMIN_USERS'] = self._old_admins

    def headers(self, user='alice'):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret)}

    def events(self, event=None):
        rows = [{k.decode(): v.decode('utf-8', 'replace') for k, v in fields.items()}
                for _eid, fields in self.redis.streams.get(audit.STREAM_KEY, [])]
        if event:
            rows = [row for row in rows if row.get('event') == event]
        return rows

    async def test_run_detail_is_only_visible_inside_its_workspace(self):
        chat = await self.client.post('/developer/chat', headers=self.headers('alice'),
                                      json={'message': 'req_auth_01 为什么 401'})
        self.assertEqual(chat.status_code, 200)
        stamped = json.loads(self.redis.values['developer:run:run-1'])
        self.assertEqual(stamped['workspace'], 'ws-a')
        own = await self.client.get('/developer/runs/run-1', headers=self.headers('alice'))
        self.assertEqual(own.status_code, 200)
        cross = await self.client.get('/developer/runs/run-1', headers=self.headers('carol'))
        self.assertEqual(cross.status_code, 404)

    async def test_legacy_and_mismatched_run_records(self):
        # A record without the workspace field (pre-P2) stays readable by its owner.
        await self.redis.setex('developer:run:old-1', 60, json.dumps(
            {'user_id': 'alice', 'response': {'conversation_id': 'c', 'run_id': 'old-1',
                                              'kind': 'auth', 'plan': {'status': 'complete'}}}))
        legacy = await self.client.get('/developer/runs/old-1', headers=self.headers('alice'))
        self.assertEqual(legacy.status_code, 200)
        # If the configuration moved the owner to another workspace, the record is hidden.
        await self.redis.setex('developer:run:mm-1', 60, json.dumps(
            {'user_id': 'alice', 'workspace': 'ws-b',
             'response': {'conversation_id': 'c', 'run_id': 'mm-1',
                          'kind': 'auth', 'plan': {'status': 'complete'}}}))
        mismatch = await self.client.get('/developer/runs/mm-1', headers=self.headers('alice'))
        self.assertEqual(mismatch.status_code, 404)

    async def test_task_endpoint_workspace_boundaries(self):
        await task_queue.enqueue(self.redis, 't-new', 'alice', '排查 req_auth_01', 'c1',
                                 workspace='ws-a')
        own = await self.client.get('/developer/tasks/t-new', headers=self.headers('alice'))
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json()['workspace'], 'ws-a')
        cross = await self.client.get('/developer/tasks/t-new', headers=self.headers('carol'))
        self.assertEqual(cross.status_code, 404)
        # Legacy queue records carry no workspace and stay readable by their owner.
        await self.redis.setex('developer:task:t-old', 60, json.dumps(
            {'task_id': 't-old', 'user_id': 'alice', 'status': 'completed'}))
        legacy = await self.client.get('/developer/tasks/t-old', headers=self.headers('alice'))
        self.assertEqual(legacy.status_code, 200)

    async def test_enqueue_defaults_to_workspace_default(self):
        plain = await task_queue.enqueue(self.redis, 't-plain', 'alice', '你好', 'c9')
        self.assertEqual(plain['workspace'], 'default')
        tagged = await task_queue.enqueue(self.redis, 't-tagged', 'alice', '你好', 'c9',
                                          workspace='ws-a')
        self.assertEqual(tagged['workspace'], 'ws-a')
        blank = await task_queue.enqueue(self.redis, 't-blank', 'alice', '你好', 'c9', workspace='')
        self.assertEqual(blank['workspace'], 'default')

    async def test_worker_tags_finished_task_and_run_with_workspace(self):
        class Done:
            async def run(self, message, user, conversation):
                await self_redis.setex('developer:run:run-9', 60, json.dumps(
                    {'user_id': user, 'response': {'run_id': 'run-9'}}))
                return {'run_id': 'run-9', 'plan': {'status': 'complete'}}

        self_redis = self.redis
        await task_queue.enqueue(self.redis, 't-w', 'alice', '排查', 'c1', workspace='ws-a')
        await api_worker.process_one(Done(), self.redis, 't-w')
        finished = self.events('task.finished')[-1]
        self.assertEqual(finished['workspace'], 'ws-a')
        self.assertEqual(finished['status'], 'completed')
        stamped = json.loads(self.redis.values['developer:run:run-9'])
        self.assertEqual(stamped['workspace'], 'ws-a')

    async def test_audit_events_carry_workspace_and_admin_can_filter(self):
        await self.client.post('/auth/login', json={'username': 'carol',
                                                    'password': 'a-strong-test-password'})
        chat = await self.client.post('/developer/chat', headers=self.headers('alice'),
                                      json={'message': 'req_quota_01 429'})
        self.assertEqual(chat.status_code, 200)
        logins = self.events('auth.login.success')
        self.assertEqual(logins[-1]['workspace'], 'ws-b')
        # tool.call was raised without an explicit workspace inside the run:
        self.assertTrue(all(row['workspace'] == 'ws-a' for row in self.events('tool.call')))
        filtered = await self.client.get('/developer/audit?workspace=ws-a',
                                         headers=self.headers('alice'))
        self.assertEqual(filtered.status_code, 200)
        rows = filtered.json()['events']
        self.assertTrue(rows)
        self.assertTrue({row['workspace'] for row in rows} == {'ws-a'})
        self.assertNotIn('ws-b', {row['workspace'] for row in rows})
        forbidden = await self.client.get('/developer/audit', headers=self.headers('carol'))
        self.assertEqual(forbidden.status_code, 403)
        bad = await self.client.get('/developer/audit?workspace=not%20valid!',
                                    headers=self.headers('alice'))
        self.assertEqual(bad.status_code, 422)

    async def test_legacy_audit_events_read_as_default_workspace(self):
        await audit.record(self.redis, 'chat.sync', actor='alice', status='success')  # no ws kwarg
        rows = await audit.read(self.redis, 10)
        self.assertEqual(rows[0]['workspace'], 'default')
        only_default = await audit.read(self.redis, 10, workspace='default')
        self.assertEqual(len(only_default), 1)
        only_ws = await audit.read(self.redis, 10, workspace='ws-a')
        self.assertEqual(only_ws, [])

    async def test_memory_results_carry_workspace(self):
        class Memory:
            async def export_memory(self, user, conversation):
                return {'messages': 2}

            async def forget_conversation(self, user, conversation):
                return {'forgotten': True}

        main._developer.memory = Memory()
        exported = await self.client.get('/developer/memory/c1', headers=self.headers('alice'))
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json()['workspace'], 'ws-a')
        deleted = await self.client.delete('/developer/memory/c1', headers=self.headers('alice'))
        self.assertEqual(deleted.json()['workspace'], 'ws-a')
        self.assertTrue(all(row['workspace'] == 'ws-a' for row in self.events('memory.export')))
        self.assertTrue(all(row['workspace'] == 'ws-a' for row in self.events('memory.delete')))

    async def test_login_audit_never_contains_the_password(self):
        bad = await self.client.post('/auth/login', json={'username': 'carol',
                                                          'password': SECRET_MARKER})
        self.assertEqual(bad.status_code, 401)
        failure = self.events('auth.login.failure')[-1]
        self.assertEqual(failure['workspace'], 'ws-b')
        ghost = await self.client.post('/auth/login', json={'username': 'ghost',
                                                            'password': SECRET_MARKER})
        self.assertEqual(ghost.status_code, 401)
        self.assertEqual(self.events('auth.login.failure')[-1]['workspace'], 'default')
        self.assertNotIn(SECRET_MARKER, self.redis.dumped())


class WorkspaceConfigTests(unittest.TestCase):
    def test_legacy_format_resolves_to_default_workspace(self):
        users, workspaces = parse_user_config({'alice': HASH_A})
        self.assertEqual(users, {'alice': HASH_A})
        self.assertEqual(workspaces, {'alice': 'default'})

    def test_new_format_and_mixed_configuration(self):
        users, workspaces = parse_user_config({
            'alice': {'password': HASH_A, 'workspace': 'acme'},
            'bob': {'password': HASH_A},
            'carol': HASH_A,
        })
        self.assertEqual(users, {'alice': HASH_A, 'bob': HASH_A, 'carol': HASH_A})
        self.assertEqual(workspaces, {'alice': 'acme', 'bob': 'default', 'carol': 'default'})

    def test_invalid_workspace_configuration_fails_at_startup(self):
        for bad in (
            {'alice': {'password': HASH_A, 'workspace': 'bad name!'}},
            {'alice': {'password': HASH_A, 'workspace': 'x' * 41}},
            {'alice': {'password': HASH_A, 'workspace': 123}},
            {'alice': {'workspace': 'acme'}},                      # missing password
            {'alice': {'password': HASH_A, 'role': 'admin'}},      # unknown field
            {'alice': 123},
            {'': HASH_A},
        ):
            with self.assertRaises(RuntimeError):
                parse_user_config(bad)

    def test_settings_returns_workspaces(self):
        raw = json.dumps({'alice': {'password': HASH_A, 'workspace': 'acme'}, 'bob': HASH_A})
        with patch.dict(os.environ, {'DEVTRACE_JWT_SECRET': 'x' * 40, 'DEVTRACE_USERS_JSON': raw}):
            secret, users, workspaces = settings()
        self.assertEqual(len(secret), 40)
        self.assertEqual(users, {'alice': HASH_A, 'bob': HASH_A})
        self.assertEqual(workspaces, {'alice': 'acme', 'bob': 'default'})


if __name__ == '__main__':
    unittest.main()
