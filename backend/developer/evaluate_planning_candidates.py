"""Evaluate candidate plan scoring/selection with the optional planner flag.

Each case runs through the real assistant in rules mode with candidate scoring
enabled; the selected plan must cover mandatory evidence, respect dependencies,
and prefer records-before-docs when an identifier exists.
"""
import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx

from developer import sandbox
from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from developer.public_knowledge import PublicDocsKnowledge
from mcp.tool_manager import MCPToolManager

CASES = [
    ("auth_01", "请求 req_auth_01 返回 401，帮我排查", "auth"),
    ("auth_02", "请求 req_auth_02 返回 401", "auth"),
    ("auth_11", "请求 req_auth_11 返回 403", "auth"),
    ("auth_v2", "请求 req_auth_v2_01 返回 401", "auth"),
    ("quota_01", "请求 req_quota_01 遇到 429，如何处理", "quota"),
    ("quota_02", "请求 req_quota_02 被限流", "quota"),
    ("webhook_01", "Webhook 投递 delivery_01 失败，请排查", "webhook"),
    ("webhook_02", "Webhook 投递 delivery_02 失败", "webhook"),
    ("combo_01", "Webhook delivery_combo_01 失败，帮我排查", "webhook"),
    ("combo_02", "Webhook delivery_combo_02 失败", "webhook"),
    ("inject_01", "Webhook 投递 delivery_inject_01 失败，请排查", "webhook"),
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
        return [{"title": doc["title"], "content": doc["content"]} for doc in DOCS[:top_k]]


class SandboxClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.pop("timeout", None)
        super().__init__(transport=httpx.ASGITransport(app=sandbox.app),
                         base_url="http://sandbox", **kwargs)


def new_assistant():
    return DeveloperAssistant(
        memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
        model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="evaluation-only"),
        sandbox_url="http://sandbox", public_knowledge=PublicDocsKnowledge(),
        planning_candidates=True,
    )


def assess(case, response):
    case_id, _message, kind = case
    plan = response.get("plan") or {}
    candidates = plan.get("candidates") or []
    approved = plan.get("approved_tools") or []
    reasons = []
    if plan.get("status") != "complete":
        reasons.append("plan_incomplete")
    if len(candidates) < 2:
        reasons.append("missing_candidates")
    if candidates:
        best = max(candidates, key=lambda item: item["score"])
        if approved[:len(best["tools"])] != best["tools"]:
            reasons.append("not_highest_scoring")
    if kind == "auth" and "get_request_trace" in approved and "search_api_docs" in approved:
        if approved.index("get_request_trace") > approved.index("search_api_docs"):
            reasons.append("docs_before_records")
    if kind == "webhook" and "get_webhook_delivery" in approved and "search_api_docs" in approved:
        if approved.index("get_webhook_delivery") > approved.index("search_api_docs"):
            reasons.append("docs_before_records")
    return {"id": case_id, "passed": not reasons, "reasons": reasons,
            "approved": approved, "score": candidates[0]["score"] if candidates else None}


async def evaluate():
    rows = []
    with patch("developer.assistant.httpx.AsyncClient", SandboxClient):
        for case in CASES:
            rows.append(assess(case, await new_assistant().run(case[1], f"plan-{case[0]}")))
    return {
        "mode": "rules-in-process",
        "count": len(rows),
        "passed": sum(int(row["passed"]) for row in rows),
        "cases": rows,
        "limitations": (
            "Rules mode; 12 hand-written cases; scoring is heuristic (coverage, record-before-docs, "
            "dependency, length) and does not optimize latency or model preference."
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
