"""Compare version-filtered dense, hybrid and rule-reranked retrieval on labelled demo queries."""
import argparse
import json
import os
from pathlib import Path

from developer.data import DOCS
from mcp.knowledge_base import KnowledgeBase


def evaluate(knowledge, cases, k=5):
    results = {name: [] for name in ('dense', 'hybrid', 'reranked')}
    for case in cases:
        rankings = knowledge.search_rankings(case['query'], k, case['version'])
        for name, items in rankings.items():
            rank = next((i for i, item in enumerate(items, 1) if item['title'] == case['expected_title']), None)
            results[name].append({'rank': rank, 'titles': [item['title'] for item in items]})
    metrics = {}
    for name, rows in results.items():
        metrics[name] = {'recall_at_k': sum(row['rank'] is not None for row in rows) / len(rows) if rows else None,
                         'mrr': sum(1 / row['rank'] if row['rank'] else 0 for row in rows) / len(rows) if rows else None}
    return {'count': len(cases), 'k': k, 'metrics': metrics, 'cases': cases, 'results': results,
            'limitations': 'Demo corpus and labels are public and correlated; this does not measure production generalization.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--k', type=int, default=5, choices=range(1, 11))
    args = parser.parse_args()
    cases = json.loads((Path(__file__).parent / 'retrieval_eval_cases.json').read_text(encoding='utf-8'))
    knowledge = KnowledgeBase(chroma_host=os.getenv('CHROMA_HOST', 'localhost'),
                              chroma_port=int(os.getenv('CHROMA_PORT', '8000')),
                              chroma_path=os.getenv('CHROMA_PERSIST_DIRECTORY', './data/chroma'),
                              collection_name='developer_retrieval_evaluation', default_documents=DOCS)
    report = evaluate(knowledge, cases, args.k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'count': report['count'], 'k': report['k'], 'metrics': report['metrics']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
