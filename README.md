# Đông Hải RAG Service

Dự án RAG độc lập với `BOT_Conversation_V2`. Nhận tài liệu → chia đoạn → tạo embedding → lưu PostgreSQL/pgvector → tìm nội dung liên quan qua HTTP API.

Service trả **ngữ cảnh và nguồn**, không tự tư vấn/chốt đơn. Bot vẫn giữ instruction, lịch sử hội thoại, điều phối và cách trả lời khách. Không cần Redis, không dùng MCP trong bản này.

## Cài đặt trên Windows

Muốn bật/tắt bằng Docker Desktop hoặc Docker Compose, xem [hướng dẫn Docker](DOCKER.md). Gói Docker dùng database PostgreSQL hiện có và giữ tài liệu trong `knowlegde` trên máy Windows.

Yêu cầu Python 3.11+, PostgreSQL đã có extension pgvector, database/tài khoản dành cho RAG, Gemini API key. Chưa tự tạo database, chưa nhập tài liệu cũ và chưa chuyển bot sang service này.

Trong CMD:

```bat
cd /d "D:\ĐÔNG HẢI\DATA\RAG_Service"
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Chạy lệnh tạo key hai lần; điền hai kết quả khác nhau vào `SEARCH_API_KEY` và `ADMIN_API_KEY` trong `.env`. Điền `DATABASE_URL` và `GEMINI_API_KEY`. Không dùng chuỗi mẫu, không commit `.env`, không nhúng admin key vào mã nguồn giao diện hay cấu hình bot.

`DATABASE_URL` phải trỏ đúng database mong muốn. Khuyến nghị database riêng; nếu dùng chung server/database thì các bảng mới vẫn nằm trong schema `rag_service`, không đụng bảng bot. Mật khẩu trong URL có ký tự đặc biệt cần percent-encode.

Sau khi kiểm tra cấu hình:

```bat
python -m app.init_db --confirm
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Lệnh init chạy `001_init.sql`, `002_local_files.sql` và `003_named_files.sql`, tạo schema/bảng và bổ sung metadata file local còn thiếu. Các script này có thể chạy lại; không phải công cụ nâng cấp mọi phiên bản schema. Cần quyền tạo schema/table và extension; nếu thiếu quyền extension, nhờ quản trị DB cài pgvector trước. Khi triển khai production nên tách tài khoản migration và tài khoản chạy API có quyền tối thiểu.

Mở `http://127.0.0.1:8001/` hoặc `/admin` để dùng giao diện quản lý tài liệu. Nếu chạy với `--port 8000`, dùng `http://127.0.0.1:8000/`.

## Giao diện quản lý tài liệu

1. Nhập giá trị `ADMIN_API_KEY` vào ô kết nối (không kèm chữ `Bearer`). Key chỉ nằm trong bộ nhớ trang, không ghi vào cookie, localStorage hay sessionStorage. Tải lại trang cần nhập lại key.
2. Bấm **Thêm tài liệu**, chọn/kéo thả file TXT, MD hoặc PDF có lớp văn bản, điền tên và chọn nhóm. Có thể nhập mã nhóm khác. File TXT/MD cần UTF-8; giới hạn upload lấy từ cấu hình server (mặc định 10 MB).
3. Bấm **Thêm tài liệu** và đợi xử lý embedding. Tài liệu thành công sẽ xuất hiện trên trang đầu của danh sách. Mỗi lần mở biểu mẫu tạo mã nguồn riêng, không ghi đè tài liệu cùng tên.
4. Bấm **tên tài liệu** để mở hộp xem toàn bộ văn bản đã lưu (PDF hiển thị phần văn bản trích xuất). Bấm **Tải về** để lấy file đã lưu. Bấm **Xóa** trên tài liệu, kiểm tra tên trong hộp xác nhận rồi bấm **Xóa tài liệu**. Tài liệu, embedding và file local bị xóa vĩnh viễn.
5. Dùng **Trước/Sau** để chuyển trang (20 tài liệu mỗi trang), **Làm mới** để cập nhật danh sách, hoặc **Ngắt kết nối** khi xong.

Nếu mất kết nối trong lúc thêm/xóa, kiểm tra lại danh sách trước khi gửi lại. Giao diện và tài nguyên tĩnh công khai, nhưng API danh sách/upload/xóa vẫn bắt buộc admin key. Giao diện dùng API cùng origin, không cần Node.js hoặc bước build. Khi cập nhật code, khởi động lại Uvicorn để nhận route mới.

Swagger vẫn có tại `http://127.0.0.1:8001/docs` để thử API tìm kiếm và các thao tác khác. Bấm **Authorize**, nhập admin key (không cần tiền tố Bearer trong ô này).

## Các API v1

| Method | URL | Quyền / chức năng |
|---|---|---|
| GET | `/health/live` | Công khai, kiểm tra process sống |
| GET | `/api/v1/health/ready` | Search/admin key; kiểm tra kết nối và bảng DB, không gọi Gemini |
| POST | `/api/v1/knowledge/search` | Search/admin key; tìm ngữ cảnh |
| PUT | `/api/v1/documents` | Admin; thêm hoặc cập nhật bằng JSON |
| POST | `/api/v1/documents/upload` | Admin; tải TXT/MD/PDF text |
| GET | `/api/v1/documents?limit=50&offset=0` | Admin; danh sách phân trang |
| GET | `/api/v1/documents/{id}` | Admin; nội dung gốc và metadata |
| PATCH | `/api/v1/documents/{id}` | Admin; `{"is_active": false}` để ngừng tìm kiếm |
| DELETE | `/api/v1/documents/{id}` | Admin; xóa tài liệu và embedding; không có thùng rác |

Các API yêu cầu header `Authorization: Bearer <API_KEY>`. Swagger/schema API công khai nhưng **dữ liệu và thao tác tài liệu đều kiểm tra key ở server**. Dùng HTTPS qua reverse proxy nếu truy cập từ máy khác. Không công khai cổng PostgreSQL.

Thêm dữ liệu thử (không phải chính sách thật), dùng `PUT /api/v1/documents`:

```json
{
  "source_key": "test/size-guide",
  "title": "Hướng dẫn đo chân — dữ liệu thử",
  "category": "size_guide",
  "text": "# Cách đo\nĐặt bàn chân lên giấy và đo từ gót đến đầu ngón dài nhất."
}
```

`source_key` là định danh logic, **không phải đường dẫn được ghi xuống ổ đĩa**. Gửi lại cùng source_key sẽ thay nội dung/chunks trong một transaction sau khi tạo thành công tất cả embedding. Lỗi embedding hoặc lỗi transaction không xóa mất bản cũ. Cập nhật không tự bật lại tài liệu đã tắt; dùng PATCH để bật rõ ràng. Việc tải lại vẫn tạo embedding và có thể phát sinh phí, chưa có bỏ qua theo checksum.

Upload dùng multipart: `file`, `source_key`, `title`, `category`. TXT/MD cần UTF-8; PDF cần lớp text, không mã hóa, tối đa 200 trang, chưa có OCR. Lưu text đã trích xuất và metadata trong DB, đồng thời giữ nguyên byte file gốc trong `knowlegde`. File giới hạn mặc định 10 MB, văn bản 200.000 ký tự/300 chunks. Tài liệu lớn cần tách nhỏ. Import hiện chạy đồng bộ trong request, chưa có hàng đợi/background job bền vững; reverse proxy/client upload cần timeout đủ dài. Không retry upload liên tục khi timeout mà chưa kiểm tra kết quả bằng GET.

## Lưu file trên máy chủ

- File giữ nguyên tên và đuôi khi hợp lệ trên Windows, ví dụ `knowlegde/CuaHang_Dong_Hai.txt`. File trùng tên sẽ được lưu thành `CuaHang_Dong_Hai (2).txt`, `(3)`... để không ghi đè tài liệu khác. Tên quá dài được rút gọn. Mỗi bản upload được lưu riêng trước khi DB cập nhật thành công; cập nhật cùng `source_key` sẽ xóa bản file trước đó sau commit.
- Upload mới lưu file tại `knowlegde/<tên file gốc>.txt|md|pdf`. Tên gốc, MIME, dung lượng, nguồn file và mã lưu trữ nằm trong bảng `rag_service.documents`. Tên file được loại bỏ thành phần đường dẫn và ký tự không hợp lệ trên Windows; `source_key` không phải đường dẫn file. Thư mục này không được phục vụ công khai và đã loại khỏi Git.
- Thêm bằng JSON (`PUT /api/v1/documents`) lưu `text` thành file TXT UTF-8. Cập nhật cùng `source_key` thay file sau khi database commit thành công; xóa tài liệu cũng xóa file được tham chiếu.
- `GET /api/v1/documents/{id}/download` yêu cầu admin key và trả file dạng tải xuống. Nếu file bị xóa thủ công trên máy chủ, endpoint trả 404; tìm kiếm vẫn dùng văn bản/embedding trong DB.
- File được ghi hoàn chỉnh trước khi DB tham chiếu tới nó. Nếu DB/commit lỗi, file mới có thể còn lại để tránh mất file khi không xác định được kết quả commit. Nếu xóa file local lỗi sau khi DB đã xóa/cập nhật, API trả trường `warning`; giao diện hiển thị cảnh báo. Chỉ dọn file dư sau khi đối chiếu `file_storage_key` trong DB và đảm bảo không còn upload đang chạy.
- Backup/di chuyển service cần sao lưu cả database và thư mục `knowlegde`. Nhiều instance phải dùng chung thư mục lưu trữ; bản hiện tại dùng ổ đĩa của máy chạy service.

Với tài liệu đã nhập trước khi có tính năng này, chạy một lần sau khi cập nhật schema:

```bat
python -m app.backfill_files --confirm
```

Lệnh xuất `source_text` của các tài liệu chưa có file local thành TXT, không gọi Gemini, không tạo lại embedding, không thay file đã có. File này có `file_origin=recovered_text`, là văn bản đã trích xuất, **không phải bản khôi phục file PDF gốc**. Có thể chạy lại; tài liệu đang bị khóa được bỏ qua cho lần sau. Muốn giữ nguyên bản PDF/TXT cũ, cần upload lại file gốc với cùng `source_key` qua API.

Để chuyển các file UUID của phiên bản trước từ `storage/documents` sang `knowlegde` theo tên đã lưu, chạy `python -m app.migrate_file_names --confirm` sau khi cập nhật schema. Lệnh sao chép file, cập nhật tham chiếu DB rồi mới xóa bản cũ; không gọi embedding. Các file cũ chưa chuyển vẫn tải về được. File TXT đã xuất từ dữ liệu cũ giữ tên đã biết trong DB, không tự suy đoán tên file upload ban đầu.

Tìm kiếm:

```json
{
  "query": "Khách muốn biết cách đo chiều dài bàn chân để chọn size",
  "categories": ["size_guide"],
  "top_k": 5
}
```

Kết quả giữ các trường tương thích callback của bot:

```json
{
  "success": true,
  "status": "knowledge_found",
  "content": "[Nguồn: Hướng dẫn đo chân — dữ liệu thử > Cách đo]\n...",
  "sources": [
    {"source_key": "test/size-guide", "title": "Hướng dẫn đo chân — dữ liệu thử", "category": "size_guide", "heading": "Cách đo", "chunk_index": 0, "similarity": 0.81}
  ],
  "elapsed_ms": 120.0
}
```

Số similarity/thời gian ở ví dụ chỉ minh họa. Không tìm thấy: HTTP 200, `success=false`, `status=knowledge_not_found`, content rỗng và sources rỗng. Lỗi DB/Gemini: HTTP 503, **không giả thành không có tài liệu**. Hết slot xử lý: HTTP 429 và Retry-After. HTTP 401/403 là lỗi key/quyền; 413 là quá dung lượng, 422 là dữ liệu không hợp lệ.

## Nối bot sau khi kiểm tra service

Có adapter mẫu tại `examples/bot_client.py`:

```python
remote = RemoteKnowledgeSearch("http://127.0.0.1:8001", api_key=search_key)
# Truyền remote.search vào chỗ dependency knowledge_search hiện tại của bot.
result = remote.search("Cách chọn size theo chiều dài chân?", categories=["size_guide"])
# Gọi remote.close() khi ứng dụng dừng.
```

Chưa tự sửa `BOT_Conversation_V2`: cần nối callback trong khởi tạo ứng dụng, xử lý timeout/503 ở bot, và chuyển trang Knowledge của bot sang API mới hoặc dùng Swagger của service. Không duy trì hai nguồn tài liệu rồi kỳ vọng chúng tự đồng bộ. Import lại tài liệu qua API trước khi chuyển; giữ nguyên category mà planner đang dùng, ví dụ `size_guide`, `warranty`, `returns`, `shipping`, `store`, `promotion`. Nhập category tùy ý được nhưng nếu bot lọc một category khác, tài liệu sẽ không được trả về.

Nhiều bot có thể dùng chung một kho qua search key. **Bản này chưa có phân quyền theo doanh nghiệp/kho tài liệu hoặc key riêng cho từng bot.** Không dùng chung deployment này cho các khách hàng cần cách ly dữ liệu. Service không viết lại câu hỏi theo lịch sử: bot phải gửi câu hỏi đủ ngữ cảnh. Nội dung trả về là dữ liệu tham khảo, không được cho phép ghi đè system instruction của bot.

## Thiết kế và giới hạn bản đầu

- Gemini `gemini-embedding-001`, 768 chiều; vector đã chuẩn hóa. Document dùng RETRIEVAL_DOCUMENT, query dùng QUESTION_ANSWERING. Xem [tài liệu Gemini embedding](https://ai.google.dev/gemini-api/docs/embeddings).
- PostgreSQL cosine search chính xác, lọc tài liệu đang bật, category và cấu hình embedding tương thích. Chưa có hybrid search, reranker hoặc index ANN. Cần đo chất lượng/tốc độ trước khi mở rộng kho lớn. Adapter DB dùng [pgvector cho Psycopg](https://github.com/pgvector/pgvector-python).
- Đổi nội dung và thêm/tắt tài liệu áp dụng sau transaction thành công, không phải restart. Đổi `.env` cần restart process. Thay model/dimension cần thay adapter/schema tương ứng và lập chỉ mục lại, không chỉ đổi tên trong env.
- API v1 giữ request/response ổn định để bot không phụ thuộc code nội bộ. Thay đổi phá vỡ tương thích nên ra API v2.
- Log có timestamp, method, route, status và thời gian mỗi request; không ghi text câu hỏi/tài liệu, token, DSN. Search có thêm `elapsed_ms`; mọi response có `X-Response-Time-Ms`.
- Semaphore giới hạn đồng thời theo process, không phải rate limit phân tán. Production cần TLS, rate/body/time limits tại reverse proxy, backup PostgreSQL, giám sát, key rotation và kiểm thử tải. Giới hạn body ở API không thay thế giới hạn tài nguyên parser PDF; chỉ admin tin cậy được upload.

## Kiểm thử

```bat
python -m unittest discover -s tests -v
```

Test mặc định dùng mock Gemini/DB, không tốn phí API và không sửa database. Test SQL/transaction mô phỏng không thay thế integration test PostgreSQL thật. Muốn thử end-to-end: dùng database riêng, init, upload một tài liệu thử, tìm kiếm, tắt tài liệu và tìm lại; sau đó kiểm tra lỗi key và kết quả khi dịch vụ phụ thuộc không khả dụng.

Có bộ integration test tự tạo PostgreSQL cluster tạm, chỉ listen localhost, dùng cổng ngẫu nhiên, rồi dừng/xóa cluster khi xong; **không đọc DATABASE_URL hay dùng database hiện có**. Cần `initdb`, `pg_ctl` trên PATH và pgvector đã cài trong PostgreSQL. Gemini vẫn được giả lập nên không phát sinh phí. Chạy trên máy phát triển riêng, vì cluster tạm dùng trust authentication trên localhost:

```bat
set RAG_TEST_REAL_DB=1
python -m unittest discover -s tests -v
set RAG_TEST_REAL_DB=
```

## Cấu trúc

```text
RAG_Service/
├── app/
│   ├── main.py          # API, auth, giới hạn request, health, log
│   ├── config.py        # Đọc/kiểm tra .env riêng
│   ├── models.py        # Hợp đồng request/response
│   ├── chunking.py      # Chia đoạn theo heading và độ dài
│   ├── embeddings.py    # Gemini embedding
│   ├── repository.py    # Truy vấn PostgreSQL/pgvector
│   ├── service.py       # Import và truy xuất ngữ cảnh
│   └── init_db.py       # Tạo schema theo lệnh rõ ràng
├── database/001_init.sql
├── examples/bot_client.py
├── tests/test_service.py
├── tests/test_postgres.py
├── .env.example
└── requirements.txt
```
