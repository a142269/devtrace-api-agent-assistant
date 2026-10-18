"""Prompt version and regression checks.

The snapshot stores the SHA-256 of each rendered prompt. If a prompt changes
without an explicit version bump and snapshot update, CI fails so the change is
reviewed rather than silently shipped.
"""
import argparse
import hashlib
import json
from pathlib import Path

from developer.prompts import PROMPT_VERSION, PROMPTS, render

SNAPSHOT_PATH = Path(__file__).resolve().parent / "prompt_snapshots.json"
FIXTURES = {
    "system": {"skill": "<skill block>"},
    "plan": {"message": "请求 req_auth_01 返回 401", "memory": "最近消息：无"},
    "action": {"state": '{"kind": "auth", "ready_steps": [{"id": "s1"}]}', "message": "继续"},
    "native_action": {"state": '{"kind": "auth", "ready_steps": [{"id": "s1"}]}', "message": "继续"},
    "revise": {"failed_tool": "get_request_trace", "state": '{"status": "running"}'},
}
CONTRACTS = {
    "system": ["不可信数据", "批准的计划", "只输出 JSON"],
    "plan": ["kind=", "public_api", "不要等待 request_id", "kind\":..."],
    "action": ["ready_steps", "step_id", "不要伪造标识符"],
    "native_action": ["只能使用提供的工具", "未就绪步骤"],
    "revise": ["不能重复", "ask_user"],
}


def fingerprints() -> dict:
    return {name: hashlib.sha256(render(name, **FIXTURES[name]).encode()).hexdigest()
            for name in PROMPTS}


def load_snapshot() -> dict:
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def evaluate() -> dict:
    snapshot = load_snapshot()
    hashes = fingerprints()
    checks = []
    for name in PROMPTS:
        rendered = render(name, **FIXTURES[name])
        checks.append({"name": name, "rendered": True, "chars": len(rendered),
                       "snapshot_match": snapshot.get("hashes", {}).get(name) == hashes[name]})
        for needle in CONTRACTS[name]:
            checks[-1].setdefault("contracts", {})[needle] = needle in rendered
    passed = (bool(PROMPT_VERSION) and set(snapshot.get("hashes", {})) == set(PROMPTS)
              and all(item["snapshot_match"] for item in checks)
              and all(all(item["contracts"].values()) for item in checks))
    return {"version": PROMPT_VERSION, "snapshot_version": snapshot.get("version"),
            "hashes": hashes, "checks": checks, "passed": bool(passed),
            "required_contracts": CONTRACTS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--update", action="store_true", help="rewrite the prompt snapshot")
    args = parser.parse_args()
    if args.update:
        SNAPSHOT_PATH.write_text(json.dumps(
            {"version": PROMPT_VERSION, "hashes": fingerprints()}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    report = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"version": report["version"], "passed": report["passed"]}, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
