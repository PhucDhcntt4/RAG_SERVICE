import unittest
from ipaddress import ip_address
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_service import config, make_service


class LocalAdminTests(unittest.TestCase):
    def setUp(self):
        self.settings = config()
        self.service = make_service(self.settings)
        self.chat = Mock()
        self.chat.answer.return_value = {'answer': 'Không đủ thông tin', 'status': 'insufficient_context',
            'sources': [], 'retrieval_query': 'Test', 'context': '', 'elapsed_ms': 1, 'model': 'test'}
        self.app = create_app(self.settings, self.service, chat=self.chat)
        self.headers = {'X-RAG-Local-UI': '1'}

    def client(self, peer='127.0.0.1', host='http://127.0.0.1:8000'):
        return TestClient(self.app, base_url=host, client=(peer, 50000))

    def test_local_dashboard_can_list_upload_preview_delete_and_chat_without_key(self):
        with self.client() as client:
            self.assertEqual(client.get('/admin-api/v1/documents', headers=self.headers).status_code, 200)
            upload = client.post('/admin-api/v1/documents/upload', headers=self.headers,
                data={'source_key': 'local.txt', 'title': 'Local'}, files={'file': ('local.txt', b'Test')})
            self.assertEqual(upload.status_code, 200)
            self.service.repository.get_document.return_value = {'id': 1, 'source_text': 'Test'}
            self.assertEqual(client.get('/admin-api/v1/documents/1', headers=self.headers).status_code, 200)
            self.service.repository.delete.return_value = {'id': 1, 'file_storage_key': None}
            self.assertEqual(client.delete('/admin-api/v1/documents/1', headers=self.headers).status_code, 200)
            self.assertEqual(client.post('/admin-api/v1/chat', headers=self.headers,
                                         json={'query': 'Test'}).status_code, 200)

    def test_bot_api_still_requires_key_even_from_localhost(self):
        with self.client() as client:
            self.assertEqual(client.get('/api/v1/documents', headers=self.headers).status_code, 401)
            self.assertEqual(client.post('/api/v1/knowledge/search', headers=self.headers,
                                         json={'query': 'Test'}).status_code, 401)
            search_key = {'Authorization': 'Bearer ' + 's' * 32}
            self.assertEqual(client.post('/api/v1/knowledge/search', headers=search_key,
                                         json={'query': 'Test'}).status_code, 200)
            self.assertEqual(client.delete('/api/v1/documents/1', headers=search_key).status_code, 403)

    def test_remote_and_rebinding_hosts_are_denied_before_upload(self):
        for peer, host in [('203.0.113.10', 'http://127.0.0.1:8000'),
                           ('127.0.0.1', 'http://attacker.example:8000')]:
            with self.subTest(peer=peer, host=host), self.client(peer, host) as client:
                self.assertEqual(client.post('/admin-api/v1/documents/upload', headers=self.headers,
                    content=b'invalid body').status_code, 403)
        self.service.storage.save.assert_not_called()

    @patch('app.local_admin._docker_default_gateway',
           return_value=ip_address('172.18.0.1'))
    def test_docker_gateway_can_use_local_dashboard_but_other_private_peers_cannot(self, _gateway):
        with self.client('172.18.0.1') as client:
            self.assertEqual(client.get('/admin-api/v1/documents',
                                        headers=self.headers).status_code, 200)
        with self.client('172.18.0.2') as client:
            self.assertEqual(client.get('/admin-api/v1/documents',
                                        headers=self.headers).status_code, 403)

    def test_cross_origin_forwarded_and_missing_custom_header_are_denied(self):
        variations = [{}, {**self.headers, 'Origin': 'https://attacker.example'},
            {**self.headers, 'Origin': 'http://127.0.0.1:8001'},
            {**self.headers, 'Sec-Fetch-Site': 'cross-site'},
            {**self.headers, 'X-Forwarded-For': '127.0.0.1'},
            {**self.headers, 'Forwarded': 'for=127.0.0.1'}]
        with self.client() as client:
            for headers in variations:
                with self.subTest(headers=headers):
                    self.assertEqual(client.get('/admin-api/v1/documents', headers=headers).status_code, 403)
            self.assertEqual(client.get('/admin-api/v1/documents', headers={**self.headers,
                'Origin': 'http://127.0.0.1:8000', 'Sec-Fetch-Site': 'same-origin'}).status_code, 200)

    def test_disabled_local_access_and_ipv6(self):
        self.settings.rag_local_admin_enabled = False
        with self.client() as client:
            self.assertEqual(client.get('/admin-api/v1/documents', headers=self.headers).status_code, 403)
        self.settings.rag_local_admin_enabled = True
        # Supply IPv6 Host directly: the installed TestClient transport cannot
        # parse bracketed IPv6 URLs, while the actual ASGI guard supports them.
        with self.client('::1') as client:
            self.assertEqual(client.get('/admin-api/v1/documents',
                headers={**self.headers, 'Host': '[::1]:8000', 'Origin': 'http://[::1]:8000'}).status_code, 200)

    def test_assets_do_not_contain_any_api_key(self):
        with self.client() as client:
            for path in ['/admin', '/assets/admin.js']:
                text = client.get(path).text
                for secret in ['a' * 32, 's' * 32, self.settings.gemini_api_key.get_secret_value()]:
                    self.assertNotIn(secret, text)
