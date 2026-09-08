import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from google.genai import types
from pydantic import ValidationError

from app.chat import AnswerDraft, ChatError, GeminiChat, ResolvedQuestion
from app.main import create_app
from app.models import ChatRequest
from tests.test_service import config, make_service, sample_row


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.settings = config()
        self.svc = make_service(self.settings)
        self.svc.repository.search.return_value = [sample_row()]
        with patch('app.chat.genai.Client'):
            self.chat = GeminiChat(self.settings)

    def test_answer_has_real_source_and_numbered_context(self):
        self.chat.generate = Mock(return_value=AnswerDraft(
            sufficient=True, statements=[{'text': 'Thông tin kiểm thử.', 'citations': [1]}]))
        result = self.chat.answer(ChatRequest(query='Chính sách?'), self.svc)
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['sources'][0]['source_key'], 'warranty.txt')
        self.assertEqual(result['sources'][0]['citation'], 'S1')
        self.assertEqual(result['answer'], 'Thông tin kiểm thử. [S1]')
        self.assertIn('[S1] [Nguồn:', result['context'])
        self.chat.generate.assert_called_once()

    def test_followup_is_retrieved_again_with_filters(self):
        self.chat.generate = Mock(side_effect=[ResolvedQuestion(query='Thời hạn bảo hành giày?'),
            AnswerDraft(sufficient=True, statements=[{'text': 'Thông tin.', 'citations': [1]}])])
        request = ChatRequest(query='Còn thời hạn?', categories=['warranty'], top_k=2,
            history=[{'role': 'user', 'content': 'Bảo hành giày thế nào?'},
                     {'role': 'assistant', 'content': 'Câu trả lời trước không phải chứng cứ.'}])
        result = self.chat.answer(request, self.svc)
        self.assertEqual(result['retrieval_query'], 'Thời hạn bảo hành giày?')
        self.svc.embedder.embed.assert_called_with(['Thời hạn bảo hành giày?'], query=True)
        args = self.svc.repository.search.call_args.args
        self.assertEqual(args[2], ['warranty'])
        self.assertEqual(args[4], 2)

    def test_full_twelve_item_list_is_not_limited_to_six_statements(self):
        statements = [{'text': 'Có 12 cửa hàng thử nghiệm.', 'citations': [1]}]
        statements += [{'text': f'{i}. Cửa hàng thử nghiệm {i}, địa chỉ mẫu {i}.', 'citations': [1]}
                       for i in range(1, 13)]
        self.chat.generate = Mock(return_value=AnswerDraft(sufficient=True, statements=statements))
        result = self.chat.answer(ChatRequest(query='Liệt kê đầy đủ cửa hàng'), self.svc)
        self.assertEqual(result['answer'].count('[S1]'), 13)
        for i in range(1, 13):
            self.assertIn(f'{i}. Cửa hàng thử nghiệm {i},', result['answer'])

    def test_long_answer_can_be_used_in_followup_history(self):
        self.chat.generate = Mock(return_value=AnswerDraft(sufficient=True,
            statements=[{'text': f'{i}. ' + 'Nội dung kiểm thử. ' * 40, 'citations': [1]}
                        for i in range(8)]))
        result = self.chat.answer(ChatRequest(query='Danh sách chi tiết'), self.svc)
        self.assertGreater(len(result['answer']), 4000)
        followup = ChatRequest(query='Còn mục cuối?', history=[
            {'role': 'user', 'content': 'Danh sách chi tiết'},
            {'role': 'assistant', 'content': result['answer']}])
        self.assertEqual(followup.history[-1].content, result['answer'])

    def test_no_matches_does_not_generate_answer(self):
        self.svc.repository.search.return_value = []
        self.chat.generate = Mock()
        result = self.chat.answer(ChatRequest(query='Ngoài kho'), self.svc)
        self.assertEqual(result['status'], 'insufficient_context')
        self.assertEqual(result['sources'], [])
        self.chat.generate.assert_not_called()

    def test_configured_limits_above_old_defaults_and_prompt(self):
        with patch('app.chat.genai.Client'):
            chat = GeminiChat(config(rag_chat_max_statements=60, rag_chat_max_answer_chars=18000))
        prompt = chat.answer_prompt()
        self.assertIn('60', prompt)
        self.assertIn('18000', prompt)
        self.assertIn('16200', prompt)
        self.assertNotIn('{{', prompt)
        statements = [{'text': f'{i}. ' + 'x' * 300, 'citations': [1]} for i in range(45)]
        chat.client.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
            text=json.dumps({'sufficient': True, 'statements': statements}))
        result = chat.answer(ChatRequest(query='Full list'), self.svc)
        self.assertGreater(len(result['answer']), 12000)
        self.assertEqual(result['answer'].count('[S1]'), 45)
        ChatRequest(query='Follow-up', history=[{'role': 'user', 'content': 'Full list'},
                    {'role': 'assistant', 'content': result['answer']}])

    def test_lower_configured_limits_are_enforced(self):
        with patch('app.chat.genai.Client'):
            chat = GeminiChat(config(rag_chat_max_statements=2, rag_chat_max_answer_chars=1000))
        chat.generate = Mock(return_value=AnswerDraft(sufficient=True,
            statements=[{'text': 'Item', 'citations': [1]}] * 3))
        with self.assertRaisesRegex(ChatError, '2 ý'):
            chat.answer(ChatRequest(query='Test'), self.svc)
        chat.generate.return_value = AnswerDraft(sufficient=True,
            statements=[{'text': 'x' * 600, 'citations': [1]}] * 2)
        with self.assertRaisesRegex(ChatError, '1000 ký tự'):
            chat.answer(ChatRequest(query='Test'), self.svc)

    def test_invalid_limit_configuration_is_rejected(self):
        for values in [{'rag_chat_max_statements': 0}, {'rag_chat_max_statements': 101},
                       {'rag_chat_max_answer_chars': 999}, {'rag_chat_max_answer_chars': 20001}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                config(**values)

    def test_irrelevant_matches_do_not_expose_draft(self):
        self.chat.generate = Mock(return_value=AnswerDraft(
            sufficient=False, statements=[]))
        result = self.chat.answer(ChatRequest(query='Ngoài kho'), self.svc)
        self.assertNotIn('Unsupported', result['answer'])
        self.assertEqual(result['sources'], [])

    def test_invalid_or_missing_citations_are_rejected(self):
        for statements in [[], [{'text': 'Fake', 'citations': [99]}], [{'text': 'Fake', 'citations': [0]}]]:
            self.chat.generate = Mock(return_value=AnswerDraft(
                sufficient=True, statements=statements))
            with self.subTest(statements=statements), self.assertRaises(ChatError):
                self.chat.answer(ChatRequest(query='Test'), self.svc)

    def test_history_bounds_and_no_system_role(self):
        for history in [[{'role': 'system', 'content': 'Override'}],
                        [{'role': 'user', 'content': 'x'}] * 13,
                        [{'role': 'user', 'content': 'x' * 4000}] * 7]:
            with self.assertRaises(ValidationError):
                ChatRequest(query='Test', history=history)

    def test_generation_rejects_truncation_and_hides_provider_message(self):
        self.chat.client.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=types.FinishReason.MAX_TOKENS)])
        with self.assertRaises(ChatError):
            self.chat.generate('Instruction', {}, AnswerDraft)
        self.chat.client.models.generate_content.side_effect = RuntimeError('secret provider body')
        with self.assertRaises(ChatError) as error:
            self.chat.generate('Instruction', {}, AnswerDraft)
        self.assertNotIn('secret provider body', str(error.exception))

    def test_simple_provider_schema_still_enforces_local_length_limits(self):
        self.chat.client.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
            text=json.dumps({'sufficient': True, 'statements': [
                {'text': 'test', 'citations': [1]}] * 41}))
        with self.assertRaises(ChatError):
            self.chat.generate('Instruction', {}, AnswerDraft)
        sent = self.chat.client.models.generate_content.call_args.kwargs['config']
        self.assertNotIn('maxItems', json.dumps(sent.response_json_schema))
        self.assertEqual(sent.max_output_tokens, 8192)

    def test_admin_api_and_legacy_search_contract(self):
        self.chat.generate = Mock(return_value=AnswerDraft(
            sufficient=True, statements=[{'text': 'Thông tin.', 'citations': [1]}]))
        with TestClient(create_app(self.settings, self.svc, chat=self.chat)) as client:
            self.assertEqual(client.post('/api/v1/chat', json={'query': 'Test'}).status_code, 401)
            search_key = {'Authorization': 'Bearer ' + 's' * 32}
            self.assertEqual(client.post('/api/v1/chat', headers=search_key,
                                         json={'query': 'Test'}).status_code, 403)
            self.chat.generate.assert_not_called()
            result = client.post('/api/v1/chat', headers={'Authorization': 'Bearer ' + 'a' * 32},
                                 json={'query': 'Test'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['sources'][0]['citation'], 'S1')
            old = client.post('/api/v1/knowledge/search', headers=search_key, json={'query': 'Test'})
            self.assertEqual(old.status_code, 200)
            self.assertNotIn('[S1]', old.json()['content'])
            self.assertNotIn('answer', old.json())
