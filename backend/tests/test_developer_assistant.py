import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from developer import sandbox
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant, RuleDecisionModel, _json_text, model_metrics
from developer.data import DOCS, EVAL_CASES
from mcp.tool_manager import MCPToolManager
from memory.conversation_memory import MemoryManager


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sorted_sets = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def zadd(self, key, mapping):
        self.sorted_sets.setdefault(key, {}).update(mapping)

    async def zremrangebyrank(self, key, start, end):
        entries = sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: item[1])
        for member, _ in entries[start:end + 1 if end != -1 else None]:
            del self.sorted_sets[key][member]

    async def expire(self, key, ttl):
        return True

    async def zrevrange(self, key, start, end):
        return [member for member, _ in sorted(self.sorted_sets.get(key, {}).items(),
                                               key=lambda item: item[1], reverse=True)][start:end + 1]


class FakeContext:
    def __init__(self, messages):
        self.recent_messages = messages
        self.relevant_history = []
        self.summary = ""
        self.user_profile = {}

    def to_prompt_text(self):
        return "\n".join(message for _, message in self.recent_messages)


class FakeMemory:
    def __init__(self):
        self.messages = {}

    async def get_context(self, user_id, conversation_id, query=""):
        return FakeContext(self.messages.get((user_id, conversation_id), []))

    async def add_message(self, user_id, conversation_id, role, content):
        self.messages.setdefault((user_id, conversation_id), []).append((role.value, content))

    async def update_profile(self, user_id, conversation_id):
        return None


class FakeKnowledge:
    async def search_async(self, query, top_k=3):
        version = 'v2' if query.startswith('v2 ') else 'v1'
        return [{"title": doc["title"], "content": doc["content"], "score": 1.0}
                for doc in [item for item in DOCS if item['title'].startswith(version + ' /')][:top_k]]


class SandboxClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.pop("timeout", None)
        super().__init__(transport=httpx.ASGITransport(app=sandbox.app), base_url="http://sandbox", **kwargs)


class DeveloperAssistantTests(unittest.IsolatedAsyncioTestCase):
    def test_model_json_ignores_empty_and_separate_thinking_blocks(self):
        blocks = [
            SimpleNamespace(text='thinking {not valid json}'),
            SimpleNamespace(text=None),
            SimpleNamespace(text='```json\n{"kind":"auth"}\n```'),
        ]
        self.assertEqual(_json_text(blocks), {"kind": "auth"})

    async def test_model_usage_tracks_reported_tokens_and_errors(self):
        class Messages:
            async def create(self, **kwargs):
                return SimpleNamespace(content=[SimpleNamespace(text='{"ok":true}')],
                                       usage=SimpleNamespace(input_tokens=7, output_tokens=3))
        model = object.__new__(AnthropicDecisionModel)
        model.client = SimpleNamespace(messages=Messages())
        model.model, model.skill = 'fake', ''
        metrics = {'calls': 0, 'errors': 0, 'latency_ms': 0.0, 'input_tokens': None, 'output_tokens': None}
        token = model_metrics.set(metrics)
        try:
            self.assertEqual(await model._ask('test'), {'ok': True})
            self.assertEqual((metrics['calls'], metrics['input_tokens'], metrics['output_tokens']), (1, 7, 3))
        finally:
            model_metrics.reset(token)

    async def test_native_tool_choice_selects_a_ready_step(self):
        class Messages:
            async def create(self, **kwargs):
                self.payload = kwargs
                return SimpleNamespace(
                    content=[SimpleNamespace(type='tool_use', name='search_api_docs',
                                             input={'query': 'v1 auth', 'top_k': 3})],
                    usage=SimpleNamespace(input_tokens=11, output_tokens=4))
        model = object.__new__(AnthropicDecisionModel)
        messages = Messages()
        model.client = SimpleNamespace(messages=messages)
        model.model, model.skill, model.native_tools_disabled = 'fake', '', False
        state = {'kind': 'auth', 'steps': [{'id': 's1', 'tool': 'search_api_docs', 'status': 'pending'}],
                 'ready_steps': [{'id': 's1', 'tool': 'search_api_docs'}]}
        decision = await model.choose_action(state, 'req_auth_01 返回 401')
        self.assertEqual(decision['action'], 'tool')
        self.assertEqual(decision['tool'], 'search_api_docs')
        self.assertEqual(decision['step_id'], 's1')
        self.assertEqual(decision['args']['query'], 'v1 auth')
        self.assertEqual(messages.payload['tools'][0]['name'], 'search_api_docs')
        self.assertEqual(messages.payload['tool_choice'], {'type': 'auto'})

    async def test_native_tool_choice_missing_args_falls_back_to_rules(self):
        class Messages:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    content=[SimpleNamespace(type='tool_use', name='search_api_docs', input={})],
                    usage=None)
        model = object.__new__(AnthropicDecisionModel)
        model.client = SimpleNamespace(messages=Messages())
        model.model, model.skill, model.native_tools_disabled = 'fake', '', False
        state = {'kind': 'auth', 'steps': [{'id': 's1', 'tool': 'search_api_docs', 'status': 'pending'}],
                 'ready_steps': [{'id': 's1', 'tool': 'search_api_docs'}]}
        decision = await model.choose_action(state, 'req_auth_01 返回 401')
        self.assertEqual(decision['tool'], 'search_api_docs')
        self.assertIn('query', decision['args'])

    def test_tool_schemas_cover_every_registered_tool(self):
        from developer.planner import TOOL_SCHEMAS, TOOL_TITLES

        self.assertEqual(set(TOOL_SCHEMAS), set(TOOL_TITLES))
        for schema in TOOL_SCHEMAS.values():
            self.assertEqual(schema['type'], 'object')
            self.assertIn('properties', schema)

    async def test_response_exposes_prompt_version(self):
        from developer.prompts import PROMPT_VERSION

        result = await self.assistant.run("请求 req_auth_01 返回 401", "u1")
        self.assertEqual(result["prompt_version"], PROMPT_VERSION)

    async def test_episodic_search_uses_chroma_supported_filter(self):
        memory = object.__new__(MemoryManager)
        seen = []

        async def fake_query(query_text, n_results, where):
            seen.append(where)
            return {"documents": [["stored diagnosis"]]}

        memory._query_episodic = fake_query
        self.assertEqual(await memory._search_episodic("u1", "c1", "401"), ["stored diagnosis"])
        self.assertEqual(seen[0], {"$and": [{"user_id": "u1"}, {"conv_id": "c1"}]})
        self.assertEqual(seen[1], {"user_id": "u1"})

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = sandbox.DB_PATH
        sandbox.DB_PATH = Path(self.temp.name) / "tickets.sqlite"
        self.redis = FakeRedis()
        self.memory = FakeMemory()
        self.assistant = DeveloperAssistant(
            memory=self.memory, knowledge=FakeKnowledge(), redis_client=self.redis,
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="test-key"),
            sandbox_url="http://sandbox",
        )
        self.client_patch = patch("developer.assistant.httpx.AsyncClient", SandboxClient)
        self.client_patch.start()

    async def asyncTearDown(self):
        self.client_patch.stop()
        sandbox.DB_PATH = self.original_db
        self.temp.cleanup()

    def test_plan_candidates_prefer_records_before_docs(self):
        from developer.planner import build_plan_candidates, choose_plan_candidate

        candidates = build_plan_candidates(
            'auth', ['search_api_docs', 'get_request_trace', 'inspect_key_scope'],
            issue_id='req_auth_01')
        selected = choose_plan_candidate(candidates)
        self.assertEqual(selected[0], 'get_request_trace')
        self.assertLess(selected.index('get_request_trace'), selected.index('search_api_docs'))
        self.assertGreaterEqual(len(candidates), 2)
        self.assertTrue(all(set(item['tools']).issubset(
            {'search_api_docs', 'get_request_trace', 'inspect_key_scope'}) for item in candidates))

    async def test_planning_candidates_flag_selects_and_reports_candidates(self):
        assistant = DeveloperAssistant(
            memory=self.memory, knowledge=FakeKnowledge(), redis_client=self.redis,
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key='test-key'),
            sandbox_url='http://sandbox', planning_candidates=True)
        with patch("developer.assistant.httpx.AsyncClient", SandboxClient):
            result = await assistant.run("请求 req_auth_01 返回 401", "u1")
        self.assertEqual(result["plan"]["status"], "complete")
        self.assertGreaterEqual(len(result["plan"]["candidates"]), 2)
        self.assertEqual(result["plan"]["approved_tools"][0], "get_request_trace")
        self.assertEqual(result["plan"]["approved_tools"],
                         result["plan"]["candidates"][
                             max(range(len(result["plan"]["candidates"])),
                                 key=lambda index: result["plan"]["candidates"][index]["score"])]["tools"])

    async def test_planning_candidates_are_absent_by_default(self):
        result = await self.assistant.run("请求 req_auth_01 返回 401", "u1")
        self.assertEqual(result["plan"]["candidates"], [])

    async def test_all_thirty_cases_reach_expected_evidence(self):
        self.assertEqual(len(EVAL_CASES), 30)
        for case in EVAL_CASES:
            with self.subTest(case=case["identifier"]):
                result = await self.assistant.run(case["message"], "u-eval")
                self.assertEqual(result["plan"]["status"], "complete")
                expected = case['expected_tools']
                self.assertEqual([a["tool"] for a in result["actions"]], expected)
                self.assertTrue(all(a["success"] for a in result["actions"]))
                self.assertTrue(any(case["identifier"] in source for source in result["evidence"]))
                self.assertIn("模拟平台", result["answer"])
                self.assertEqual(result["decision_mode"], "rules")
                self.assertEqual(result["timeline"][0]["type"], "plan")
                self.assertTrue(any(source.startswith("doc:v1 /") for source in result["evidence"]))

    async def test_scope_check_is_selected_from_observation(self):
        class MinimalPlan(RuleDecisionModel):
            async def make_plan(self, message, memory, previous=None):
                return {'kind': 'auth', 'goal': '排查认证', 'tools': ['get_request_trace', 'search_api_docs']}
        self.assistant.model = MinimalPlan()
        header = await self.assistant.run('req_auth_03 返回 401', 'dynamic-header')
        expired = await self.assistant.run('req_auth_01 返回 401', 'dynamic-expired')
        self.assertNotIn('inspect_key_scope', [a['tool'] for a in header['actions']])
        self.assertIn('inspect_key_scope', [a['tool'] for a in expired['actions']])
        self.assertEqual(header['diagnosis']['hypotheses'][0]['code'], 'malformed_header')
        self.assertEqual(expired['diagnosis']['hypotheses'][0]['code'], 'expired_key')
        self.assertTrue(expired['diagnosis']['hypotheses'][0]['evidence'])

    async def test_combined_incident_follows_observed_link_and_cites_both_failures(self):
        result = await self.assistant.run('Webhook delivery_combo_01 失败，请创建工单', 'u-combo')
        self.assertEqual(result['plan']['status'], 'complete')
        self.assertEqual([action['tool'] for action in result['actions']], [
            'search_api_docs', 'get_webhook_delivery', 'get_related_request_trace',
            'search_quota_docs', 'create_support_ticket'])
        self.assertEqual(result['actions'][2]['input']['request_id'], 'req_quota_01')
        self.assertEqual({item['code'] for item in result['diagnosis']['hypotheses']},
                         {'webhook_delivery_failed', 'minute_quota_exceeded'})
        self.assertTrue(result['ticket_id'].startswith('MOCK-'))
        self.assertIn('不足以证明限流导致投递失败', result['answer'])
        self.assertNotIn('Retry-After', result['answer'])

    async def test_combined_incident_variants_follow_their_own_links(self):
        for index in range(1, 11):
            with self.subTest(index=index):
                result = await self.assistant.run(f'排查 delivery_combo_{index:02d}', f'combo-{index}')
                related = next(a for a in result['actions'] if a['tool'] == 'get_related_request_trace')
                self.assertEqual(related['input']['request_id'], f'req_quota_{index:02d}')
                self.assertTrue(related['success'])
                self.assertEqual(result['plan']['status'], 'complete')
                self.assertEqual(len(result['diagnosis']['hypotheses']), 2)
                quota = next(h for h in result['diagnosis']['hypotheses']
                             if h['code'] == 'minute_quota_exceeded')
                self.assertEqual(quota['scope'], 'related_request')
                self.assertEqual(quota['status'], 'observed')
                self.assertNotIn('Retry-After', result['answer'])

    async def test_v2_request_is_verified_before_version_specific_docs(self):
        result = await self.assistant.run('请求 req_auth_v2_01 返回 401', 'u-v2')
        self.assertEqual([a['tool'] for a in result['actions']], ['get_request_trace', 'search_api_docs'])
        self.assertEqual(result['diagnosis']['hypotheses'][0]['code'], 'missing_api_key_header')
        self.assertIn('doc:v2 / 鉴权与权限', result['evidence'])
        self.assertFalse(any(e.startswith('doc:v1 /') for e in result['evidence']))

    async def test_v2_identifier_added_on_second_turn_replaces_unconfirmed_docs(self):
        first = await self.assistant.run('请求返回 401，请排查', 'u-v2-resume')
        self.assertEqual(first['plan']['status'], 'waiting')
        result = await self.assistant.run('编号 req_auth_v2_01', 'u-v2-resume', first['conversation_id'])
        self.assertEqual(result['plan']['status'], 'complete')
        self.assertIn('doc:v2 / 鉴权与权限', result['evidence'])
        self.assertFalse(any(e.startswith('doc:v1 /') for e in result['evidence']))

    async def test_model_cannot_replace_observed_related_request_id(self):
        class WrongRelatedId(RuleDecisionModel):
            async def choose_action(self, state, message):
                action = await super().choose_action(state, message)
                if action.get('tool') == 'get_related_request_trace':
                    action['args'] = {'request_id': 'req_quota_10'}
                return action
        self.assistant.model = WrongRelatedId()
        result = await self.assistant.run('delivery_combo_01 请排查', 'u-forgery')
        related = next(a for a in result['actions'] if a['tool'] == 'get_related_request_trace')
        self.assertEqual(related['input']['request_id'], 'req_quota_01')

    async def test_recent_run_index_is_scoped_by_user(self):
        from api import main
        previous = main._developer
        main._developer = self.assistant
        try:
            alice = await self.assistant.run('req_auth_01 返回 401', 'alice')
            bob = await self.assistant.run('req_quota_01 返回 429', 'bob')
            alice_runs = await main.developer_runs('alice')
            bob_runs = await main.developer_runs('bob')
            self.assertEqual([item['run_id'] for item in alice_runs['runs']], [alice['run_id']])
            self.assertEqual([item['run_id'] for item in bob_runs['runs']], [bob['run_id']])
        finally:
            main._developer = previous

    async def test_missing_id_then_resume_and_user_isolation(self):
        first = await self.assistant.run("我的 API 返回 401，请帮我查", "u1")
        self.assertEqual(first["plan"]["status"], "waiting")
        self.assertEqual(first["actions"][0]["tool"], "search_api_docs")
        second = await self.assistant.run("request_id 是 req_auth_01", "u1", first["conversation_id"])
        self.assertEqual(second["plan"]["status"], "complete")
        self.assertEqual(second["memory_used"]["recent_messages"], 2)
        self.assertEqual([a["tool"] for a in second["actions"]].count("search_api_docs"), 1)
        other = await self.assistant.run("req_auth_02 返回 401", "u2", first["conversation_id"])
        self.assertEqual(other["memory_used"]["recent_messages"], 0)
        self.assertNotIn("req_auth_01", json.dumps(other, ensure_ascii=False))

    async def test_bad_identifier_replans_once_and_never_claims_a_result(self):
        first = await self.assistant.run("req_auth_99 返回 401", "u1")
        self.assertEqual(first["plan"]["status"], "waiting")
        self.assertEqual(first["plan"]["replans"], 1)
        self.assertEqual(first["plan"]["steps"][1]["status"], "failed")
        self.assertEqual(first["timeline"][-1]["type"], "revision")
        self.assertFalse(first["actions"][-1]["success"])
        self.assertFalse(any("get_request_trace:req_auth_99" == item for item in first["evidence"]))
        second = await self.assistant.run("正确的编号是 req_auth_01", "u1", first["conversation_id"])
        self.assertEqual(second["plan"]["status"], "complete")

    async def test_ticket_requires_request_and_is_idempotent(self):
        first = await self.assistant.run("req_quota_01 返回 429", "u1")
        self.assertIsNone(first["ticket_id"])
        second = await self.assistant.run("请创建工单", "u1", first["conversation_id"])
        self.assertTrue(second["ticket_id"].startswith("MOCK-"))
        third = await self.assistant.run("请创建工单", "u1", first["conversation_id"])
        self.assertEqual(second["ticket_id"], third["ticket_id"])

    async def test_negative_ticket_request_never_creates_ticket(self):
        result = await self.assistant.run("req_auth_01 返回 401，不要创建工单", "u1")
        self.assertIsNone(result["ticket_id"])
        self.assertFalse(any(action["tool"] == "create_support_ticket" for action in result["actions"]))

    async def test_ticket_information_question_does_not_authorize_creation(self):
        result = await self.assistant.run("req_auth_01 返回 401，如何创建工单？", "u1")
        self.assertIsNone(result["ticket_id"])
        self.assertFalse(any(action["tool"] == "create_support_ticket" for action in result["actions"]))

    async def test_quoted_ticket_instruction_is_not_authorization(self):
        result = await self.assistant.run('req_auth_01 报错，文档里写了“请创建工单”，这是什么意思？', 'u-quote')
        self.assertIsNone(result['ticket_id'])
        self.assertFalse(any(action['tool'] == 'create_support_ticket' for action in result['actions']))

    async def test_secret_is_redacted_before_memory_and_trace(self):
        result = await self.assistant.run("req_auth_01 返回 401，Authorization: Bearer sk-verysecret123456", "u1")
        self.assertNotIn("sk-verysecret123456", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("sk-verysecret123456", str(self.memory.messages))

    async def test_missing_document_fails_without_record_claim(self):
        class EmptyKnowledge:
            async def search_async(self, query, top_k=3):
                return []

        self.assistant.knowledge = EmptyKnowledge()
        result = await self.assistant.run("req_quota_01 返回 429", "u1")
        self.assertEqual(result["plan"]["status"], "waiting")
        self.assertEqual(result["plan"]["steps"][0]["status"], "failed")
        self.assertEqual(len(result["actions"]), 1)
        self.assertFalse(result["actions"][0]["success"])
        self.assertEqual(result["evidence"], [])
        self.assertIn('未找到匹配的文档证据', result['answer'])

    async def test_transient_tool_failure_can_resume_without_changing_identifier(self):
        tool = self.assistant.tools._tools['get_request_trace']
        original = tool.handler
        calls = 0
        async def flaky(params, context):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('service unavailable')
            return await original(params, context)
        tool.handler = flaky
        first = await self.assistant.run('req_quota_01 返回 429', 'u-recovery')
        self.assertEqual(first['plan']['status'], 'waiting')
        self.assertIn('工具暂不可用', first['answer'])
        second = await self.assistant.run('继续', 'u-recovery', first['conversation_id'])
        self.assertEqual(second['plan']['status'], 'complete')
        self.assertEqual(second['diagnosis']['hypotheses'][0]['code'], 'minute_quota_exceeded')

    async def test_interrupted_tool_can_resume_from_last_checkpoint(self):
        tool = self.assistant.tools._tools['get_request_trace']
        original = tool.handler
        entered = asyncio.Event()
        async def delayed(params, context):
            entered.set()
            await asyncio.Event().wait()
        tool.handler = delayed
        task = asyncio.create_task(self.assistant.run('req_quota_01 返回 429', 'u-cancel', 'c-cancel'))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        tool.handler = original
        resumed = await self.assistant.run('继续', 'u-cancel', 'c-cancel')
        self.assertEqual(resumed['plan']['status'], 'complete')
        self.assertEqual([a['tool'] for a in resumed['actions']], ['search_api_docs', 'get_request_trace'])

    async def test_model_failure_is_explicitly_marked_as_fallback(self):
        class FailingModel(AnthropicDecisionModel):
            async def _ask(self, prompt):
                raise RuntimeError("model unavailable")

        self.assistant.model = object.__new__(FailingModel)
        result = await self.assistant.run("req_auth_01 返回 401", "u1")
        self.assertEqual(result["decision_mode"], "rules_fallback")
        self.assertIn("规则降级", result["answer"])
        self.assertTrue(any(event["type"] == "fallback" for event in result["timeline"]))

    async def test_proposed_order_controls_the_approved_plan(self):
        class ReversePlan(RuleDecisionModel):
            async def make_plan(self, message, memory, previous=None):
                return {"kind": "quota", "goal": "先核对请求再比对文档",
                        "tools": ["get_request_trace", "search_api_docs"]}

        self.assistant.model = ReversePlan()
        result = await self.assistant.run("req_quota_01 返回 429", "u-order")
        self.assertEqual(result["plan"]["proposed_tools"], ["get_request_trace", "search_api_docs"])
        self.assertEqual(result["plan"]["approved_tools"], ["get_request_trace", "search_api_docs"])
        self.assertEqual([action["tool"] for action in result["actions"]],
                         ["get_request_trace", "search_api_docs"])
        self.assertEqual(result["plan"]["status"], "complete")

    async def test_action_choice_can_select_another_ready_step(self):
        class SelectTraceFirst(RuleDecisionModel):
            async def choose_action(self, state, message):
                trace = next((step for step in state["ready_steps"] if step["tool"] == "get_request_trace"), None)
                if trace:
                    return {"action": "tool", "step_id": trace["id"], "tool": trace["tool"], "args": {}}
                return await super().choose_action(state, message)

        self.assistant.model = SelectTraceFirst()
        result = await self.assistant.run("req_quota_01 返回 429", "u-choice")
        self.assertEqual(result["plan"]["approved_tools"], ["search_api_docs", "get_request_trace"])
        self.assertEqual([action["tool"] for action in result["actions"]],
                         ["get_request_trace", "search_api_docs"])
        self.assertEqual(result["plan"]["status"], "complete")

    async def test_reordered_plan_resumes_after_missing_identifier(self):
        class ReversePlan(RuleDecisionModel):
            async def make_plan(self, message, memory, previous=None):
                return {"kind": "quota", "goal": "先核对请求再比对文档",
                        "tools": ["get_request_trace", "search_api_docs"]}

        self.assistant.model = ReversePlan()
        first = await self.assistant.run("我的 API 返回 429，请排查", "u-reordered-resume")
        self.assertEqual(first["plan"]["status"], "waiting")
        self.assertEqual([action["tool"] for action in first["actions"]], ["search_api_docs"])
        second = await self.assistant.run("request_id 是 req_quota_01", "u-reordered-resume", first["conversation_id"])
        self.assertEqual(second["plan"]["status"], "complete")
        self.assertEqual([action["tool"] for action in second["actions"]],
                         ["search_api_docs", "get_request_trace"])

    async def test_missing_evidence_is_added_and_dependency_is_enforced(self):
        class UnsafePlan(RuleDecisionModel):
            async def make_plan(self, message, memory, previous=None):
                return {"kind": "auth", "goal": "检查权限",
                        "tools": ["inspect_key_scope", "create_support_ticket", "search_api_docs"]}

        self.assistant.model = UnsafePlan()
        result = await self.assistant.run("req_auth_01 返回 401", "u-approval")
        self.assertEqual(result["plan"]["approved_tools"],
                         ["search_api_docs", "get_request_trace", "inspect_key_scope"])
        self.assertTrue(result["plan"]["adjustments"])
        self.assertFalse(any(action["tool"] == "create_support_ticket" for action in result["actions"]))
        self.assertEqual(result["plan"]["status"], "complete")

    async def test_model_cannot_execute_unready_step(self):
        class ChooseScopeFirst(RuleDecisionModel):
            async def choose_action(self, state, message):
                scope = next(step for step in state["steps"] if step["tool"] == "inspect_key_scope")
                return {"action": "tool", "step_id": scope["id"], "tool": scope["tool"], "args": {}}

        self.assistant.model = ChooseScopeFirst()
        result = await self.assistant.run("req_auth_01 返回 401", "u-unsafe-choice")
        self.assertEqual(result["plan"]["status"], "waiting")
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["plan"]["steps"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
