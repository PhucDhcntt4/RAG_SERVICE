-- Hỗ trợ mã tham chiếu file có tên gốc trong thư mục knowlegde.
ALTER TABLE rag_service.documents ALTER COLUMN file_storage_key TYPE VARCHAR(500);
