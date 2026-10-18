"""MCP protocol/client tests and the optional MCP tool path in the assistant."""
import unittest

import httpx

from developer import sandbox
from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from developer.public_knowledge import PublicDocsKnowledge
from mcp.client import InProcessTransport, MCPClient
from mcp.server import MCPToolError, build_default_server
from mcp.tool_manager import MCPToolManager


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def zadd(self, *_args):
        pass

    async def zremrangebyrank(self, *_args):
        pass

    async def expire(self, *_args):
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


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MCPClient(InProcessTransport(build_default_server()))
        await self.client.initialize()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_initialize_and_list_tools(self):
        info = self.client.server_info
        self.assertEqual(info["protocolVersion"], "2024-11-05")
        self.assertIn("tools", info["capabilities"])
        tools = await self.client.list_tools()
        self.assertEqual({tool["name"] for tool in tools},
                         {"search_public_docs", "lookup_error_playbook"})
        self.assertIn("inputSchema", tools[0])

    async def test_call_tool_returns_structured_result(self):
        docs = await self.client.call_tool("search_public_docs",
                                           {"query": "GitHub rate limit", "api": "GitHub"})
        self.assertIsInstance(docs, list)
        self.assertTrue(docs)
        self.assertIn("docs.github.com", docs[0]["source_url"])

    async def test_unknown_tool_raises(self):
        with self.assertRaises(MCPToolError):
            await self.client.call_tool("missing_tool", {})

    async def test_agent_public_api_uses_mcp_tools(self):
        assistant = DeveloperAssistant(
            memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="test-key"),
            sandbox_url="http://sandbox", public_knowledge=PublicDocsKnowledge(),
            mcp_client=self.client, mcp_public_tools=True,
        )
        result = await assistant.run("GitHub API 返回 403 rate limit 怎么处理", "u-mcp")
        self.assertEqual(result["kind"], "public_api")
        self.assertEqual(result["plan"]["status"], "complete")
        tools = [action["tool"] for action in result["actions"]]
        self.assertEqual(tools, ["mcp_lookup_error_playbook", "mcp_search_public_docs"])
        self.assertIn("处理建议", result["answer"])
        self.assertIn("docs.github.com", result["answer"])
        self.assertTrue(any("MCP 返回" in action["summary"] for action in result["actions"]))


if __name__ == '__main__':
    unittest.main()
