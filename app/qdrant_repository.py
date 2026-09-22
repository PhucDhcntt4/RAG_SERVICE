
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import quote

import httpx


class QdrantRepository:
    def __init__(self, settings):
        self.base_url = settings.qdrant_url.rstrip("/")
        self.collection = settings.qdrant_collection
        self.dimension = 768
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=settings.qdrant_timeout_seconds,
        )

    @property
    def collection_path(self):
        return "/collections/" + quote(self.collection, safe="")

    def close(self):
        self.client.close()

    def _request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") not in (None, "ok"):
            raise RuntimeError("Qdrant trả về trạng thái không hợp lệ")
        return payload.get("result")

    def collection_info(self):
        return self._request("GET", self.collection_path)

    def create_collection(self):
        return self._request(
            "PUT",
            self.collection_path,
            json={"vectors": {"size": self.dimension, "distance": "Cosine"}},
        )

    def delete_collection(self):
        return self._request("DELETE", self.collection_path)

    def ready(self):
        info = self.collection_info()
        vectors = info["config"]["params"]["vectors"]
        if vectors.get("size") != self.dimension or vectors.get("distance") != "Cosine":
            raise RuntimeError("Qdrant collection phải dùng vector 768 chiều và Cosine")
        if info.get("status") not in {"green", "yellow"}:
            raise RuntimeError("Qdrant collection chưa sẵn sàng")

    @staticmethod
    def _stable_id(kind, value):
        digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1) or 1

    @classmethod
    def _document_id(cls, source_key):
        return cls._stable_id("document", source_key)

    @classmethod
    def _chunk_id(cls, source_key, chunk_index):
        return cls._stable_id("chunk", f"{source_key}:{chunk_index}")

    @staticmethod
    def _filter(must=None):
        return {"must": [item for item in (must or []) if item]}

    @staticmethod
    def _match(key, value):
        return {"key": key, "match": {"value": value}}

    @staticmethod
    def _match_any(key, values):
        return {"key": key, "match": {"any": list(values)}}

    def _embedding_filter(self, embedder, categories=None, *, active=True):
        conditions = [
            self._match("embedding_provider", embedder.provider),
            self._match("embedding_model", embedder.model),
            self._match("embedding_dimension", embedder.dimension),
        ]
        if active is not None:
            conditions.append(self._match("is_active", active))
        if categories:
            conditions.append(self._match_any("category", categories))
        return self._filter(conditions)

    def _scroll(self, query_filter=None, *, with_vectors=False):
        offset = None
        while True:
            body = {
                "limit": 256,
                "with_payload": True,
                "with_vector": with_vectors,
            }
            if query_filter:
                body["filter"] = query_filter
            if offset is not None:
                body["offset"] = offset
            result = self._request(
                "POST", self.collection_path + "/points/scroll", json=body
            )
            for point in result.get("points", []):
                yield point
            next_offset = result.get("next_page_offset")
            if next_offset is None or next_offset == offset:
                break
            offset = next_offset

    @staticmethod
    def _row(point, similarity=None):
        payload = point.get("payload") or {}
        return {
            "chunk_id": point["id"],
            "document_id": payload.get("document_id"),
            "source_key": payload.get("source_key"),
            "title": payload.get("title"),
            "category": payload.get("category"),
            "doc_type_id": payload.get("doc_type_id"),
            "group_id": payload.get("group_id"),
            "heading": payload.get("heading"),
            "section_path": payload.get("section_path") or [],
            "chunk_index": payload.get("chunk_index", 0),
            "content": payload.get("content", ""),
            "similarity": similarity,
        }

    def _points_for_source(self, source_key):
        query_filter = self._filter([self._match("source_key", source_key)])
        return list(self._scroll(query_filter))

    def replace(self, document, chunks, vectors, embedder, stored_file=None):
        if len(chunks) != len(vectors):
            raise ValueError("Chunk/vector count mismatch")
        if any(len(vector) != self.dimension for vector in vectors):
            raise ValueError("Embedding dimension mismatch")
        self.ready()

        document_id = self._document_id(document.source_key)
        existing = self._points_for_source(document.source_key)
        old_payload = next(
            (point.get("payload") or {} for point in existing
             if (point.get("payload") or {}).get("chunk_index") == 0),
            {},
        )
        collision = list(self._scroll(self._filter([
            self._match("document_id", document_id)
        ])))
        if any((point.get("payload") or {}).get("source_key") != document.source_key
               for point in collision):
            raise RuntimeError("Phát hiện document ID trùng trong Qdrant")

        now = datetime.now(timezone.utc).isoformat()
        created_at = old_payload.get("created_at", now)
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        point_ids = []
        points = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            point_id = self._chunk_id(document.source_key, chunk.index)
            point_ids.append(point_id)
            payload = {
                "document_id": document_id,
                "source_key": document.source_key,
                "title": document.title,
                "category": document.category,
                "doc_type_id": document.doc_type_id,
                "group_id": document.group_id,
                "heading": chunk.heading,
                "section_path": list(chunk.section_path),
                "chunk_index": chunk.index,
                "content": chunk.content,
                "source_checksum": checksum,
                "embedding_provider": embedder.provider,
                "embedding_model": embedder.model,
                "embedding_dimension": embedder.dimension,
                "is_active": old_payload.get("is_active", True),
                "created_at": created_at,
                "updated_at": now,
                "file_storage_key": stored_file.key if stored_file else None,
                "file_name": stored_file.name if stored_file else None,
                "file_mime_type": stored_file.mime_type if stored_file else None,
                "file_size": stored_file.size if stored_file else None,
                "file_origin": stored_file.origin if stored_file else None,
            }
            if chunk.index == 0:
                payload["source_text"] = document.text
            points.append({"id": point_id, "vector": list(vector), "payload": payload})

        for start in range(0, len(points), 64):
            self._request(
                "PUT",
                self.collection_path + "/points?wait=true",
                json={"points": points[start:start + 64]},
            )

        stale = [point["id"] for point in existing if point["id"] not in point_ids]
        if stale:
            self._request(
                "POST",
                self.collection_path + "/points/delete?wait=true",
                json={"points": stale},
            )
        return {
            "id": str(document_id),
            "source_key": document.source_key,
            "title": document.title,
            "category": document.category,
            "doc_type_id": document.doc_type_id,
            "group_id": document.group_id,
            "is_active": old_payload.get("is_active", True),
            "updated_at": now,
            "chunk_count": len(chunks),
            "file_name": stored_file.name if stored_file else None,
            "_old_file_key": old_payload.get("file_storage_key"),
        }

    def search(self, vector, embedder, categories, threshold, limit):
        result = self._request(
            "POST",
            self.collection_path + "/points/query",
            json={
                "query": list(vector),
                "filter": self._embedding_filter(embedder, categories),
                "limit": limit,
                "score_threshold": threshold,
                "with_payload": True,
                "with_vector": False,
            },
        )
        return [self._row(point, point.get("score"))
                for point in result.get("points", [])]

    def list_search_chunks(self, embedder):
        points = self._scroll(self._embedding_filter(embedder))
        return [self._row(point) for point in points]

    def expand_documents(self, vector, document_ids, embedder, categories, limit):
        if not document_ids:
            return []
        query_filter = self._embedding_filter(embedder, categories)
        query_filter["must"].append(self._match_any("document_id", document_ids))
        order = {document_id: rank for rank, document_id in enumerate(document_ids)}
        rows = [self._row(point) for point in self._scroll(query_filter)]
        rows.sort(key=lambda row: (
            order.get(row["document_id"], len(order)), row["chunk_index"], row["chunk_id"]
        ))
        return rows[:limit]

    def expand_sections(self, vector, scopes, embedder, categories, limit):
        if not scopes:
            return []
        document_ids = list(dict.fromkeys(document_id for document_id, _ in scopes))
        query_filter = self._embedding_filter(embedder, categories)
        query_filter["must"].append(self._match_any("document_id", document_ids))
        ranked_scopes = [(rank, document_id, tuple(path))
                         for rank, (document_id, path) in enumerate(scopes) if path]
        selected = []
        for point in self._scroll(query_filter):
            row = self._row(point)
            path = tuple(row["section_path"])
            ranks = [rank for rank, document_id, scope in ranked_scopes
                     if document_id == row["document_id"] and path[:len(scope)] == scope]
            if ranks:
                selected.append((min(ranks), row))
        selected.sort(key=lambda item: (
            item[0], item[1]["chunk_index"], item[1]["chunk_id"]
        ))
        return [row for _, row in selected[:limit]]

    @staticmethod
    def _document_row(payload, chunk_count):
        # Qdrant accepts 64-bit integer IDs, while JavaScript Number is only
        # exact through 2**53 - 1. Expose IDs as decimal strings at the API
        # boundary so the admin UI never rounds them before a detail/delete URL.
        document_id = payload.get("document_id")
        return {
            "id": str(document_id) if document_id is not None else None,
            "source_key": payload.get("source_key"),
            "title": payload.get("title"),
            "category": payload.get("category"),
            "doc_type_id": payload.get("doc_type_id"),
            "group_id": payload.get("group_id"),
            "source_text": payload.get("source_text", ""),
            "source_checksum": payload.get("source_checksum"),
            "embedding_provider": payload.get("embedding_provider"),
            "embedding_model": payload.get("embedding_model"),
            "embedding_dimension": payload.get("embedding_dimension"),
            "is_active": payload.get("is_active", True),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "file_storage_key": payload.get("file_storage_key"),
            "file_name": payload.get("file_name"),
            "file_mime_type": payload.get("file_mime_type"),
            "file_size": payload.get("file_size"),
            "file_origin": payload.get("file_origin"),
            "has_local_file": bool(payload.get("file_storage_key")),
            "chunk_count": chunk_count,
        }

    def _documents(self):
        documents = {}
        for point in self._scroll():
            payload = point.get("payload") or {}
            document_id = payload.get("document_id")
            if document_id is None:
                continue
            entry = documents.setdefault(document_id, {"payload": payload, "count": 0})
            entry["count"] += 1
            if payload.get("chunk_index") == 0:
                entry["payload"] = payload
        return [self._document_row(value["payload"], value["count"])
                for value in documents.values()]

    def list_documents(self, limit, offset, *, doc_type_id=None, group_id=None,
                       categories=None):
        rows = self._documents()
        category_set = set(categories or ())
        if group_id is not None:
            rows = [
                row for row in rows
                if self._same_identifier(row.get("group_id"), group_id)
                or (row.get("group_id") is None
                    and row.get("category") in category_set)
            ]
        elif doc_type_id is not None:
            rows = [
                row for row in rows
                if self._same_identifier(row.get("doc_type_id"), doc_type_id)
                or (row.get("doc_type_id") is None
                    and row.get("category") in category_set)
            ]
        rows.sort(key=lambda row: (
            row.get("updated_at") or "", int(row["id"])
        ), reverse=True)
        return rows[offset:offset + limit]

    @staticmethod
    def _same_identifier(value, expected):
        try:
            return int(value) == int(expected)
        except (TypeError, ValueError):
            return False

    def get_document(self, document_id):
        document_id = int(document_id)
        points = list(self._scroll(self._filter([
            self._match("document_id", document_id)
        ])))
        if not points:
            return None
        payload = next(
            (point.get("payload") or {} for point in points
             if (point.get("payload") or {}).get("chunk_index") == 0),
            points[0].get("payload") or {},
        )
        return self._document_row(payload, len(points))

    def get_document_by_source_key(self, source_key):
        points = self._points_for_source(source_key)
        if not points:
            return None
        payload = next(
            (point.get("payload") or {} for point in points
             if (point.get("payload") or {}).get("chunk_index") == 0),
            points[0].get("payload") or {},
        )
        return self._document_row(payload, len(points))

    def list_categories(self):
        return sorted({row["category"] for row in self._documents() if row.get("category")})

    def count_documents_by_group(self, group_id, category=None):
        return sum(
            1 for row in self._documents()
            if row.get("group_id") == group_id
            or (row.get("group_id") is None and category and row.get("category") == category)
        )

    def backfill_classification(self, category, doc_type_id, group_id):
        self._request(
            "POST",
            self.collection_path + "/points/payload?wait=true",
            json={
                "payload": {"doc_type_id": doc_type_id, "group_id": group_id},
                "filter": self._filter([self._match("category", category)]),
            },
        )

    def set_classification(self, document_id, *, category, doc_type_id, group_id):
        document_id = int(document_id)
        row = self.get_document(document_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        self._request(
            "POST",
            self.collection_path + "/points/payload?wait=true",
            json={
                "payload": {
                    "category": category,
                    "doc_type_id": doc_type_id,
                    "group_id": group_id,
                    "updated_at": now,
                },
                "filter": self._filter([self._match("document_id", document_id)]),
            },
        )
        return {
            "id": str(document_id),
            "category": category,
            "doc_type_id": doc_type_id,
            "group_id": group_id,
            "updated_at": now,
        }

    def set_active(self, document_id, active):
        document_id = int(document_id)
        row = self.get_document(document_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        self._request(
            "POST",
            self.collection_path + "/points/payload?wait=true",
            json={
                "payload": {"is_active": active, "updated_at": now},
                "filter": self._filter([self._match("document_id", document_id)]),
            },
        )
        return {"id": str(document_id), "is_active": active}

    def delete(self, document_id):
        document_id = int(document_id)
        row = self.get_document(document_id)
        if row is None:
            return None
        self._request(
            "POST",
            self.collection_path + "/points/delete?wait=true",
            json={"filter": self._filter([self._match("document_id", document_id)])},
        )
        return {"id": str(document_id), "file_storage_key": row.get("file_storage_key")}
