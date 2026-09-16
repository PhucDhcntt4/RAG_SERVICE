"""Stateless RAG chat: resolve follow-ups, retrieve, then answer with source IDs."""
import json
import logging
from pathlib import Path
from threading import BoundedSemaphore
from time import perf_counter

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from app.models import SearchRequest
from app.debug_log import debug_event
from app.service import ServiceBusy
from app.retrieval import needs_document_context, needs_section_context

logger = logging.getLogger('rag_service')
PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'


class ChatError(RuntimeError):
    pass


def load_prompt(name):
    """Read on each request so local prompt edits apply without restarting Python."""
    try:
        text = (PROMPT_DIR / name).read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError) as exc:
        logger.error('Cannot read chat prompt file=%s error_type=%s', name, type(exc).__name__)
        raise ChatError(f'Không đọc được file hướng dẫn app/prompts/{name}. Kiểm tra file UTF-8.') from exc
    if not text:
        raise ChatError(f'File hướng dẫn app/prompts/{name} đang trống.')
    return text


class ResolvedQuestion(BaseModel):
    query: str = Field(min_length=1, max_length=4000)


class AnswerStatement(BaseModel):
    # The final answer has its own configured total bound. A list model may
    # occasionally group many rows into one statement even when the prompt asks
    # for one row per statement, so a smaller per-statement cap caused valid
    # complete lists to fail with a 503 after generation.
    text: str = Field(min_length=1, max_length=20000)
    citations: list[int] = Field(min_length=1, max_length=100)


class AnswerDraft(BaseModel):
    sufficient: bool
    statements: list[AnswerStatement] = Field(default_factory=list, max_length=100)


def generation_schema(schema):
    # Nested length limits can exceed Gemini's grammar complexity. Keep the wire
    # shape simple; model_validate_json below still enforces every local limit.
    def simplify(value):
        if isinstance(value, dict):
            return {key: simplify(item) for key, item in value.items()
                    if key not in {'minItems', 'maxItems', 'minLength', 'maxLength'}}
        if isinstance(value, list):
            return [simplify(item) for item in value]
        return value
    return simplify(schema.model_json_schema())


class GeminiChat:
    def __init__(self, settings):
        self.model = settings.rag_chat_model
        self.max_statements = settings.rag_chat_max_statements
        self.max_answer_chars = settings.rag_chat_max_answer_chars
        self.slots = BoundedSemaphore(settings.rag_max_concurrent_requests)
        self.client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=settings.rag_chat_timeout_seconds * 1000,
                retry_options=types.HttpRetryOptions(attempts=1)))

    def close(self):
        self.client.close()

    def answer_prompt(self):
        prompt = load_prompt('answer.txt')
        values = {'max_statements': self.max_statements,
                  'max_answer_chars': self.max_answer_chars,
                  'answer_text_budget': self.max_answer_chars * 9 // 10}
        for name, value in values.items():
            prompt = prompt.replace('{{' + name + '}}', str(value))
        return prompt

    def generate(self, instruction, payload, schema):
        started = perf_counter()
        stage = 'answer' if schema is AnswerDraft else 'rewrite'
        debug_event('RAG LLM START', stage=stage, model=self.model)
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=json.dumps(payload, ensure_ascii=False),
                config=types.GenerateContentConfig(system_instruction=instruction,
                    temperature=0.1, max_output_tokens=8192 if schema is AnswerDraft else 4096,
                    response_mime_type='application/json', response_json_schema=generation_schema(schema)))
            if not response.candidates or response.candidates[0].finish_reason != types.FinishReason.STOP:
                raise ValueError('Incomplete or blocked response')
            result = schema.model_validate_json(response.text or '')
            if isinstance(result, AnswerDraft) and len(result.statements) > self.max_statements:
                raise ValueError('Answer exceeds configured statement limit')
            debug_event('RAG LLM DONE', stage=stage, model=self.model,
                        time_ms=round((perf_counter() - started) * 1000, 2))
            return result
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if isinstance(exc, ValidationError):
                issues = [
                    {
                        'loc': '.'.join(str(part) for part in issue.get('loc', ())),
                        'type': issue.get('type'),
                    }
                    for issue in exc.errors(include_input=False, include_url=False)
                ]
                logger.warning('RAG response schema invalid stage=%s issues=%s',
                               stage, issues)
            debug_event('RAG LLM FAILED', stage=stage, model=self.model,
                        http_code=code if isinstance(code, int) else None, error_type=type(exc).__name__,
                        time_ms=round((perf_counter() - started) * 1000, 2))
            logger.warning('RAG chat unavailable http_code=%s error_type=%s',
                           code if isinstance(code, int) else None, type(exc).__name__)
            message = 'Gemini chưa tạo được câu trả lời. Hãy thử lại.'
            if code == 429:
                message = 'Gemini báo giới hạn quota/tần suất (429). Đợi rồi thử lại hoặc kiểm tra Google AI Studio.'
            elif code in (400, 401, 403, 404):
                message = 'Gemini từ chối yêu cầu. Kiểm tra GEMINI_API_KEY và RAG_CHAT_MODEL rồi khởi động lại server.'
            raise ChatError(message) from exc

    def answer(self, request, service):
        if not self.slots.acquire(blocking=False):
            raise ServiceBusy('Trợ lý đang bận, vui lòng thử lại')
        try:
            return self._answer(request, service)
        finally:
            self.slots.release()

    def _answer(self, request, service):
        started = perf_counter()
        history = [message.model_dump() for message in request.history]
        query = request.query
        debug_event('RAG CHAT INPUT', query=query, categories=request.categories or [],
                    history_messages=len(history), model=self.model)
        if history:
            rewritten = self.generate(
                load_prompt('rewrite_question.txt'),
                {'history': history, 'question': request.query}, ResolvedQuestion)
            query = rewritten.query.strip()
            if not query:
                raise ChatError('Chưa xác định được câu hỏi. Hãy nhập câu hỏi đầy đủ hơn.')
        debug_event('RAG CHAT QUERY', original_query=request.query, retrieval_query=query,
                    rewritten=query != request.query, categories=request.categories or [])
        found = service.search(SearchRequest(query=query, categories=request.categories,
                                             top_k=request.top_k), numbered_sources=True,
                               expand_documents=needs_document_context(query),
                               expand_sections=needs_section_context(query))
        response = {'answer': load_prompt('no_answer.txt'), 'status': 'insufficient_context', 'sources': [],
                    'retrieval_query': query, 'context': found['content'], 'model': self.model}
        if found['success'] and found['sources']:
            draft = self.generate(
                self.answer_prompt(),
                {'question': request.query, 'resolved_question': query,
                 'history': history, 'context': found['content'],
                 'retrieval_coverage': found.get('retrieval_coverage', {})}, AnswerDraft)
            if draft.sufficient:
                if len(draft.statements) > self.max_statements:
                    raise ChatError(f'Câu trả lời vượt giới hạn {self.max_statements} ý. Hãy thu hẹp câu hỏi hoặc điều chỉnh RAG_CHAT_MAX_STATEMENTS.')
                ids = list(dict.fromkeys(i for statement in draft.statements for i in statement.citations))
                if not ids or any(i < 1 or i > len(found['sources']) for i in ids):
                    raise ChatError('Câu trả lời chưa có nguồn trích dẫn hợp lệ. Hãy thử lại.')
                answer = '\n\n'.join(statement.text.strip() + ' ' + ' '.join(
                    f'[S{i}]' for i in dict.fromkeys(statement.citations)) for statement in draft.statements)
                if len(answer) > self.max_answer_chars:
                    raise ChatError(f'Câu trả lời vượt giới hạn {self.max_answer_chars} ký tự. Hãy thu hẹp câu hỏi hoặc điều chỉnh RAG_CHAT_MAX_ANSWER_CHARS.')
                response.update(answer=answer, status='answered', sources=[
                    {**found['sources'][i - 1], 'citation': f'S{i}'} for i in ids])
        response['elapsed_ms'] = round((perf_counter() - started) * 1000, 2)
        debug_event('RAG CHAT RESULT', status=response['status'], answer_chars=len(response['answer']),
                    citations=[source['citation'] for source in response['sources']],
                    source_categories=sorted({source['category'] for source in response['sources']}),
                    time_ms=response['elapsed_ms'])
        return response
