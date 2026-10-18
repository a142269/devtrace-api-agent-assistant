"""Structured error-remediation playbooks for real developer platforms.

Each entry captures symptom words, the likely meaning, whether a retry can help,
the retry policy and concrete recovery steps, with an attributed source link.
Lookup uses the small-corpus BM25 scorer and supports API/status filters.
"""
import json
from pathlib import Path

from developer.retrieval import lexical_rank

PLAYBOOK_PATH = Path(__file__).resolve().parent / "error_playbooks.json"


def _load():
    try:
        data = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


PLAYBOOKS = _load()


def _searchable(entry):
    parts = [
        entry.get("title", ""), entry.get("meaning", ""), entry.get("error_code", ""),
        " ".join(entry.get("symptoms") or []), " ".join(entry.get("recovery") or []),
    ]
    return {**entry, "content": " ".join(part for part in parts if part)}


def lookup_playbook(symptom, api="", http_status=None, top_k=3):
    """Return matching playbooks, preferring entries for the same API and status."""
    candidates = [entry for entry in PLAYBOOKS
                  if (not api or entry.get("api") == api)
                  and (http_status is None or entry.get("http_status") == http_status)]
    if not candidates:
        candidates = [entry for entry in PLAYBOOKS if not api or entry.get("api") == api]
    if not candidates:
        candidates = PLAYBOOKS
    readable = [_searchable(entry) for entry in candidates]
    ranked = lexical_rank(symptom, readable)
    if not ranked:
        ranked = candidates
    if http_status is not None:
        exact = [entry for entry in ranked if entry.get("http_status") == http_status]
        ranked = exact + [entry for entry in ranked if entry not in exact]
    if api:
        preferred = [entry for entry in ranked if entry.get("api") == api]
        ranked = preferred + [entry for entry in ranked if entry not in preferred]
    return [{key: entry.get(key) for key in (
        "id", "api", "http_status", "error_code", "title", "meaning",
        "retryable", "retry_policy", "recovery", "source_url", "updated")}
        for entry in ranked[:top_k]]


def count():
    return len(PLAYBOOKS)
