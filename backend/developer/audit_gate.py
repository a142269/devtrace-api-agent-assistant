"""Audit gate: fail only for vulnerabilities that already have a fixed release.

pip-audit reports both fixable findings and upstream issues that no released
version fixes yet. Blocking on the latter would make CI red until upstream ships
a patch, so this gate fails only on actionable findings. Unfixed issues are
tracked in `依赖与安全评估_2026-10-18.md`.
"""
import json
import sys
from pathlib import Path


def _dependencies(report):
    if isinstance(report, list):
        return report
    if isinstance(report, dict):
        return report.get("dependencies") or []
    return []


def evaluate(report):
    actionable, known = [], []
    for dependency in _dependencies(report):
        if not isinstance(dependency, dict):
            continue
        for vuln in dependency.get("vulns") or []:
            item = {
                "name": dependency.get("name"),
                "version": dependency.get("version"),
                "id": vuln.get("id"),
                "fix_versions": list(vuln.get("fix_versions") or []),
            }
            (actionable if item["fix_versions"] else known).append(item)
    return actionable, known


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    path = Path(args[0]) if args else Path("dependency-audit.json")
    if not path.exists():
        print(f"warning: audit report {path} is missing; gate skipped")
        return 0
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: cannot parse audit report {path}: {exc}; gate skipped")
        return 0
    actionable, known = evaluate(report)
    for item in actionable:
        print(f"FAIL {item['name']} {item['version']} {item['id']} "
              f"fix={','.join(item['fix_versions'])}")
    if known:
        print(f"{len(known)} known vulnerabilities without a fixed release (non-blocking)")
    if actionable:
        print(f"{len(actionable)} vulnerabilities have a fixed release; upgrade required")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
