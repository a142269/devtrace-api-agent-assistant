"""Small-corpus BM25 lexical scoring and reciprocal rank fusion."""
import math
import re
from collections import Counter


def tokens(text):
    parts = re.findall(r'[a-z0-9_/:.-]+|[\u4e00-\u9fff]+', text.lower())
    result = []
    for part in parts:
        if re.fullmatch(r'[\u4e00-\u9fff]+', part):
            result.extend(part[i:i + 2] for i in range(max(1, len(part) - 1)))
        else:
            result.append(part)
    return result


def lexical_rank(query, docs):
    words = set(tokens(query))
    corpus = [tokens(item.get('title', '') + ' ' + item.get('content', '')) for item in docs]
    if not corpus or not words:
        return []
    average = sum(map(len, corpus)) / len(corpus) or 1
    frequency = {word: sum(word in doc for doc in corpus) for word in words}
    ranked = []
    for item, doc in zip(docs, corpus):
        counts = Counter(doc)
        score = 0.0
        for word in words:
            n = frequency[word]
            if not n:
                continue
            tf = counts[word]
            score += math.log(1 + (len(corpus) - n + 0.5) / (n + 0.5)) * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(doc) / average))
        if score > 0:
            ranked.append((score, item))
    return [item for _, item in sorted(ranked, key=lambda x: -x[0])]


def fuse(dense, lexical, top_k=4):
    scores, items = {}, {}
    for ranking in (dense, lexical):
        for index, item in enumerate(ranking):
            key = (item.get('title', ''), item.get('chunk', 0), item.get('content', ''))
            items[key] = item
            scores[key] = scores.get(key, 0) + 1 / (60 + index + 1)
    return [{**items[key], 'fusion_score': scores[key]} for key in sorted(scores, key=scores.get, reverse=True)[:top_k]]


def rerank_exact(query, candidates, top_k=4):
    """Rerank fused candidates by exact technical token coverage, preserving RRF ties."""
    terms = {word for word in tokens(query) if re.search(r'[a-z0-9]', word) and word not in {'v1', 'v2'}}
    def score(item):
        words = set(tokens(item.get('title', '') + ' ' + item.get('content', '')))
        return len(terms & words) / len(terms) if terms else 0.0
    return [{**item, 'exact_match_score': score(item)} for item in
            sorted(candidates, key=lambda item: (-score(item), -item.get('fusion_score', 0)))[:top_k]]
