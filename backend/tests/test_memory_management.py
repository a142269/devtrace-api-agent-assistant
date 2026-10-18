"""Tests for memory export/forget and the memory management endpoints."""
import json
import unittest

import httpx
from api import main
from api.auth import issue_token, password_hash
from memory.conversation_memory import MemoryManager, MsgRole


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def delete(self, key):
        had = key in self.values or key in self.lists
        self.values.pop(key, None)
        self.lists.pop(key, None)
        return int(had)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def lrange(self, key, start, end):
        rows = self.lists.get(key, [])
        return rows[start:] if end == -1 else rows[start:end + 1]

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def expire(self, *_args):
        return True


class FakeCollection:
    def __init__(self, profile=None):
        self.profile = profile or {}
        self.deleted_where = None

    def get(self, ids=None, where=None):
        if ids:
            return {"documents": [json.dumps(self.profile)] if self.profile else []}
        return {"documents": []}

    def delete(self, where=None):
        self.deleted_where = where

    def add(self, **_kwargs):
        return None

    def query(self, **_kwargs):
        return {"documents": [[]]}


def build_memory(profile=None):
    memory = object.__new__(MemoryManager)
    memory._redis = FakeRedis()
    memory._episodic = FakeCollection()
    memory._profile = FakeCollection(profile)
    memory._profile_prefix = 'user_profile:'
    memory._namespace = ''
    return memory


class MemoryManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_returns_messages_summary_and_profile(self):
        memory = build_memory(profile={"preferences": ["中文回答"]})
        await memory.add_message('alice', 'c1', MsgRole.USER, '请求 req_auth_01 返回 401')
        await memory.add_message('alice', 'c1', MsgRole.ASSISTANT, '结论是凭证过期')
        memory._redis.values[memory._summary_key('alice', 'c1')] = '摘要：req_auth_01 凭证过期'
        exported = await memory.export_memory('alice', 'c1')
        self.assertEqual(exported['counts']['messages'], 2)
        self.assertIn('req_auth_01', exported['summary'])
        self.assertEqual(exported['user_profile']['preferences'], ['中文回答'])
        self.assertIn('凭证过期', exported['messages'][1]['content'])

    async def test_forget_clears_conversation_but_keeps_profile(self):
        memory = build_memory(profile={"preferences": ["中文回答"]})
        await memory.add_message('alice', 'c1', MsgRole.USER, 'hello')
        memory._redis.values[memory._summary_key('alice', 'c1')] = 'summary'
        result = await memory.forget_conversation('alice', 'c1')
        self.assertTrue(result['working_memory_deleted'])
        self.assertTrue(result['summary_deleted'])
        self.assertTrue(result['episodic_deleted'])
        self.assertTrue(result['profile_kept'])
        self.assertEqual(memory._episodic.deleted_where,
                         {'$and': [{'user_id': 'alice'}, {'conv_id': 'c1'}]})
        exported = await memory.export_memory('alice', 'c1')
        self.assertEqual(exported['counts']['messages'], 0)
        self.assertEqual(exported['summary'], '')

    async def test_memory_is_isolated_per_user(self):
        memory = build_memory()
        await memory.add_message('alice', 'c1', MsgRole.USER, 'alice secret')
        exported = await memory.export_memory('bob', 'c1')
        self.assertEqual(exported['counts']['messages'], 0)


class MemoryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        salt = '01' * 16
        encoded = f'pbkdf2_sha256${salt}${password_hash("a-strong-test-password", salt)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded}

        class Memory:
            async def export_memory(self, user_id, conversation_id):
                return {'conversation_id': conversation_id, 'user_id': user_id}

            async def forget_conversation(self, user_id, conversation_id):
                return {'conversation_id': conversation_id, 'user_id': user_id,
                        'working_memory_deleted': True, 'summary_deleted': True,
                        'episodic_deleted': True, 'profile_kept': True}

        class Assistant:
            def __init__(self):
                self.memory = Memory()
                self.redis = FakeRedis()

        self.old = main._developer
        main._developer = Assistant()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                       base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main._developer = self.old

    def headers(self):
        return {'Authorization': 'Bearer ' + issue_token('alice', self.secret)}

    async def test_export_endpoint_uses_token_user(self):
        response = await self.client.get('/developer/memory/c1', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user_id'], 'alice')

    async def test_delete_endpoint_clears_plan_checkpoint(self):
        import hashlib
        plan_key = 'developer:plan:' + hashlib.sha256(b'alice\x00c1').hexdigest()
        main._developer.redis.values[plan_key] = 'plan'
        response = await self.client.delete('/developer/memory/c1', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['plan_deleted'])
        self.assertEqual(response.json()['user_id'], 'alice')

    async def test_memory_endpoints_require_auth(self):
        self.assertEqual((await self.client.get('/developer/memory/c1')).status_code, 401)
        self.assertEqual((await self.client.delete('/developer/memory/c1')).status_code, 401)


if __name__ == '__main__':
    unittest.main()
