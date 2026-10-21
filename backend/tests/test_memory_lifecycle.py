"""P5 memory lifecycle: configurable TTL, profile forgetting, export metadata, live rubric.

Everything here runs against in-memory doubles and the deterministic rules model, so CI
never needs Redis, ChromaDB or a paid model call.
"""
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import chromadb
import httpx
from api import main
from api.auth import issue_token, password_hash
from developer import audit
from developer.evaluate_memory_quality_live import (
    CASES, build_report, fabricated_identifiers, run, score_case, turns,
)
from memory import conversation_memory
from memory.conversation_memory import (
    DEFAULT_MEMORY_TTL_SECONDS, MIN_MEMORY_TTL_SECONDS, MsgRole, MemoryManager,
    parse_memory_ttl_seconds,
)


class RecordingRedis:
    """In-memory Redis double that records every TTL it is handed."""

    def __init__(self):
        self.values = {}
        self.lists = {}
        self.streams = {}
        self.expire_calls = []
        self.setex_calls = []
        self._seq = 0

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl))
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

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))
        return True

    async def xadd(self, key, fields, maxlen=None, approximate=False, id='*'):
        entry_id = f'{self._seq:013d}-0'
        self._seq += 1
        self.streams.setdefault(key, []).append(
            (entry_id, {k: str(v) for k, v in fields.items()}))
        return entry_id

    async def xrevrange(self, key, min='+', max='-', count=None):
        items = list(reversed(self.streams.get(key, [])))
        return items[:count] if count else items

    def every_ttl(self):
        return [ttl for _key, ttl in self.expire_calls + self.setex_calls]

    def audit_blob(self):
        parts = []
        for _entry_id, fields in self.streams.get(audit.STREAM_KEY, []):
            parts.extend(fields.values())
        return ' '.join(parts)


class ProfileCollection:
    """Chroma collection double that supports the get(ids)/get(where)/delete(ids) trio."""

    def __init__(self):
        self.docs = {}  # id -> (document, metadata)

    def add(self, ids=None, documents=None, metadatas=None):
        for index, doc_id in enumerate(ids or []):
            self.docs[doc_id] = (documents[index], (metadatas or [{}])[index])

    def get(self, ids=None, where=None):
        if ids is not None:
            picked = [doc_id for doc_id in ids if doc_id in self.docs]
        else:
            picked = [doc_id for doc_id, (_d, meta) in self.docs.items()
                      if meta.get('user_id') == (where or {}).get('user_id')]
        return {'ids': picked,
                'documents': [self.docs[doc_id][0] for doc_id in picked],
                'metadatas': [self.docs[doc_id][1] for doc_id in picked]}

    def delete(self, ids=None, where=None):
        for doc_id in ids or []:
            self.docs.pop(doc_id, None)
        if where:
            for doc_id, (_d, meta) in list(self.docs.items()):
                if meta.get('user_id') == where.get('user_id'):
                    self.docs.pop(doc_id, None)

    def query(self, **_kwargs):
        return {'documents': [[]]}


class EpisodicCollection(ProfileCollection):
    def get(self, ids=None, where=None):
        return {'ids': [], 'documents': [], 'metadatas': []}


class StubSummarizer:
    class Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text='摘要：req_auth_01 凭证过期，密钥前缀 sk_live_demo。')])

    def __init__(self):
        self.messages = self.Messages()


def build_memory(ttl=DEFAULT_MEMORY_TTL_SECONDS, profile=None):
    """A MemoryManager assembled from doubles, mirroring the existing test style."""
    memory = object.__new__(MemoryManager)
    memory._redis = RecordingRedis()
    memory._episodic = EpisodicCollection()
    memory._profile = profile if profile is not None else ProfileCollection()
    memory._client = StubSummarizer()
    memory._model = 'stub-summary'
    memory._namespace = 'developer'
    memory._profile_prefix = 'developer:user_profile:'
    memory.memory_ttl_seconds = ttl
    return memory


class MemoryTtlParsingTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('DEVTRACE_MEMORY_TTL_SECONDS', None)

    def test_unset_or_empty_env_uses_24h_default(self):
        os.environ.pop('DEVTRACE_MEMORY_TTL_SECONDS', None)
        self.assertEqual(parse_memory_ttl_seconds(), DEFAULT_MEMORY_TTL_SECONDS)
        os.environ['DEVTRACE_MEMORY_TTL_SECONDS'] = ''
        self.assertEqual(parse_memory_ttl_seconds(), DEFAULT_MEMORY_TTL_SECONDS)

    def test_env_value_is_used_and_clamped_to_the_minimum(self):
        os.environ['DEVTRACE_MEMORY_TTL_SECONDS'] = '7200'
        self.assertEqual(parse_memory_ttl_seconds(), 7200)
        os.environ['DEVTRACE_MEMORY_TTL_SECONDS'] = '60'
        self.assertEqual(parse_memory_ttl_seconds(), MIN_MEMORY_TTL_SECONDS)

    def test_invalid_env_value_falls_back_to_the_default(self):
        for raw in ('not-a-number', '3600.5', '  ', '86400s'):
            os.environ['DEVTRACE_MEMORY_TTL_SECONDS'] = raw
            self.assertEqual(parse_memory_ttl_seconds(), DEFAULT_MEMORY_TTL_SECONDS, raw)


class MemoryTtlBehaviourTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_reads_ttl_from_env_and_constructor(self):
        with mock.patch.object(conversation_memory.redis, 'from_url', return_value=RecordingRedis()), \
                mock.patch.object(conversation_memory, 'AsyncAnthropic'), \
                mock.patch.object(chromadb, 'HttpClient') as http_client:
            http_client.return_value.get_or_create_collection.return_value = ProfileCollection()
            with mock.patch.dict(os.environ, {'DEVTRACE_MEMORY_TTL_SECONDS': '10800'}):
                memory = MemoryManager(api_key='x')
                self.assertEqual(memory.memory_ttl_seconds, 10800)
                # An explicit constructor value wins over the environment.
                explicit = MemoryManager(api_key='x', ttl_seconds=7200)
                self.assertEqual(explicit.memory_ttl_seconds, 7200)

    async def test_working_memory_and_summary_use_the_configured_ttl(self):
        memory = build_memory(ttl=7200)
        for index in range(memory.COMPRESS_AT):
            await memory.add_message('alice', 'c1', MsgRole.USER, f'第 {index} 条 请求 req_auth_01')
        self.assertTrue(memory._redis.setex_calls, 'compression should write a summary')
        self.assertEqual(set(memory._redis.every_ttl()), {7200})
        wm_keys = {key for key, _ttl in memory._redis.expire_calls}
        self.assertIn(memory._wm_key('alice', 'c1'), wm_keys)
        summary_keys = {key for key, _ttl in memory._redis.setex_calls}
        self.assertIn(memory._summary_key('alice', 'c1'), summary_keys)

    async def test_default_ttl_is_24_hours(self):
        memory = build_memory()
        await memory.add_message('alice', 'c1', MsgRole.USER, 'hello')
        self.assertEqual(memory._redis.expire_calls,
                         [(memory._wm_key('alice', 'c1'), 86400)])


class ProfileForgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_forget_profile_removes_current_and_legacy_documents(self):
        profile = ProfileCollection()
        memory = build_memory(profile=profile)
        doc_id = memory._profile_doc_id('alice')
        profile.add(ids=[doc_id, 'developer:user_profile:legacy_alice'],
                    documents=['{"preferences": ["中文回答"]}', '{"preferences": ["旧格式"]}'],
                    metadatas=[{'user_id': 'alice'}, {'user_id': 'alice'}])
        result = await memory.forget_profile('alice')
        self.assertTrue(result['profile_deleted'])
        self.assertEqual(result['deleted_documents'], 2)
        self.assertEqual(profile.docs, {})
        self.assertEqual(await memory._get_profile('alice'), {})

    async def test_forget_profile_keeps_conversation_and_episodic_memory(self):
        profile = ProfileCollection()
        memory = build_memory(profile=profile)
        profile.add(ids=[memory._profile_doc_id('alice')], documents=['{"preferences": ["中文"]}'],
                    metadatas=[{'user_id': 'alice'}])
        await memory.add_message('alice', 'c1', MsgRole.USER, '请求 req_auth_01 返回 401')
        memory._episodic.add(ids=['ep-1'], documents=['旧会话摘要'],
                             metadatas=[{'user_id': 'alice', 'conv_id': 'c1'}])
        result = await memory.forget_profile('alice')
        self.assertTrue(result['conversation_memory_kept'])
        self.assertTrue(result['episodic_kept'])
        exported = await memory.export_memory('alice', 'c1')
        self.assertEqual(exported['counts']['messages'], 1)
        self.assertFalse(exported['counts']['has_profile'])
        self.assertIn('ep-1', memory._episodic.docs)

    async def test_forget_profile_only_touches_the_calling_user(self):
        profile = ProfileCollection()
        memory = build_memory(profile=profile)
        profile.add(ids=[memory._profile_doc_id('alice'), memory._profile_doc_id('bob')],
                    documents=['{"preferences": ["alice-only"]}', '{"preferences": ["bob-only"]}'],
                    metadatas=[{'user_id': 'alice'}, {'user_id': 'bob'}])
        await memory.forget_profile('alice')
        self.assertEqual(await memory._get_profile('alice'), {})
        self.assertEqual(await memory._get_profile('bob'), {'preferences': ['bob-only']})

    async def test_forget_profile_without_a_profile_reports_nothing_deleted(self):
        memory = build_memory()
        result = await memory.forget_profile('nobody')
        self.assertFalse(result['profile_deleted'])
        self.assertEqual(result['deleted_documents'], 0)
        self.assertEqual(result['error'], '')


class ExportMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_adds_timestamp_and_workspace(self):
        memory = build_memory()
        await memory.add_message('alice', 'c1', MsgRole.USER, '请求 req_auth_01 返回 401')
        exported = await memory.export_memory('alice', 'c1', 'ws-a')
        self.assertEqual(exported['workspace'], 'ws-a')
        self.assertEqual(datetime.fromisoformat(exported['exported_at']).year,
                         datetime.now().year)
        self.assertEqual(exported['messages'][0]['content'], '请求 req_auth_01 返回 401')
        self.assertIn('ts', exported['messages'][0])

    async def test_export_defaults_to_the_default_workspace(self):
        memory = build_memory()
        exported = await memory.export_memory('alice', 'c1')
        self.assertEqual(exported['workspace'], 'default')

    async def test_export_keeps_legacy_fields_and_hides_the_profile_doc_id(self):
        profile = ProfileCollection()
        memory = build_memory(profile=profile)
        doc_id = memory._profile_doc_id('alice')
        profile.add(ids=[doc_id], documents=['{"preferences": ["中文回答"]}'],
                    metadatas=[{'user_id': 'alice'}])
        exported = await memory.export_memory('alice', 'c1')
        for key in ('conversation_id', 'messages', 'summary', 'user_profile', 'counts'):
            self.assertIn(key, exported)
        self.assertEqual(exported['user_profile'], {'preferences': ['中文回答']})
        self.assertNotIn(doc_id, json.dumps(exported, ensure_ascii=False))


class ProfileEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.secret = 'test-secret-with-at-least-32-bytes-long'
        salt = '01' * 16
        encoded = f'pbkdf2_sha256${salt}${password_hash("a-strong-test-password", salt)}'
        main.app.state.jwt_secret = self.secret
        main.app.state.users = {'alice': encoded, 'bob': encoded}
        main.app.state.user_workspaces = {'alice': 'ws-a'}
        self.memory = build_memory()
        self.memory._profile.add(
            ids=[self.memory._profile_doc_id('alice'), self.memory._profile_doc_id('bob')],
            documents=['{"preferences": ["alice-secret-preference"]}',
                       '{"preferences": ["bob-secret-preference"]}'],
            metadatas=[{'user_id': 'alice'}, {'user_id': 'bob'}])
        await self.memory.add_message('alice', 'c1', MsgRole.USER, 'alice-secret-message-body')

        class Assistant:
            def __init__(self, memory):
                self.memory = memory
                self.redis = memory._redis

        self.old = main._developer
        main._developer = Assistant(self.memory)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                        base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main._developer = self.old
        main.app.state.user_workspaces = {}

    def headers(self, user='alice'):
        return {'Authorization': 'Bearer ' + issue_token(user, self.secret)}

    async def test_profile_delete_requires_auth(self):
        self.assertEqual((await self.client.delete('/developer/profile')).status_code, 401)

    async def test_profile_delete_returns_workspace_and_audits_the_event(self):
        response = await self.client.delete('/developer/profile', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['profile_deleted'])
        self.assertEqual(body['workspace'], 'ws-a')
        self.assertTrue(body['conversation_memory_kept'])
        blob = self.memory._redis.audit_blob()
        self.assertIn('memory.profile_delete', blob)
        self.assertIn('ws-a', blob)

    async def test_profile_delete_never_leaks_content_or_credentials(self):
        response = await self.client.delete('/developer/profile',
                                           headers=self.headers('bob'))
        blob = response.text + self.memory._redis.audit_blob()
        for secret_text in ('alice-secret-preference', 'bob-secret-preference',
                            'alice-secret-message-body', 'a-strong-test-password',
                            'developer:user_profile'):
            self.assertNotIn(secret_text, blob)

    async def test_profile_delete_keeps_the_other_users_profile_and_conversation(self):
        await self.client.delete('/developer/profile', headers=self.headers())
        self.assertEqual(await self.memory._get_profile('bob'),
                         {'preferences': ['bob-secret-preference']})
        exported = await self.memory.export_memory('alice', 'c1')
        self.assertEqual(exported['counts']['messages'], 1)
        self.assertFalse(exported['counts']['has_profile'])


class LiveQualityRubricTests(unittest.IsolatedAsyncioTestCase):
    """One session is scored twice: strict is verbatim, semantic is per fact category."""

    @staticmethod
    def verbatim(case):
        return (f'{case.request_id} 使用 {case.key_prefix}（{case.scope}），'
                f'结论是{case.conclusion}。')

    @staticmethod
    def paraphrase(case):
        """Satisfies the category rubric with synonyms, yet never quotes the strings."""
        resource, _, action = case.scope.partition(':')
        parts = [case.request_id, case.key_prefix, resource,
                 '只读' if action == 'read' else '写入']
        parts += [group[0] for group in case.concept_groups]
        parts += list(case.required_tokens)
        return '，'.join(parts) + '。'

    def test_verbatim_summary_passes_both_rubrics(self):
        for case in CASES:
            scored = score_case(case, self.verbatim(case), 900)
            with self.subTest(case=case.name):
                for rubric in ('strict', 'semantic'):
                    self.assertTrue(scored[rubric]['perfect'], scored[rubric]['missing_facts'])
                    self.assertEqual(scored[rubric]['missing_facts'], [])
                self.assertEqual(scored['fabricated_identifiers'], [])
                self.assertTrue(scored['within_summary_limit'])

    def test_semantic_equivalent_summary_passes_while_strict_fails(self):
        for case in CASES:
            scored = score_case(case, self.paraphrase(case), 900)
            with self.subTest(case=case.name):
                self.assertTrue(scored['semantic']['perfect'], scored['semantic']['missing_facts'])
                self.assertFalse(scored['strict']['perfect'])
                self.assertEqual(scored['strict']['facts_retained'], 1)
                self.assertEqual(scored['strict']['missing_facts'],
                                 [case.key_prefix, case.conclusion])

    def test_negated_conclusion_is_never_a_retained_fact(self):
        case = CASES[0]
        summary = (f'{case.request_id} 和 {case.key_prefix} 都没问题，{case.scope} 也在，'
                   '凭证未过期，也不需轮换。')
        scored = score_case(case, summary, 900)
        self.assertFalse(scored['semantic']['conclusion'])
        self.assertFalse(scored['strict']['conclusion'])
        self.assertTrue(scored['semantic']['identifier'])
        self.assertTrue(scored['semantic']['key_permissions'])
        self.assertEqual(scored['semantic']['missing_facts'], [case.conclusion])
        self.assertFalse(scored['semantic']['perfect'])

    def test_negation_still_allows_an_affirmative_restatement(self):
        case = CASES[0]
        summary = (f'{case.request_id} / {case.key_prefix} / {case.scope}：'
                   '一开始以为凭证未过期，核对状态后确认已失效，需要轮换。')
        self.assertTrue(score_case(case, summary, 900)['semantic']['conclusion'])

    def test_scope_synonym_counts_but_the_wrong_action_does_not(self):
        case = CASES[0]
        read_only = f'{case.request_id} {case.key_prefix} logs 只读 {case.conclusion}'
        self.assertTrue(score_case(case, read_only, 900)['semantic']['key_permissions'])
        self.assertFalse(score_case(case, read_only, 900)['strict']['key_permissions'])
        write = f'{case.request_id} {case.key_prefix} logs 写入权限 {case.conclusion}'
        self.assertFalse(score_case(case, write, 900)['semantic']['key_permissions'])

    def test_identifier_must_stay_verbatim(self):
        case = CASES[0]
        loose = f'请求 101 的 {case.key_prefix}（{case.scope}）{case.conclusion}'
        scored = score_case(case, loose, 900)
        self.assertFalse(scored['semantic']['identifier'])
        self.assertFalse(scored['strict']['identifier'])
        self.assertEqual(scored['semantic']['missing_facts'], [case.request_id])

    def test_invented_identifiers_and_thin_summaries_are_flagged(self):
        case = CASES[2]
        thin = '这个问题可能是配置引起的，建议检查设置。'
        scored = score_case(case, thin + ' req_fabricated_999 sk_fabricated_zz', 50)
        self.assertEqual(scored['strict']['facts_retained'], 0)
        self.assertEqual(scored['semantic']['facts_retained'], 0)
        self.assertEqual(len(scored['semantic']['missing_facts']), len(case.facts))
        self.assertEqual(scored['fabricated_identifiers'], ['req_fabricated_999', 'sk_fabricated_zz'])
        self.assertIn('sk_fabricated_zz', fabricated_identifiers(
            scored['summary'], case.facts))

    def test_overlong_summary_breaks_the_char_limit(self):
        case = CASES[1]
        scored = score_case(case, case.request_id + case.key_prefix + case.conclusion
                            + '长' * (MemoryManager.SUMMARY_MAX_CHARS + 10), 10)
        self.assertFalse(scored['within_summary_limit'])

    def test_report_gates_on_semantic_and_still_reports_strict(self):
        good = [score_case(case, self.verbatim(case), 90) for case in CASES]
        report = build_report(good)
        self.assertTrue(report['passed'])
        self.assertEqual(report['strict']['perfect_sessions'], 10)
        self.assertEqual(report['semantic']['perfect_sessions'], 10)
        self.assertEqual(report['strict']['fact_retention_rate'], 1.0)

        loosened = [score_case(case, self.verbatim(case) if index % 4 else self.paraphrase(case), 90)
                    for index, case in enumerate(CASES)]
        mixed = build_report(loosened)
        self.assertTrue(mixed['passed'])
        self.assertEqual(mixed['semantic']['perfect_sessions'], 10)
        self.assertEqual(mixed['strict']['perfect_sessions'], 7)
        self.assertEqual(mixed['strict']['by_category']['conclusion'], 7)
        self.assertEqual(mixed['thresholds']['rubric'], 'semantic')

        lost = list(good)
        for index in (0, 2, 4):
            lost[index] = score_case(CASES[index], '结论不明，需要更多信息。', 90)
        self.assertFalse(build_report(lost)['passed'])

        invented = list(good)
        invented[1] = score_case(CASES[1], CASES[1].request_id + ' req_hallucinated_7 '
                                 + CASES[1].key_prefix + ' ' + CASES[1].conclusion, 90)
        self.assertFalse(build_report(invented)['passed'])

    async def test_run_without_a_key_skips_instead_of_failing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            report = await run('')
        self.assertTrue(report['skipped'])
        self.assertTrue(report['passed'])
        self.assertEqual(report['cases'], 0)
        self.assertIn('未配置', report['reason'])

    def test_conversation_seed_is_long_enough_and_plants_three_facts(self):
        for case in CASES:
            messages = turns(case)
            self.assertGreaterEqual(len(messages), 20)
            self.assertEqual(len(case.facts), 3)
            joined = ' '.join(content for _role, content in messages)
            for fact in case.facts:
                self.assertIn(fact, joined)


if __name__ == '__main__':
    unittest.main()
