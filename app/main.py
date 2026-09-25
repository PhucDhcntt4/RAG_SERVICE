import logging
import secrets
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import httpx
import psycopg
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
from app.models import (
    ActiveRequest, ChatRequest, ChatResponse, ClassificationRequest,
    DocTypeCreate, DocTypeUpdate, DocumentRequest, GroupCreate, GroupUpdate,
    SearchRequest, SearchResponse,
)
from app.local_admin import allow_local_admin
from app.qdrant_repository import QdrantRepository
from app.service import InvalidDocument, KnowledgeService, ServiceBusy
from app.storage import StorageError
from app.taxonomy_repository import (
    CompatibilityTaxonomy, TaxonomyConflict, TaxonomyError, TaxonomyNotFound,
    TaxonomyRepository,
)
from app.product_repository import ProductRepository
from app.product_sync.api import create_product_sync_router
from app.product_sync.repository import ProductSyncRepository
from app.product_sync.inventory_api import create_inventory_sync_router
from app.product_sync.inventory_repository import InventorySyncRepository

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
        if scope["path"].startswith(
            (
                "/api/v1/documents",
                "/api/v1/products",
            )
        ) and role != "admin":
            return await JSONResponse(
                {"detail": "Cần ADMIN_API_KEY"},
                status_code=403,
            )(scope, receive, send)
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


def create_app(settings=None, service=None, chat=None, taxonomy=None):
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
        application.state.product_sync = None
        application.state.inventory_sync = None
        owned_embedder = None
        owned_chat = None
        owned_repository = None
        owned_taxonomy = None
        owned_products = None
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

            owned_products = ProductRepository(config)
            application.state.products = owned_products

            if config.database_url is not None:
                product_sync = ProductSyncRepository(config)
                product_sync.initialize()
                application.state.product_sync = product_sync

                inventory_sync = InventorySyncRepository(config)
                inventory_sync.initialize()
                application.state.inventory_sync = inventory_sync
            if taxonomy is not None:
                application.state.taxonomy = taxonomy
            elif config.database_url is not None:
                owned_taxonomy = TaxonomyRepository(config)
                owned_taxonomy.initialize()
                application.state.taxonomy = owned_taxonomy
            elif service is None:
                raise RuntimeError("Thiếu DATABASE_URL cho PostgreSQL metadata")
            else:
                application.state.taxonomy = CompatibilityTaxonomy()

            if owned_taxonomy is not None:
                repository = application.state.service.repository
                try:
                    for category in repository.list_categories():
                        group = application.state.taxonomy.ensure_group(category)
                        repository.backfill_classification(
                            group["code"], group["doc_type_id"], group["id"]
                        )
                except Exception as exc:
                    logger.warning("RAG taxonomy Qdrant backfill deferred error_type=%s",
                                   type(exc).__name__)
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
            if owned_products is not None:
                owned_products.close()

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

    @application.get("/taxonomy", include_in_schema=False)
    @application.get("/admin/taxonomy", include_in_schema=False)
    def taxonomy_dashboard():
        return FileResponse(ROOT / "app" / "static" / "taxonomy.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    @application.get("/products", include_in_schema=False)
    @application.get("/admin/products", include_in_schema=False)
    def products_dashboard():
        return FileResponse(ROOT / "app" / "static" / "products.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://cdn.shopify.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
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

        quiet_routes = {
            "/api/v1/products/sync/status",
            "/api/v1/products/sync/history",
            "/api/v1/products/inventory-sync/status",
        }

        if route not in quiet_routes:
            logger.info(
                "RAG REQUEST request_id=%s method=%s route=%s status=%s time_ms=%.2f",
                trace,
                request.method,
                route,
                response.status_code,
                elapsed,
            )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"]}
                                        for e in exc.errors()]}, status_code=422)

    @application.exception_handler(InvalidDocument)
    async def invalid_document(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @application.exception_handler(TaxonomyNotFound)
    async def taxonomy_not_found(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @application.exception_handler(TaxonomyConflict)
    async def taxonomy_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(TaxonomyError)
    async def taxonomy_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @application.exception_handler(ServiceBusy)
    async def service_busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "5"})

    @application.exception_handler(EmbeddingError)
    @application.exception_handler(StorageError)
    @application.exception_handler(httpx.HTTPError)
    @application.exception_handler(psycopg.Error)
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

    application.include_router(create_product_sync_router(admin))
    application.include_router(create_inventory_sync_router(admin))

    def current_service(request: Request):
        return request.app.state.service

    def current_products(request: Request):
        return request.app.state.products

    def current_taxonomy(request: Request):
        value = request.app.state.taxonomy
        if value is None:
            raise HTTPException(503, "PostgreSQL metadata chưa được cấu hình")
        return value

    def classified_document(payload, catalog):
        group = (catalog.get_group(payload.group_id) if payload.group_id
                 else catalog.ensure_group(payload.category))
        if group is None or not group["is_active"]:
            raise TaxonomyNotFound("Nhóm tài liệu không tồn tại hoặc đã ngừng sử dụng")
        parent = catalog.get_doc_type(group["doc_type_id"])
        if parent is None or not parent["is_active"]:
            raise TaxonomyConflict("Loại tài liệu cha đã ngừng sử dụng")
        return payload.model_copy(update={
            "category": group["code"],
            "doc_type_id": group["doc_type_id"],
            "group_id": group["id"],
        })

    def scoped_retrieval(payload, catalog):
        """Resolve relational taxonomy IDs to category codes used by retrieval."""
        if payload.doc_type_id is None and payload.group_ids is None:
            return payload

        parent = None
        if payload.doc_type_id is not None:
            parent = catalog.get_doc_type(payload.doc_type_id)
            if parent is None or not parent["is_active"]:
                raise TaxonomyNotFound(
                    "Loại tài liệu truy vấn không tồn tại hoặc đã ngừng sử dụng"
                )

        selected_groups = []
        if payload.group_ids is not None:
            for group_id in payload.group_ids:
                group = catalog.get_group(group_id)
                if group is None or not group["is_active"]:
                    raise TaxonomyNotFound(
                        "Nhóm tài liệu truy vấn không tồn tại hoặc đã ngừng sử dụng"
                    )
                if parent is not None and group["doc_type_id"] != parent["id"]:
                    raise TaxonomyConflict(
                        "Nhóm truy vấn không thuộc loại tài liệu đã chọn"
                    )
                group_parent = catalog.get_doc_type(group["doc_type_id"])
                if group_parent is None or not group_parent["is_active"]:
                    raise TaxonomyConflict(
                        "Loại cha của nhóm truy vấn đã ngừng sử dụng"
                    )
                selected_groups.append(group)
        else:
            selected_groups = [
                group
                for item in catalog.list_tree(include_inactive=False)
                if item["id"] == parent["id"]
                for group in item.get("groups", [])
            ]

        categories = list(dict.fromkeys(group["code"] for group in selected_groups))
        if not categories:
            raise TaxonomyConflict("Phạm vi đã chọn chưa có nhóm tài liệu đang sử dụng")
        return payload.model_copy(update={"categories": categories})

    @application.get("/health/live", tags=["Health"])
    def live():
        return {"status": "alive"}

    @application.get("/api/v1/health/ready", dependencies=[Depends(authorize)], tags=["Health"])
    def ready(request: Request, svc=Depends(current_service)):
        svc.repository.ready()
        if request.app.state.taxonomy is not None:
            request.app.state.taxonomy.ready()
        return {"status": "ready", "embedding_provider_checked": False,
                "vector_provider": "qdrant",
                "qdrant_collection": request.app.state.settings.qdrant_collection,
                "bm25_indexed_chunks": svc.bm25_count}

    @application.post("/api/v1/knowledge/search", response_model=SearchResponse,
                      dependencies=[Depends(authorize)], tags=["Search"])
    def search(payload: SearchRequest, svc=Depends(current_service),
               catalog=Depends(current_taxonomy)):
        return svc.search(scoped_retrieval(payload, catalog))

    @application.post('/api/v1/chat', response_model=ChatResponse,
                      dependencies=[Depends(admin)], tags=['Chat'])
    def chat_answer(payload: ChatRequest, request: Request, svc=Depends(current_service),
                    catalog=Depends(current_taxonomy)):
        return request.app.state.chat.answer(scoped_retrieval(payload, catalog), svc)

    @application.put("/api/v1/documents", dependencies=[Depends(admin)], tags=["Documents"])
    def upsert(payload: DocumentRequest, svc=Depends(current_service),
               catalog=Depends(current_taxonomy)):
        return svc.ingest(classified_document(payload, catalog))

    @application.post("/api/v1/documents/upload", dependencies=[Depends(admin)], tags=["Documents"])
    def upload(file: Annotated[UploadFile, File()], source_key: Annotated[str, Form()],
               title: Annotated[str, Form()], category: Annotated[str, Form()] = "customer_care",
               group_id: Annotated[int | None, Form()] = None,
               svc=Depends(current_service), catalog=Depends(current_taxonomy)):
        try:
            data = file.file.read(svc.settings.rag_max_upload_bytes + 1)
            if len(data) > svc.settings.rag_max_upload_bytes:
                raise HTTPException(413, "File vượt giới hạn dung lượng")
            text, prepared_chunks = svc.prepare_upload(file.filename or "", data)
            try:
                payload = DocumentRequest(source_key=source_key, title=title,
                                          category=category, group_id=group_id, text=text)
                payload = classified_document(payload, catalog)
            except ValidationError as exc:
                raise HTTPException(422, "Tên nguồn, tiêu đề, nhóm hoặc nội dung không hợp lệ") from exc
            return svc.ingest(payload, original_data=data, original_filename=file.filename,
                              prepared_chunks=prepared_chunks)
        finally:
            file.file.close()

    @application.get("/api/v1/documents", dependencies=[Depends(admin)], tags=["Documents"])
    def documents(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  doc_type_id: int | None = Query(None, ge=1),
                  group_id: int | None = Query(None, ge=1),
                  svc=Depends(current_service), catalog=Depends(current_taxonomy)):
        if doc_type_id is None and group_id is None:
            rows = svc.repository.list_documents(limit, offset)
        else:
            category_codes = []
            if group_id is not None:
                group = catalog.get_group(group_id)
                if group is None:
                    raise TaxonomyNotFound("Không tìm thấy nhóm tài liệu cần lọc")
                if doc_type_id is not None and group["doc_type_id"] != doc_type_id:
                    raise TaxonomyConflict("Nhóm không thuộc loại tài liệu đã chọn")
                doc_type_id = group["doc_type_id"] if doc_type_id is None else doc_type_id
                category_codes = [group["code"]]
            else:
                parent = catalog.get_doc_type(doc_type_id)
                if parent is None:
                    raise TaxonomyNotFound("Không tìm thấy loại tài liệu cần lọc")
                category_codes = [
                    group["code"]
                    for item in catalog.list_tree()
                    if item["id"] == doc_type_id
                    for group in item.get("groups", [])
                ]
            rows = svc.repository.list_documents(
                limit,
                offset,
                doc_type_id=doc_type_id,
                group_id=group_id,
                categories=category_codes,
            )
        return {"documents": rows, "limit": limit, "offset": offset}

    @application.get("/api/v1/documents/by-source-key",
                     dependencies=[Depends(admin)], tags=["Documents"])
    def document_by_source_key(source_key: str = Query(min_length=1, max_length=500),
                               svc=Depends(current_service)):
        row = svc.repository.get_document_by_source_key(source_key)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        return row

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

    @application.patch("/api/v1/documents/{document_id}/classification",
                       dependencies=[Depends(admin)], tags=["Documents"])
    def classification(document_id: int, payload: ClassificationRequest,
                       svc=Depends(current_service), catalog=Depends(current_taxonomy)):
        group = catalog.get_group(payload.group_id)
        if group is None or not group["is_active"]:
            raise TaxonomyNotFound("Nhóm tài liệu không tồn tại hoặc đã ngừng sử dụng")
        row = svc.set_classification(document_id, group)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        return row

    @application.get("/api/v1/taxonomy", dependencies=[Depends(admin)], tags=["Taxonomy"])
    def taxonomy_tree(catalog=Depends(current_taxonomy), svc=Depends(current_service)):
        tree = catalog.list_tree()
        for doc_type in tree:
            for group in doc_type["groups"]:
                group["document_count"] = svc.repository.count_documents_by_group(
                    group["id"], group["code"]
                )
        return {"doc_types": tree}

    @application.post("/api/v1/doc-types", dependencies=[Depends(admin)], tags=["Taxonomy"])
    def create_doc_type(payload: DocTypeCreate, catalog=Depends(current_taxonomy)):
        return catalog.create_doc_type(**payload.model_dump())

    @application.patch("/api/v1/doc-types/{doc_type_id}",
                       dependencies=[Depends(admin)], tags=["Taxonomy"])
    def update_doc_type(doc_type_id: int, payload: DocTypeUpdate,
                        catalog=Depends(current_taxonomy)):
        return catalog.update_doc_type(doc_type_id, **payload.model_dump())

    @application.delete("/api/v1/doc-types/{doc_type_id}",
                        dependencies=[Depends(admin)], tags=["Taxonomy"])
    def delete_doc_type(doc_type_id: int, catalog=Depends(current_taxonomy)):
        return catalog.delete_doc_type(doc_type_id)

    @application.post("/api/v1/groups", dependencies=[Depends(admin)], tags=["Taxonomy"])
    def create_group(payload: GroupCreate, catalog=Depends(current_taxonomy)):
        return catalog.create_group(**payload.model_dump())

    @application.patch("/api/v1/groups/{group_id}",
                       dependencies=[Depends(admin)], tags=["Taxonomy"])
    def update_group(group_id: int, payload: GroupUpdate,
                     catalog=Depends(current_taxonomy), svc=Depends(current_service)):
        group = catalog.update_group(group_id, **payload.model_dump())
        svc.repository.backfill_classification(
            group["code"], group["doc_type_id"], group["id"]
        )
        return group

    @application.delete("/api/v1/groups/{group_id}",
                        dependencies=[Depends(admin)], tags=["Taxonomy"])
    def delete_group(group_id: int, catalog=Depends(current_taxonomy),
                     svc=Depends(current_service)):
        group = catalog.get_group(group_id)
        if group is None:
            raise TaxonomyNotFound("Không tìm thấy nhóm tài liệu")
        if svc.repository.count_documents_by_group(group["id"], group["code"]):
            raise TaxonomyConflict(
                "Nhóm đang có tài liệu; hãy chuyển tài liệu sang nhóm khác trước"
            )
        return catalog.delete_group(group_id)

    @application.delete("/api/v1/documents/{document_id}", dependencies=[Depends(admin)], tags=["Documents"])
    def delete(document_id: int, svc=Depends(current_service)):
        row = svc.delete(document_id)
        if row is None:
            raise HTTPException(404, "Không tìm thấy tài liệu")
        return {"deleted": True, **row}

    @application.get(
    "/api/v1/products/collections",
    dependencies=[Depends(admin)],
    tags=["Products"],
    )
    def product_collections(
        products=Depends(current_products),
    ):
        return products.collection_status()


    @application.get(
        "/api/v1/products",
        dependencies=[Depends(admin)],
        tags=["Products"],
    )
    def product_list(
        limit: int = Query(20, ge=1, le=100),
        page: int | None = Query(None, ge=1),
        cursor: str | None = Query(None),
        q: str | None = Query(None, max_length=200),
        product_type: str | None = Query(None, max_length=200),
        status: str | None = Query(None, max_length=30),
        products=Depends(current_products),
    ):
        if page is not None:
            result = products.list_products_page(
                page=page,
                page_size=limit,
                query=q,
                product_type=product_type,
                status=status,
            )
        else:
            result = products.list_products(
                limit=limit,
                cursor=cursor,
                query=q,
                product_type=product_type,
                status=status,
            )

        return {
            **result,
            "limit": limit,
            "query": (q or "").strip(),
            "product_type": (product_type or "").strip(),
            "status": (status or "").strip().upper(),
        }


    @application.get(
        "/api/v1/products/filters",
        dependencies=[Depends(admin)],
        tags=["Products"],
    )
    def product_filters(
        products=Depends(current_products),
    ):
        return products.list_filter_options()


    @application.get(
        "/api/v1/products/{product_code}",
        dependencies=[Depends(admin)],
        tags=["Products"],
    )
    def product_detail(
        product_code: str,
        products=Depends(current_products),
    ):
        row = products.get_product(product_code)

        if row is None:
            raise HTTPException(
                404,
                "Không tìm thấy sản phẩm",
            )

        return row


    return application


app = create_app()
