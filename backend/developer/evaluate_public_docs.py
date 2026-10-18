"""Evaluate the public API documentation knowledge base (rules mode, in-process).

Each case asks a realistic public-API question; the check requires a completed
plan, the public-docs tool, a matching attributed document and a public-docs
disclaimer in the answer. It does not call the real APIs.
"""
import argparse
import asyncio
import json
from pathlib import Path

from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.public_knowledge import PublicDocsKnowledge
from mcp.tool_manager import MCPToolManager

CASES = [
    {"id": "github_401", "message": "GitHub API 返回 401 Bad credentials，怎么排查",
     "source": "docs.github.com", "title": "GitHub REST API：401"},
    {"id": "github_rate_limit", "message": "GitHub API 403 rate limit，Retry-After 怎么处理",
     "source": "docs.github.com", "title": "GitHub REST API：速率限制"},
    {"id": "github_secondary", "message": "GitHub 遇到二级限流，应该怎么退避",
     "source": "docs.github.com", "title": "GitHub REST API：二级限流"},
    {"id": "github_webhook", "message": "GitHub Webhook 投递失败怎么排查",
     "source": "docs.github.com", "title": "GitHub Webhooks"},
    {"id": "stripe_401", "message": "Stripe API 返回 401 invalid API Key 怎么办",
     "source": "docs.stripe.com", "title": "Stripe API：401"},
    {"id": "stripe_rate_limit", "message": "Stripe 429 限流，重试策略怎么写",
     "source": "docs.stripe.com", "title": "Stripe API：429"},
    {"id": "stripe_webhook", "message": "Stripe Webhook 签名验证失败怎么排查",
     "source": "docs.stripe.com", "title": "Stripe Webhooks"},
    {"id": "stripe_idempotency", "message": "Stripe 幂等键导致 409 是什么原因",
     "source": "docs.stripe.com", "title": "Stripe API：幂等键"},
    {"id": "openai_401", "message": "OpenAI API 401 invalid_api_key 怎么处理",
     "source": "platform.openai.com", "title": "OpenAI API：401"},
    {"id": "openai_429", "message": "OpenAI 429 是限流还是额度用尽，怎么区分",
     "source": "platform.openai.com", "title": "OpenAI API：429"},
    {"id": "twilio_429", "message": "Twilio 返回 429 Too Many Requests 怎么处理",
     "source": "twilio.com", "title": "Twilio API：429"},
    {"id": "generic_retry", "message": "公开 API 文档里 HTTP 429 和 503 的 Retry-After 应该怎么用",
     "source": "developer.mozilla.org", "title": "HTTP 429/503"},
    {"id": "stripe_spec_payment_intent", "message": "Stripe 创建 PaymentIntent 的接口是哪个",
     "source": "github.com/stripe/openapi", "title": "POST /v1/payment_intents"},
    {"id": "stripe_spec_webhook", "message": "Stripe 创建 webhook endpoint 的接口是什么",
     "source": "github.com/stripe/openapi", "title": "POST /v1/webhook_endpoints"},
    {"id": "stripe_spec_refund", "message": "Stripe 退款接口怎么调用",
     "source": "github.com/stripe/openapi", "title": "POST /v1/refunds"},
    {"id": "stripe_spec_charge", "message": "Stripe 查询 charge 的接口",
     "source": "github.com/stripe/openapi", "title": "GET /v1/charges"},
    {"id": "stripe_spec_dispute_close", "message": "Stripe 关闭 dispute 的接口",
     "source": "github.com/stripe/openapi", "title": "/v1/disputes/{dispute}/close"},
    {"id": "stripe_spec_events", "message": "Stripe 事件列表 events 接口",
     "source": "github.com/stripe/openapi", "title": "GET /v1/events"},
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


def new_assistant():
    return DeveloperAssistant(
        memory=FakeMemory(), knowledge=FakeKnowledge(), redis_client=FakeRedis(),
        model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="evaluation-only"),
        sandbox_url="http://sandbox",
        public_knowledge=PublicDocsKnowledge(),
    )


def assess(case, response):
    reasons = []
    tools = [action.get("tool") for action in response.get("actions") or []]
    answer = response.get("answer") or ""
    if response.get("plan", {}).get("status") != "complete":
        reasons.append("plan_incomplete")
    if "search_public_docs" not in tools:
        reasons.append("public_docs_tool_not_used")
    if case["source"] not in answer:
        reasons.append("missing_source")
    if case["title"] not in answer:
        reasons.append("missing_title")
    if "公开 API 文档" not in answer:
        reasons.append("missing_public_disclaimer")
    return {"id": case["id"], "passed": not reasons, "reasons": reasons,
            "tools": tools, "answer_chars": len(answer)}


async def evaluate():
    rows = []
    for case in CASES:
        rows.append(assess(case, await new_assistant().run(case["message"], f"public-{case['id']}")))
    return {
        "mode": "rules-in-process",
        "count": len(rows),
        "passed": sum(int(row["passed"]) for row in rows),
        "cases": rows,
        "limitations": (
            "Curated summaries plus the full 612-operation Stripe OpenAPI spec; "
            "no real API calls; rules mode only; "
            "Chinese query expansion is a hand-written map and the corpus needs manual upkeep."
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
