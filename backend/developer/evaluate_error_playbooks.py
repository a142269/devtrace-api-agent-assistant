"""Evaluate the error-remediation playbooks across Stripe/GitHub/Twilio.

40 symptom-style questions; each check requires a completed plan, the playbook
tool, the matching entry (title + source host), a retryability marker and
concrete recovery steps. Rules mode, in-process; no real platform calls.
"""
import argparse
import asyncio
import json
from pathlib import Path

from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.public_knowledge import PublicDocsKnowledge
from mcp.tool_manager import MCPToolManager

CASES = [
    # Stripe (14)
    ("stripe_401", "Stripe", "Stripe 401 invalid api key 认证失败怎么处理", "invalid_api_key", "docs.stripe.com", False),
    ("stripe_403", "Stripe", "Stripe 403 permission restricted key 权限不足", "403 权限不足", "docs.stripe.com", False),
    ("stripe_429", "Stripe", "Stripe 429 too many requests 限流怎么退避", "429 限流", "docs.stripe.com", True),
    ("stripe_400", "Stripe", "Stripe 400 invalid request missing parameter 参数错误", "400 参数错误", "docs.stripe.com", False),
    ("stripe_402", "Stripe", "Stripe 402 card declined 卡被拒怎么办", "402", "docs.stripe.com", False),
    ("stripe_404", "Stripe", "Stripe 404 resource missing 资源不存在", "404 资源不存在", "docs.stripe.com", False),
    ("stripe_409", "Stripe", "Stripe 409 idempotency_error 幂等键冲突", "409 幂等键冲突", "docs.stripe.com", False),
    ("stripe_webhook", "Stripe", "Stripe webhook 签名 verification failed 投递失败", "Webhook 签名验证失败", "docs.stripe.com", False),
    ("stripe_500", "Stripe", "Stripe 500 api_error 服务端错误", "500 服务端错误", "docs.stripe.com", True),
    ("stripe_503", "Stripe", "Stripe 503 service unavailable 服务不可用", "503 服务不可用", "docs.stripe.com", True),
    ("stripe_key_mode", "Stripe", "Stripe 401 test key live key 测试密钥 生产密钥混用", "测试/生产密钥混用", "docs.stripe.com", False),
    ("stripe_concurrency", "Stripe", "Stripe 429 lock timeout 并发 同时请求", "并发锁/速率限制", "docs.stripe.com", True),
    ("stripe_400b", "Stripe", "Stripe 400 parameter_invalid 参数类型错误", "400 参数错误", "docs.stripe.com", False),
    ("stripe_402b", "Stripe", "Stripe 402 insufficient_funds decline_code 余额不足", "402", "docs.stripe.com", False),
    # GitHub (13)
    ("github_401", "GitHub", "GitHub 401 bad credentials token 无效", "401 Bad credentials", "docs.github.com", False),
    ("github_403", "GitHub", "GitHub 403 rate limit 限流 X-RateLimit-Remaining", "403 主限流", "docs.github.com", True),
    ("github_secondary", "GitHub", "GitHub 403 secondary rate limit 二级限流 突发请求", "403 二级限流", "docs.github.com", True),
    ("github_scope", "GitHub", "GitHub 403 forbidden SSO 权限不足", "403 权限/SSO", "docs.github.com", False),
    ("github_404", "GitHub", "GitHub 404 not found 仓库不存在", "404 资源不存在", "docs.github.com", False),
    ("github_422", "GitHub", "GitHub 422 validation failed 字段错误", "422 Validation failed", "docs.github.com", False),
    ("github_400", "GitHub", "GitHub 400 bad request json 解析失败", "400 请求格式错误", "docs.github.com", False),
    ("github_429", "GitHub", "GitHub 429 too many requests 请求过多", "429 Too Many Requests", "docs.github.com", True),
    ("github_500", "GitHub", "GitHub 500 server error 服务端错误", "500 服务端错误", "docs.github.com", True),
    ("github_503", "GitHub", "GitHub 503 unavailable 服务不可用", "503 服务不可用", "docs.github.com", True),
    ("github_expired", "GitHub", "GitHub 401 token 过期 expired 失效", "Token 过期", "docs.github.com", False),
    ("github_webhook", "GitHub", "GitHub webhook x-hub-signature 验签失败", "Webhook 签名校验失败", "docs.github.com", False),
    ("github_delivery", "GitHub", "GitHub webhook 投递失败 delivery 超时", "Webhook 投递失败", "docs.github.com", True),
    # Twilio (13)
    ("twilio_401", "Twilio", "Twilio 401 authentication error 20003 认证失败", "20003", "twilio.com", False),
    ("twilio_403", "Twilio", "Twilio 403 20403 权限不足", "20403", "twilio.com", False),
    ("twilio_429", "Twilio", "Twilio 429 20429 限流 请求过快", "20429", "twilio.com", True),
    ("twilio_21211", "Twilio", "Twilio 400 21211 invalid to 号码无效", "21211", "twilio.com", False),
    ("twilio_20404", "Twilio", "Twilio 404 20404 资源不存在", "20404", "twilio.com", False),
    ("twilio_21602", "Twilio", "Twilio 400 21602 message body 内容为空", "21602", "twilio.com", False),
    ("twilio_queue", "Twilio", "Twilio 429 queue 队列溢出 突发发送", "队列溢出", "twilio.com", True),
    ("twilio_500", "Twilio", "Twilio 500 20500 服务端错误", "20500", "twilio.com", True),
    ("twilio_503", "Twilio", "Twilio 503 20503 服务不可用", "20503", "twilio.com", True),
    ("twilio_webhook_signature", "Twilio", "Twilio webhook x-twilio-signature 验签失败", "Webhook 签名校验失败", "twilio.com", False),
    ("twilio_webhook_400", "Twilio", "Twilio webhook 400 回调失败 参数缺失", "Webhook 400", "twilio.com", True),
    ("twilio_geo", "Twilio", "Twilio 403 21408 geo 地区限制", "21408", "twilio.com", False),
    ("twilio_daily", "Twilio", "Twilio 429 daily 配额耗尽", "每日配额", "twilio.com", True),
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
        sandbox_url="http://sandbox", public_knowledge=PublicDocsKnowledge(),
    )


def assess(case, response):
    case_id, api, message, title, source, retryable = case
    reasons = []
    answer = response.get("answer") or ""
    tools = [action.get("tool") for action in response.get("actions") or []]
    if response.get("plan", {}).get("status") != "complete":
        reasons.append("plan_incomplete")
    if "lookup_error_playbook" not in tools:
        reasons.append("playbook_tool_not_used")
    if title not in answer:
        reasons.append("missing_entry")
    if source not in answer:
        reasons.append("missing_source")
    expected_retry = "（可重试" if retryable else "（不可重试"
    if expected_retry not in answer:
        reasons.append("wrong_retryability")
    if "处理建议" not in answer:
        reasons.append("missing_recovery")
    return {"id": case_id, "api": api, "passed": not reasons, "reasons": reasons, "tools": tools}


async def evaluate():
    rows = []
    for case in CASES:
        rows.append(assess(case, await new_assistant().run(case[2], f"playbook-{case[0]}")))
    return {
        "mode": "rules-in-process",
        "count": len(rows),
        "passed": sum(int(row["passed"]) for row in rows),
        "by_api": {api: {"passed": sum(int(row["passed"]) for row in rows if row["api"] == api),
                         "count": sum(1 for row in rows if row["api"] == api)}
                   for api in ("Stripe", "GitHub", "Twilio")},
        "cases": rows,
        "limitations": (
            "36 hand-curated playbooks from official public docs; no real platform calls; "
            "rules mode only; status/title checks are keyword-based, not a human review of every "
            "recommendation; playbooks can lag vendor documentation."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": report["count"], "passed": report["passed"], "by_api": report["by_api"]},
                     ensure_ascii=False))
    if report["passed"] != report["count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
