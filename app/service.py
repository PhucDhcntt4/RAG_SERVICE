import logging
import math
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, RLock
from time import perf_counter

from app.bm25 import BM25Index
from app.adaptive_chunking import adaptive_chunk_text
from app.chunking import chunk_text
from app.debug_log import debug_event
from app.models import DocumentRequest
from app.pdf_chunking import prepare_pdf_document
from app.pdf_extract import extract_pdf
from app.search_text import build_search_text
from app.retrieval import (
    condense_part_overview,
    heading_context_match,
    reciprocal_rank_fusion,
    select_section_scopes,
)
from app.storage import LocalFileStore


logger = logging.getLogger("rag_service")


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
        self._bm25_lock = RLock()
        self._bm25_index = None

    @property
    def bm25_count(self):
        index = self._bm25_index
        return index.count if index is not None else 0

    def refresh_bm25(self):
        """Build a complete index, then publish it atomically for readers."""
        rows = self.repository.list_search_chunks(self.embedder)
        index = BM25Index(rows)
        with self._bm25_lock:
            self._bm25_index = index
        debug_event("RAG BM25 INDEX", indexed_chunks=index.count)
        return index.count

    def _bm25_search(self, query, categories, limit):
        index = self._bm25_index
        if index is None:
            self.refresh_bm25()
            index = self._bm25_index
        return index.search(query, categories=categories, limit=limit)

    def _refresh_bm25_after_write(self, result):
        if not self.settings.rag_hybrid_enabled:
            return
        try:
            self.refresh_bm25()
        except Exception as exc:
            # The database transaction already committed. Keep the API result
            # truthful and retry index construction on the next search.
            with self._bm25_lock:
                self._bm25_index = None
            logger.error("RAG BM25 refresh failed error_type=%s", type(exc).__name__)
            if result is not None:
                warning = ("Dữ liệu đã thay đổi nhưng chỉ mục BM25 chưa làm mới; "
                           "hệ thống sẽ thử lại khi tìm kiếm.")
                result["warning"] = (result.get("warning", "") + " " + warning).strip()

    @contextmanager
    def capacity(self):
        if not self.slots.acquire(blocking=False):
            raise ServiceBusy("Service đang bận, vui lòng thử lại")
        try:
            yield
        finally:
            self.slots.release()

    def ingest(self, document: DocumentRequest, *, original_data=None, original_filename=None,
               prepared_chunks=None):
        if len(document.text) > self.settings.rag_max_document_chars or "\x00" in document.text:
            raise InvalidDocument("Tài liệu quá dài hoặc chứa ký tự không hợp lệ")
        if prepared_chunks is not None and len(prepared_chunks) > self.settings.rag_max_chunks:
            raise InvalidDocument("Quá nhiều đoạn; hãy chia tài liệu thành các phần nhỏ hơn")
        # Reject documents that cannot possibly fit before making a paid semantic call.
        minimum_chunks = math.ceil(len(document.text) / self.settings.rag_chunk_size)
        if minimum_chunks > self.settings.rag_max_chunks:
            raise InvalidDocument("Quá nhiều đoạn; hãy chia tài liệu thành các phần nhỏ hơn")

        natural_headings = Path(original_filename or "").suffix.lower() != ".pdf"
        with self.capacity():
            chunking_stats = None
            if self.settings.rag_chunking_strategy == "adaptive":
                chunks, chunking_stats = adaptive_chunk_text(
                    document.text,
                    self.embedder,
                    max_chars=self.settings.rag_chunk_size,
                    # Keep older small RAG_CHUNK_SIZE configurations valid.
                    min_chars=min(
                        self.settings.rag_semantic_min_chars,
                        self.settings.rag_chunk_size - 1,
                    ),
                    breakpoint_percentile=(
                        self.settings.rag_semantic_breakpoint_percentile
                    ),
                    natural_headings=natural_headings,
                    base_chunks=prepared_chunks,
                )
            else:
                chunks = prepared_chunks if prepared_chunks is not None else chunk_text(
                    document.text,
                    self.settings.rag_chunk_size,
                    self.settings.rag_chunk_overlap,
                    natural_headings=natural_headings,
                )
            if not chunks:
                raise InvalidDocument("Tài liệu không có nội dung văn bản để lập chỉ mục")
            if len(chunks) > self.settings.rag_max_chunks:
                raise InvalidDocument("Quá nhiều đoạn; hãy chia tài liệu thành các phần nhỏ hơn")
            # Finish ALL embeddings before opening the write transaction.
            vectors = self.embedder.embed([
                build_search_text(
                    title=document.title,
                    heading=chunk.heading,
                    content=chunk.content,
                    section_path=chunk.section_path,
                )
                for chunk in chunks
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
            if chunking_stats is not None:
                result["chunking_method"] = chunking_stats.method
                result["chunking_stats"] = {
                    "sections": chunking_stats.sections,
                    "semantic_units": chunking_stats.semantic_units,
                    "semantic_sections": chunking_stats.semantic_sections,
                    "average_threshold": chunking_stats.average_threshold,
                    "recursive_splits": chunking_stats.recursive_splits,
                }
            else:
                result["chunking_method"] = "fixed"
            old_key = result.pop("_old_file_key", None)
            if not self.storage.remove(old_key):
                result["warning"] = "Đã lưu tài liệu mới, nhưng chưa xóa được file cũ trên máy chủ."
            self._refresh_bm25_after_write(result)
            return result

    def delete(self, document_id):
        row = self.repository.delete(document_id)
        if row is None:
            return None
        removed = self.storage.remove(row.pop("file_storage_key", None))
        result = {**row, "local_file_deleted": removed}
        if not removed:
            result["warning"] = "Đã xóa dữ liệu tìm kiếm, nhưng chưa xóa được file local. Cần kiểm tra thư mục lưu trữ trên máy chủ."
        self._refresh_bm25_after_write(result)
        return result

    def set_active(self, document_id, active):
        row = self.repository.set_active(document_id, active)
        if row is not None:
            self._refresh_bm25_after_write(row)
        return row

    def set_classification(self, document_id, group):
        row = self.repository.set_classification(
            document_id,
            category=group["code"],
            doc_type_id=group["doc_type_id"],
            group_id=group["id"],
        )
        if row is not None:
            self._refresh_bm25_after_write(row)
        return row

    def extract(self, filename: str, data: bytes) -> str:
        """Compatibility helper for callers that only need the document text."""
        return self.prepare_upload(filename, data)[0]

    def prepare_upload(self, filename: str, data: bytes):
        """Return source text and optional PDF chunks; parse each upload once."""
        suffix = Path(filename).suffix.lower()
        if suffix not in {".txt", ".md", ".pdf"}:
            raise InvalidDocument("Chỉ hỗ trợ TXT, MD và PDF có lớp văn bản")
        if len(data) > self.settings.rag_max_upload_bytes:
            raise InvalidDocument("File vượt giới hạn dung lượng")
        chunks = None
        try:
            if suffix != ".pdf":
                text = data.decode("utf-8-sig")
            else:
                with self.capacity():
                    started = perf_counter()
                    extraction = extract_pdf(
                        data, max_pages=200,
                        max_chars=self.settings.rag_max_document_chars,
                        max_bytes=self.settings.rag_max_upload_bytes,
                    )
                    if extraction.pages_without_text:
                        pages = ", ".join(map(str, extraction.pages_without_text))
                        raise InvalidDocument(
                            f"PDF có trang không đọc được chữ: {pages}. "
                            "Kiểm tra trang trắng hoặc OCR trang scan trước khi tải lên."
                        )
                    text, chunks = prepare_pdf_document(
                        extraction, self.settings.rag_chunk_size,
                        self.settings.rag_chunk_overlap,
                        split_sections=self.settings.rag_chunking_strategy != "adaptive",
                    )
                    debug_event(
                        'RAG PDF PREPARED', pages=extraction.page_count,
                        extracted_lines=len(extraction.lines), chunks=len(chunks),
                        tables=len(extraction.tables),
                        table_rows=sum(len(table.rows) for table in extraction.tables),
                        sections=len({chunk.section_path for chunk in chunks}),
                        time_ms=round((perf_counter() - started) * 1000, 2),
                    )
        except (InvalidDocument, ServiceBusy):
            raise
        except ValueError as exc:
            message = str(exc) if suffix == ".pdf" else "TXT/MD cần mã hóa UTF-8"
            raise InvalidDocument(message) from exc
        except Exception as exc:
            raise InvalidDocument("Không đọc được file; kiểm tra file PDF hoặc mã hóa UTF-8 của TXT/MD") from exc
        if not text.strip():
            raise InvalidDocument("Không tìm thấy văn bản; PDF scan cần OCR trước khi tải lên")
        return text, chunks

    def search(self, request, *, numbered_sources=False, expand_documents=False,
               expand_sections=False):
        started = perf_counter()
        retrieval_scope = 'top_k_chunks'
        expansion_limited = False
        top_k = request.top_k or self.settings.rag_top_k
        hybrid = self.settings.rag_hybrid_enabled
        candidate_limit = max(top_k, self.settings.rag_hybrid_candidates) if hybrid else top_k
        debug_event('RAG SEARCH', query=request.query, categories=request.categories or [],
                    category_scope='filtered' if request.categories else 'all', top_k=top_k,
                    retrieval='hybrid' if hybrid else 'vector', candidates=candidate_limit,
                    min_similarity=self.settings.rag_min_similarity,
                    embedding_model=self.embedder.model, context_limit=self.settings.rag_max_context_chars)
        with self.capacity():
            stage = 'bm25_search'
            stage_started = perf_counter()
            try:
                bm25_rows = self._bm25_search(
                    request.query, request.categories, candidate_limit,
                ) if hybrid else []
                debug_event('RAG BM25', candidates=len(bm25_rows),
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
                stage = 'embedding'
                stage_started = perf_counter()
                vector = self.embedder.embed([request.query], query=True)[0]
                debug_event('RAG EMBEDDING', dimension=len(vector),
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
                stage = 'vector_search'
                stage_started = perf_counter()
                vector_rows = self.repository.search(
                    vector, self.embedder, request.categories,
                    self.settings.rag_min_similarity, candidate_limit,
                )
                debug_event('RAG VECTOR', candidates=len(vector_rows),
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
                if hybrid:
                    stage = 'rrf_fusion'
                    stage_started = perf_counter()
                    rows = reciprocal_rank_fusion(
                        bm25_rows, vector_rows, k=self.settings.rag_rrf_k, limit=top_k,
                    )
                    for rank, row in enumerate(rows, 1):
                        debug_event(
                            'RAG RRF CANDIDATE', rank=rank, chunk_id=row['chunk_id'],
                            document_id=row.get('document_id'), bm25_rank=row.get('bm25_rank'),
                            vector_rank=row.get('vector_rank'),
                            bm25_score=round(row['bm25_score'], 6)
                            if row.get('bm25_score') is not None else None,
                            similarity=round(row['similarity'], 6)
                            if row.get('similarity') is not None else None,
                            rrf_score=round(row['rrf_score'], 6),
                        )
                    debug_event('RAG RRF', candidates=len(rows), rrf_k=self.settings.rag_rrf_k,
                                time_ms=round((perf_counter() - stage_started) * 1000, 2))
                else:
                    rows = vector_rows
                structural_section_context = heading_context_match(rows, request.query) if rows else False
                if (expand_sections or structural_section_context) and rows:
                    scopes = select_section_scopes(rows, request.query)
                    selected = scopes[:self.settings.rag_overview_max_documents]
                    if selected:
                        stage = 'section_expansion'
                        limit = self.settings.rag_overview_max_chunks
                        expanded = self.repository.expand_sections(
                            vector, selected, self.embedder, request.categories, limit + 1)
                        if expanded:
                            expansion_limited = len(expanded) > limit or len(scopes) > len(selected)
                            rows = condense_part_overview(expanded[:limit], selected)
                            retrieval_scope = 'selected_sections'
                        debug_event('RAG SECTION EXPANSION', scopes=selected,
                                    trigger='heading_match' if structural_section_context
                                    and not expand_sections else 'query_intent',
                                    expanded_chunks=len(expanded), selected_chunks=len(rows),
                                    limited=expansion_limited)
                elif expand_documents and rows:
                    document_ids = list(dict.fromkeys(
                        row['document_id'] for row in rows if row.get('document_id') is not None
                    ))
                    selected_ids = document_ids[:self.settings.rag_overview_max_documents]
                    if selected_ids:
                        stage = 'document_expansion'
                        limit = self.settings.rag_overview_max_chunks
                        expanded = self.repository.expand_documents(
                            vector, selected_ids, self.embedder, request.categories, limit + 1,
                        )
                        if expanded:
                            expansion_limited = len(expanded) > limit or len(document_ids) > len(selected_ids)
                            rows = expanded[:limit]
                            retrieval_scope = 'selected_documents'
                        debug_event('RAG DOCUMENT EXPANSION', document_ids=selected_ids,
                                    expanded_chunks=len(expanded), limited=expansion_limited)
                debug_event('RAG RETRIEVAL', matched_chunks=len(rows),
                            matched_categories=sorted({row['category'] for row in rows}),
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
            except Exception as exc:
                debug_event('RAG SEARCH FAILED', stage=stage, error_type=type(exc).__name__,
                            time_ms=round((perf_counter() - stage_started) * 1000, 2))
                raise
        parts, sources, used = [], [], 0
        context_full = False
        context_truncated = False
        for rank, row in enumerate(rows, 1):
            section_label = (
                " > ".join(row.get("section_path") or ())
                or row.get("heading")
                or ""
            )
            label = row["title"] + (f" > {section_label}" if section_label else "")
            prefix = f"[Nguồn: {label}]\n"
            if numbered_sources:
                prefix = f"[S{len(sources) + 1}] " + prefix
            room = self.settings.rag_max_context_chars - used - (2 if parts else 0)
            metadata = {
                key: row.get(key)
                for key in (
                    "chunk_id",
                    "document_id",
                    "source_key",
                    "title",
                    "category",
                    "heading",
                    "section_path",
                    "chunk_index",
                    "similarity",
                    "bm25_score",
                    "rrf_score",
                    "bm25_rank",
                    "vector_rank",
                )
            }
            if context_full or room <= len(prefix):
                context_full = True
                context_truncated = True
                debug_event('RAG SOURCE', rank=rank, selected=False, reason='context_limit', **metadata)
                continue
            included = row["content"][:room - len(prefix)]
            if row["content"].startswith("Bảng, trang PDF ") and len(included) < len(row["content"]):
                # Never give the LLM half a table row without its conditions/value.
                if row["content"][len(included):len(included) + 1] != "\n":
                    included = included.rsplit("\n", 1)[0] if "\n" in included else ""
                if not any(line.startswith("Dòng ") for line in included.splitlines()):
                    context_truncated = True
                    debug_event('RAG SOURCE', rank=rank, selected=False, reason='table_row_limit', **metadata)
                    continue
            part = prefix + included
            context_truncated |= len(part) - len(prefix) < len(row['content'])
            debug_event('RAG SOURCE', rank=rank, selected=True,
                        citation=f'S{len(sources) + 1}' if numbered_sources else None,
                        content_chars=len(row['content']), included_chars=len(part) - len(prefix),
                        truncated=len(part) - len(prefix) < len(row['content']), **metadata)
            used += len(part) + (2 if parts else 0)
            parts.append(part)
            sources.append({key: row.get(key) for key in (
                "source_key", "title", "category", "heading", "chunk_index", "similarity",
                "bm25_score", "rrf_score", "bm25_rank", "vector_rank",
            )})
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        debug_event('RAG CONTEXT', status='knowledge_found' if parts else 'knowledge_not_found',
                    selected_chunks=len(sources), context_chars=used, time_ms=elapsed_ms,
                    scope=retrieval_scope, truncated=context_truncated or expansion_limited)
        return {
            "success": bool(parts),
            "status": "knowledge_found" if parts else "knowledge_not_found",
            "content": "\n\n".join(parts),
            "sources": sources,
            "elapsed_ms": elapsed_ms,
            "retrieval_coverage": {
                "scope": retrieval_scope,
                "truncated": context_truncated or expansion_limited,
                "selected_documents_complete": retrieval_scope == 'selected_documents'
                                               and not (context_truncated or expansion_limited),
                "selected_sections_complete": retrieval_scope == 'selected_sections'
                                              and not (context_truncated or expansion_limited),
            },
        }
