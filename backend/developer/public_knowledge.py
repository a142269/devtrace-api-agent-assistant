"""Small in-process knowledge base over the curated public API documentation."""
from developer.public_docs import PUBLIC_DOCS
from developer.retrieval import lexical_rank

# Chinese questions are common; map intent words to the English terms used by
# imported OpenAPI documents so lexical ranking can reach the right operation.
QUERY_EXPANSIONS = {
    "退款": "refund refunds",
    "创建": "create post",
    "新建": "create post",
    "查询": "get retrieve list",
    "获取": "get retrieve",
    "关闭": "close",
    "取消": "cancel",
    "确认": "confirm",
    "事件": "event events",
    "争议": "dispute disputes",
    "纠纷": "dispute",
    "支付": "payment payment_intent",
    "客户": "customer customers",
    "订阅": "subscription subscriptions",
    "发票": "invoice invoices",
    "回调": "webhook",
    "接口": "endpoint",
    "列表": "list",
}


def expand_query(query: str) -> str:
    additions = [english for chinese, english in QUERY_EXPANSIONS.items() if chinese in query]
    return query + " " + " ".join(additions) if additions else query


class PublicDocsKnowledge:
    """Deterministic lexical retrieval over a small attributed document set.

    The corpus is deliberately small (public documentation summaries), so a
    BM25-style ranking with exact-token reranking is enough and keeps the demo
    runnable without ChromaDB or embedding downloads.
    """

    def __init__(self, documents=None):
        self.documents = list(documents if documents is not None else PUBLIC_DOCS)

    def search(self, query, api="", top_k=4):
        expanded = expand_query(query)
        candidates = [doc for doc in self.documents
                      if not api or doc.get("api") == api]
        if not candidates:
            candidates = self.documents
        ranked = lexical_rank(expanded, candidates)
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
