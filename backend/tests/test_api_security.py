import asyncio
import json
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
        self.values, self.held = {}, set()

    async def eval(self, *_args):
        return 1

    def lock(self, key, **_kwargs):
        return Lock(self, key)

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value


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
        self.assertIn('event: started', await anext(iterator))
        self.assertIn('event: timeline', await anext(iterator))
        self.assertTrue(self.assistant.redis.held)
        await iterator.aclose()
        self.assertFalse(self.assistant.redis.held)
