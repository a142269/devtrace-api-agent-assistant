"""Cross-turn task evaluation: does working memory / plan reuse actually help?

Each task has a first turn that lacks an identifier and a second turn that supplies
it. Two modes are compared:
  resume : second turn in the same conversation (working memory + Redis plan checkpoint)
  fresh  : second turn in a new conversation (no state reuse)

This measures plan/working-memory reuse, not ChromaDB episodic recall: the harness
uses an in-process working-memory stub and a fake Redis, with the ASGI mock platform.
"""
import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TASKS = [
    {"id": "auth_expired", "first": "我的接口返回 401，帮我排查", "second": "编号是 req_auth_01",
     "codes": ["expired_key"]},
    {"id": "auth_malformed", "first": "请求一直 401，帮我看看", "second": "编号 req_auth_03",
     "codes": ["malformed_header"]},
    {"id": "auth_scope_conflict", "first": "接口返回 403，帮我确认权限", "second": "编号 req_auth_12",
     "codes": ["missing_scope"], "conflict": True},
    {"id": "auth_v2", "first": "接入 401 了，帮我排查", "second": "编号 req_auth_v2_01",
     "codes": ["missing_api_key_header"], "v2": True},
    {"id": "quota_minute", "first": "请求被限流了，怎么排查", "second": "编号 req_quota_02",
     "codes": ["minute_quota_exceeded"]},
    {"id": "quota_retry", "first": "遇到 429 了，帮我看看", "second": "编号 req_quota_20",
     "codes": ["minute_quota_exceeded"]},
    {"id": "webhook_401", "first": "Webhook 投递失败，帮我排查", "second": "编号 delivery_02",
     "codes": ["webhook_delivery_failed"]},
    {"id": "webhook_400", "first": "回调一直失败，查一下", "second": "编号 delivery_21",
     "codes": ["webhook_delivery_failed"]},
    {"id": "combo_quota", "first": "Webhook 投递有问题，帮我排查", "second": "编号 delivery_combo_01",
     "codes": ["webhook_delivery_failed", "minute_quota_exceeded"]},
    {"id": "combo_related_auth", "first": "投递失败，排查一下", "second": "编号 delivery_combo_20",
     "codes": ["webhook_delivery_failed", "expired_key"]},
    {"id": "unknown_then_fixed", "first": "报错编号 req_auth_99", "second": "正确的是 req_auth_01",
     "codes": ["expired_key"]},
    {"id": "multi_after_failure", "first": "请求返回 429，帮我查", "second": "正确的编号是 req_quota_01",
     "codes": ["minute_quota_exceeded"]},
]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sorted_sets = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def zadd(self, key, mapping):
        self.sorted_sets.setdefault(key, {}).update(mapping)

    async def zremrangebyrank(self, key, _start, _end):
        return None

    async def expire(self, _key, _ttl):
        return True

    async def zrevrange(self, key, start, end):
        entries = sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: item[1], reverse=True)
        return [member for member, _ in entries[start:end + 1]]


class WorkingMemory:
    """Minimal working-memory implementation shared by both turns of one task."""

    def __init__(self):
        self.messages = {}

    async def get_context(self, user_id, conversation_id, query=""):
        recent = self.messages.get((user_id, conversation_id), [])
        return SimpleNamespace(
            recent_messages=[SimpleNamespace(role=role, content=content) for role, content in recent],
            relevant_history=[], summary="", user_profile={},
            to_prompt_text=lambda: "\n".join(content for _, content in recent),
        )

    async def add_message(self, user_id, conversation_id, role, content):
        self.messages.setdefault((user_id, conversation_id), []).append((role, content))

    async def update_profile(self, _user_id, _conversation_id):
        return None


def new_assistant(redis, memory):
    from developer.assistant import DeveloperAssistant, RuleDecisionModel
    from mcp.tool_manager import MCPToolManager

    class VersionedKnowledge:
        async def search_async(self, query, top_k=4):
            from developer.data import DOCS
            version = "v2" if query.lstrip().lower().startswith("v2") else "v1"
            return [{"title": doc["title"], "content": doc["content"], "score": 1.0}
                    for doc in DOCS if doc["title"].startswith(version + " /")][:top_k]

    return DeveloperAssistant(
        memory=memory, knowledge=VersionedKnowledge(), redis_client=redis,
        model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="evaluation-only"),
        sandbox_url="http://sandbox",
    )


def assess(task, first, second, mode):
    hypotheses = {item.get("code"): item for item in (second.get("diagnosis") or {}).get("hypotheses") or []}
    missing = [code for code in task["codes"] if code not in hypotheses or not hypotheses[code].get("evidence")]
    reasons = []
    if second.get("plan", {}).get("status") != "complete":
        reasons.append("second_turn_incomplete")
    if missing:
        reasons.append("missing_diagnosis:" + ",".join(missing))
    if task.get("conflict") and not (second.get("diagnosis") or {}).get("conflicts"):
        reasons.append("conflict_not_reported")
    if task.get("v2") and not any("doc:v2 /" in str(item) for item in second.get("evidence") or []):
        reasons.append("v2_document_not_used")
    first_actions = first.get("actions") or []
    returned_actions = second.get("actions") or []
    if mode == "resume" and first_actions:
        prefix = [action.get("source") for action in returned_actions[:len(first_actions)]]
        if prefix == [action.get("source") for action in first_actions]:
            # The resumed response returns the accumulated plan state; only the tail is this turn.
            returned_actions = returned_actions[len(first_actions):]
    second_actions = returned_actions
    all_actions = first_actions + second_actions
    docs = sum(1 for action in all_actions if action.get("tool") in {"search_api_docs", "search_quota_docs"})
    second_docs = sum(1 for action in second_actions
                      if action.get("tool") in {"search_api_docs", "search_quota_docs"})
    resumed = any(event.get("type") == "resume" for event in second.get("timeline") or [])
    return {
        "success": not reasons, "reasons": reasons,
        "total_tool_calls": len(all_actions), "document_calls": docs,
        "second_turn_tool_calls": len(second_actions),
        "second_turn_document_calls": second_docs, "resumed": resumed,
        "second_plan_status": second.get("plan", {}).get("status"),
    }


async def run_mode(tasks, mode):
    from developer.evaluate_planning import SandboxClient

    rows = []
    with patch("developer.assistant.httpx.AsyncClient", SandboxClient):
        for task in tasks:
            started = time.perf_counter()
            redis, memory = FakeRedis(), WorkingMemory()
            assistant = new_assistant(redis, memory)
            user = f"memory-eval-{task['id']}"
            first = await assistant.run(task["first"], user, None)
            second_conversation = first["conversation_id"] if mode == "resume" else str(uuid.uuid4())
            second = await assistant.run(task["second"], user, second_conversation)
            row = assess(task, first, second, mode)
            row.update({"id": task["id"], "mode": mode,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
            rows.append(row)
    return rows


def summarize(rows):
    def mean(key):
        return round(sum(row[key] for row in rows) / len(rows), 2) if rows else None

    return {
        "count": len(rows),
        "success": sum(int(row["success"]) for row in rows),
        "mean_total_tool_calls": mean("total_tool_calls"),
        "mean_document_calls": mean("document_calls"),
        "mean_second_turn_document_calls": mean("second_turn_document_calls"),
        "resumed": sum(int(row["resumed"]) for row in rows),
        "mean_elapsed_ms": mean("elapsed_ms"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    resume_rows = asyncio.run(run_mode(TASKS, "resume"))
    fresh_rows = asyncio.run(run_mode(TASKS, "fresh"))
    resume, fresh = summarize(resume_rows), summarize(fresh_rows)
    report = {
        "mode": "rules-in-process",
        "tasks": len(TASKS),
        "resume": resume,
        "fresh": fresh,
        "delta": {
            "success_delta": resume["success"] - fresh["success"],
            "tool_call_reduction": round(fresh["mean_total_tool_calls"] - resume["mean_total_tool_calls"], 2),
            "second_turn_document_call_reduction": round(
                fresh["mean_second_turn_document_calls"] - resume["mean_second_turn_document_calls"], 2),
        },
        "cases": {"resume": resume_rows, "fresh": fresh_rows},
        "limitations": (
            "Rules mode with an in-process working-memory stub, fake Redis and the ASGI mock platform. "
            "It measures conversation/plan reuse, not ChromaDB episodic retrieval quality, model behaviour, "
            "or production latency; 12 hand-written two-turn tasks are a small sample."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"resume": resume, "fresh": fresh, "delta": report["delta"]}, ensure_ascii=False))
    if resume["success"] != resume["count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
