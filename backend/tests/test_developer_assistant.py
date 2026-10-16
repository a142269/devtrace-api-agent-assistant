import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from developer import sandbox
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant, RuleDecisionModel, _json_text
from developer.data import DOCS, EVAL_CASES
from mcp.tool_manager import MCPToolManager
from memory.conversation_memory import MemoryManager


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value


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
        return [{"title": doc["title"], "content": doc["content"], "score": 1.0}
                for doc in DOCS[:top_k]]


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

    async def test_all_thirty_cases_reach_expected_evidence(self):
        self.assertEqual(len(EVAL_CASES), 30)
        for case in EVAL_CASES:
            with self.subTest(case=case["identifier"]):
                result = await self.assistant.run(case["message"], "u-eval")
                self.assertEqual(result["plan"]["status"], "complete")
                self.assertEqual([a["tool"] for a in result["actions"]], case["expected_tools"])
                self.assertTrue(all(a["success"] for a in result["actions"]))
                self.assertTrue(any(case["identifier"] in source for source in result["evidence"]))
                self.assertIn("模拟平台", result["answer"])
                self.assertEqual(result["decision_mode"], "rules")
                self.assertEqual(result["timeline"][0]["type"], "plan")
                self.assertTrue(any(source.startswith("doc:v1 /") for source in result["evidence"]))

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
