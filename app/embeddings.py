import math

from google import genai
from google.genai import types

from app.config import Settings


class EmbeddingError(RuntimeError):
    pass


class GeminiEmbedder:
    provider = "gemini"
    dimension = 768

    def __init__(self, settings: Settings):
        self.model = settings.rag_embedding_model
        self.client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(
                timeout=settings.rag_embedding_timeout_seconds * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def close(self):
        self.client.close()

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        vectors = []
        try:
            for start in range(0, len(texts), 32):
                batch = texts[start:start + 32]
                response = self.client.models.embed_content(
                    model=self.model,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type="QUESTION_ANSWERING" if query else "RETRIEVAL_DOCUMENT",
                        output_dimensionality=self.dimension,
                    ),
                )
                items = response.embeddings or []
                if len(items) != len(batch):
                    raise ValueError("Embedding count mismatch")
                for item in items:
                    values = list(item.values or [])
                    if len(values) != self.dimension or not all(math.isfinite(v) for v in values):
                        raise ValueError("Invalid embedding vector")
                    norm = math.sqrt(sum(v * v for v in values))
                    if not math.isfinite(norm) or norm == 0:
                        raise ValueError("Invalid embedding magnitude")
                    vectors.append([v / norm for v in values])
        except Exception as exc:
            # Do not propagate provider response bodies or credentials to HTTP/logs.
            raise EmbeddingError("Dịch vụ embedding tạm thời không khả dụng") from exc
        return vectors
