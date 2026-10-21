"""Live memory-quality evaluation: real model summaries against planted facts.

Ten synthetic conversations (22 messages each) hide three facts: a request id, a
credential prefix with its scope, and a conclusion. The messages go through the
production `MemoryManager._compress` prompt path with a real model client, while
Redis and ChromaDB stay in-memory so the run cannot touch any store.

Deliberately NOT in CI: it costs real model calls and its result is not stable.
Without a configured key it writes `skipped: true` and exits 0.
"""
import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from anthropic import AsyncAnthropic

from developer.evaluate_memory_quality import FakeCollection, FakeRedis
from memory.conversation_memory import MemoryManager, MsgRole

IDENTIFIER_RE = re.compile(r"\b(?:req|delivery|tck)_[A-Za-z0-9_]+|\bsk_[A-Za-z0-9_]+")
SUMMARY_LIMIT = MemoryManager.SUMMARY_MAX_CHARS


@dataclass
class Case:
    name: str
    request_id: str
    key_prefix: str
    scope: str
    conclusion: str

    @property
    def facts(self) -> List[str]:
        return [self.request_id, self.key_prefix, self.conclusion]


CASES: List[Case] = [
    Case("auth-expired", "req_live_101", "sk_live_a1", "logs:read", "凭证已过期，需要轮换"),
    Case("auth-scope", "req_live_202", "sk_live_b2", "charges:write", "缺少 charges:write 权限"),
    Case("quota-429", "req_live_303", "sk_live_c3", "reads:read", "分钟配额耗尽，按 Retry-After 退避"),
    Case("webhook-503", "delivery_live_404", "sk_live_d4", "webhooks:read", "接收端 503 暂不可用"),
    Case("webhook-signature", "delivery_live_505", "sk_live_e5", "webhooks:read", "签名密钥不匹配"),
    Case("rate-retry", "req_live_606", "sk_live_f6", "api:read", "重试风暴触发限流，需要指数退避"),
    Case("key-rotated", "req_live_707", "sk_live_g7", "balances:read", "旧密钥已轮换，缓存里还是旧值"),
    Case("region-timeout", "req_live_808", "sk_live_h8", "tokens:read", "区域网关超时，与权限无关"),
    Case("duplicate-billing", "req_live_909", "sk_live_i9", "charges:read", "幂等键缺失导致重复扣款"),
    Case("token-revoked", "delivery_live_110", "sk_live_j10", "events:read", "回调令牌被撤销"),
]


def turns(case: Case) -> List[tuple[str, str]]:
    """One synthetic 22-message conversation with the three facts planted early."""
    return [
        ("user", f"{case.request_id} 出了问题，帮我排查"),
        ("assistant", "先看请求记录和平台文档，再确认密钥状态。"),
        ("user", f"记录里的密钥前缀是 {case.key_prefix}，需要的权限是 {case.scope}"),
        ("assistant", "收到，我按这个前缀查权限范围。"),
        ("user", f"结论是什么？我这边看到的是 {case.conclusion}"),
        ("assistant", f"目前的结论是{case.conclusion}。"),
        ("user", "有没有别的解释？"),
        ("assistant", "也可能是网络或客户端重试，但记录不支持这个方向。"),
        ("user", "那我下一步做什么"),
        ("assistant", "先处理上面那个结论，再回来看重试。"),
        ("user", "这个结论确定吗"),
        ("assistant", f"确定，依据是 {case.request_id} 的记录和 {case.key_prefix} 的状态。"),
        ("user", "需要我提供 request id 吗"),
        ("assistant", f"已经用到了：{case.request_id}。"),
        ("user", "权限名称再说一遍"),
        ("assistant", f"{case.scope}。"),
        ("user", "好，我先按你说的改"),
        ("assistant", "改完再发一次请求验证。"),
        ("user", "如果还是失败呢"),
        ("assistant", "回来核对同一份记录和权限，不要盲目重试。"),
        ("user", "明白了"),
        ("assistant", f"总结：{case.request_id} / {case.key_prefix} / {case.conclusion}。"),
    ]


def fact_hits(summary: str, facts: Sequence[str]) -> List[str]:
    return [fact for fact in facts if fact in summary]


def fabricated_identifiers(summary: str, allowed: Sequence[str]) -> List[str]:
    """Identifier-shaped strings the conversation never contained."""
    found = set()
    for match in IDENTIFIER_RE.findall(summary):
        if not any(match in fact or fact in match for fact in allowed):
            found.add(match)
    return sorted(found)


def score_case(case: Case, summary: str, latency_ms: int) -> Dict[str, Any]:
    hits = fact_hits(summary, case.facts)
    fabricated = fabricated_identifiers(summary, [*case.facts, case.scope, case.key_prefix])
    return {
        "case": case.name,
        "facts_total": len(case.facts),
        "facts_retained": len(hits),
        "missing_facts": [fact for fact in case.facts if fact not in hits],
        "fabricated_identifiers": fabricated,
        "summary_chars": len(summary),
        "within_summary_limit": len(summary) <= SUMMARY_LIMIT,
        "latency_ms": latency_ms,
        "summary": summary,
    }


def build_report(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate the per-case scores and apply the pass thresholds."""
    perfect = sum(1 for item in results if item["facts_retained"] == item["facts_total"])
    fabricated = sum(len(item["fabricated_identifiers"]) for item in results)
    overlong = sum(1 for item in results if not item["within_summary_limit"])
    latencies = [item["latency_ms"] for item in results]
    passed = (perfect >= 8 and fabricated == 0 and overlong == 0)
    return {
        "skipped": False,
        "cases": len(results),
        "perfect_sessions": perfect,
        "fact_retention_rate": round(perfect / max(1, len(results)), 3),
        "fabricated_identifiers": fabricated,
        "overlong_summaries": overlong,
        "latency_ms": {"mean": round(sum(latencies) / max(1, len(latencies)), 1),
                       "max": max(latencies) if latencies else 0},
        "thresholds": {"perfect_sessions_at_least": 8, "fabricated_identifiers": 0,
                       "summary_max_chars": SUMMARY_LIMIT},
        "passed": bool(passed),
        "results": results,
        "limitations": (
            "Template-generated synthetic conversations by one author, in-memory Redis/Chroma "
            "doubles, a single provider; it measures whether planted facts survive the "
            "compression prompt, not retrieval accuracy or production memory quality."
        ),
    }


def build_memory(client: Any) -> MemoryManager:
    """A MemoryManager with real model client and in-memory storage doubles."""
    memory = object.__new__(MemoryManager)
    memory._redis = FakeRedis()
    memory._episodic = FakeCollection()
    memory._profile = FakeCollection()
    memory._client = client
    memory._model = "live-summary"
    memory._namespace = ""
    memory._profile_prefix = "user_profile:"
    memory.memory_ttl_seconds = MemoryManager.memory_ttl_seconds
    return memory


async def run(model_name: str) -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
    if not key:
        return {"skipped": True, "passed": True, "cases": 0,
                "reason": "ANTHROPIC_API_KEY 未配置，未做任何真实模型调用",
                "command": "ANTHROPIC_API_KEY=... python -m developer.evaluate_memory_quality_live "
                           "--output developer/eval_memory_quality_live_<date>.json",
                "limitations": "Without a key this script verifies nothing about summary quality."}
    client = AsyncAnthropic(api_key=key, base_url=base_url)
    model_name = model_name or os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip()
    results: List[Dict[str, Any]] = []
    try:
        for case in CASES:
            memory = build_memory(client)
            memory._model = model_name
            started = time.perf_counter()
            for role, content in turns(case):
                await memory.add_message("live-user", f"c-{case.name}", MsgRole(role), content)
            latency_ms = round((time.perf_counter() - started) * 1000)
            summary = memory._redis.values.get(
                memory._summary_key("live-user", f"c-{case.name}"), "")
            if not summary:
                # Conversation never reached COMPRESS_AT; compress explicitly so the
                # scored summary always comes from the production prompt path.
                await memory._compress("live-user", f"c-{case.name}")
                summary = memory._redis.values.get(
                    memory._summary_key("live-user", f"c-{case.name}"), "")
            results.append(score_case(case, summary, latency_ms))
    finally:
        await client.close()
    report = build_report(results)
    report["model"] = model_name
    report["transport"] = "anthropic-compatible" if base_url else "anthropic"
    report["base_url_configured"] = bool(base_url)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="", help="override ANTHROPIC_MODEL")
    args = parser.parse_args()
    report = asyncio.run(run(args.model))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    for secret in (os.getenv("ANTHROPIC_API_KEY", ""), os.getenv("DEVTRACE_JWT_SECRET", "")):
        if secret and secret in text:
            raise SystemExit("拒绝写出包含凭据的报告")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                      ("skipped", "cases", "perfect_sessions", "fact_retention_rate",
                       "fabricated_identifiers", "overlong_summaries", "passed")
                      if key in report}, ensure_ascii=False))
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
