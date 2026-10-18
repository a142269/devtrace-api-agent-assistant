"""Small in-process knowledge base over the curated public API documentation."""
from developer.public_docs import PUBLIC_DOCS
from developer.retrieval import lexical_rank


class PublicDocsKnowledge:
    """Deterministic lexical retrieval over a small attributed document set.

    The corpus is deliberately small (public documentation summaries), so a
    BM25-style ranking with exact-token reranking is enough and keeps the demo
    runnable without ChromaDB or embedding downloads.
    """

    def __init__(self, documents=None):
        self.documents = list(documents if documents is not None else PUBLIC_DOCS)

    def search(self, query, api="", top_k=4):
        candidates = [doc for doc in self.documents
                      if not api or doc.get("api") == api]
        if not candidates:
            candidates = self.documents
        ranked = lexical_rank(query, candidates)
        if not ranked:
            ranked = candidates
        if api:
            preferred = [doc for doc in ranked if doc.get("api") == api]
            ranked = preferred + [doc for doc in ranked if doc not in preferred]
        return [
            {
                "id": doc.get("id"),
                "api": doc.get("api"),
                "title": doc.get("title"),
                "content": doc.get("content"),
                "source_url": doc.get("source_url"),
                "updated": doc.get("updated"),
            }
            for doc in ranked[:top_k]
        ]

    def count(self):
        return len(self.documents)
