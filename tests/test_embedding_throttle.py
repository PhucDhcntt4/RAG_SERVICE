import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import errors

from app.embeddings import EmbeddingError, GeminiEmbedder, quota_details
from tests.test_service import config


def response(texts):
    return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767) for _ in texts])


def quota_error(quota_id="EmbedContentPerMinute", value="100", delay="15s"):
    return errors.APIError(429, {"error": {"message": "private provider data", "details": [
        {"violations": [{"quotaId": quota_id, "quotaValue": value}]},
        {"retryDelay": delay},
    ]}})


class EmbeddingThrottleTests(unittest.TestCase):
    def test_193_chunks_use_small_batches_in_order_with_spacing(self):
        texts = [f"synthetic-{i}" for i in range(193)]
        with patch("app.embeddings.genai.Client") as client, patch("app.embeddings.sleep") as sleep:
            call = client.return_value.models.embed_content
            call.side_effect = lambda **kwargs: response(kwargs["contents"])
            embedder = GeminiEmbedder(config(rag_embedding_batch_size=8, rag_embedding_batch_interval_seconds=5))
            vectors = embedder.embed(texts)
        self.assertEqual(len(vectors), 193)
        self.assertEqual(call.call_count, 25)
        self.assertEqual([text for c in call.call_args_list for text in c.kwargs["contents"]], texts)
        self.assertTrue(all(len(c.kwargs["contents"]) <= 8 for c in call.call_args_list))
        self.assertEqual(sleep.call_count, 24)
        self.assertTrue(all(c.args == (5,) for c in sleep.call_args_list))

    def test_retry_only_failed_batch_and_honor_provider_delay(self):
        with patch("app.embeddings.genai.Client") as client, patch("app.embeddings.sleep") as sleep:
            call = client.return_value.models.embed_content
            call.side_effect = [response(["a", "b"]), quota_error(), response(["c"])]
            vectors = GeminiEmbedder(config(rag_embedding_batch_size=2, rag_embedding_max_retries=2)).embed(["a", "b", "c"])
        self.assertEqual(len(vectors), 3)
        self.assertEqual([c.kwargs["contents"] for c in call.call_args_list], [["a", "b"], ["c"], ["c"]])
        sleep.assert_called_once_with(15)

    def test_retry_limit_is_bounded(self):
        with patch("app.embeddings.genai.Client") as client, patch("app.embeddings.sleep") as sleep:
            call = client.return_value.models.embed_content
            call.side_effect = quota_error(delay="1s")
            with self.assertRaises(EmbeddingError):
                GeminiEmbedder(config(rag_embedding_max_retries=2)).embed(["a"])
        self.assertEqual(call.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_known_daily_zero_long_wait_and_auth_errors_are_not_retried(self):
        for error in (quota_error(quota_id="EmbedContentPerDay"), quota_error(value="0"),
                      quota_error(delay="65s"), errors.APIError(403, {"error": {"message": "denied"}})):
            with self.subTest(error_type=type(error).__name__), patch("app.embeddings.genai.Client") as client, patch("app.embeddings.sleep") as sleep:
                call = client.return_value.models.embed_content
                call.side_effect = error
                with self.assertRaises(EmbeddingError):
                    GeminiEmbedder(config(rag_embedding_max_retries=2)).embed(["a"])
                self.assertEqual(call.call_count, 1)
                sleep.assert_not_called()

    def test_diagnostics_omit_raw_quota_identifiers(self):
        windows, zero, delay = quota_details(quota_error(quota_id="private_perminute_secret"))
        self.assertEqual(windows, {"minute"})
        self.assertFalse(zero)
        self.assertEqual(delay, 15)
        self.assertEqual(quota_details(errors.APIError(429, {"error": {"details": "invalid"}})), (set(), False, None))


if __name__ == "__main__":
    unittest.main()
