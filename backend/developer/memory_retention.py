"""Episodic memory retention CLI.

ChromaDB episodic records are the one memory tier with no TTL: Redis working memory and
summaries expire through ``DEVTRACE_MEMORY_TTL_SECONDS``, but cross-session history stays
forever unless something removes it. This module is that explicit maintenance path.

Boundaries (deliberate, and asserted by tests):
  - only the episodic collection is opened; profile collections are refused outright,
    so a user profile can still only leave through the API's explicit delete;
  - ``--dry-run`` is the default: nothing is removed unless ``--apply`` is passed;
  - a record with no parsable ``ts`` is counted as skipped, never deleted — the CLI does
    not guess an age;
  - failures are readable and exit non-zero; they are never reported as success.

Usage:
    python -m developer.memory_retention --days 90
    python -m developer.memory_retention --days 90 --user alice --json
    python -m developer.memory_retention --days 90 --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger('developer.memory_retention')

MIN_DAYS = 1
DEFAULT_EPISODIC_COLLECTION = 'developer_episodic'
DELETE_BATCH_SIZE = 200
# Any collection whose name mentions profile is off-limits: cleaning episodic memory
# must never be able to remove a user profile as a side effect.
FORBIDDEN_COLLECTION_HINTS = ('profile',)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


class RetentionError(Exception):
    """Raised for anything that must stop the run with a readable message."""


def parse_days(raw: str) -> int:
    """``--days`` validator: positive integers only, so the cutoff can never be today+."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'--days 必须是整数，收到: {raw!r}') from None
    if value < MIN_DAYS:
        raise argparse.ArgumentTypeError(f'--days 必须 >= {MIN_DAYS}，收到: {value}')
    return value


def guard_collection(name: str) -> str:
    lowered = (name or '').lower()
    if any(hint in lowered for hint in FORBIDDEN_COLLECTION_HINTS):
        raise RetentionError(
            f'拒绝操作集合 {name!r}：本工具只清理情景记忆，用户画像只能显式通过 API 删除')
    return name


def _as_aware(value: datetime) -> datetime:
    """Chroma timestamps are written with local ``datetime.now().isoformat()``."""
    return value if value.tzinfo else value.astimezone()


def _cutoff(now: Optional[datetime], days: int) -> datetime:
    reference = now or datetime.now()
    return _as_aware(reference) - timedelta(days=days)


def parse_ts(value: Any) -> Optional[datetime]:
    """Return an aware timestamp, or None when the record carries no usable age."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return _as_aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def classify(records: List[Dict[str, Any]], *, cutoff: datetime,
             user: Optional[str] = None) -> Dict[str, Any]:
    """Split episodic rows into deletion candidates and everything that must stay."""
    cutoff = _as_aware(cutoff)  # callers may hand in a naive local cutoff
    candidates: List[str] = []
    by_user: Dict[str, int] = {}
    conversations: List[str] = []
    skipped_no_ts: List[str] = []
    out_of_scope = 0
    kept_fresh = 0

    for record in records:
        doc_id = str(record.get('id') or '')
        metadata = record.get('metadata') or {}
        if not doc_id:
            continue
        owner = str(metadata.get('user_id') or '')
        if user is not None and owner != user:
            out_of_scope += 1
            continue
        moment = parse_ts(metadata.get('ts'))
        if moment is None:
            skipped_no_ts.append(doc_id)
            continue
        if moment >= cutoff:
            kept_fresh += 1
            continue
        candidates.append(doc_id)
        by_user[owner or '(unknown)'] = by_user.get(owner or '(unknown)', 0) + 1
        conv = str(metadata.get('conv_id') or '')
        if conv and conv not in conversations:
            conversations.append(conv)

    return {
        'scanned': len(records),
        'candidates': candidates,
        'by_user': by_user,
        'conversations': conversations,
        'skipped_no_ts': skipped_no_ts,
        'kept_fresh': kept_fresh,
        'out_of_scope': out_of_scope,
    }


def fetch_all(collection: Any) -> List[Dict[str, Any]]:
    """Read every episodic row. A demo-scale collection is small; failure is explicit."""
    try:
        payload = collection.get(include=['metadatas'])
    except TypeError:      # older/newer Chroma builds differ on the include kwarg
        payload = collection.get()
    except Exception as exc:  # noqa: BLE001 - surfaced to the CLI as a readable failure
        raise RetentionError(f'读取情景记忆失败: {type(exc).__name__}: {exc}') from exc

    ids = list((payload or {}).get('ids') or [])
    metadatas = list((payload or {}).get('metadatas') or [])
    if len(metadatas) != len(ids):
        metadatas = [{} for _ in ids]
    return [{'id': doc_id, 'metadata': metadata or {}}
            for doc_id, metadata in zip(ids, metadatas)]


def delete_ids(collection: Any, doc_ids: List[str], *,
               batch_size: int = DELETE_BATCH_SIZE) -> int:
    """Delete in batches; a partial failure stops and reports how far it got."""
    deleted = 0
    for start in range(0, len(doc_ids), batch_size):
        batch = doc_ids[start:start + batch_size]
        try:
            collection.delete(ids=batch)
        except Exception as exc:  # noqa: BLE001 - never pretend a failure deleted nothing
            raise RetentionError(
                f'删除情景记忆失败（已删除 {deleted}/{len(doc_ids)}）: '
                f'{type(exc).__name__}: {exc}') from exc
        deleted += len(batch)
    return deleted


def run_retention(collection: Any, *, days: int, apply: bool = False,
                  user: Optional[str] = None, now: Optional[datetime] = None,
                  batch_size: int = DELETE_BATCH_SIZE) -> Dict[str, Any]:
    """Plan one retention pass; delete only when ``apply`` is true."""
    if days < MIN_DAYS:
        raise RetentionError(f'--days 必须 >= {MIN_DAYS}，收到: {days}')
    cutoff = _cutoff(now, days)
    plan = classify(fetch_all(collection), cutoff=cutoff, user=user)
    report: Dict[str, Any] = {
        'mode': 'apply' if apply else 'dry-run',
        'days': days,
        'cutoff': cutoff.isoformat(),
        'user': user,
        'scanned': plan['scanned'],
        'candidates': len(plan['candidates']),
        'candidate_ids': plan['candidates'],
        'by_user': plan['by_user'],
        'conversations': plan['conversations'],
        'skipped_no_ts': len(plan['skipped_no_ts']),
        'skipped_no_ts_ids': plan['skipped_no_ts'],
        'kept_fresh': plan['kept_fresh'],
        'out_of_scope': plan['out_of_scope'],
        'deleted': 0,
        'profile_collection_touched': False,
    }
    if apply and plan['candidates']:
        report['deleted'] = delete_ids(collection, plan['candidates'], batch_size=batch_size)
        report['remaining_candidates'] = report['candidates'] - report['deleted']
    return report


def build_chroma_client():
    """Connect the same way MemoryManager does: service first, embedded directory second."""
    import chromadb

    host = os.getenv('CHROMA_HOST', 'chromadb')
    port = os.getenv('CHROMA_PORT', '8000')
    path = os.getenv('CHROMA_PERSIST_DIRECTORY', '/app/data/chroma')
    try:
        client = chromadb.HttpClient(host=host, port=int(port),
                                     settings=chromadb.Settings(anonymized_telemetry=False))
        client.heartbeat()
    except Exception as exc:  # noqa: BLE001 - fall back to the embedded store
        logger.info('ChromaDB 服务不可用（%s），改用本地嵌入式目录: %s',
                    type(exc).__name__, path)
        client = chromadb.PersistentClient(path=path,
                                           settings=chromadb.Settings(anonymized_telemetry=False))
    return client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m developer.memory_retention',
        description='按时间保留策略清理 Chroma 情景记忆（默认 dry-run，不清理用户画像）')
    parser.add_argument('--days', type=parse_days, required=True,
                        help=f'保留天数（>= {MIN_DAYS}）；早于该时间的 episodic 记录成为候选')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true',
                      help='只统计并列出候选（默认行为，不删除任何记录）')
    mode.add_argument('--apply', action='store_true', help='真正删除候选记录')
    parser.add_argument('--user', default=None,
                        help='只处理该用户的记录（只缩小范围，不会扩大删除面）')
    parser.add_argument('--collection', default=DEFAULT_EPISODIC_COLLECTION,
                        help=f'情景记忆集合名（默认 {DEFAULT_EPISODIC_COLLECTION}；含 profile 的名称会被拒绝）')
    parser.add_argument('--json', action='store_true', help='输出 JSON 报告')
    return parser


def format_text(report: Dict[str, Any]) -> str:
    lines = [
        f"模式: {report['mode']}（保留 {report['days']} 天，cutoff {report['cutoff']}）",
        f"扫描: {report['scanned']} 条，候选 {report['candidates']} 条，"
        f"较新保留 {report['kept_fresh']} 条，无可用 ts 跳过 {report['skipped_no_ts']} 条",
        f"按用户: {json.dumps(report['by_user'], ensure_ascii=False, sort_keys=True)}",
        f"涉及会话: {len(report['conversations'])} 个"
        + (f" -> {', '.join(report['conversations'][:10])}" if report['conversations'] else ''),
        f"已删除: {report['deleted']} 条",
        "用户画像: 未触碰（画像只能显式通过 API 删除）",
    ]
    if report['mode'] == 'dry-run' and report['candidates']:
        lines.append('提示: 以上为预演，未删除任何记录；确认后用 --apply 执行。')
    return '\n'.join(lines)


def main(argv: Optional[List[str]] = None, *, client_factory=build_chroma_client) -> int:
    args = build_parser().parse_args(argv)
    try:
        collection_name = guard_collection(args.collection)
        client = client_factory()
        collection = client.get_or_create_collection(collection_name)
        report = run_retention(collection, days=args.days, apply=args.apply,
                               user=args.user)
    except RetentionError as exc:
        print(f'保留策略未执行: {exc}', file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - Chroma/连接故障必须可读退出，不静默
        print(f'Chroma 不可用或操作失败: {type(exc).__name__}: {exc}', file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json
          else format_text(report))
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
