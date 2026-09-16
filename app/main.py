import logging
import secrets
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.config import ROOT, Settings
from app.debug_log import request_id
from app.chat import ChatError, GeminiChat
from app.embeddings import EmbeddingError, GeminiEmbedder
from app.models import ActiveRequest, ChatRequest, ChatResponse, DocumentRequest, SearchRequest, SearchResponse
from app.local_admin import allow_local_admin
from app.qdrant_repository import QdrantRepository
from app.service import InvalidDocument, KnowledgeService, ServiceBusy
from app.storage import StorageError

logger = logging.getLogger("rag_service")
bearer = HTTPBearer(auto_error=False)


def token_role(token, settings):
    raw = token.encode("utf-8")
    if secrets.compare_digest(raw, settings.admin_api_key.get_secret_value().encode()):
        return "admin"
    if secrets.compare_digest(raw, settings.search_api_key.get_secret_value().encode()):
        return "search"
    return None


class RequestGuard:
    """Authenticate BEFORE parsing uploads; bound both chunked and normal request bodies."""
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        local_ui = scope['path'].startswith('/admin-api/')
        if not local_ui and not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        settings = self.owner.state.settings
        headers = dict(scope["headers"])
        if local_ui:
            if not allow_local_admin(scope, settings):
                return await JSONResponse({'detail': 'Truy cập tự động chỉ dùng trực tiếp trên localhost khi được bật.'},
                                          status_code=403)(scope, receive, send)
            path = '/api/' + scope['path'][len('/admin-api/'):]
            scope.update(path=path, raw_path=path.encode('utf-8'),
                         state={**scope.get('state', {}), 'local_admin': True})
            role = 'admin'
        else:
            auth = headers.get(b"authorization", b"").decode("latin-1").split()
            role = token_role(auth[1], settings) if len(auth) == 2 and auth[0].lower() == "bearer" else None
        if role is None:
            return await JSONResponse({"detail": "API key không hợp lệ"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        if scope["path"].startswith("/api/v1/documents") and role != "admin":
            return await JSONResponse({"detail": "Cần ADMIN_API_KEY"}, status_code=403)(scope, receive, send)
        # JSON may use escaped Unicode; allow bounded encoding overhead.
        limit = max(settings.rag_max_upload_bytes + 65536,
                    settings.rag_max_document_chars * 6 + 65536)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse({"detail": "Request quá lớn"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, service=None, chat=None):
    @asynccontextmanager
    async def lifespan(application):
        config = settings or Settings.load()
        logging.basicConfig(level=config.log_level,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logger.setLevel(config.log_level)
        # Third-party debug logs can include HTTP data. Keep our logs separate.
        for name in ("httpx", "httpx2", "httpcore", "httpcore2", "google_genai"):
            logging.getLogger(name).setLevel(logging.WARNING)
        application.state.settings = config
        owned_embedder = None
        owned_chat = None
        owned_repository = None
        try:
            if chat is None:
                owned_chat = GeminiChat(config)
            application.state.chat = chat if chat is not None else owned_chat
            if service is None:
                owned_embedder = GeminiEmbedder(config)
                owned_repository = QdrantRepository(config)
                application.state.service = KnowledgeService(
                    config, owned_repository, owned_embedder
                )
            else:
                application.state.service = service
            try:
                indexed_chunks = (application.state.service.refresh_bm25()
                                  if config.rag_hybrid_enabled else 0)
            except Exception as exc:
                # Keep liveness available while Qdrant is starting. The first
                # search retries the lazy BM25 load and readiness still checks it.
                indexed_chunks = 0
                logger.warning("RAG BM25 startup load failed error_type=%s",
                               type(exc).__name__)
            logger.info("RAG SERVICE started vector_provider=qdrant model=%s "
                        "dimension=768 bm25_chunks=%s",
                        config.rag_embedding_model, indexed_chunks)
            yield
        finally:
            if owned_chat is not None:
                owned_chat.close()
            if owned_embedder is not None:
                owned_embedder.close()
            if owned_repository is not None and hasattr(owned_repository, "close"):
                owned_repository.close()

    application = FastAPI(title="Đông Hải RAG Service", version="1.0.0", lifespan=lifespan)
    application.add_middleware(RequestGuard, owner=application)
    application.mount("/assets", StaticFiles(directory=ROOT / "app" / "static"), name="assets")

    @application.get("/", include_in_schema=False)
    @application.get("/admin", include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT / "app" / "static" / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    @application.middleware("http")
    async def log_request(request, call_next):
        started = perf_counter()
        trace = uuid4().hex
        token = request_id.set(trace)
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error("RAG REQUEST failed request_id=%s error_type=%s", trace, type(exc).__name__)
            response = JSONResponse({"detail": "Lỗi nội bộ RAG service"}, status_code=500)
        finally:
            request_id.reset(token)
        elapsed = (perf_counter() - started) * 1000
        response.headers["X-Response-Time-Ms"] = f"{elapsed:.2f}"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = trace
        # Route template, not user-supplied query strings or paths.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        logger.info("RAG REQUEST request_id=%s method=%s route=%s status=%s time_ms=%.2f",
                    trace, request.method, route, response.status_code, elapsed)
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"]}
                                        for e in exc.errors()]}, status_code=422)

    @application.exception_handler(InvalidDocument)
    async def invalid_document(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @application.exception_handler(ServiceBusy)
    async def service_busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "5"})

    @application.exception_handler(EmbeddingError)
    @application.exception_handler(StorageError)
    @application.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        logger.error("RAG dependency unavailable error_type=%s", type(exc).__name__)
        detail = exc.public_message if isinstance(exc, EmbeddingError) else "RAG tạm thời không khả dụng; vui lòng thử lại"
        return JSONResponse({"detail": detail},
                            status_code=503, headers={"Retry-After": "5"})

    @application.exception_handler(ChatError)
    async def chat_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=503)

    def authorize(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if getattr(request.state, 'local_admin', False):
            return 'admin'
        role = token_role(credentials.credentials, request.app.state.settings) if credentials else None
        if role is None:
            raise HTTPException(401, "API key không hợp lệ", headers={"WWW-Authenticate": "Bearer"})
        return role

    def admin(role=Depends(authorize)):
        if role != "admin":
            raise HTTPException(403, "Cần ADMIN_API_KEY")

    def current_service(request: Request):
        return request.app.state.service

    @application.get("/health/live", tags=["Health"])
    def live():
        return {"status": "alive"}

    @application.get("/api/v1/health/ready", dependencies=[Depends(authorize)], tags=["Health"])
    def ready(request: Request, svc=Depends(current_service)):
        svc.repository.ready()
        return {"status": "ready", "embedding_provider_checked": False,
                "vector_provider": "qdrant",
                "qdrant_collection": request.app.state.settings.qdrant_collection,
                "bm25_indexed_chunks": svc.bm25_count}

    @application.post("/api/v1/knowledge/search", response_model=SearchResponse,
                      dependencies=[Depends(authorize)], tags=["Search"])
    def search(payload: SearchRequest, svc=Depends(current_service)):
        return svc.search(payload)

    @application.post('/api/v1/chat', response_model=ChatResponse,
                      dependencies=[Depends(admin)], tags=['Chat'])
    def chat_answer(payload: ChatRequest, request: Request, svc=Depends(current_service)):
        return request.app.state.chat.answer(payload, svc)

    @application.put("/api/v1/documents", dependencies=[Depends(admin)], tags=["Documents"])
    def upsert(payload: DocumentRequest, svc=Depends(current_service)):
        return svc.ingest(payload)

    @application.post("/api/v1/documents/upload", dependencies=[Depends(admin)], tags=["Documents"])
    def upload(file: Annotated[UploadFile, File()], source_key: Annotated[str, Form()],
               title: Annotated[str, Form()], category: Annotated[str, Form()] = "customer_care",
               svc=Depends(current_service)):
        try:
            data = file.file.read(svc.settings.rag_max_upload_bytes + 1)
            if len(data) > svc.settings.rag_max_upload_bytes:
                raise HTTPException(413, "File vượt giới hạn dung lượng")
            text, prepared_chunks = svc.prepare_upload(file.filename or "", data)
            try:
                payload = DocumentRequest(source_key=source_key, title=title, category=category, text=text)
            except ValidationError as exc:
                raise HTTPException(422, "Tên nguồn, tiêu đề, nhóm hoặc nội dung không hợp lệ") from exc
            return svc.ingest(payload, original_data=data, original_filename=file.filename,
                              prepared_chunks=prepared_chunks)
        finally:
            file.file.close()

    @application.get("/api/v1/documents", dependencies=[Depends(admin)], tags=["Documents"])
    def documents(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  svc=Depends(current_service)):
        return {"documents": svc.repository.list_documents(limit, offset), "limit": limit, "offset": offset}

    @application.get("/api/v1/documents/{document_id}", dependencies=[Depends(admin)], tags=["Documents"])
    def document(document_id: int, svc=Depends(current_service)):
        row = svc.repository.get_document(document_id)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        return row

    @application.get("/api/v1/documents/{document_id}/download", dependencies=[Depends(admin)], tags=["Documents"])
    def download(document_id: int, svc=Depends(current_service)):
        row = svc.repository.get_document(document_id)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        if not row.get("file_storage_key"):
            raise HTTPException(404, "Tài liệu chưa có file local. Hãy xuất văn bản cũ hoặc tải lại file gốc.")
        try:
            data = svc.storage.read(row["file_storage_key"])
        except FileNotFoundError:
            raise HTTPException(404, "File local không còn trên máy chủ; dữ liệu tìm kiếm vẫn nằm trong database") from None
        return Response(data, media_type=row["file_mime_type"] or "application/octet-stream", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["file_name"] or f"document-{document_id}.txt", safe=""),
            "X-Content-Type-Options": "nosniff",
        })

    @application.patch("/api/v1/documents/{document_id}", dependencies=[Depends(admin)], tags=["Documents"])
    def active(document_id: int, payload: ActiveRequest, svc=Depends(current_service)):
        row = svc.set_active(document_id, payload.is_active)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        return row

    @application.delete("/api/v1/documents/{document_id}", dependencies=[Depends(admin)], tags=["Documents"])
    def delete(document_id: int, svc=Depends(current_service)):
        row = svc.delete(document_id)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        return {"deleted": True, **row}

    return application


app = create_app()
