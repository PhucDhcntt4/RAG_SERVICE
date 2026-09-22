from __future__ import annotations

import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


class TaxonomyError(ValueError):
    pass


class TaxonomyNotFound(TaxonomyError):
    pass


class TaxonomyConflict(TaxonomyError):
    pass


DEFAULT_TYPES = (
    ("CSKH", "Tài liệu Khách hàng", 10),
    ("internal", "Tài liệu nội bộ", 20),
)

DEFAULT_GROUPS = (
    ("store", "Cửa hàng", "CSKH", 10),
    ("size_guide", "Hướng dẫn chọn size", "CSKH", 20),
    ("warranty", "Bảo hành", "CSKH", 30),
    ("returns", "Đổi trả", "CSKH", 40),
    ("shipping", "Giao hàng", "CSKH", 50),
    ("promotion", "Khuyến mãi", "CSKH", 60),
    ("customer_care", "Chăm sóc khách hàng", "CSKH", 70),
    ("policy", "Chính sách", "CSKH", 80),
    ("product_links", "Liên kết sản phẩm", "CSKH", 90),
    ("brand", "Thương hiệu", "CSKH", 100),
    ("company", "Thông tin công ty", "internal", 10),
)


def validate_code(code):
    value = (code or "").strip()
    if not CODE_RE.fullmatch(value):
        raise TaxonomyError(
            "Mã chỉ gồm chữ thường không dấu, số, dấu gạch dưới hoặc gạch ngang"
        )
    return value


def validate_name(name):
    value = (name or "").strip()
    if not value or len(value) > 200:
        raise TaxonomyError("Tên phải có từ 1 đến 200 ký tự")
    return value


class TaxonomyRepository:
    """PostgreSQL catalog; document chunks and vectors remain in Qdrant."""

    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thiếu DATABASE_URL cho PostgreSQL metadata")
        self.dsn = settings.database_url.get_secret_value()

    @contextmanager
    def connection(self):
        with psycopg.connect(
            self.dsn,
            connect_timeout=5,
            row_factory=dict_row,
            options="-c statement_timeout=15000 -c lock_timeout=5000",
        ) as conn:
            yield conn

    def initialize(self):
        with self.connection() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS rag_metadata")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.doc_types (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    code VARCHAR(100) NOT NULL UNIQUE,
                    name VARCHAR(200) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.document_groups (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    doc_type_id BIGINT NOT NULL REFERENCES rag_metadata.doc_types(id)
                        ON DELETE RESTRICT,
                    code VARCHAR(100) NOT NULL UNIQUE,
                    name VARCHAR(200) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_document_groups_type
                   ON rag_metadata.document_groups(doc_type_id, sort_order, name)"""
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO rag_metadata.doc_types(code, name, sort_order)
                       VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING""",
                    DEFAULT_TYPES,
                )
                cursor.executemany(
                    """INSERT INTO rag_metadata.document_groups
                           (code, name, doc_type_id, sort_order)
                       SELECT %s, %s, id, %s FROM rag_metadata.doc_types WHERE code=%s
                       ON CONFLICT (code) DO NOTHING""",
                    [(code, name, order, type_code)
                     for code, name, type_code, order in DEFAULT_GROUPS],
                )
        return self.list_tree()

    def ready(self):
        with self.connection() as conn:
            conn.execute("SELECT id FROM rag_metadata.doc_types LIMIT 0")
            conn.execute("SELECT id FROM rag_metadata.document_groups LIMIT 0")

    def list_doc_types(self, *, include_inactive=True):
        where = "" if include_inactive else "WHERE is_active"
        with self.connection() as conn:
            return conn.execute(
                f"SELECT * FROM rag_metadata.doc_types {where} ORDER BY sort_order, name, id"
            ).fetchall()

    def get_doc_type(self, doc_type_id):
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM rag_metadata.doc_types WHERE id=%s", (doc_type_id,)
            ).fetchone()

    def create_doc_type(self, code, name, description="", sort_order=0):
        try:
            with self.connection() as conn:
                return conn.execute(
                    """INSERT INTO rag_metadata.doc_types
                           (code, name, description, sort_order)
                       VALUES (%s, %s, %s, %s) RETURNING *""",
                    (validate_code(code), validate_name(name), description.strip(), sort_order),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise TaxonomyConflict("Mã loại tài liệu đã tồn tại") from exc

    def update_doc_type(self, doc_type_id, *, name, description, is_active, sort_order):
        with self.connection() as conn:
            row = conn.execute(
                """UPDATE rag_metadata.doc_types
                   SET name=%s, description=%s, is_active=%s, sort_order=%s,
                       updated_at=NOW() WHERE id=%s RETURNING *""",
                (validate_name(name), description.strip(), is_active, sort_order, doc_type_id),
            ).fetchone()
        if row is None:
            raise TaxonomyNotFound("Không tìm thấy loại tài liệu")
        return row

    def delete_doc_type(self, doc_type_id):
        with self.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM rag_metadata.document_groups WHERE doc_type_id=%s",
                (doc_type_id,),
            ).fetchone()["count"]
            if count:
                raise TaxonomyConflict("Loại tài liệu vẫn còn nhóm; hãy chuyển hoặc xóa nhóm trước")
            row = conn.execute(
                "DELETE FROM rag_metadata.doc_types WHERE id=%s RETURNING id", (doc_type_id,)
            ).fetchone()
        if row is None:
            raise TaxonomyNotFound("Không tìm thấy loại tài liệu")
        return {"id": row["id"], "deleted": True}

    def list_groups(self, doc_type_id=None, *, include_inactive=True):
        clauses, values = [], []
        if doc_type_id is not None:
            clauses.append("g.doc_type_id=%s")
            values.append(doc_type_id)
        if not include_inactive:
            clauses.extend(("g.is_active", "t.is_active"))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as conn:
            return conn.execute(
                f"""SELECT g.*, t.code AS doc_type_code, t.name AS doc_type_name
                    FROM rag_metadata.document_groups g
                    JOIN rag_metadata.doc_types t ON t.id=g.doc_type_id
                    {where} ORDER BY t.sort_order, t.name, g.sort_order, g.name, g.id""",
                values,
            ).fetchall()

    def get_group(self, group_id):
        with self.connection() as conn:
            return conn.execute(
                """SELECT g.*, t.code AS doc_type_code, t.name AS doc_type_name
                   FROM rag_metadata.document_groups g
                   JOIN rag_metadata.doc_types t ON t.id=g.doc_type_id
                   WHERE g.id=%s""",
                (group_id,),
            ).fetchone()

    def get_group_by_code(self, code):
        with self.connection() as conn:
            return conn.execute(
                """SELECT g.*, t.code AS doc_type_code, t.name AS doc_type_name
                   FROM rag_metadata.document_groups g
                   JOIN rag_metadata.doc_types t ON t.id=g.doc_type_id
                   WHERE g.code=%s""",
                ((code or "").strip().lower(),),
            ).fetchone()

    def create_group(self, doc_type_id, code, name, description="", sort_order=0):
        if self.get_doc_type(doc_type_id) is None:
            raise TaxonomyNotFound("Không tìm thấy loại tài liệu cha")
        try:
            with self.connection() as conn:
                return conn.execute(
                    """INSERT INTO rag_metadata.document_groups
                           (doc_type_id, code, name, description, sort_order)
                       VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                    (doc_type_id, validate_code(code), validate_name(name),
                     description.strip(), sort_order),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise TaxonomyConflict("Mã nhóm đã tồn tại") from exc

    def ensure_group(self, code, name=None):
        code = validate_code(code)
        current = self.get_group_by_code(code)
        if current:
            return current
        public = next(row for row in self.list_doc_types() if row["code"] == "public")
        label = name or code.replace("_", " ").replace("-", " ").title()
        try:
            return self.create_group(public["id"], code, label)
        except TaxonomyConflict:
            return self.get_group_by_code(code)

    def update_group(self, group_id, *, doc_type_id, name, description, is_active, sort_order):
        if self.get_doc_type(doc_type_id) is None:
            raise TaxonomyNotFound("Không tìm thấy loại tài liệu cha")
        with self.connection() as conn:
            row = conn.execute(
                """UPDATE rag_metadata.document_groups
                   SET doc_type_id=%s, name=%s, description=%s, is_active=%s,
                       sort_order=%s, updated_at=NOW() WHERE id=%s RETURNING *""",
                (doc_type_id, validate_name(name), description.strip(), is_active,
                 sort_order, group_id),
            ).fetchone()
        if row is None:
            raise TaxonomyNotFound("Không tìm thấy nhóm tài liệu")
        return self.get_group(group_id)

    def delete_group(self, group_id):
        with self.connection() as conn:
            row = conn.execute(
                "DELETE FROM rag_metadata.document_groups WHERE id=%s RETURNING id", (group_id,)
            ).fetchone()
        if row is None:
            raise TaxonomyNotFound("Không tìm thấy nhóm tài liệu")
        return {"id": row["id"], "deleted": True}

    def list_tree(self, *, include_inactive=True):
        types = self.list_doc_types(include_inactive=include_inactive)
        groups = self.list_groups(include_inactive=include_inactive)
        by_type = {row["id"]: [] for row in types}
        for group in groups:
            by_type.setdefault(group["doc_type_id"], []).append(group)
        return [{**row, "groups": by_type.get(row["id"], [])} for row in types]


class CompatibilityTaxonomy:
    """Small catalog used only when tests inject a complete KnowledgeService."""

    def __init__(self):
        self.types = {
            1: {"id": 1, "code": "public", "name": "Kiến thức công khai",
                "is_active": True, "description": "", "sort_order": 10},
            2: {"id": 2, "code": "internal", "name": "Tài liệu nội bộ",
                "is_active": True, "description": "", "sort_order": 20},
        }
        self.groups = {}
        for identifier, (code, name, type_code, order) in enumerate(DEFAULT_GROUPS, 1):
            # CompatibilityTaxonomy keeps the historical public/internal IDs
            # used by injected test services. The production seed now names
            # the public-facing type CSKH, so treat both codes as type 1.
            doc_type_id = 1 if type_code in {"public", "CSKH"} else 2
            self.groups[identifier] = {
                "id": identifier, "code": code, "name": name,
                "doc_type_id": doc_type_id, "doc_type_code": type_code,
                "doc_type_name": self.types[doc_type_id]["name"],
                "is_active": True, "description": "", "sort_order": order,
            }

    def ready(self):
        return None

    def get_doc_type(self, identifier):
        return self.types.get(identifier)

    def get_group(self, identifier):
        return self.groups.get(identifier)

    def get_group_by_code(self, code):
        return next((row for row in self.groups.values() if row["code"] == code), None)

    def ensure_group(self, code, name=None):
        code = validate_code(code)
        current = self.get_group_by_code(code)
        if current:
            return current
        identifier = max(self.groups, default=0) + 1
        self.groups[identifier] = {
            "id": identifier, "code": code,
            "name": name or code.replace("_", " ").title(),
            "doc_type_id": 1, "doc_type_code": "public",
            "doc_type_name": self.types[1]["name"], "is_active": True,
            "description": "", "sort_order": 0,
        }
        return self.groups[identifier]

    def list_tree(self, **_):
        return [
            {**row, "groups": [group for group in self.groups.values()
                                if group["doc_type_id"] == row["id"]]}
            for row in self.types.values()
        ]
