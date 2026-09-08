import io
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore
from time import perf_counter

from pypdf import PdfReader

from app.chunking import chunk_text
from app.debug_log import debug_event
from app.models import DocumentRequest
from app.storage import LocalFileStore


class InvalidDocument(ValueError):
    pass


class ServiceBusy(RuntimeError):
    pass


class KnowledgeService:
    def __init__(self, settings, repository, embedder, storage=None):
        self.settings = settings
        self.repository = repository
        self.embedder = embedder
        self.storage = storage if storage is not None else LocalFileStore()
        self.slots = BoundedSemaphore(settings.rag_max_concurrent_requests)

    @contextmanager
    def capacity(self):
        if not self.slots.acquire(blocking=False):
            raise ServiceBusy("Service đang bận, vui lòng thử lại")
        try:
            yield
        finally:
            self.slots.release()

    def ingest(self, document: DocumentRequest, *, original_data=None, original_filename=None):
        if len(document.text) > self.settings.rag_max_document_chars or "\x00" in document.text:
            raise InvalidDocument("Tài liệu quá dài hoặc chứa ký tự không hợp lệ")
        chunks = chunk_text(document.text, self.settings.rag_chunk_size,
                            self.settings.rag_chunk_overlap)
        if not chunks:
            raise InvalidDocument("Tài liệu không có nội dung văn bản để lập chỉ mục")
        if len(chunks) > self.settings.rag_max_chunks:
            raise InvalidDocument("Quá nhiều đoạn; hãy chia tài liệu thành các phần nhỏ hơn")
        with self.capacity():
            # Finish ALL embeddings before opening the write transaction.
            vectors = self.embedder.embed([
                f"{document.title}\n{c.heading or ''}\n{c.content}" for c in chunks
            ])
            stored = self.storage.save(
                original_data if original_data is not None else document.text.encode("utf-8"),
                original_filename if original_data is not None else document.title + ".txt",
                origin="upload" if original_data is not None else "text",
            )
            # Persist the complete file BEFORE committing a database reference to it.
            # A DB/commit failure may leave an unreferenced file. Retain it: a lost
            # commit acknowledgement is ambiguous and deleting could lose a committed file.
            result = self.repository.replace(document, chunks, vectors, self.embedder, stored_file=stored)
            old_key = result.pop("_old_file_key", None)
            if not self.storage.remove(old_key):
                result["warning"] = "Đã lưu tài liệu mới, nhưng chưa xóa được file cũ trên máy chủ."
            return result

    def delete(self, document_id):
        row = self.repository.delete(document_id)
        if row is None:
            return None
        removed = self.storage.remove(row.pop("file_storage_key", None))
        result = {**row, "local_file_deleted": removed}
        if not removed:
            result["warning"] = "Đã xóa dữ liệu tìm kiếm, nhưng chưa xóa được file local. Cần kiểm tra thư mục lưu trữ trên máy chủ."
        return result

    def extract(self, filename: str, data: bytes) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".txt", ".md", ".pdf"}:
            raise InvalidDocument("Chỉ hỗ trợ TXT, MD và PDF có lớp văn bản")
        if len(data) > self.settings.rag_max_upload_bytes:
            raise InvalidDocument("File vượt giới hạn dung lượng")
        try:
            if suffix != ".pdf":
                text = data.decode("utf-8-sig")
            else:
                reader = PdfReader(io.BytesIO(data))
                if reader.is_encrypted or len(reader.pages) > 200:
                    raise InvalidDocument("PDF mã hóa hoặc quá 200 trang không được hỗ trợ")
                parts, length = [], 0
                for page in reader.pages:
                    part = page.extract_text() or ""
                    length += len(part) + 2
                    if length > self.settings.rag_max_document_chars:
                        raise InvalidDocument("PDF có quá nhiều nội dung")
                    parts.append(part)
                text = "\n\n".join(parts)
        except InvalidDocument:
            raise
        except Exception as exc:
            raise InvalidDocument("Không đọc được file; TXT/MD cần mã hóa UTF-8") from exc
        if not text.strip():
            raise InvalidDocument("Không tìm thấy văn bản; PDF scan cần OCR trước khi tải lên")
        return text

    def search(self, request, *, numbered_sources=False):
        started = perf_counter()
        top_k = request.top_k or self.settings.rag_top_k
        debug_event('RAG SEARCH', query=request.query, categories=request.categories or [],
                    category_scope='filtered' if request.categories else 'all', top_k=top_k,
                    min_similarity=self.settings.rag_min_similarity,
                    embedding_model=self.embedder.model, context_limit=self.settings.rag_max_context_chars)
        with self.capacity():
            stage = 'embedding'
            stage_started = perf_counter()
            try:
                vector = self.embedder.embed([request.query], query=True)[0]
                debug_event('RAG EMBEDDING', dimension=len(vector),
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
                stage = 'database_search'
                stage_started = perf_counter()
                rows = self.repository.search(
                    vector, self.embedder, request.categories, self.settings.rag_min_similarity, top_k)
                debug_event('RAG RETRIEVAL', matched_chunks=len(rows),
                            matched_categories=sorted({row['category'] for row in rows}),
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
            except Exception as exc:
                debug_event('RAG SEARCH FAILED', stage=stage, error_type=type(exc).__name__,
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
                raise
        parts, sources, used = [], [], 0
        context_full = False
        for rank, row in enumerate(rows, 1):
            label = row["title"] + (f" > {row['heading']}" if row.get("heading") else "")
            prefix = f"[Nguồn: {label}]\n"
            if numbered_sources:
                prefix = f"[S{len(sources) + 1}] " + prefix
            room = self.settings.rag_max_context_chars - used - (2 if parts else 0)
            metadata = {key: row.get(key) for key in (
                'source_key', 'title', 'category', 'heading', 'chunk_index', 'similarity')}
            if context_full or room <= len(prefix):
                context_full = True
                debug_event('RAG SOURCE', rank=rank, selected=False, reason='context_limit', **metadata)
                continue
            part = prefix + row["content"][:room - len(prefix)]
            debug_event('RAG SOURCE', rank=rank, selected=True,
                        citation=f'S{len(sources) + 1}' if numbered_sources else None,
                        content_chars=len(row['content']), included_chars=len(part) - len(prefix),
                        truncated=len(part) - len(prefix) < len(row['content']), **metadata)
            used += len(part) + (2 if parts else 0)
            parts.append(part)
            sources.append({key: row.get(key) for key in (
                "source_key", "title", "category", "heading", "chunk_index", "similarity"
            )})
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        debug_event('RAG CONTEXT', status='knowledge_found' if parts else 'knowledge_not_found',
                    selected_chunks=len(sources), context_chars=used, time_ms=elapsed_ms)
        return {
            "success": bool(parts),
            "status": "knowledge_found" if parts else "knowledge_not_found",
            "content": "\n\n".join(parts),
            "sources": sources,
            "elapsed_ms": elapsed_ms,
        }
