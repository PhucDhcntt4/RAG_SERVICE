import math
import logging
import re
from time import perf_counter, sleep

import httpx
from google import genai
from google.genai import errors, types

from app.config import Settings
from app.debug_log import request_id


logger = logging.getLogger("rag_service")
EMBEDDING_MESSAGES = {
    "invalid_key": "Gemini API key không hợp lệ. Kiểm tra GEMINI_API_KEY rồi khởi động lại server.",
    "blocked_key": "Gemini đã chặn API key do phát hiện bị lộ. Thay GEMINI_API_KEY rồi khởi động lại server.",
    "permission_denied": "Gemini từ chối quyền truy cập. Kiểm tra API key và quyền sử dụng model.",
    "rate_or_quota": "Gemini đang giới hạn tốc độ hoặc đã hết quota. Kiểm tra quota trước khi thử lại.",
    "model_not_found": "Gemini không tìm thấy model embedding. Kiểm tra RAG_EMBEDDING_MODEL và quyền truy cập.",
    "invalid_request": "Gemini từ chối yêu cầu embedding. Kiểm tra model và cấu hình embedding trong log server.",
    "timeout": "Kết nối Gemini quá thời gian chờ. Kiểm tra mạng và RAG_EMBEDDING_TIMEOUT_SECONDS.",
    "connection": "Không kết nối được Gemini. Kiểm tra mạng, DNS hoặc proxy của server.",
    "provider_unavailable": "Dịch vụ Gemini tạm thời không khả dụng. Vui lòng thử lại sau.",
    "invalid_response": "Gemini trả về vector embedding không hợp lệ. Kiểm tra log server.",
    "unknown": "Tạo embedding chưa thành công. Kiểm tra log RAG EMBEDDING FAILED trên server.",
}


class EmbeddingError(RuntimeError):
    def __init__(self, message, *, reason="unknown", provider_code=None):
        super().__init__(message)
        self.reason = reason if reason in EMBEDDING_MESSAGES else "unknown"
        self.provider_code = provider_code

    @property
    def public_message(self):
        # Never expose the exception message/cause supplied by a provider.
        return EMBEDDING_MESSAGES[self.reason]


def classify_embedding_error(exc):
    """Return fixed categories and numeric status only, never response bodies."""
    if isinstance(exc, errors.APIError):
        code = exc.code if type(exc.code) is int and 400 <= exc.code <= 599 else None
        # Some API-key failures use HTTP 400, others HTTP 403. Read the provider
        # message for classification, but never include it in logs or responses.
        message = str(exc.message or "").lower()
        if "reported as leaked" in message or "api_key_leaked" in message:
            return "blocked_key", code
        if "api key not valid" in message or "api_key_invalid" in message or code == 401:
            return "invalid_key", code
        reason = {
            400: "invalid_request", 403: "permission_denied", 404: "model_not_found",
            408: "timeout", 429: "rate_or_quota", 504: "timeout",
        }.get(code, "provider_unavailable" if code and code >= 500 else "unknown")
        return reason, code
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout", None
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return "connection", None
    if isinstance(exc, ValueError):
        return "invalid_response", None
    return "unknown", None


def quota_details(exc):
    """Extract safe quota categories and numeric delay; omit response contents."""
    windows = set()
    zero_limit = False
    retry_after = None
    if not isinstance(exc, errors.APIError):
        return windows, zero_limit, retry_after
    body = exc.details if isinstance(exc.details, dict) else {}
    body = body.get("error", body)
    if not isinstance(body, dict):
        return windows, zero_limit, retry_after
    details = body.get("details", [])
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and re.fullmatch(r"\d{1,8}(?:\.\d{1,9})?s", delay):
            retry_after = max(retry_after or 0, float(delay[:-1]))
        violations = detail.get("violations", [])
        for violation in violations if isinstance(violations, list) else []:
            if not isinstance(violation, dict):
                continue
            quota_id = str(violation.get("quotaId", "")).lower()
            for window in ("minute", "day", "month"):
                if window in quota_id:
                    windows.add(window)
            zero_limit |= str(violation.get("quotaValue", "")) == "0"
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("retry-after", "")
        if re.fullmatch(r"\d{1,8}(?:\.\d{1,9})?", header):
            retry_after = max(retry_after or 0, float(header))
    return windows, zero_limit, retry_after


class GeminiEmbedder:
    provider = "gemini"
    dimension = 768

    def __init__(self, settings: Settings):
        self.model = settings.rag_embedding_model
        self.batch_size = settings.rag_embedding_batch_size
        self.batch_interval = settings.rag_embedding_batch_interval_seconds
        self.max_retries = settings.rag_embedding_max_retries
        self.client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(
                timeout=settings.rag_embedding_timeout_seconds * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def close(self):
        self.client.close()

    def _embed_batch(self, batch, query, batch_number):
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.models.embed_content(
                    model=self.model, contents=batch,
                    config=types.EmbedContentConfig(
                        task_type="QUESTION_ANSWERING" if query else "RETRIEVAL_DOCUMENT",
                        output_dimensionality=self.dimension,
                    ),
                )
            except errors.APIError as exc:
                windows, zero_limit, retry_after = quota_details(exc)
                # Never retry known exhausted daily/monthly or zero quotas.
                if (exc.code not in (429, 503) or attempt >= self.max_retries
                        or zero_limit or windows & {"day", "month"}):
                    raise
                delay = max(5 * 2 ** attempt, retry_after or 0)
                if delay > 60:
                    raise  # Do not retry earlier than the provider requested.
                logger.warning(
                    "RAG EMBEDDING RETRY request_id=%s batch=%s retry=%s provider_code=%s wait_seconds=%.2f",
                    request_id.get(), batch_number, attempt + 1, exc.code, delay,
                )
                sleep(delay)

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        vectors = []
        started = perf_counter()
        batch_number, batch_size = 0, 0
        try:
            for start in range(0, len(texts), self.batch_size):
                if start and self.batch_interval:
                    sleep(self.batch_interval)
                batch = texts[start:start + self.batch_size]
                batch_number, batch_size = start // self.batch_size + 1, len(batch)
                response = self._embed_batch(batch, query, batch_number)
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
                if not query and len(texts) > self.batch_size:
                    logger.info(
                        "RAG EMBEDDING PROGRESS request_id=%s batch=%s completed_vectors=%s total_vectors=%s",
                        request_id.get(), batch_number, len(vectors), len(texts),
                    )
        except Exception as exc:
            reason, code = classify_embedding_error(exc)
            windows, zero_limit, retry_after = quota_details(exc)
            logger.error(
                "RAG EMBEDDING FAILED request_id=%s reason=%s provider_code=%s "
                "error_type=%s model=%s batch=%s batch_size=%s completed_vectors=%s time_ms=%.2f "
                "quota_windows=%s zero_quota=%s retry_after_seconds=%s",
                request_id.get(), reason, code, type(exc).__name__, self.model,
                batch_number, batch_size, len(vectors), (perf_counter() - started) * 1000,
                ",".join(sorted(windows)) or "unknown", zero_limit, retry_after,
            )
            raise EmbeddingError(EMBEDDING_MESSAGES[reason], reason=reason, provider_code=code) from exc
        return vectors
