"""Run the independent holdout set in rules mode (in-process) or model mode (HTTP).

The case labels are hand-written against fixtures that were added together with
this set and never used to tune the agent. It is a *second public holdout*, not a
hidden third-party benchmark.

Examples:
  python -m developer.evaluate_holdout --mode rules --output developer/eval_holdout_rules_2026-10-18.json
  DEVTRACE_EVAL_TOKEN=<token> python -m developer.evaluate_holdout --mode model \\
      --base-url http://localhost:8000 --output developer/eval_holdout_model_2026-10-18.json
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

CASES_PATH = Path(__file__).parent / "eval_holdout_cases.json"
# Cases written in 2026-10-18 carry no batch field; the 2026-10-21 expansion marks
# itself so both batches stay countable in one report.
ORIGINAL_BATCH = "2026-10-18"
HYPOTHESIS_CODES = {
    "expired_key", "missing_scope", "malformed_header", "missing_api_key_header",
    "minute_quota_exceeded", "webhook_delivery_failed",
}
EXPECT_KEYS = {
    "plan_status", "tools_include", "tools_exclude", "tools_first", "hypotheses",
    "hypothesis_scope", "conflicts_min", "injection", "ticket", "replans",
    "hypotheses_empty", "answer_contains", "answer_not_contains",
    "evidence_contains", "evidence_not_contains", "tainted_action",
}


def load_cases():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    seen = set()
    for case in cases:
        case_id = case.get("id")
        if not case_id or case_id in seen:
            raise ValueError(f"holdout case id missing or duplicated: {case_id!r}")
        seen.add(case_id)
        if not case.get("group"):
            raise ValueError(f"holdout case {case_id} has no group")
        if ("message" in case) == ("steps" in case):
            raise ValueError(f"holdout case {case_id} needs either message or steps")
        steps = case.get("steps") or [{"message": case.get("message"), "expect": case.get("expect")}]
        for step in steps:
            if not step.get("message"):
                raise ValueError(f"holdout case {case_id} has an empty message")
            expect = step.get("expect")
            if not isinstance(expect, dict):
                raise ValueError(f"holdout case {case_id} has no expect object")
            unknown = set(expect) - EXPECT_KEYS
            if unknown:
                raise ValueError(f"holdout case {case_id} has unknown expectations: {sorted(unknown)}")
            for code in (expect.get("hypotheses") or {}):
                if code not in HYPOTHESIS_CODES:
                    raise ValueError(f"holdout case {case_id} references unknown hypothesis {code}")
            for code in (expect.get("hypothesis_scope") or {}):
                if code not in HYPOTHESIS_CODES:
                    raise ValueError(f"holdout case {case_id} references unknown hypothesis {code}")
    return cases


def assess(case_id, group, response, expect):
    reasons = []
    plan = response.get("plan") or {}
    actions = response.get("actions") or []
    tools = [action.get("tool") for action in actions]
    evidence = [str(item) for item in (response.get("evidence") or [])]
    diagnosis = response.get("diagnosis") or {}
    hypotheses = {item.get("code"): item for item in (diagnosis.get("hypotheses") or [])}
    answer = response.get("answer") or ""
    security = response.get("security") or {}

    if "plan_status" in expect and plan.get("status") != expect["plan_status"]:
        reasons.append(f"plan_status={plan.get('status')}")
    for tool in expect.get("tools_include") or []:
        if tool not in tools:
            reasons.append(f"missing_tool:{tool}")
    for tool in expect.get("tools_exclude") or []:
        if tool in tools:
            reasons.append(f"unexpected_tool:{tool}")
    if expect.get("tools_first") and (not tools or tools[0] != expect["tools_first"]):
        reasons.append(f"first_tool={tools[:1]}")
    for code, status in (expect.get("hypotheses") or {}).items():
        hypothesis = hypotheses.get(code)
        if not hypothesis:
            reasons.append(f"missing_hypothesis:{code}")
            continue
        if hypothesis.get("status") != status:
            reasons.append(f"hypothesis_status:{code}={hypothesis.get('status')}")
        if not hypothesis.get("evidence"):
            reasons.append(f"hypothesis_without_evidence:{code}")
    for code, scope in (expect.get("hypothesis_scope") or {}).items():
        hypothesis = hypotheses.get(code)
        if hypothesis and hypothesis.get("scope") != scope:
            reasons.append(f"hypothesis_scope:{code}={hypothesis.get('scope')}")
    if "conflicts_min" in expect and len(diagnosis.get("conflicts") or []) < expect["conflicts_min"]:
        reasons.append("conflict_not_reported")
    if "injection" in expect and bool(security.get("injection_suspected")) != expect["injection"]:
        reasons.append("injection_flag_mismatch")
    if "tainted_action" in expect and any(bool(action.get("tainted")) for action in actions) != expect["tainted_action"]:
        reasons.append("tainted_action_mismatch")
    if "ticket" in expect and bool(response.get("ticket_id")) != expect["ticket"]:
        reasons.append(f"ticket={response.get('ticket_id')}")
    if "replans" in expect and (plan.get("replans") or 0) != expect["replans"]:
        reasons.append(f"replans={plan.get('replans')}")
    if expect.get("hypotheses_empty") and hypotheses:
        reasons.append("unexpected_hypotheses")
    for text in expect.get("answer_contains") or []:
        if text not in answer:
            reasons.append(f"answer_missing:{text}")
    for text in expect.get("answer_not_contains") or []:
        if text in answer:
            reasons.append(f"answer_forbidden:{text}")
    for fragment in expect.get("evidence_contains") or []:
        if not any(fragment in item for item in evidence):
            reasons.append(f"evidence_missing:{fragment}")
    for fragment in expect.get("evidence_not_contains") or []:
        if any(fragment in item for item in evidence):
            reasons.append(f"evidence_forbidden:{fragment}")
    return {
        "id": case_id, "group": group, "passed": not reasons, "reasons": reasons,
        "decision_mode": response.get("decision_mode"),
        "tools": tools, "hypotheses": sorted(hypotheses),
        "ticket": response.get("ticket_id"),
        "injection": bool(security.get("injection_suspected")),
    }


async def run_case(case, send):
    conversation = None
    rows = []
    for index, step in enumerate(case.get("steps") or [{"message": case["message"], "expect": case["expect"]}]):
        response = await send(step["message"], conversation)
        conversation = response.get("conversation_id") or conversation
        row = assess(case["id"], case["group"], response, step["expect"])
        if len(case.get("steps") or []) > 1:
            row["id"] = f"{case['id']}#{index + 1}"
            row["reasons"] = [f"step{index + 1}:{reason}" for reason in row["reasons"]]
        rows.append(row)
    merged = dict(rows[-1])
    merged["passed"] = all(row["passed"] for row in rows)
    merged["reasons"] = [reason for row in rows for reason in row["reasons"]]
    merged["batch"] = case.get("batch", ORIGINAL_BATCH)
    return merged


async def rules_run(cases):
    from unittest.mock import patch

    from developer.assistant import DeveloperAssistant, RuleDecisionModel
    from developer.data import DOCS
    from developer.evaluate_planning import Memory, Redis, SandboxClient
    from developer.public_knowledge import PublicDocsKnowledge
    from mcp.tool_manager import MCPToolManager

    class HoldoutKnowledge:
        """Version-aware stub; evaluate_planning's stub only returns the first v1 docs."""

        async def search_async(self, query, top_k=4):
            version = "v2" if query.lstrip().lower().startswith("v2") else "v1"
            return [{"title": doc["title"], "content": doc["content"], "score": 1.0}
                    for doc in DOCS if doc["title"].startswith(version + " /")][:top_k]

    rows = []
    with patch("developer.assistant.httpx.AsyncClient", SandboxClient):
        for case in cases:
            assistant = DeveloperAssistant(
                memory=Memory(), knowledge=HoldoutKnowledge(), redis_client=Redis(),
                model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key="evaluation-only"),
                sandbox_url="http://sandbox",
                # The public_api path needs the offline attributed corpus; without it
                # search_public_docs raises and every public case stalls in waiting.
                public_knowledge=PublicDocsKnowledge(),
            )

            async def send(message, conversation, assistant=assistant, case=case):
                return await assistant.run(message, f"holdout-{case['id']}", conversation)

            rows.append(await run_case(case, send))
    return rows


async def http_run(cases, base_url, token):
    rows = []
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, timeout=300) as client:
        for case in cases:
            async def send(message, conversation, case=case):
                payload = {"message": message}
                if conversation:
                    payload["conversation_id"] = conversation
                response = await client.post("/developer/chat", headers=headers, json=payload)
                response.raise_for_status()
                return response.json()

            rows.append(await run_case(case, send))
    return rows


def summarize(rows):
    def bucket(key):
        result = {}
        for row in rows:
            name = row.get(key) or "unknown"
            entry = result.setdefault(name, {"count": 0, "passed": 0})
            entry["count"] += 1
            entry["passed"] += int(row["passed"])
        return result

    return {
        "count": len(rows), "passed": sum(int(row["passed"]) for row in rows),
        "by_group": bucket("group"), "by_decision_mode": bucket("decision_mode"),
        "by_batch": bucket("batch"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["rules", "model"], required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token-env", default="DEVTRACE_EVAL_TOKEN")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    cases = load_cases()
    if args.mode == "rules":
        rows = asyncio.run(rules_run(cases))
    else:
        token = os.getenv(args.token_env, "")
        if not token:
            parser.error(f"Set {args.token_env} to an access token; do not put it on the command line")
        rows = asyncio.run(http_run(cases, args.base_url, token))

    summary = summarize(rows)
    report = {
        "mode": args.mode,
        "base_url": args.base_url if args.mode == "model" else None,
        "holdout_version": "2026-10-18 (19 cases) + 2026-10-21 expansion (21 cases)",
        "labels": "hand-written against dedicated fixtures; not used to tune the agent",
        "summary": summary,
        "cases": rows,
        "limitations": (
            "Second public holdout set: new fixtures and hand-written labels, but still written by "
            "the project author, not an independent third-party benchmark. Rules results do not "
            "predict model quality; model results are a small sample from one provider."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    if summary["passed"] != summary["count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
