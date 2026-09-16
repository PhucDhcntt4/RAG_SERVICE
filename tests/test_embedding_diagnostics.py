import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from google.genai import errors

from app.embeddings import EmbeddingError, GeminiEmbedder, classify_embedding_error
from app.main import create_app
from tests.test_service import config, make_service


class EmbeddingDiagnosticsTests(unittest.TestCase):
    def test_provider_categories_and_key_errors(self):
        for code, message, expected in (
            (400, "API key not valid", "invalid_key"),
            (403, "Your API key was reported as leaked", "blocked_key"),
            (403, "denied", "permission_denied"),
            (429, "quota", "rate_or_quota"),
            (404, "model missing", "model_not_found"),
            (400, "bad request", "invalid_request"),
            (503, "unavailable", "provider_unavailable"),
            (504, "timeout", "timeout"),
        ):
            with self.subTest(code=code, expected=expected):
                error = errors.APIError(code, {"error": {"message": message}})
                self.assertEqual(classify_embedding_error(error), (expected, code))

    def test_network_and_invalid_response_categories(self):
        for error, expected in (
            (httpx.ConnectError("private proxy"), "connection"),
            (httpx.ReadTimeout("private URL"), "timeout"),
            (ValueError("private response"), "invalid_response"),
            (TypeError("private SDK detail"), "unknown"),
        ):
            self.assertEqual(classify_embedding_error(error), (expected, None))

    def test_second_batch_error_logs_progress_without_provider_body(self):
        secret = "secret-key-and-document-content"
        with patch("app.embeddings.genai.Client") as client:
            client.return_value.models.embed_content.side_effect = [
                SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767)] * 32),
                errors.APIError(429, {"error": {"message": secret}}),
            ]
            with self.assertLogs("rag_service", level="ERROR") as logs:
                with self.assertRaises(EmbeddingError) as raised:
                    GeminiEmbedder(config()).embed([secret] * 33)
        output = "\n".join(logs.output)
        self.assertIn("reason=rate_or_quota", output)
        self.assertIn("provider_code=429", output)
        self.assertIn("batch=2 batch_size=1 completed_vectors=32", output)
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, raised.exception.public_message)

    def test_http_returns_safe_actionable_detail(self):
        service = make_service()
        service.embedder.embed.side_effect = EmbeddingError("secret-body", reason="rate_or_quota")
        with TestClient(create_app(service.settings, service)) as client:
            response = client.post(
                "/api/v1/documents/upload",
                headers={"Authorization": "Bearer " + "a" * 32},
                data={"source_key": "test", "title": "Test"},
                files={"file": ("a.txt", b"synthetic text", "text/plain")},
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("quota", response.json()["detail"])
        self.assertNotIn("secret-body", response.text)
        service.repository.replace.assert_not_called()
        service.storage.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
