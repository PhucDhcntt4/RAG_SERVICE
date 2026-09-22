# Cài đặt source code và kết nối Qdrant

Tài liệu này hướng dẫn chạy trực tiếp RAG Service bằng Python trên Windows và kết nối tới một Qdrant đang hoạt động. Bản hiện tại cần thêm PostgreSQL để quản lý loại tài liệu và nhóm; không dùng pgvector. Xem cấu hình tại [POSTGRES_TAXONOMY.md](POSTGRES_TAXONOMY.md).

## 1. Mô hình kết nối

```text
Trình duyệt / Bot
        │ HTTP :8000
        ▼
RAG Service (FastAPI)
        ├── Gemini API: embedding, rewrite, answer
        ├── Qdrant REST :6333: vector và metadata chunk
        ├── PostgreSQL : loại tài liệu và nhóm
        └── knowlegde/: file tài liệu gốc
```

Qdrant phải cho phép máy chạy RAG Service truy cập REST API. Khi cả hai chạy trên cùng máy Windows, dùng `http://127.0.0.1:6333`.

## 2. Yêu cầu

- Windows 10/11 hoặc Windows Server.
- Python 3.11 trở lên; khuyến nghị Python 3.12.
- Qdrant đang chạy và mở REST port `6333`.
- PostgreSQL 16 đang chạy và có database/user dành cho RAG Service.
- Gemini API key có quyền dùng model embedding và chat đã cấu hình.
- PowerShell.

Không công khai cổng Qdrant ra Internet. Nếu Qdrant ở máy khác, chỉ cho phép IP của RAG Service truy cập qua firewall hoặc mạng riêng.

## 3. Cài source code

Mở PowerShell tại thư mục dự án:

```powershell
Set-Location "D:\ĐÔNG HẢI\DATA\RAG_Service"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

Không bắt buộc kích hoạt virtual environment. Các lệnh trong tài liệu gọi trực tiếp Python trong `.venv` để tránh chạy nhầm Python hệ thống.

## 4. Tạo `.env`

Nếu chưa có `.env`:

```powershell
Copy-Item -LiteralPath .\.env.example -Destination .\.env
```

Tạo hai API key khác nhau:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Điền các giá trị vào `.env`:

```dotenv
SEARCH_API_KEY=<key dành cho bot/search>
ADMIN_API_KEY=<key dành cho chat và quản trị tài liệu>
GEMINI_API_KEY=<Gemini API key>

QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=rag_service_compare_v1
QDRANT_TIMEOUT_SECONDS=15

RAG_EMBEDDING_MODEL=gemini-embedding-001
RAG_CHAT_MODEL=gemini-3.1-flash-lite
```

Quy tắc quan trọng:

- `SEARCH_API_KEY` và `ADMIN_API_KEY` phải khác nhau.
- Mỗi key nội bộ phải có ít nhất 32 ký tự ASCII và không có khoảng trắng.
- Không commit `.env` và không gửi các key qua chat hoặc log.
- Biến môi trường của Windows sẽ ghi đè giá trị cùng tên trong `.env`.

## 5. Kiểm tra Qdrant

Kiểm tra REST API:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:6333/collections"
```

Nếu kết nối đúng, Qdrant trả về `status=ok`. Nếu báo `Connection refused`, Qdrant chưa chạy, sai port hoặc bị firewall chặn.

Kiểm tra collection của dự án:

```powershell
$collection = "rag_service_compare_v1"
$info = Invoke-RestMethod -Uri "http://127.0.0.1:6333/collections/$collection"
$info.result.status
$info.result.config.params.vectors.size
$info.result.config.params.vectors.distance
```

Collection hợp lệ phải có:

```text
status   = green hoặc yellow
size     = 768
distance = Cosine
```

### Tạo collection nếu chưa tồn tại

Chỉ chạy lệnh sau khi chắc chắn collection chưa có. Không xóa hoặc tạo lại collection đang chứa dữ liệu.

```powershell
$collection = "rag_service_compare_v1"
$body = @{
    vectors = @{
        size     = 768
        distance = "Cosine"
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Put `
    -Uri "http://127.0.0.1:6333/collections/$collection" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

Tên trong `$collection` phải giống `QDRANT_COLLECTION` trong `.env`.

## 6. Kiểm tra cấu hình ứng dụng

Lệnh sau validate `.env` nhưng không in API key:

```powershell
.\.venv\Scripts\python.exe -c "from app.config import Settings; s=Settings.load(); print({'qdrant_url': s.qdrant_url, 'collection': s.qdrant_collection, 'embedding_model': s.rag_embedding_model})"
```

Nếu lệnh báo `ValidationError`, sửa đúng biến được nêu trong thông báo trước khi khởi động service.

## 7. Chạy RAG Service

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Nếu sử dụng đồng bộ sản phẩm Shopify, mở thêm một cửa sổ PowerShell và chạy worker:

```powershell
.\.venv\Scripts\python.exe -m app.product_sync.worker
```

API chỉ tạo và theo dõi job; worker là tiến trình nhận job, ghi catalog/ảnh vào Qdrant và chạy lịch đồng bộ tồn kho. Khi chạy bằng Docker Compose, service `product-worker` đã thực hiện phần này tự động.

Giữ cửa sổ PowerShell này mở. Sau đó truy cập:

- Giao diện quản trị: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- Liveness: `http://127.0.0.1:8000/health/live`

Nếu cổng 8000 đang được sử dụng, đổi thành `--port 8001` và dùng cổng mới cho mọi URL bên dưới.

## 8. Xác nhận RAG Service đã kết nối Qdrant

Mở cửa sổ PowerShell khác:

```powershell
$searchKey = Read-Host "SEARCH_API_KEY"
$headers = @{ Authorization = "Bearer $searchKey" }

Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/api/v1/health/ready" `
    -Headers $headers
```

Kết quả đúng có dạng:

```text
status              : ready
vector_provider     : qdrant
qdrant_collection   : rag_service_compare_v1
bm25_indexed_chunks : <số chunk đang active>
```

Endpoint này kiểm tra Qdrant nhưng không gọi Gemini. Liveness thành công mà readiness thất bại thường có nghĩa là Qdrant URL, collection hoặc schema vector chưa đúng.

## 9. Thêm tài liệu thử

Cách đơn giản nhất:

1. Mở `http://127.0.0.1:8000/`.
2. Kết nối bằng `ADMIN_API_KEY` nếu giao diện yêu cầu.
3. Upload file TXT, Markdown hoặc PDF có lớp văn bản.
4. Chờ hoàn tất chunking và embedding.
5. Kiểm tra tài liệu xuất hiện trong danh sách.

Có thể thêm văn bản bằng API:

```powershell
$adminKey = Read-Host "ADMIN_API_KEY"
$headers = @{ Authorization = "Bearer $adminKey" }
$document = @{
    source_key = "setup:test"
    title      = "Tài liệu kiểm tra"
    category   = "test"
    text       = "Đông Hải kiểm tra kết nối Qdrant thành công."
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Put `
    -Uri "http://127.0.0.1:8000/api/v1/documents" `
    -Headers $headers `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($document))
```

Thao tác này gọi Gemini để tạo embedding và lưu point vào collection Qdrant.

## 10. Thử tìm kiếm

```powershell
$query = @{
    query = "Đã kết nối Qdrant thành công chưa?"
    top_k = 5
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/v1/knowledge/search" `
    -Headers $headers `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($query))
```

Nếu dùng `$headers` từ bước thêm tài liệu thì đây là admin key và vẫn hợp lệ. Bot production chỉ nên giữ `SEARCH_API_KEY` cho endpoint search.

## 11. Kết nối ứng dụng hoặc bot

Cấu hình phía bot:

```dotenv
RAG_SERVICE_URL=http://127.0.0.1:8000
RAG_SERVICE_API_KEY=<SEARCH_API_KEY của RAG Service>
```

Gửi `Authorization: Bearer <SEARCH_API_KEY>` tới `/api/v1/knowledge/search`. Adapter Python mẫu nằm trong `examples/bot_client.py`.

Không cung cấp `ADMIN_API_KEY` cho bot chỉ cần tìm kiếm. Endpoint `/api/v1/chat` hiện yêu cầu admin key vì nó gọi LLM và có chi phí riêng.

## 12. Dữ liệu được lưu ở đâu?

- Qdrant collection: vector, nội dung chunk và metadata.
- `knowlegde/`: file gốc được upload.
- RAM của RAG Service: chỉ mục BM25; được tạo lại khi khởi động.
- `.env`: cấu hình và secret local.

Sao lưu phải bao gồm snapshot Qdrant và thư mục `knowlegde`. Chỉ sao chép một trong hai sẽ không đủ để khôi phục đầy đủ hệ thống.

## 13. Lỗi thường gặp

### Qdrant trả 404 collection

`QDRANT_COLLECTION` không tồn tại hoặc tên không khớp. Tạo collection 768/Cosine hoặc sửa `.env` trỏ tới collection đúng.

### Collection phải dùng vector 768 chiều và Cosine

Collection hiện tại được tạo bằng schema khác. Không đổi schema collection đang có; tạo collection mới đúng cấu hình rồi nhập lại tài liệu.

### Gemini trả 429 hoặc quota exceeded

Giảm batch, tăng khoảng nghỉ hoặc kiểm tra quota của Gemini:

```dotenv
RAG_EMBEDDING_BATCH_SIZE=8
RAG_EMBEDDING_BATCH_INTERVAL_SECONDS=5
RAG_EMBEDDING_MAX_RETRIES=2
```

### Chữ tiếng Việt bị lỗi trong PowerShell

Luôn gửi JSON bằng UTF-8 bytes như các ví dụ trong tài liệu, thay vì truyền trực tiếp chuỗi JSON cho `-Body`.

### Đổi `.env` nhưng ứng dụng chưa nhận

Dừng Uvicorn bằng `Ctrl+C`, sau đó chạy lại. `Settings` được nạp khi process khởi động.

## 14. Kiểm thử source code

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
node --test tests/test_admin_ui.cjs
```

Test Python mặc định dùng mock, không ghi vào collection Qdrant production và không tiêu tốn quota Gemini.

## 15. Checklist trước khi sử dụng thật

- Readiness trả `status=ready`.
- Collection dùng 768 chiều và Cosine.
- Upload và search thử thành công.
- Bot chỉ giữ `SEARCH_API_KEY`.
- `ADMIN_API_KEY` và `GEMINI_API_KEY` được giữ kín.
- `RAG_LOCAL_ADMIN_ENABLED=false` nếu service không chỉ chạy trên máy local.
- Dùng HTTPS/reverse proxy nếu cho phép máy khác truy cập API.
- Không public port `6333` của Qdrant.
- Có lịch snapshot Qdrant và sao lưu `knowlegde`.
