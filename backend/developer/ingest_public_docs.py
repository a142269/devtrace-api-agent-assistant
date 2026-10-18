"""Import public API documentation files into the public-docs JSON corpus.

Supported inputs:
  - OpenAPI 3.x JSON or YAML: every operation becomes one attributed document.
  - Markdown: split on level-2 headings (## ...).

Example:
  python -m developer.ingest_public_docs --source petstore.json --api Petstore \
      --source-url https://petstore.swagger.io/ --license "Apache-2.0" \
      --output developer/public_docs_petstore.json --max-operations 40
"""
import argparse
import json
import re
from pathlib import Path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _load_spec(path: Path):
    text = _read_text(path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SystemExit("YAML input needs PyYAML; convert the spec to JSON instead") from exc
        return yaml.safe_load(text)
    return json.loads(text)


def operations_from_openapi(spec, api, source_url, license_text, max_operations=100, filters=None):
    filters = [item.strip().lower() for item in (filters or []) if item.strip()]
    title = (spec.get("info") or {}).get("title") or f"{api} API"
    version = str((spec.get("info") or {}).get("version") or "")
    documents = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId") or f"{method.upper()} {path}"
            summary = operation.get("summary") or operation.get("description") or ""
            tags = [str(tag) for tag in (operation.get("tags") or [])]
            haystack = f"{path} {operation_id} {summary} {' '.join(tags)}".lower()
            if filters and not any(word in haystack for word in filters):
                continue
            parameters = []
            for parameter in (operation.get("parameters") or [])[:6]:
                if isinstance(parameter, dict):
                    parameters.append(f"{parameter.get('name')}({parameter.get('in')})")
            responses = []
            for code, response in list((operation.get("responses") or {}).items())[:6]:
                description = (response or {}).get("description") if isinstance(response, dict) else ""
                responses.append(f"{code} {description or ''}".strip())
            content = " ".join(part for part in [
                f"{title} {version}：{method.upper()} {path}",
                summary,
                f"参数：{', '.join(parameters)}" if parameters else "",
                f"响应：{'; '.join(responses)}" if responses else "",
            ] if part)
            documents.append({
                "id": re.sub(r"[^A-Za-z0-9_.-]+", "-", operation_id)[:80],
                "api": api,
                "title": f"{api}：{method.upper()} {path}",
                "content": content[:900],
                "source_url": source_url,
                "license": license_text,
                "updated": "imported",
                "tags": [method.lower()] + tags[:4],
            })
            if len(documents) >= max_operations:
                return documents
    return documents


def documents_from_markdown(text, api, source_url, license_text):
    sections = re.split(r"(?m)^##\s+", text)
    documents = []
    for section in sections:
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0][:120]
        body = " ".join(lines[1:])[:900]
        if not body:
            continue
        documents.append({
            "id": re.sub(r"[^A-Za-z0-9_.-]+", "-", title)[:80],
            "api": api,
            "title": f"{api}：{title}",
            "content": body,
            "source_url": source_url,
            "license": license_text,
            "updated": "imported",
            "tags": ["docs"],
        })
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--license", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-operations", type=int, default=100)
    parser.add_argument("--filter", default="",
                        help="comma-separated keywords; keep operations whose path/operationId/summary/tags match any")
    args = parser.parse_args()

    filters = [item for item in args.filter.split(",") if item.strip()]
    if args.source.suffix.lower() in {".md", ".markdown"}:
        documents = documents_from_markdown(_read_text(args.source), args.api,
                                            args.source_url, args.license)
    else:
        documents = operations_from_openapi(_load_spec(args.source), args.api, args.source_url,
                                            args.license, args.max_operations, filters)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"documents": len(documents), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
