import unittest
from developer.retrieval import fuse, lexical_rank
from mcp.knowledge_base import KnowledgeBase


class RetrievalTests(unittest.TestCase):
    def test_exact_api_token_and_fusion(self):
        docs = [{'title': 'v1 / 认证', 'content': 'Authorization Bearer header', 'chunk': 0},
                {'title': 'v1 / 限流', 'content': '429 Retry-After quota', 'chunk': 0}]
        lexical = lexical_rank('Retry-After 429', docs)
        self.assertEqual(lexical[0]['title'], 'v1 / 限流')
        fused = fuse([docs[0], docs[1]], lexical)
        self.assertEqual(fused[0]['title'], 'v1 / 限流')
        self.assertEqual(len(fused), 2)

    def test_version_filter_applies_to_both_rankings(self):
        class Collection:
            def get(self, **kwargs):
                return {'documents': ['Authorization v1', 'Authorization v2'],
                        'metadatas': [{'title': 'v1 / 认证'}, {'title': 'v2 / 认证'}]}
        kb = object.__new__(KnowledgeBase)
        kb._collection = Collection()
        kb.search = lambda query, top_k: [
            {'title': 'v2 / 认证', 'content': 'Authorization v2'},
            {'title': 'v1 / 认证', 'content': 'Authorization v1'}]
        self.assertEqual([item['title'] for item in kb.search_hybrid('Authorization', version='v1')], ['v1 / 认证'])
