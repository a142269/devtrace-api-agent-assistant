"""Compare memory context availability using real Redis and ChromaDB on synthetic users."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from memory.conversation_memory import MemoryManager, MsgRole


async def evaluate():
    memory = MemoryManager(redis_url=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
        chroma_host=os.getenv('CHROMA_HOST', 'localhost'), chroma_port=int(os.getenv('CHROMA_PORT', '8000')),
        chroma_path=os.getenv('CHROMA_PERSIST_DIRECTORY', './data/memory-eval'), api_key='evaluation-only',
        namespace='memory_eval', episodic_collection='memory_eval_episodic', profile_collection='memory_eval_profile')
    rows = []
    try:
        for i in range(1, 11):
            user, old_conv, marker = f'memory-eval-user-{i}', f'old-{i}', f'req_quota_{i:02d}'
            text = f'模拟请求 {marker} 返回 429，分钟配额用尽'
            await memory.add_message(user, old_conv, MsgRole.USER, text)
            # Seed a known historical summary directly; this experiment does not test LLM compression.
            await memory._store_episodic(user, old_conv, text, text)
            for label, conv in [('same_session', old_conv), ('new_session', f'new-{i}')]:
                context = await memory.get_context(user, conv, query='429 分钟配额')
                short = any(marker in message.content for message in context.recent_messages)
                layered = short or any(marker in item for item in context.relevant_history)
                foreign = await memory.get_context(f'foreign-{i}', conv, query='429 分钟配额')
                rows.append({'case': f'{label}-{i:02d}', 'scope': label,
                             'none': False, 'short': short, 'layered': layered,
                             'cross_user_leak': marker in foreign.to_prompt_text()})
    finally:
        await memory.close()
    result = {}
    for scope in ('same_session', 'new_session'):
        subset = [row for row in rows if row['scope'] == scope]
        result[scope] = {'count': len(subset), **{mode: sum(row[mode] for row in subset)
                                                 for mode in ('none', 'short', 'layered')},
                         'cross_user_leaks': sum(row['cross_user_leak'] for row in subset)}
    return {'summary': result, 'cases': rows,
            'limitations': 'Synthetic context recall only. History is seeded directly; this does not measure LLM compression, profile quality or diagnostic task success.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
