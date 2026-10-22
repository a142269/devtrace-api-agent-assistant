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
  - failures are readable and exit non-zero; they are never reported as success;
  - the scan reads ``--page-size`` records at a time (default 500, 1..5000) and only keeps
    the ids it must report, so a large collection is never loaded whole; paging changes
    memory use and the page count in the report, never the verdicts.

Usage:
    python -m developer.memory_retention --days 90
    python -m developer.memory_retention --days 90 --user alice --json
    python -m developer.memory_retention --days 90 --apply
    python -m developer.memory_retention --days 90 --page-size 1000 --apply
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
DEFAULT_PAGE_SIZE = 500
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 5000
# Any collection whose name mentions profile is off-limits: cleaning episodic memory
# must never be able to remove a user profile as a side effect.
FORBIDDEN_COLLECTION_HINTS = ('profile',)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


class RetentionError(Exception):
    """Raised for anything that must stop the run with a readable message."""


class _PagingUnsupported(Exception):
    """Internal: this Chroma build (or test double) does not accept limit/offset."""


def parse_days(raw: str) -> int:
    """``--days`` validator: positive integers only, so the cutoff can never be today+."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'--days 必须是整数，收到: {raw!r}') from None
    if value < MIN_DAYS:
        raise argparse.ArgumentTypeError(f'--days 必须 >= {MIN_DAYS}，收到: {value}')
    return value


def parse_page_size(raw: str) -> int:
    """``--page-size`` validator: bounded so a typo cannot request a full-set page."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f'--page-size 必须是整数，收到: {raw!r}') from None
    if value < MIN_PAGE_SIZE or value > MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(
            f'--page-size 必须在 {MIN_PAGE_SIZE}..{MAX_PAGE_SIZE} 之间，收到: {value}')
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


def _rows_of(payload: Any) -> List[Dict[str, Any]]:
    ids = list((payload or {}).get('ids') or [])
    metadatas = list((payload or {}).get('metadatas') or [])
    if len(metadatas) != len(ids):
        metadatas = [{} for _ in ids]
    return [{'id': doc_id, 'metadata': metadata or {}}
            for doc_id, metadata in zip(ids, metadatas)]


def fetch_page(collection: Any, *, limit: int, offset: int) -> List[Dict[str, Any]]:
    """Read one ``limit``-sized page. Raises ``_PagingUnsupported`` when it cannot page."""
    try:
        payload = collection.get(limit=limit, offset=offset, include=['metadatas'])
    except TypeError:
        try:
            payload = collection.get(limit=limit, offset=offset)
        except TypeError as exc:       # this Chroma build / double has no limit+offset
            raise _PagingUnsupported() from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the CLI as a readable failure
            raise RetentionError(
                f'读取情景记忆失败（offset={offset}）: {type(exc).__name__}: {exc}') from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the CLI as a readable failure
        raise RetentionError(
            f'读取情景记忆失败（offset={offset}）: {type(exc).__name__}: {exc}') from exc
    return _rows_of(payload)


def fetch_all(collection: Any) -> List[Dict[str, Any]]:
    """Read every episodic row in one request — only used when the client cannot page."""
    try:
        payload = collection.get(include=['metadatas'])
    except TypeError:      # older/newer Chroma builds differ on the include kwarg
        payload = collection.get()
    except Exception as exc:  # noqa: BLE001 - surfaced to the CLI as a readable failure
        raise RetentionError(f'读取情景记忆失败: {type(exc).__name__}: {exc}') from exc
    return _rows_of(payload)


def iter_pages(collection: Any, *, page_size: int):
    """Yield pages of rows, one at a time, so the run never holds the whole collection.

    A short or empty final page ends the walk. A client that accepts ``limit``/``offset``
    but ignores them (or rejects them outright) is detected and degrades to one full read
    instead of spinning forever.
    """
    try:
        offset = 0
        previous_ids: Optional[tuple] = None
        while True:
            rows = fetch_page(collection, limit=page_size, offset=offset)
            ids = tuple(row['id'] for row in rows)
            if ids and ids == previous_ids:
                return
            previous_ids = ids
            if rows:
                yield rows
            if len(rows) < page_size:
                return
            offset += len(rows)
    except _PagingUnsupported:
        rows = fetch_all(collection)
        if rows:
            yield rows


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
                  batch_size: int = DELETE_BATCH_SIZE,
                  page_size: int = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
    """Plan one retention pass; delete only when ``apply`` is true.

    The scan walks the collection one ``page_size`` slice at a time and keeps only the
    ids it needs for the report, so a large collection never lands in memory at once.
    Candidate ids are de-duplicated as they accumulate: a page that overlaps a neighbour
    (a client that ignores ``offset``) can never make the same record delete twice.
    """
    if days < MIN_DAYS:
        raise RetentionError(f'--days 必须 >= {MIN_DAYS}，收到: {days}')
    if page_size < MIN_PAGE_SIZE or page_size > MAX_PAGE_SIZE:
        raise RetentionError(
            f'--page-size 必须在 {MIN_PAGE_SIZE}..{MAX_PAGE_SIZE} 之间，收到: {page_size}')
    cutoff = _cutoff(now, days)
    scanned = 0
    kept_fresh = 0
    out_of_scope = 0
    pages = 0
    candidate_ids: List[str] = []
    seen_candidates: set = set()
    skipped_no_ts: List[str] = []
    seen_skipped: set = set()
    by_user: Dict[str, int] = {}
    conversations: List[str] = []
    seen_conversations: set = set()

    for rows in iter_pages(collection, page_size=page_size):
        pages += 1
        plan = classify(rows, cutoff=cutoff, user=user)
        scanned += plan['scanned']
        kept_fresh += plan['kept_fresh']
        out_of_scope += plan['out_of_scope']
        page_candidates = set(plan['candidates'])
        owner_by_id: Dict[str, str] = {}
        conv_by_id: Dict[str, str] = {}
        for row in rows:
            doc_id = str(row.get('id') or '')
            if doc_id in page_candidates:
                metadata = row.get('metadata') or {}
                owner_by_id[doc_id] = str(metadata.get('user_id') or '')
                conv_by_id[doc_id] = str(metadata.get('conv_id') or '')
        for doc_id in plan['candidates']:
            if doc_id in seen_candidates:
                continue
            seen_candidates.add(doc_id)
            candidate_ids.append(doc_id)
            owner = owner_by_id.get(doc_id, '')
            by_user[owner or '(unknown)'] = by_user.get(owner or '(unknown)', 0) + 1
            conv = conv_by_id.get(doc_id, '')
            if conv and conv not in seen_conversations:
                seen_conversations.add(conv)
                conversations.append(conv)
        for doc_id in plan['skipped_no_ts']:
            if doc_id not in seen_skipped:
                seen_skipped.add(doc_id)
                skipped_no_ts.append(doc_id)

    report: Dict[str, Any] = {
        'mode': 'apply' if apply else 'dry-run',
        'days': days,
        'cutoff': cutoff.isoformat(),
        'user': user,
        'pages': pages,
        'page_size': page_size,
        'scanned': scanned,
        'candidates': len(candidate_ids),
        'candidate_ids': candidate_ids,
        'by_user': by_user,
        'conversations': conversations,
        'skipped_no_ts': len(skipped_no_ts),
        'skipped_no_ts_ids': skipped_no_ts,
        'kept_fresh': kept_fresh,
        'out_of_scope': out_of_scope,
        'deleted': 0,
        'profile_collection_touched': False,
    }
    if apply and candidate_ids:
        report['deleted'] = delete_ids(collection, candidate_ids, batch_size=batch_size)
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
    parser.add_argument('--page-size', type=parse_page_size, default=DEFAULT_PAGE_SIZE,
                        help=f'每次读取的记录数（{MIN_PAGE_SIZE}..{MAX_PAGE_SIZE}，默认 {DEFAULT_PAGE_SIZE}）；'
                             '只影响读取批次与内存占用，不改变判定结果')
    parser.add_argument('--json', action='store_true', help='输出 JSON 报告')
    return parser


def format_text(report: Dict[str, Any]) -> str:
    lines = [
        f"模式: {report['mode']}（保留 {report['days']} 天，cutoff {report['cutoff']}）",
        f"扫描: {report['scanned']} 条（分 {report.get('pages', 1)} 页读取，"
        f"每页 {report.get('page_size', DEFAULT_PAGE_SIZE)} 条），"
        f"候选 {report['candidates']} 条，"
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
                               user=args.user, page_size=args.page_size)
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
