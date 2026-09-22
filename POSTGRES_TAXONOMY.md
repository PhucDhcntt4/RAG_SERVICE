# Triển khai PostgreSQL quản lý loại và nhóm tài liệu

Tài liệu này áp dụng cho RAG Service chạy trực tiếp bằng Python trên Windows, không sử dụng Docker.

## 1. Kiến trúc lưu trữ

Hệ thống sử dụng PostgreSQL và Qdrant cho hai nhiệm vụ khác nhau:

```text
PostgreSQL
└── Loại tài liệu (Doc type)
    └── Nhóm tài liệu (Document group)

Qdrant
└── Tài liệu
    └── Chunk + vector + metadata phân loại
```

PostgreSQL lưu:

- Loại tài liệu.
- Nhóm trực thuộc từng loại.
- Tên, mô tả, trạng thái và thứ tự hiển thị.

Qdrant tiếp tục lưu:

- Nội dung chunk và embedding.
- `document_id`, `doc_type_id`, `group_id` và `category`.
- Metadata phục vụ tìm kiếm RAG.

Đổi tên loại/nhóm hoặc chuyển tài liệu sang nhóm khác không tạo lại embedding.

## 2. Yêu cầu

- PostgreSQL đang chạy trên Windows.
- PostgreSQL cho phép kết nối TCP tại `127.0.0.1`.
- Python và môi trường `.venv` của dự án đã được cài đặt.
- Qdrant vẫn đang chạy theo cấu hình hiện tại của dự án.

Ví dụ trong hệ thống hiện tại:

| Thành phần | Giá trị |
|---|---|
| PostgreSQL host | `127.0.0.1` |
| PostgreSQL port | `5433` |
| Database | `rag_service` |
| Schema ứng dụng | `rag_metadata` |

## 3. Tạo database và tài khoản

### Cách khuyến nghị: tài khoản riêng cho RAG Service

Đăng nhập PostgreSQL bằng tài khoản quản trị, sau đó chạy:

```sql
CREATE ROLE rag_service_app
    WITH LOGIN
    PASSWORD 'thay_bang_mat_khau_manh';

CREATE DATABASE rag_service
    WITH OWNER = rag_service_app
    ENCODING = 'UTF8';
```

Nếu database `rag_service` đã tồn tại, không tạo lại. Có thể chuyển quyền sở hữu bằng tài khoản quản trị:

```sql
ALTER DATABASE rag_service OWNER TO rag_service_app;
```

Trong môi trường thử nghiệm nội bộ, có thể dùng tài khoản `postgres` hiện có. Không nên sử dụng tài khoản quản trị này cho production.

## 4. Cấu hình `.env`

Thêm `DATABASE_URL` vào file `.env` ở đúng thư mục dự án:

```dotenv
DATABASE_URL=postgresql://rag_service_app:thay_bang_mat_khau@127.0.0.1:5433/rag_service
```

Nếu đang dùng tài khoản `postgres`:

```dotenv
DATABASE_URL=postgresql://postgres:thay_bang_mat_khau@127.0.0.1:5433/rag_service
```

Lưu ý:

- Không viết `\@`; ký tự ngăn cách phải là `@`.
- Nên dùng tên database chữ thường `rag_service`.
- Nếu mật khẩu chứa `@`, `:`, `/`, `#` hoặc `%`, phải URL-encode mật khẩu.
- Không commit `.env` lên Git.
- Khi chạy trực tiếp trên Windows chỉ cần `DATABASE_URL`; không cần `POSTGRES_DB`, `POSTGRES_USER` hay `POSTGRES_PASSWORD`.

Ví dụ mã hóa mật khẩu bằng Python:

```powershell
.\.venv\Scripts\python.exe -c "from urllib.parse import quote; print(quote(input('Password: '), safe=''))"
```

## 5. Cài thư viện PostgreSQL

Tại thư mục dự án:

```powershell
Set-Location "C:\D\ĐÔNG HẢI\DATA\RAG_Service"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Driver được sử dụng là:

```text
psycopg[binary]==3.2.10
```

## 6. Khởi tạo schema và bảng

Chạy:

```powershell
.\.venv\Scripts\python.exe -m app.init_db --confirm
```

Kết quả thành công:

```text
PostgreSQL taxonomy initialized: 2 document types, 13 groups.
Qdrant vectors were not changed.
```

Lệnh này có thể chạy lại an toàn. Các câu lệnh dùng `IF NOT EXISTS` và `ON CONFLICT DO NOTHING`, vì vậy không tạo trùng dữ liệu mặc định.

Ứng dụng cũng tự gọi quá trình khởi tạo khi khởi động. Lệnh `app.init_db` được dùng để kiểm tra kết nối và quyền PostgreSQL trước khi chạy API.

## 7. Các bảng được tạo

Ứng dụng tạo schema riêng:

```text
rag_metadata
├── doc_types
└── document_groups
```

### `rag_metadata.doc_types`

Quản lý loại tài liệu cấp cha.

| Cột | Mục đích |
|---|---|
| `id` | Khóa chính tự tăng |
| `code` | Mã kỹ thuật duy nhất, ví dụ `public` |
| `name` | Tên hiển thị |
| `description` | Mô tả |
| `is_active` | Trạng thái sử dụng |
| `sort_order` | Thứ tự hiển thị |
| `created_at` | Thời gian tạo |
| `updated_at` | Thời gian cập nhật |

### `rag_metadata.document_groups`

Quản lý nhóm tài liệu cấp con.

| Cột | Mục đích |
|---|---|
| `id` | Khóa chính tự tăng |
| `doc_type_id` | Khóa ngoại tới `doc_types.id` |
| `code` | Mã nhóm duy nhất, tương thích với `category` cũ |
| `name` | Tên hiển thị |
| `description` | Mô tả |
| `is_active` | Trạng thái sử dụng |
| `sort_order` | Thứ tự hiển thị |
| `created_at` | Thời gian tạo |
| `updated_at` | Thời gian cập nhật |

Quan hệ:

```text
doc_types (1) ──────── (n) document_groups
```

Không tạo bảng document, chunk hoặc vector trong PostgreSQL. Các dữ liệu đó vẫn nằm trong Qdrant.

## 8. Kiểm tra trong pgAdmin

Mở theo đường dẫn:

```text
Servers
└── PostgreSQL
    └── Databases
        └── rag_service
            └── Schemas
                └── rag_metadata
                    └── Tables
                        ├── doc_types
                        └── document_groups
```

Nếu chưa thấy bảng, bấm chuột phải vào `Schemas` hoặc `rag_metadata`, sau đó chọn **Refresh**.

Kiểm tra bằng SQL:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'rag_metadata'
ORDER BY table_name;

SELECT *
FROM rag_metadata.doc_types
ORDER BY sort_order, id;

SELECT
    dt.name AS doc_type,
    dg.name AS document_group,
    dg.code,
    dg.is_active
FROM rag_metadata.doc_types AS dt
LEFT JOIN rag_metadata.document_groups AS dg
    ON dg.doc_type_id = dt.id
ORDER BY dt.sort_order, dg.sort_order, dg.id;
```

## 9. Khởi động RAG Service

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Khi khởi động, ứng dụng thực hiện:

1. Kết nối PostgreSQL bằng `DATABASE_URL`.
2. Kiểm tra/tạo schema `rag_metadata`.
3. Seed loại và nhóm mặc định nếu chưa có.
4. Đọc các `category` cũ trong Qdrant.
5. Tạo nhóm tương thích nếu cần.
6. Bổ sung `doc_type_id` và `group_id` vào payload Qdrant cũ.
7. Khởi tạo chỉ mục BM25 trong bộ nhớ.

Việc bổ sung metadata Qdrant không gọi Gemini và không tạo lại embedding.

## 10. Quản lý trên giao diện

Trang Kho tài liệu:

```text
http://127.0.0.1:8000/admin
```

Trang quản lý loại và nhóm:

```text
http://127.0.0.1:8000/taxonomy
```

Chức năng hỗ trợ:

- Thêm loại tài liệu.
- Sửa tên, mô tả và trạng thái loại tài liệu.
- Thêm nhóm vào một loại cha.
- Sửa tên, mô tả, trạng thái hoặc loại cha của nhóm.
- Xóa loại không còn nhóm.
- Xóa nhóm không được tài liệu Qdrant sử dụng.
- Chuyển nhóm của tài liệu mà không tạo lại embedding.
- Lọc danh sách tài liệu theo loại và nhóm.

`code` là định danh kỹ thuật ổn định nên bị khóa sau khi tạo. Hai mã mặc định `public` và `internal` không nên thay đổi.

## 11. Kiểm tra API

API chuẩn yêu cầu `ADMIN_API_KEY`:

```powershell
$adminKey = Read-Host "ADMIN_API_KEY"
$headers = @{ Authorization = "Bearer $adminKey" }

Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/api/v1/taxonomy" `
    -Headers $headers

Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/api/v1/health/ready" `
    -Headers $headers
```

Readiness chỉ trả về thành công khi PostgreSQL và Qdrant đều sẵn sàng.

Lọc tài liệu theo loại/nhóm:

```powershell
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/api/v1/documents?doc_type_id=1&group_id=1" `
    -Headers $headers
```

## 12. Quy tắc an toàn dữ liệu

- Không thể xóa loại tài liệu nếu loại đó vẫn còn nhóm.
- Không thể xóa nhóm nếu tài liệu Qdrant đang sử dụng nhóm.
- Tắt loại/nhóm sẽ ngăn sử dụng cho tài liệu mới nhưng không xóa dữ liệu.
- Đổi loại cha của nhóm giữ nguyên `group_id` và cập nhật `doc_type_id` trong Qdrant.
- Xóa dữ liệu PostgreSQL không tự xóa tài liệu hoặc vector Qdrant.
- Xóa collection Qdrant không tự xóa loại và nhóm PostgreSQL.

## 13. Sao lưu và khôi phục

Sao lưu riêng schema phân loại:

```powershell
pg_dump `
    --host=127.0.0.1 `
    --port=5433 `
    --username=rag_service_app `
    --dbname=rag_service `
    --schema=rag_metadata `
    --format=custom `
    --file=rag_metadata.backup
```

Khôi phục:

```powershell
pg_restore `
    --host=127.0.0.1 `
    --port=5433 `
    --username=rag_service_app `
    --dbname=rag_service `
    --clean `
    --if-exists `
    rag_metadata.backup
```

Một bản sao lưu RAG đầy đủ phải gồm:

1. Schema PostgreSQL `rag_metadata`.
2. Snapshot collection Qdrant.
3. Thư mục `knowlegde` chứa file gốc.
4. Bản sao riêng tư của `.env` hoặc hệ thống quản lý secret.

## 14. Xử lý lỗi thường gặp

### `connection refused`

- Kiểm tra PostgreSQL service đang chạy.
- Kiểm tra cổng thực tế là `5433` hay `5432`.
- Kiểm tra `listen_addresses` và firewall nếu PostgreSQL nằm trên máy khác.

### `password authentication failed`

- Kiểm tra user/password trong `DATABASE_URL`.
- URL-encode mật khẩu có ký tự đặc biệt.
- Kiểm tra rule tương ứng trong `pg_hba.conf`.

### `database ... does not exist`

- Kiểm tra database đã được tạo.
- Dùng tên chữ thường `rag_service` nếu database được tạo không có dấu ngoặc kép.

### `permission denied for database` hoặc `permission denied for schema`

Tài khoản ứng dụng cần quyền kết nối database và tạo/sử dụng schema trong lần khởi tạo đầu tiên. Với database đã có:

```sql
GRANT CONNECT ON DATABASE rag_service TO rag_service_app;
GRANT CREATE ON DATABASE rag_service TO rag_service_app;
```

Sau khi kết nối vào database `rag_service`:

```sql
GRANT USAGE, CREATE ON SCHEMA rag_metadata TO rag_service_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA rag_metadata TO rag_service_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA rag_metadata TO rag_service_app;
```

### Không thấy hai bảng trong pgAdmin

Hai bảng không nằm trong schema `public`. Mở `Schemas → rag_metadata → Tables` và bấm **Refresh**.

### `Thiếu DATABASE_URL cho PostgreSQL metadata`

- Kiểm tra `.env` nằm tại thư mục gốc dự án.
- Kiểm tra tên biến viết đúng là `DATABASE_URL`.
- Khởi động lại Uvicorn sau khi sửa `.env`.

### PostgreSQL hoạt động nhưng readiness trả `503`

Readiness còn kiểm tra Qdrant. Xem log để xác định dịch vụ nào không kết nối được.

## 15. Các file liên quan

| File | Chức năng |
|---|---|
| `app/config.py` | Đọc `DATABASE_URL` |
| `app/taxonomy_repository.py` | Tạo bảng và CRUD PostgreSQL |
| `app/init_db.py` | Kiểm tra/khởi tạo schema thủ công |
| `app/main.py` | API loại, nhóm và đồng bộ Qdrant |
| `app/qdrant_repository.py` | Lưu ID phân loại trong payload Qdrant |
| `app/static/taxonomy.html` | Trang quản lý loại/nhóm |
| `app/static/taxonomy.js` | Logic giao diện quản lý |
