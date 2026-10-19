"""Evaluate the agent's public_api path when it is served by the bundled MCP server.

Each case must use the MCP-hosted tools (not local functions), cite the source and
include remediation or documentation evidence. In-process transport; no network.
"""
import argparse
import asyncio
import json
from pathlib import Path

from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.public_knowledge import PublicDocsKnowledge
from mcp.client import InProcessTransport, MCPClient
from mcp.server import build_default_server
from mcp.tool_manager import MCPToolManager

CASES = [
    ("github_401", "GitHub API 返回 401 Bad credentials 怎么处理", "docs.github.com"),
    ("github_rate_limit", "GitHub API 403 rate limit 怎么退避", "docs.github.com"),
    ("stripe_401", "Stripe API 返回 401 invalid API Key 怎么办", "docs.stripe.com"),
    ("stripe_429", "Stripe 429 限流，重试策略怎么写", "docs.stripe.com"),
    ("stripe_webhook", "Stripe Webhook 签名验证失败怎么排查", "docs.stripe.com"),
    ("deepseek_402", "DeepSeek 402 Insufficient Balance 是余额不足吗", "api-docs.deepseek.com"),
    ("anthropic_429", "Anthropic 429 rate_limit_error 怎么退避", "docs.anthropic.com"),
    ("gemini_429", "Gemini 429 RESOURCE_EXHAUSTED 怎么处理", "ai.google.dev"),
    ("twilio_429", "Twilio 返回 429 Too Many Requests 怎么处理", "twilio.com"),
    ("generic_retry", "公开 API 文档里 HTTP 429 和 503 的 Retry-After 应该怎么用", "developer.mozilla.org"),
]


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
        return []


def new_assistant(client):
    return DeveloperAssistant(
        memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
        model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="evaluation-only"),
        sandbox_url="http://sandbox", public_knowledge=PublicDocsKnowledge(),
        mcp_client=client, mcp_public_tools=True,
    )


def assess(case, response):
    case_id, _message, source = case
    answer = response.get("answer") or ""
    tools = [action.get("tool") for action in response.get("actions") or []]
    reasons = []
    if response.get("plan", {}).get("status") != "complete":
        reasons.append("plan_incomplete")
    if tools != ["mcp_lookup_error_playbook", "mcp_search_public_docs"]:
        reasons.append("not_mcp_tools")
    if source not in answer:
        reasons.append("missing_source")
    if "公开 API 文档" not in answer:
        reasons.append("missing_public_disclaimer")
    return {"id": case_id, "passed": not reasons, "reasons": reasons, "tools": tools}


async def evaluate():
    client = MCPClient(InProcessTransport(build_default_server()))
    await client.initialize()
    rows = []
    try:
        for case in CASES:
            rows.append(assess(case, await new_assistant(client).run(case[1], f"mcp-{case[0]}")))
    finally:
        await client.close()
    return {
        "mode": "rules-in-process+mcp",
        "server": client.server_info,
        "count": len(rows),
        "passed": sum(int(row["passed"]) for row in rows),
        "cases": rows,
        "limitations": (
            "In-process MCP transport (stdio transport is implemented and covered by "
            "tests/test_mcp_all_tools.py, but not exercised here); rules mode only; the "
            "MCP server hosts eight read tools, and the diagnosis path only routes them "
            "when DEVTRACE_MCP_ALL_TOOLS is enabled. Ticket creation stays local."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": report["count"], "passed": report["passed"]}, ensure_ascii=False))
    if report["passed"] != report["count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
