"""Run the fixed developer cases against a live assistant endpoint.

Example: python -m developer.evaluate --base-url http://localhost:8000 --mode rules
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import httpx

EVAL_CASES = json.loads((Path(__file__).parent / 'eval_baseline_cases.json').read_text(encoding='utf-8'))


def assess(case: dict, response: dict, expected_mode: str) -> dict:
    actions = response.get("actions") or []
    plan = response.get("plan") or {}
    actual_tools = [action.get("tool") for action in actions]
    evidence = response.get("evidence") or []
    reasons = []
    if plan.get("status") != "complete":
        reasons.append("plan_incomplete")
    expected_code = case.get('expected_cause')
    hypotheses = (response.get('diagnosis') or {}).get('hypotheses') or []
    observed_sources = {item.get('source') for item in actions if item.get('success')}
    supported = bool(hypotheses) and all(h.get('evidence') and set(h['evidence']).issubset(observed_sources) for h in hypotheses)
    diagnosis_correct = (any(h.get('code') == expected_code and h.get('evidence') for h in hypotheses)
                         if expected_code else None)
    if expected_code and not diagnosis_correct:
        reasons.append('missing_supported_diagnosis')
    if not supported:
        reasons.append('unsupported_diagnosis')
    if len(actual_tools) != len(set(actual_tools)):
        reasons.append('duplicate_tool_call')
    if not all(action.get("success") for action in actions):
        reasons.append("tool_failed")
    if not any(case["identifier"] in str(source) for source in evidence):
        reasons.append("missing_record_evidence")
    if not any(str(source).startswith("doc:v1 /") for source in evidence):
        reasons.append("missing_doc_evidence")
    if "模拟平台" not in response.get("answer", ""):
        reasons.append("missing_mock_disclosure")
    if response.get("decision_mode") != expected_mode:
        reasons.append("decision_mode_mismatch")
    if ("inspect_key_scope" in actual_tools and "get_request_trace" in actual_tools
            and actual_tools.index("inspect_key_scope") < actual_tools.index("get_request_trace")):
        reasons.append("dependency_violation")
    return {
        "case": case["identifier"], "family": case["family"],
        "passed": not reasons, "reasons": reasons,
        "decision_mode": response.get("decision_mode"),
        "tool_order": actual_tools, "tool_actions": len(actions),
        "plan_adjustments": plan.get("adjustments") or [],
        'diagnosis_correct': diagnosis_correct, 'evidence_supported': supported,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--mode", choices=["rules", "model"], required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=300,
                        help="Per-request timeout in seconds; model calls can be slow")
    parser.add_argument("--sample-per-family", type=int, default=0,
                        help="Take the first N cases of each fault family before applying --limit")
    parser.add_argument("--output", type=Path)
    parser.add_argument('--token-env', default='DEVTRACE_EVAL_TOKEN', help='Environment variable containing a bearer token')
    args = parser.parse_args()
    token = os.getenv(args.token_env, '')
    if not token:
        parser.error(f'Set {args.token_env} to an access token; do not put it in the command line')
    source = EVAL_CASES
    if args.sample_per_family > 0:
        source = []
        for family in ("auth", "quota", "webhook"):
            source.extend([item for item in EVAL_CASES if item["family"] == family][:args.sample_per_family])
    cases = source[:max(0, min(args.limit, len(source)))]
    results = []
    with httpx.Client(base_url=args.base_url, timeout=args.timeout, trust_env=False,
                      headers={'Authorization': f'Bearer {token}'}) as client:
        for index, case in enumerate(cases):
            started = time.perf_counter()
            try:
                response = client.post("/developer/chat", json={
                    "message": case["message"],
                })
                response.raise_for_status()
                result = assess(case, response.json(), args.mode)
            except (httpx.HTTPError, ValueError) as exc:
                result = {"case": case["identifier"], "family": case["family"],
                          "passed": False, "reasons": ["request_error"], "error": str(exc)[:160]}
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            results.append(result)
            print(f"{result['case']}: {'PASS' if result['passed'] else 'FAIL'} ({result['elapsed_ms']} ms)")
    report = {
        "mode": args.mode, "base_url": args.base_url, "count": len(results),
        "passed": sum(result["passed"] for result in results),
        "fallbacks": sum(result.get("decision_mode") == "rules_fallback" for result in results),
        'diagnosis_correct': sum(result.get('diagnosis_correct') is True for result in results),
        'diagnosis_applicable': sum(result.get('diagnosis_correct') is not None for result in results),
        'evidence_supported': sum(result.get('evidence_supported') is True for result in results),
        'mean_tool_calls': round(statistics.mean(result.get('tool_actions', 0) for result in results), 2) if results else None,
        "median_elapsed_ms": round(statistics.median(result["elapsed_ms"] for result in results), 1) if results else None,
        "cases": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    if report["passed"] != report["count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
