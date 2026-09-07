-- Chạy trên database của service. Chỉ bổ sung metadata file, giữ nguyên dữ liệu.
ALTER TABLE rag_service.documents
    ADD COLUMN IF NOT EXISTS file_storage_key VARCHAR(500),
    ADD COLUMN IF NOT EXISTS file_name VARCHAR(500),
    ADD COLUMN IF NOT EXISTS file_mime_type VARCHAR(100),
    ADD COLUMN IF NOT EXISTS file_size BIGINT,
    ADD COLUMN IF NOT EXISTS file_origin VARCHAR(30);
