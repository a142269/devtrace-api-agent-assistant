"""Run the independent holdout set in rules mode (in-process) or model mode (HTTP).

The case labels are hand-written against fixtures that were added together with
this set and never used to tune the agent. It is a *second public holdout*, not a
hidden third-party benchmark.

Examples:
  python -m developer.evaluate_holdout --mode rules --output developer/eval_holdout_rules_2026-10-18.json
  DEVTRACE_EVAL_TOKEN=<token> python -m developer.evaluate_holdout --mode model \\
      --base-url http://localhost:8000 --output developer/eval_holdout_model_2026-10-18.json
  python -m developer.evaluate_holdout --mode rescore --source developer/eval_holdout_model_2026-10-22_full40.json \\
      --output developer/eval_holdout_model_2026-10-22_full40_base_names.json

Rescore is offline only: it re-judges a stored model report under base tool names
(the MCP transport prefix stripped onto registered local tools) and never calls a
model or rewrites the official score next to it.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

CASES_PATH = Path(__file__).parent / "eval_holdout_cases.json"
# Cases written in 2026-10-18 carry no batch field; the 2026-10-21 expansion marks
# itself so both batches stay countable in one report.
ORIGINAL_BATCH = "2026-10-18"
# Tool identity is transport-independent, but the labels were written with the local
# spelling. Only this prefix is ever stripped, and only onto a registered local tool.
MCP_TRANSPORT_PREFIX = "mcp_"
_STEP_REASON = re.compile(r"^step\d+:")
_KNOWN_TOOLS = None
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


def filter_batch(cases, batch):
    """Select one label batch; an unknown batch is an error, not an empty run."""
    if not batch:
        return cases
    selected = [case for case in cases if case.get("batch", ORIGINAL_BATCH) == batch]
    if not selected:
        raise ValueError(f"no holdout cases in batch {batch!r}")
    return selected


def known_local_tool_names():
    """Base names this service can execute locally, straight from the planner registry."""
    global _KNOWN_TOOLS
    if _KNOWN_TOOLS is None:
        from developer.planner import TOOL_SCHEMAS
        _KNOWN_TOOLS = frozenset(TOOL_SCHEMAS)
    return _KNOWN_TOOLS


def normalize_tool_name(name):
    """Strip the MCP transport prefix, but only onto a real local tool name.

    Anything else - a local spelling, an unknown or external name, a non-string, a
    differently cased prefix, stray whitespace - is returned untouched, so
    normalization can never invent a tool that was never called.
    """
    if not isinstance(name, str) or not name.startswith(MCP_TRANSPORT_PREFIX):
        return name
    base = name[len(MCP_TRANSPORT_PREFIX):]
    return base if base and base in known_local_tool_names() else name


def assess(case_id, group, response, expect, normalize_tools=False):
    reasons = []
    plan = response.get("plan") or {}
    actions = response.get("actions") or []
    tools = [action.get("tool") for action in actions]
    # Reported as called; normalization only decides how tool expectations are compared.
    compared = [normalize_tool_name(tool) for tool in tools] if normalize_tools else tools
    evidence = [str(item) for item in (response.get("evidence") or [])]
    diagnosis = response.get("diagnosis") or {}
    hypotheses = {item.get("code"): item for item in (diagnosis.get("hypotheses") or [])}
    answer = response.get("answer") or ""
    security = response.get("security") or {}

    if "plan_status" in expect and plan.get("status") != expect["plan_status"]:
        reasons.append(f"plan_status={plan.get('status')}")
    for tool in expect.get("tools_include") or []:
        if tool not in compared:
            reasons.append(f"missing_tool:{tool}")
    for tool in expect.get("tools_exclude") or []:
        if tool in compared:
            reasons.append(f"unexpected_tool:{tool}")
    if expect.get("tools_first") and (not compared or compared[0] != expect["tools_first"]):
        reasons.append(f"first_tool={compared[:1]}")
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
    row = {
        "id": case_id, "group": group, "passed": not reasons, "reasons": reasons,
        "decision_mode": response.get("decision_mode"),
        "tools": tools, "hypotheses": sorted(hypotheses),
        "ticket": response.get("ticket_id"),
        "injection": bool(security.get("injection_suspected")),
    }
    if normalize_tools:
        row["tools_base"] = compared
    return row


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


TOOL_REASONS = ("missing_tool:", "unexpected_tool:", "first_tool=")


def tool_reasons(tools, expect):
    """The tool-name half of assess(), so a stored run can be re-judged offline."""
    reasons = []
    for tool in expect.get("tools_include") or []:
        if tool not in tools:
            reasons.append(f"missing_tool:{tool}")
    for tool in expect.get("tools_exclude") or []:
        if tool in tools:
            reasons.append(f"unexpected_tool:{tool}")
    if expect.get("tools_first") and (not tools or tools[0] != expect["tools_first"]):
        reasons.append(f"first_tool={tools[:1]}")
    return reasons


def rescore_row(row):
    """Re-judge one stored case result under base tool names without calling anything.

    Only tool-name expectations are re-evaluated; every other recorded failure reason
    is carried over verbatim, so this can re-attribute a label binding but can never
    talk a genuine evidence or answer failure into a pass.
    """
    tools = list(row.get("tools") or [])
    base_tools = [normalize_tool_name(tool) for tool in tools]
    official = list(row.get("reasons") or [])
    result = {
        "id": row.get("id"), "group": row.get("group"),
        "batch": row.get("batch", ORIGINAL_BATCH),
        "decision_mode": row.get("decision_mode"),
        "official_passed": bool(row.get("passed")), "base_names_passed": bool(row.get("passed")),
        "rescored": False, "changed": False, "tools": tools, "tools_base": base_tools,
        "official_reasons": official, "base_names_reasons": official,
        "binding_fixed": [], "still_failed_reasons": official,
    }
    expects = row.get("expected") or []
    if any(_STEP_REASON.match(reason) for reason in official) or "#" in str(row.get("id") or ""):
        result["why_not_rescored"] = (
            "multi-step case: the merged record keeps only the last step's tools")
        return result
    if len(expects) != 1:
        result["why_not_rescored"] = f"stored report has {len(expects)} expectation sets"
        return result
    expect = expects[0]
    official_tool = [reason for reason in official if reason.startswith(TOOL_REASONS)]
    other = [reason for reason in official if not reason.startswith(TOOL_REASONS)]
    if sorted(official_tool) != sorted(tool_reasons(tools, expect)):
        result["why_not_rescored"] = (
            "stored reasons do not reproduce from the stored tools and expectations")
        return result
    base_tool = tool_reasons(base_tools, expect)
    base_reasons = other + base_tool
    result.update({
        "rescored": True,
        "base_names_passed": not base_reasons,
        "base_names_reasons": base_reasons,
        "binding_fixed": [reason for reason in official_tool if reason not in base_tool],
        "still_failed_reasons": base_reasons,
        "new_tool_reasons": [reason for reason in base_tool if reason not in official_tool],
    })
    result["changed"] = result["base_names_passed"] != result["official_passed"]
    return result


def rescore_report(official_report):
    """Split view: the untouched official numbers next to the base-name re-attribution."""
    rows = [rescore_row(row) for row in official_report.get("cases") or []]
    base_rows = [{**row, "passed": row["base_names_passed"],
                  "reasons": row["base_names_reasons"]} for row in rows]
    summary = summarize(base_rows)
    return {
        "mode": "rescore",
        "source_report": official_report.get("source_report") or "eval_holdout_model_2026-10-22_full40.json",
        "rescored_at": "2026-10-22",
        "labels": official_report.get("labels"),
        "official": {
            "summary": official_report.get("summary"),
            "note": ("verbatim from the stored run - not recomputed, not rewritten, still the "
                     "project's reported score"),
        },
        "base_names": {
            "summary": summary,
            "changed_cases": [row for row in rows if row.get("changed")],
            "still_failing": [row for row in rows
                              if not row["official_passed"] and not row["base_names_passed"]],
            "not_rescored": [{"id": row["id"], "why": row.get("why_not_rescored")}
                             for row in rows if not row["rescored"]],
        },
        "cases": rows,
        "normalization": {
            "prefix_stripped": MCP_TRANSPORT_PREFIX,
            "target_registry": "developer.planner.TOOL_SCHEMAS",
            "unknown_names": "returned unchanged",
            "default_assess_behaviour": "unchanged - assess() only normalizes when normalize_tools=True",
            "tool_reasons_recomputed": list(TOOL_REASONS),
            "other_reasons": "carried over verbatim, never re-judged",
        },
        "cost": {"model_calls": 0, "http_requests": 0,
                 "note": "offline recompute from the stored observed tools"},
        "limitations": (
            "A supplementary attribution, not a second score to publish: it re-reads the same "
            "stored run through one lens (tool identity ignores the MCP transport prefix). "
            "Non-tool failures are untouched, multi-step rows cannot be re-attributed offline, "
            "and a base-name pass proves the label matched, not that the agent improved."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["rules", "model", "rescore"], required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token-env", default="DEVTRACE_EVAL_TOKEN")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=None,
                        help="rescore mode: a stored model-mode report to re-judge offline")
    parser.add_argument("--batch", default="",
                        help="only run one label batch (2026-10-18 or 2026-10-21); empty runs all")
    args = parser.parse_args()

    if args.mode == "rescore":
        if not args.source:
            parser.error("rescore mode needs --source with the stored official report")
        report = rescore_report(json.loads(args.source.read_text(encoding="utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"official": report["official"]["summary"],
                          "base_names": report["base_names"]["summary"]}, ensure_ascii=False))
        if report["base_names"]["summary"]["passed"] != report["base_names"]["summary"]["count"]:
            raise SystemExit(1)
        return

    cases = load_cases()
    if args.batch:
        try:
            cases = filter_batch(cases, args.batch)
        except ValueError as error:
            parser.error(str(error))
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
        "batch_filter": args.batch or None,
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
