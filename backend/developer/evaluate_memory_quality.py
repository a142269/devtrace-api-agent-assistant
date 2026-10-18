"""Memory-quality harness: compression retention, export and forget semantics.

This exercises the real MemoryManager compression path with a deterministic
summarizer and in-memory Redis/Chroma doubles. It measures whether facts survive
the pipeline (not whether a particular model writes a good summary).
"""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from memory.conversation_memory import MemoryManager, MsgRole

FACTS = ["req_auth_01", "sk_live_demo", "凭证过期"]
SUMMARY = "历史会话：请求 req_auth_01 的 key_prefix 为 sk_live_demo，结论为凭证过期。"
SEED = [
    ("user", "请求 req_auth_01 返回 401，帮我排查"),
    ("assistant", "先查文档和请求记录。"),
    ("user", "编号 req_auth_01，密钥前缀 sk_live_demo"),
    ("assistant", "记录显示凭证过期。"),
    ("user", "那要不要换密钥？"),
    ("assistant", "需要轮换密钥。"),
    ("user", "确认结论是凭证过期"),
    ("assistant", "是。"),
    ("user", "还有别的可能吗？"),
    ("assistant", "也可能是 scope 缺失，但记录不支持。"),
    ("user", "怎么避免"),
    ("assistant", "轮换并监控过期时间。"),
    ("user", "好"),
    ("assistant", "如还有问题请提供新的请求编号。"),
    ("user", "继续"),
]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def delete(self, key):
        had = key in self.values or key in self.lists
        self.values.pop(key, None)
        self.lists.pop(key, None)
        return int(had)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def lrange(self, key, start, end):
        rows = self.lists.get(key, [])
        return rows[start:] if end == -1 else rows[start:end + 1]

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def expire(self, *_args):
        return True


class FakeCollection:
    def __init__(self):
        self.deleted_where = None
        self.added = []

    def get(self, ids=None, where=None):
        return {"documents": []}

    def add(self, ids=None, documents=None, metadatas=None):
        self.added.append({"ids": ids, "documents": documents, "metadatas": metadatas})

    def delete(self, where=None):
        self.deleted_where = where

    def query(self, **_kwargs):
        return {"documents": [[]]}


class Summarizer:
    class Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text=SUMMARY)])

    def __init__(self):
        self.messages = self.Messages()


def build_memory():
    memory = object.__new__(MemoryManager)
    memory._redis = FakeRedis()
    memory._episodic = FakeCollection()
    memory._profile = FakeCollection()
    memory._client = Summarizer()
    memory._model = 'summary-test'
    memory._namespace = ''
    memory._profile_prefix = 'user_profile:'
    return memory


async def evaluate():
    memory = build_memory()
    for role, content in SEED:
        await memory.add_message('alice', 'c1', MsgRole(role), content)
    summary = memory._redis.values.get(memory._summary_key('alice', 'c1'), '')
    exported = await memory.export_memory('alice', 'c1')
    working = await memory._get_working_memory('alice', 'c1')
    isolation = await memory.export_memory('bob', 'c1')
    forgotten = await memory.forget_conversation('alice', 'c1')
    after_delete = await memory.export_memory('alice', 'c1')
    facts_retained = sum(1 for fact in FACTS if fact in summary)
    checks = {
        "compression_triggered": bool(summary),
        "facts_retained": facts_retained,
        "facts_total": len(FACTS),
        "working_after_compress": len(working),
        "episodic_written": len(memory._episodic.added),
        "export_has_summary": bool(exported.get('summary')),
        "user_isolation": isolation['counts']['messages'] == 0,
        "forget_cleared": after_delete['counts']['messages'] == 0 and not after_delete['summary'],
    }
    passed = (checks["compression_triggered"] and facts_retained == len(FACTS)
              and checks["working_after_compress"] <= memory.WORKING_MAX
              and checks["episodic_written"] >= 1 and checks["export_has_summary"]
              and checks["user_isolation"] and checks["forget_cleared"]
              and forgotten['working_memory_deleted'])
    return {
        "checks": checks,
        "passed": bool(passed),
        "summary_chars": len(summary),
        "limitations": (
            "Uses a deterministic summarizer and in-memory Redis/Chroma doubles, so it measures "
            "the compression/export/forget pipeline, not the quality of a live model summary."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": report["checks"]}, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
