"""Tests for the curated public API documentation knowledge base."""
import unittest

import httpx

from developer import sandbox
from developer.assistant import DeveloperAssistant, RuleDecisionModel, _kind
from developer.data import DOCS
from developer.public_knowledge import PublicDocsKnowledge
from mcp.tool_manager import MCPToolManager


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def zadd(self, _key, _mapping):
        pass

    async def zremrangebyrank(self, *_args):
        pass

    async def expire(self, _key, _ttl):
        pass


class FakeMemory:
    class Context:
        recent_messages = []
        relevant_history = []
        summary = ""
        user_profile = {}

        def to_prompt_text(self):
            return ""

    async def get_context(self, *_args, **_kwargs):
        return self.Context()

    async def add_message(self, *_args, **_kwargs):
        pass

    async def update_profile(self, *_args, **_kwargs):
        pass


class FakeKnowledge:
    async def search_async(self, _query, top_k=3):
        return [{"title": doc["title"], "content": doc["content"]} for doc in DOCS[:top_k]]


class SandboxClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.pop("timeout", None)
        super().__init__(transport=httpx.ASGITransport(app=sandbox.app), base_url="http://sandbox", **kwargs)


class PublicDocsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assistant = DeveloperAssistant(
            memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="test-key"),
            sandbox_url="http://sandbox", public_knowledge=PublicDocsKnowledge(),
        )

    def test_lexical_search_returns_the_matching_api_doc(self):
        results = PublicDocsKnowledge().search("GitHub 403 rate limit Retry-After", api="GitHub")
        self.assertTrue(results)
        self.assertEqual(results[0]["api"], "GitHub")
        self.assertTrue(any("速率限制" in item["title"] for item in results))

    def test_imported_openapi_documents_are_loaded_and_searchable(self):
        knowledge = PublicDocsKnowledge()
        self.assertGreaterEqual(knowledge.count(), 100)
        results = knowledge.search("Stripe 创建 webhook endpoint 接口", api="Stripe", top_k=3)
        self.assertTrue(results)
        self.assertTrue(any("webhook_endpoints" in item["title"] for item in results))
        self.assertTrue(any(item["source_url"] == "https://github.com/stripe/openapi"
                            for item in results))

    def test_api_names_route_to_public_docs_but_mock_ids_keep_their_path(self):
        self.assertEqual(_kind("GitHub API 403 rate limit 怎么处理"), "public_api")
        self.assertEqual(_kind("Stripe Webhook 签名验证失败"), "public_api")
        self.assertEqual(_kind("DeepSeek 429 并发限制怎么处理"), "public_api")
        self.assertEqual(_kind("Claude 429 rate_limit_error 怎么处理"), "public_api")
        self.assertEqual(_kind("通义千问 DashScope 401 怎么排查"), "public_api")
        self.assertEqual(_kind("请求 req_auth_01 返回 401"), "auth")
        self.assertEqual(_kind("Webhook 投递 delivery_01 失败"), "webhook")

    def test_ai_api_docs_are_searchable(self):
        results = PublicDocsKnowledge().search("DeepSeek 402 余额不足", api="DeepSeek", top_k=3)
        self.assertTrue(results)
        self.assertTrue(any("402" in item["title"] for item in results))
        anthropic = PublicDocsKnowledge().search("Anthropic 429 退避", api="Anthropic", top_k=3)
        self.assertTrue(any("429" in item["title"] for item in anthropic))

    async def test_public_docs_question_is_answered_with_sources(self):
        result = await self.assistant.run("GitHub API 返回 403 rate limit 怎么处理", "u-public")
        self.assertEqual(result["kind"], "public_api")
        self.assertEqual(result["plan"]["status"], "complete")
        self.assertEqual([action["tool"] for action in result["actions"]],
                         ["lookup_error_playbook", "search_public_docs"])
        self.assertIn("docs.github.com", result["answer"])
        self.assertIn("公开 API 文档摘要", result["answer"])
        self.assertIn("速率限制", result["answer"])
        self.assertIn("处理建议", result["answer"])

    async def test_model_plan_uses_public_path_for_named_third_party_apis(self):
        from developer.assistant import AnthropicDecisionModel

        model = object.__new__(AnthropicDecisionModel)
        plan = await model.make_plan("GitHub API 返回 403 rate limit 怎么处理", "")
        self.assertEqual(plan["kind"], "public_api")
        self.assertEqual(plan["tools"], ["lookup_error_playbook", "search_public_docs"])

    def test_error_playbook_lookup_returns_retry_guidance(self):
        from developer.error_playbooks import lookup_playbook, count

        self.assertGreaterEqual(count(), 30)
        results = lookup_playbook("Stripe 429 too many requests", api="Stripe", http_status=429)
        self.assertTrue(results)
        self.assertEqual(results[0]["api"], "Stripe")
        self.assertTrue(results[0]["retryable"])
        self.assertTrue(results[0]["recovery"])
        no_retry = lookup_playbook("Stripe 401 invalid api key", api="Stripe", http_status=401)
        self.assertFalse(no_retry[0]["retryable"])

    async def test_unknown_public_api_still_cites_available_docs(self):
        result = await self.assistant.run("公开 API 文档里 Stripe webhook 签名失败怎么办", "u-public-2")
        self.assertEqual(result["kind"], "public_api")
        self.assertEqual(result["plan"]["status"], "complete")
        self.assertIn("stripe.com", result["answer"])
        self.assertIn("公开 API 文档", result["answer"])


if __name__ == "__main__":
    unittest.main()
