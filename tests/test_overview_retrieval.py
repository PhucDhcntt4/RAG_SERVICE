import unittest
from unittest.mock import Mock, patch

from app.chat import AnswerDraft, GeminiChat
from app.models import ChatRequest, SearchRequest
from app.retrieval import needs_document_context
from tests.test_service import config, make_service, sample_row


def row(index, content, document_id=1):
    return sample_row(chunk_id=document_id * 1000 + index, document_id=document_id,
                      chunk_index=index, category='store', content=content,
                      section_path=['Danh sách', f'Mục {index}'])


class OverviewRetrievalTests(unittest.TestCase):
    def test_overview_detection_is_not_store_specific(self):
        for query in ('hiện có cửa hàng ở đâu', 'liệt kê các điều kiện bảo hành',
                      'tất cả các bước nghỉ phép', 'danh sách phòng ban', 'có những cơ sở nào'):
            self.assertTrue(needs_document_context(query), query)
        for query in ('Cửa hàng Hai Bà Trưng ở đâu?', 'thời hạn bảo hành là bao lâu?'):
            self.assertFalse(needs_document_context(query), query)

    def test_expansion_recovers_missing_region_and_status_then_llm_answers(self):
        service = make_service(config(rag_max_context_chars=30000))
        seed = row(7, 'Có cơ sở tại nhiều khu vực.')
        service.repository.search.return_value = [seed]
        expanded = [
            row(2, '1. 101-103 Đường Mẫu, P. Mẫu.\n2. Tầng 3, 88 Đường Khác.'),
            row(3, '3. 222 Đường Bắc.'), row(4, '4. 1159 Đường Đông.'),
            row(8, 'Cơ sở 101-103 Đường Mẫu tạm ngưng từ 03/09/2026.'),
        ]
        service.repository.expand_documents.return_value = expanded
        with patch('app.chat.genai.Client'):
            chat = GeminiChat(service.settings)
        chat.generate = Mock(return_value=AnswerDraft(sufficient=True, statements=[
            {'text': 'Theo nguồn, cơ sở phía Đông ở 1159 Đường Đông.', 'citations': [3]},
        ]))
        result = chat.answer(ChatRequest(query='hiện có cửa hàng ở đâu', categories=['store']), service)
        self.assertEqual(result['answer'], 'Theo nguồn, cơ sở phía Đông ở 1159 Đường Đông. [S3]')
        service.embedder.embed.assert_called_once()
        service.repository.expand_documents.assert_called_once()
        args = service.repository.expand_documents.call_args.args
        self.assertEqual(args[1], [1])
        self.assertEqual(args[3], ['store'])
        self.assertEqual(args[4], 101)
        chat.generate.assert_called_once()
        prompt, payload, schema = chat.generate.call_args.args
        self.assertIs(schema, AnswerDraft)
        for record in expanded:
            self.assertIn(record['content'], payload['context'])
        self.assertTrue(payload['retrieval_coverage']['selected_documents_complete'])
        self.assertIn('sao chép nguyên văn', prompt)

    def test_precise_query_and_legacy_search_do_not_expand(self):
        service = make_service()
        service.repository.search.return_value = [row(1, 'Thông tin.')]
        service.search(SearchRequest(query='Danh sách cửa hàng'))
        service.repository.expand_documents.assert_not_called()

    def test_expansion_obeys_document_and_chunk_caps(self):
        service = make_service(config(rag_overview_max_documents=1, rag_overview_max_chunks=2))
        service.repository.search.return_value = [row(2, 'A'), row(1, 'B', document_id=2)]
        service.repository.expand_documents.return_value = [row(0, 'X'), row(1, 'Y'), row(2, 'Z')]
        result = service.search(SearchRequest(query='liệt kê'), expand_documents=True)
        self.assertEqual(service.repository.expand_documents.call_args.args[1], [1])
        self.assertEqual(len(result['sources']), 2)
        self.assertTrue(result['retrieval_coverage']['truncated'])
        self.assertFalse(result['retrieval_coverage']['selected_documents_complete'])

    def test_context_budget_marks_partial_without_claiming_complete_documents(self):
        service = make_service(config(rag_max_context_chars=500))
        service.repository.search.return_value = [row(1, 'seed')]
        service.repository.expand_documents.return_value = [row(1, 'A' * 1000), row(2, 'B')]
        result = service.search(SearchRequest(query='toàn bộ'), expand_documents=True)
        self.assertLessEqual(len(result['content']), 500)
        self.assertTrue(result['retrieval_coverage']['truncated'])
        self.assertFalse(result['retrieval_coverage']['selected_documents_complete'])

    def test_no_seed_does_not_read_unrelated_documents(self):
        service = make_service()
        service.repository.search.return_value = []
        result = service.search(SearchRequest(query='liệt kê'), expand_documents=True)
        service.repository.expand_documents.assert_not_called()
        self.assertFalse(result['success'])

if __name__ == '__main__':
    unittest.main()
