"""DEVTRACE_MCP_ALL_TOOLS: the diagnosis path can be served by the bundled MCP server.

Checklist coverage for TASK-0004 #15/#17: the server exposes eight read tools and no
ticket tool, the stdio entrypoint starts, a routed plan keeps dependency order on
``mcp_*`` names, the unswitched path is unchanged, and a failed MCP call never falls
back to the local tool.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

import httpx

from developer import sandbox
from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from developer.planner import (MCP_READ_TOOLS, MCP_ROUTED_KINDS, TOOL_EVIDENCE,
                              TOOL_SCHEMAS, TOOL_TITLES, _approved_tools, _ready_steps,
                              base_tool, mcp_name)
from developer.verifier import build_diagnosis
from mcp.client import InProcessTransport, MCPClient
from mcp.runtime import MCPToolRuntime
from mcp.server import build_default_server
from mcp.tool_manager import MCPToolManager

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_READ_TOOLS = {"search_api_docs", "search_quota_docs", "get_request_trace",
                     "get_related_request_trace", "inspect_key_scope",
                     "get_webhook_delivery", "search_public_docs", "lookup_error_playbook"}


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
    """Serves both retrieval methods so the shared docs tool works offline."""

    async def search_async(self, _query, top_k=3):
        return [{"title": doc["title"], "content": doc["content"]} for doc in DOCS[:top_k]]

    async def search_hybrid_async(self, _query, top_k=3, version="v1"):
        return [{"title": doc["title"], "content": doc["content"]}
                for doc in [d for d in DOCS if d["title"].startswith(version + " /")][:top_k]]


class SandboxClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.pop("timeout", None)
        super().__init__(transport=httpx.ASGITransport(app=sandbox.app),
                         base_url="http://sandbox", **kwargs)


class BrokenClient:
    """An MCP server that is configured but unavailable."""

    server_info = {"serverInfo": {"name": "broken", "version": "1"}}

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        raise RuntimeError("mcp unavailable")

    async def close(self):
        pass


class MCPTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = MCPToolRuntime(knowledge=FakeKnowledge(), sandbox_url="http://sandbox")
        self.server = build_default_server(self.runtime)
        self.client = MCPClient(InProcessTransport(self.server))
        await self.client.initialize()
        for target in ("developer.assistant.httpx.AsyncClient", "mcp.runtime.httpx.AsyncClient"):
            patched = patch(target, SandboxClient)
            patched.start()
            self.addCleanup(patched.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def assistant(self, *, all_tools=False):
        return DeveloperAssistant(
            memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="test-key"),
            sandbox_url="http://sandbox", mcp_client=self.client, mcp_all_tools=all_tools,
        )


class ServerSurfaceTests(MCPTestBase):
    async def test_tools_list_exposes_the_eight_read_tools(self):
        tools = await self.client.list_tools()
        self.assertEqual({tool["name"] for tool in tools}, SERVER_READ_TOOLS)
        self.assertEqual(len(tools), 8)

    async def test_tools_list_never_exposes_the_ticket_tool(self):
        tools = await self.client.list_tools()
        self.assertNotIn("create_support_ticket", {tool["name"] for tool in tools})

    async def test_stdio_entrypoint_starts_and_answers_tools_list(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        payload = "\n".join(json.dumps(item) for item in requests) + "\n"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp.server", cwd=BACKEND_DIR,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload.encode()), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            self.fail("the stdio MCP entrypoint did not finish")
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace")[:400])
        lines = [json.loads(line) for line in stdout.decode("utf-8").splitlines() if line.strip()]
        by_id = {item.get("id"): item for item in lines}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2024-11-05")
        names = {tool["name"] for tool in by_id[2]["result"]["tools"]}
        self.assertEqual(names, SERVER_READ_TOOLS)

    async def test_stdio_entrypoint_reports_a_tool_failure_instead_of_crashing(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "get_request_trace", "arguments": {"request_id": "req_auth_01"}}},
        ]
        payload = "\n".join(json.dumps(item) for item in requests) + "\n"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp.server", cwd=BACKEND_DIR,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload.encode()), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            self.fail("the stdio MCP entrypoint did not finish")
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace")[:400])
        # The sandbox is not reachable here, so the boundary must answer with isError.
        calls = [item for item in
                 (json.loads(line) for line in stdout.decode("utf-8").splitlines() if line.strip())
                 if item.get("id") == 2]
        self.assertTrue(calls[0]["result"]["isError"])


class RoutedExecutionTests(MCPTestBase):
    async def test_auth_plan_uses_mcp_names_in_dependency_order(self):
        result = await self.assistant(all_tools=True).run(
            "请求 req_auth_01 返回 401，帮我排查", "u-all-auth")
        self.assertEqual(result["kind"], "auth")
        self.assertEqual(result["plan"]["status"], "complete")
        tools = [action["tool"] for action in result["actions"]]
        self.assertEqual(tools, ["mcp_search_api_docs", "mcp_get_request_trace",
                                 "mcp_inspect_key_scope"])
        self.assertTrue(all(tool != base_tool(tool) for tool in tools))

    async def test_quota_and_webhook_plans_are_routed(self):
        for kind, message in (("quota", "请求 req_quota_01 遇到 429，如何处理"),
                              ("webhook", "Webhook 投递 delivery_01 失败，请排查")):
            with self.subTest(kind=kind):
                result = await self.assistant(all_tools=True).run(message, f"u-all-{kind}")
                self.assertEqual(result["plan"]["status"], "complete")
                tools = [action["tool"] for action in result["actions"]]
                self.assertTrue(all(tool.startswith("mcp_") for tool in tools), tools)
                self.assertNotIn("create_support_ticket", tools)

    async def test_appended_steps_stay_on_the_mcp_transport(self):
        result = await self.assistant(all_tools=True).run(
            "Webhook 投递 delivery_combo_01 失败，请排查", "u-all-combo")
        tools = [action["tool"] for action in result["actions"]]
        self.assertEqual(tools, ["mcp_search_api_docs", "mcp_get_webhook_delivery",
                                 "mcp_get_related_request_trace", "mcp_search_quota_docs"])
        self.assertEqual(result["plan"]["status"], "complete")

    async def test_ticket_step_stays_local_when_routing_is_on(self):
        result = await self.assistant(all_tools=True).run(
            "请求 req_auth_01 返回 401，请创建工单", "u-all-ticket")
        tools = [action["tool"] for action in result["actions"]]
        self.assertIn("create_support_ticket", tools)
        self.assertNotIn("mcp_create_support_ticket", tools)
        self.assertTrue(any(tool.startswith("mcp_") for tool in tools))

    async def test_unswitched_assistant_keeps_local_tool_names(self):
        for message, expected in (
            ("请求 req_auth_01 返回 401，帮我排查",
             ["search_api_docs", "get_request_trace", "inspect_key_scope"]),
            ("Webhook 投递 delivery_combo_01 失败，请排查",
             ["search_api_docs", "get_webhook_delivery", "get_related_request_trace",
              "search_quota_docs"]),
        ):
            with self.subTest(message=message):
                result = await self.assistant(all_tools=False).run(message, "u-off")
                self.assertEqual([action["tool"] for action in result["actions"]], expected)
                self.assertEqual(result["plan"]["status"], "complete")

    async def test_routed_and_local_runs_agree_on_status_and_answer(self):
        message = "Webhook 投递 delivery_combo_01 失败，请排查"
        local = await self.assistant(all_tools=False).run(message, "u-parity-local")
        routed = await self.assistant(all_tools=True).run(message, "u-parity-mcp")
        self.assertEqual(local["kind"], routed["kind"])
        self.assertEqual(local["plan"]["status"], routed["plan"]["status"])
        self.assertEqual([step["status"] for step in local["plan"]["steps"]],
                         [step["status"] for step in routed["plan"]["steps"]])
        self.assertEqual(local["answer"], routed["answer"])
        self.assertEqual([e for e in local["evidence"] if e.startswith("doc:")],
                         [e for e in routed["evidence"] if e.startswith("doc:")])

    async def test_v2_identifier_resets_the_docs_step_under_mcp_names(self):
        assistant = self.assistant(all_tools=True)
        first = await assistant.run("请求返回 401，帮我排查", "u-all-v2")
        second = await assistant.run("编号 req_auth_v2_01", "u-all-v2",
                                     first["conversation_id"])
        self.assertEqual(second["plan"]["status"], "complete")
        docs_done = [action for action in second["actions"]
                     if base_tool(action["tool"]) == "search_api_docs"]
        self.assertTrue(docs_done)
        self.assertTrue(all("v2" in action["input"]["query"] for action in docs_done))
        self.assertTrue(any(step["tool"] == "mcp_search_api_docs"
                            for step in second["plan"]["steps"]))

    async def test_mcp_failure_does_not_fall_back_to_the_local_tool(self):
        broken = BrokenClient()
        assistant = DeveloperAssistant(
            memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
            model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="test-key"),
            sandbox_url="http://sandbox", mcp_client=broken, mcp_all_tools=True,
        )
        result = await assistant.run("请求 req_auth_01 返回 401，帮我排查", "u-broken")
        self.assertEqual(result["plan"]["status"], "waiting")
        self.assertEqual([action["tool"] for action in result["actions"]],
                         ["mcp_search_api_docs"])
        self.assertFalse(result["actions"][0]["success"])
        # The MCP server is asked by its own tool name; the locally registered tool
        # with that name is never executed as a fallback.
        self.assertEqual([name for name, _ in broken.calls], ["search_api_docs"])
        self.assertIn("当前不推断根因", result["answer"])

    async def test_observation_summary_names_the_mcp_transport(self):
        result = await self.assistant(all_tools=True).run(
            "请求 req_auth_01 返回 401，帮我排查", "u-all-summary")
        summaries = [action["summary"] for action in result["actions"]]
        self.assertTrue(any(summary.startswith("MCP 返回") for summary in summaries), summaries)
        local = await self.assistant(all_tools=False).run(
            "请求 req_auth_01 返回 401，帮我排查", "u-off-summary")
        self.assertTrue(any(summary.startswith("找到")
                            for summary in [a["summary"] for a in local["actions"]]))

    def test_routed_kinds_exclude_the_public_api_path(self):
        # public_api has its own switch (DEVTRACE_MCP_PUBLIC_TOOLS) and its own tools.
        self.assertEqual(set(MCP_ROUTED_KINDS), {"auth", "quota", "webhook"})
        self.assertNotIn("public_api", MCP_ROUTED_KINDS)

    def test_routing_registers_only_read_twins(self):
        names = set(self.assistant(all_tools=True).tools.tool_names)
        for tool in MCP_READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, names)
                self.assertIn(mcp_name(tool), names)
        self.assertNotIn("mcp_create_support_ticket", names)
        self.assertIn("create_support_ticket", names)


class PlannerBaseNameTests(unittest.TestCase):
    """The plan rules compare base names, so both spellings behave identically."""

    def test_base_tool_strips_only_the_mcp_prefix(self):
        self.assertEqual(base_tool("mcp_get_request_trace"), "get_request_trace")
        self.assertEqual(base_tool("get_request_trace"), "get_request_trace")
        self.assertEqual(base_tool("mcpext_search"), "mcpext_search")
        self.assertEqual(base_tool(None), "")

    def test_mcp_name_is_idempotent(self):
        self.assertEqual(mcp_name("get_request_trace"), "mcp_get_request_trace")
        self.assertEqual(mcp_name(mcp_name("get_request_trace")), "mcp_get_request_trace")

    def test_titles_schemas_and_evidence_cover_both_spellings(self):
        for tool in MCP_READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(mcp_name(tool), TOOL_TITLES)
                self.assertIn(mcp_name(tool), TOOL_EVIDENCE)
                self.assertEqual(TOOL_SCHEMAS[mcp_name(tool)], TOOL_SCHEMAS[tool])

    def test_approved_tools_accepts_either_spelling_from_the_model(self):
        for proposed in (["inspect_key_scope", "get_request_trace", "search_api_docs"],
                         ["mcp_inspect_key_scope", "mcp_get_request_trace",
                          "mcp_search_api_docs"]):
            with self.subTest(proposed=proposed[0]):
                allowed = [mcp_name(tool) for tool in ("search_api_docs", "get_request_trace",
                                                       "inspect_key_scope")]
                _raw, chosen, _adjustments = _approved_tools("auth", proposed, allowed)
                self.assertEqual(chosen, ["mcp_get_request_trace", "mcp_inspect_key_scope",
                                         "mcp_search_api_docs"])

    def test_approved_tools_rejects_tools_outside_the_kind(self):
        _raw, chosen, adjustments = _approved_tools(
            "quota", ["mcp_get_webhook_delivery"],
            ["mcp_search_api_docs", "mcp_get_request_trace"])
        self.assertEqual(chosen, ["mcp_search_api_docs", "mcp_get_request_trace"])
        self.assertIn("移除不属于该故障类型的工具", adjustments)

    @staticmethod
    def _state(tool_names, statuses=None, **extra):
        statuses = statuses or ["pending"] * len(tool_names)
        state = {"kind": "auth", "issue_id": "req_auth_01", "key_prefix": "",
                 "require_version_observation": False, "ticket_authorized": False,
                 "approved_tools": list(tool_names),
                 "steps": [{"id": f"s{i + 1}", "tool": name, "title": name,
                            "required_evidence": "", "status": status}
                           for i, (name, status) in enumerate(zip(tool_names, statuses))]}
        state.update(extra)
        return state

    def test_ready_steps_hold_key_scope_until_the_trace_completes(self):
        for spelling in ("inspect_key_scope", "mcp_inspect_key_scope"):
            with self.subTest(tool=spelling):
                state = self._state(["mcp_get_request_trace", spelling])
                self.assertEqual([step["tool"] for step in _ready_steps(state)],
                                 ["mcp_get_request_trace"])
                state = self._state(["mcp_get_request_trace", spelling], ["done", "pending"],
                                    key_prefix="demo-auth-01")
                self.assertEqual([step["tool"] for step in _ready_steps(state)],
                                 [spelling])

    def test_ready_steps_gate_on_mcp_named_completed_dependencies(self):
        state = self._state(["mcp_get_webhook_delivery", "mcp_get_related_request_trace"],
                            ["done", "pending"])
        state["kind"] = "webhook"
        self.assertEqual([step["tool"] for step in _ready_steps(state)],
                         ["mcp_get_related_request_trace"])

    def test_ready_steps_require_an_issue_identifier_for_record_tools(self):
        state = self._state(["mcp_get_request_trace"], issue_id="")
        self.assertEqual(_ready_steps(state), [])
        state = self._state(["mcp_get_webhook_delivery"], kind="webhook", issue_id="")
        self.assertEqual(_ready_steps(state), [])

    def test_ready_steps_gate_the_ticket_on_authorization_and_completed_peers(self):
        names = ["mcp_search_api_docs", "mcp_get_request_trace", "create_support_ticket"]
        state = self._state(names, ["done", "done", "pending"], key_prefix="demo-auth-01",
                            ticket_authorized=False)
        self.assertEqual(_ready_steps(state), [])
        state["ticket_authorized"] = True
        self.assertEqual([step["tool"] for step in _ready_steps(state)],
                         ["create_support_ticket"])

    def test_ready_steps_wait_for_the_version_observation_under_mcp_names(self):
        state = self._state(["mcp_search_api_docs", "mcp_get_request_trace"],
                            require_version_observation=True)
        self.assertEqual([step["tool"] for step in _ready_steps(state)],
                         ["mcp_get_request_trace"])


class VerifierBaseNameTests(unittest.TestCase):
    @staticmethod
    def _normalize(diagnosis):
        """Evidence strings name the transport that produced them; compare structure."""
        text = json.dumps(diagnosis, ensure_ascii=False)
        return json.loads(text.replace('"mcp_get', '"get').replace('"mcp_inspect', '"inspect'))

    def test_diagnosis_is_identical_for_both_spellings(self):
        actions = [
            {"tool": "get_webhook_delivery", "success": True,
             "source": "get_webhook_delivery:delivery_01",
             "result": {"delivery_id": "delivery_01", "status": 401, "retry_count": 1,
                        "error_summary": "接收端拒绝签名"}},
            {"tool": "get_request_trace", "success": True,
             "source": "get_request_trace:req_auth_01",
             "result": {"request_id": "req_auth_01", "status": 401,
                        "reason_code": "expired_key", "error_summary": "凭证已过期"}},
            {"tool": "inspect_key_scope", "success": True,
             "source": "inspect_key_scope:demo-auth-01",
             "result": {"key_prefix": "demo-auth-01", "status": "active",
                        "scopes": ["messages:write"]}},
        ]
        local = build_diagnosis("webhook", actions)
        routed = build_diagnosis("webhook", [{**action, "tool": mcp_name(action["tool"]),
                                             "source": action["source"].replace(
                                                 action["tool"], mcp_name(action["tool"]))}
                                            for action in actions])
        self.assertEqual(local, self._normalize(routed))
        self.assertTrue(any("mcp_" in json.dumps(item, ensure_ascii=False)
                            for item in routed["hypotheses"]),
                        "the MCP transport must stay visible in the evidence")


if __name__ == "__main__":
    unittest.main()
