import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.embeddings import EmbeddingError
from app.main import create_app
from app.models import SearchRequest
from tests.test_service import config, make_service, sample_row


class DebugLogTests(unittest.TestCase):
    def test_request_trace_query_and_metadata_without_document_or_key(self):
        settings = config(log_level='DEBUG')
        service = make_service(settings)
        service.repository.search.return_value = [sample_row(content='PRIVATE_DOCUMENT_SENTINEL')]
        with TestClient(create_app(settings, service, chat=Mock())) as client:
            with self.assertLogs('rag_service', level='DEBUG') as logs:
                first = client.post('/api/v1/knowledge/search',
                    headers={'Authorization': 'Bearer ' + 's' * 32},
                    json={'query': 'Bảo hành\nDEBUG: injected', 'categories': ['warranty'], 'top_k': 2})
                second = client.post('/api/v1/knowledge/search',
                    headers={'Authorization': 'Bearer ' + 's' * 32}, json={'query': 'Next'})
        self.assertEqual(first.status_code, 200)
        trace = first.headers['X-Request-ID']
        self.assertRegex(trace, r'^[0-9a-f]{32}$')
        self.assertNotEqual(trace, second.headers['X-Request-ID'])
        events = [record.getMessage() for record in logs.records if trace in record.getMessage()]
        self.assertTrue(any('RAG SEARCH' in line and 'categories=["warranty"]' in line
                            and 'top_k=2' in line for line in events))
        self.assertTrue(any('RAG SOURCE' in line and 'category="warranty"' in line
                            and 'chunk_index=0' in line and 'similarity=0.8' in line for line in events))
        self.assertTrue(any('RAG CONTEXT' in line for line in events))
        self.assertTrue(any('RAG REQUEST' in line for line in events))
        self.assertNotIn('PRIVATE_DOCUMENT_SENTINEL', str(events))
        self.assertNotIn('s' * 32, str(events))
        self.assertTrue(all('\n' not in line for line in events))

    def test_context_logs_truncation_and_skipped_sources(self):
        service = make_service(config(rag_max_context_chars=500))
        service.repository.search.return_value = [sample_row(content='x' * 1000), sample_row()]
        with self.assertLogs('rag_service', level='DEBUG') as logs:
            service.search(SearchRequest(query='Test'))
        text = '\n'.join(logs.output)
        self.assertIn('truncated=true', text)
        self.assertIn('selected=false reason="context_limit"', text)

    def test_failure_logs_stage_not_provider_exception_body(self):
        service = make_service()
        service.embedder.embed.side_effect = EmbeddingError('PRIVATE_PROVIDER_BODY')
        with self.assertLogs('rag_service', level='DEBUG') as logs:
            with self.assertRaises(EmbeddingError):
                service.search(SearchRequest(query='Test'))
        text = '\n'.join(logs.output)
        self.assertIn('RAG SEARCH FAILED', text)
        self.assertIn('stage="embedding"', text)
        self.assertNotIn('PRIVATE_PROVIDER_BODY', text)

    def test_info_level_does_not_log_query(self):
        settings = config(log_level='INFO')
        with TestClient(create_app(settings, make_service(settings), chat=Mock())) as client:
            with self.assertLogs('rag_service', level='INFO') as logs:
                client.post('/api/v1/knowledge/search',
                    headers={'Authorization': 'Bearer ' + 's' * 32},
                    json={'query': 'PRIVATE_QUERY_SENTINEL'})
        self.assertNotIn('PRIVATE_QUERY_SENTINEL', '\n'.join(logs.output))
