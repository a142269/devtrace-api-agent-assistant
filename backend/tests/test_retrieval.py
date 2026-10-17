import unittest
import chromadb
from developer.retrieval import fuse, lexical_rank, rerank_exact
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

    def test_rerank_promotes_exact_technical_tokens(self):
        candidates = [{'title': 'generic', 'content': 'API guide', 'fusion_score': .04},
                      {'title': 'quota', 'content': '429 Retry-After', 'fusion_score': .03}]
        result = rerank_exact('v1 429 Retry-After', candidates, 1)
        self.assertEqual(result[0]['title'], 'quota')
        self.assertEqual(result[0]['exact_match_score'], 1.0)

    def test_version_filter_applies_to_both_rankings(self):
        class Collection:
            def get(self, **kwargs):
                self.where = kwargs['where']
                return {'documents': ['Authorization v1'], 'metadatas': [{'title': 'v1 / 认证'}]}
            def query(self, **kwargs):
                self.query_where = kwargs['where']
                return {'documents': [['Authorization v1']], 'metadatas': [[{'title': 'v1 / 认证'}]]}
        kb = object.__new__(KnowledgeBase)
        kb._collection = Collection()
        self.assertEqual([item['title'] for item in kb.search_hybrid('Authorization', version='v1')], ['v1 / 认证'])
        self.assertEqual(kb._collection.where, kb._collection.query_where)
        self.assertIn({'api_version': 'v1'}, kb._collection.where['$and'])
        self.assertIn({'status': 'active'}, kb._collection.where['$and'])

    def test_chroma_filters_version_and_deprecated_documents_before_retrieval(self):
        class Embeddings:
            def __call__(self, input):
                return [[1.0, 0.0] if 'auth' in text.lower() else [0.0, 1.0] for text in input]

        client = chromadb.EphemeralClient(settings=chromadb.Settings(anonymized_telemetry=False))
        collection = client.create_collection('retrieval_integration', embedding_function=Embeddings())
        collection.add(ids=['v1', 'v2', 'old'],
                       documents=['auth v1', 'auth v2', 'auth old'],
                       metadatas=[{'title': 'v1 / auth', 'api_version': 'v1', 'status': 'active'},
                                  {'title': 'v2 / auth', 'api_version': 'v2', 'status': 'active'},
                                  {'title': 'v1 / old', 'api_version': 'v1', 'status': 'deprecated'}])
        kb = object.__new__(KnowledgeBase)
        kb._collection = collection
        self.assertEqual([item['title'] for item in kb.search_hybrid('auth', version='v1')], ['v1 / auth'])
        self.assertEqual([item['title'] for item in kb.search_hybrid('auth', version='v2')], ['v2 / auth'])
