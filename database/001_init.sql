-- Database riêng của service: "RAG_SERVICE", PostgreSQL tại 127.0.0.1:5433.
-- Trong pgAdmin: mở Query Tool trên database RAG_SERVICE rồi chạy file này.
-- Hoặc dùng psql (mật khẩu được hỏi riêng):
-- psql -h 127.0.0.1 -p 5433 -U postgres -d RAG_SERVICE -v ON_ERROR_STOP=1 -1 -f database/001_init.sql
-- Khi chạy qua app.init_db, database đích lấy từ DATABASE_URL trong .env.
-- File này chạy trong database đã kết nối; không tạo hoặc chuyển database.
-- Database tên RAG_SERVICE; schema rag_service được app/repository.py sử dụng.
-- Có thể chạy lại để tạo đối tượng còn thiếu; không nâng cấp cấu trúc bảng đã có.

-- Cài pgvector trong schema public của database đang kết nối.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE SCHEMA IF NOT EXISTS rag_service;

CREATE TABLE IF NOT EXISTS rag_service.documents (
    id BIGSERIAL PRIMARY KEY,
    source_key VARCHAR(500) NOT NULL UNIQUE,
    title VARCHAR(500) NOT NULL,
    category VARCHAR(100) NOT NULL,
    source_text TEXT NOT NULL,
    file_storage_key VARCHAR(500),
    file_name VARCHAR(500),
    file_mime_type VARCHAR(100),
    file_size BIGINT,
    file_origin VARCHAR(30),
    source_checksum VARCHAR(64) NOT NULL,
    embedding_provider VARCHAR(50) NOT NULL,
    embedding_model VARCHAR(150) NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension = 768),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rag_service.chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES rag_service.documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading VARCHAR(500),
    content TEXT NOT NULL,
    embedding public.vector(768) NOT NULL,
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS rag_documents_category_idx
ON rag_service.documents(category, is_active);
-- Start with exact cosine search to preserve recall with category filters.
-- Add/tune a vector index after measuring corpus size and retrieval recall.
