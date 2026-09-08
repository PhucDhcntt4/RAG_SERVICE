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

1. Mở trang trực tiếp bằng `http://127.0.0.1:8000/admin` hoặc `http://localhost:8000/admin`: giao diện tự kết nối, không cần nhập key khi `RAG_LOCAL_ADMIN_ENABLED=true` (mặc định). Nếu truy cập từ máy khác hoặc đã tắt chế độ này, dùng `ADMIN_API_KEY` ở ô kết nối; key chỉ giữ trong bộ nhớ trang.
2. Bấm **Thêm tài liệu**, chọn/kéo thả file TXT, MD hoặc PDF có lớp văn bản, điền tên và chọn nhóm. Có thể nhập mã nhóm khác. File TXT/MD cần UTF-8; giới hạn upload lấy từ cấu hình server (mặc định 10 MB).
3. Bấm **Thêm tài liệu** và đợi xử lý embedding. Tài liệu thành công sẽ xuất hiện trên trang đầu của danh sách. Mỗi lần mở biểu mẫu tạo mã nguồn riêng, không ghi đè tài liệu cùng tên.
4. Bấm **tên tài liệu** để mở hộp xem toàn bộ văn bản đã lưu (PDF hiển thị phần văn bản trích xuất). Bấm **Tải về** để lấy file đã lưu. Bấm **Xóa** trên tài liệu, kiểm tra tên trong hộp xác nhận rồi bấm **Xóa tài liệu**. Tài liệu, embedding và file local bị xóa vĩnh viễn.
5. Dùng **Trước/Sau** để chuyển trang (20 tài liệu mỗi trang), **Làm mới** để cập nhật danh sách. Nút **Ngắt kết nối** chỉ hiện khi đăng nhập bằng key.

Nếu mất kết nối trong lúc thêm/xóa, kiểm tra lại danh sách trước khi gửi lại. Giao diện dùng API cùng origin, không cần Node.js hoặc bước build. Khi cập nhật code, khởi động lại Uvicorn để nhận route mới.

Truy cập tự động dùng `/admin-api/v1/...`, kiểm tra địa chỉ loopback của client,
Host localhost, Origin cùng trang và header `X-RAG-Local-UI`. Không cho truy cập
tự động qua reverse proxy/tunnel hoặc từ máy khác. Không nhúng API key vào HTML/JS.
Các API `/api/v1/...` dành cho bot/client vẫn yêu cầu Bearer key: `SEARCH_API_KEY`
để tìm kiếm; `ADMIN_API_KEY` để quản lý tài liệu hoặc gọi API chat quản trị.
Đổi `RAG_LOCAL_ADMIN_ENABLED=false` trong `.env` rồi chạy lại để yêu cầu key cả trên máy local.

Swagger vẫn có tại `http://127.0.0.1:8001/docs` để thử API tìm kiếm và các thao tác khác. Bấm **Authorize**, nhập admin key (không cần tiền tố Bearer trong ô này).

## Các API v1

### Thử hỏi đáp RAG trên giao diện

Mở `/admin` trên localhost (tự kết nối) và bấm **Thử RAG**. Gemini tổng hợp câu trả lời
từ kết quả tìm kiếm, kèm nhãn `[S1]`, `[S2]` đối chiếu với nguồn phía dưới.
Mở **Xem nội dung truy xuất** để xem câu hỏi tìm kiếm và các đoạn được đưa vào model.
Bạn có thể hỏi tiếp; lịch sử tối đa 6 lượt hoàn tất/24.000 ký tự được giữ trong bộ nhớ
trang và gửi cùng câu hỏi. **Xóa hội thoại**, ngắt kết nối hoặc tải lại trang sẽ xóa lịch sử.

Luồng chat: làm rõ câu hỏi tiếp nối bằng lịch sử → tìm kiếm vector hiện có → Gemini
trả các ý cùng số nguồn → server kiểm tra số nguồn và gắn nhãn. Lịch sử không được
dùng làm bằng chứng thay tài liệu. Khi không tìm được dữ liệu hoặc model nhận thấy
chứng cứ không đủ, API trả `insufficient_context`. Kiểm tra mã nguồn trích dẫn không
đảm bảo mọi diễn giải của model đều đúng; vẫn cần đối chiếu nội dung truy xuất.

Chat dùng `GEMINI_API_KEY` hiện có. Có thể cấu hình trong `.env`:

```env
RAG_CHAT_MODEL=gemini-2.5-flash
RAG_CHAT_TIMEOUT_SECONDS=60
RAG_CHAT_MAX_STATEMENTS=40
RAG_CHAT_MAX_ANSWER_CHARS=12000
```

Prompt được quản lý riêng trong `app/prompts`:

- `answer.txt`: vai trò, giọng văn, quy tắc dùng tài liệu và trích dẫn khi trả lời.
- `rewrite_question.txt`: hướng dẫn làm rõ câu hỏi tiếp nối trước khi tìm kiếm.
- `no_answer.txt`: câu trả lời cố định khi thiếu dữ liệu; đây là văn bản hiển thị, không gửi làm prompt LLM.

Mở file bằng trình soạn thảo, sửa nội dung và lưu UTF-8. Lượt hỏi tiếp theo đọc lại
file, không cần khởi động lại server khi chạy trực tiếp bằng Python. Bấm **Xóa hội thoại**
trước khi so sánh các phiên bản prompt để lịch sử cũ không ảnh hưởng kết quả.
File rỗng, bị xóa hoặc sai mã hóa sẽ báo lỗi rõ trên giao diện.

Có thể thay đổi cách xưng hô, độ chi tiết và quy tắc nghiệp vụ. Các trường
`sufficient`, `statements`, `text`, `citations` trong `answer.txt` phải khớp schema
ở `app/chat.py`; số nguồn phải thuộc kết quả truy xuất. Payload gồm câu hỏi,
lịch sử và tài liệu vẫn do code truyền riêng, không cần chèn biến vào file prompt.
Prompt nằm trong thư mục `app`, nên Dockerfile hiện có cũng đóng gói các file này.

Câu trả lời mặc định tối đa 40 ý, 12.000 ký tự sau khi gắn nhãn. Đổi hai giá trị
`RAG_CHAT_MAX_STATEMENTS` (1–100) và `RAG_CHAT_MAX_ANSWER_CHARS` (1.000–20.000) trong
`.env`, rồi khởi động lại server để áp dụng. Đây là giới hạn trên, không phải độ dài
bắt buộc; ngân sách sinh vẫn là 8.192 token nên tăng ký tự không bảo đảm model sinh
hết giới hạn đó. Lịch sử chấp nhận câu trả lời tối đa 20.000 ký tự mỗi tin, tổng
24.000 ký tự; giao diện bỏ các lượt cũ khi hết chỗ và giữ nguyên lượt vừa hoàn tất.

Trong `answer.txt`, `{{max_statements}}`, `{{max_answer_chars}}` và
`{{answer_text_budget}}` được thay bằng cấu hình khi gọi model. Biến cuối dành 90%
giới hạn ký tự cho văn bản, phần còn lại dành cho trích dẫn. Giữ nguyên tên biến
khi sửa prompt. Prompt yêu cầu danh sách đầy đủ trong phạm vi context, đánh số và
đối chiếu tổng số với các mục liệt kê. Giới hạn này không mở rộng dữ liệu tìm kiếm:
`RAG_TOP_K` và `RAG_MAX_CONTEXT_CHARS` vẫn quyết định tài liệu LLM được thấy.
Nếu context thiếu dữ liệu, model phải nói rõ phạm vi thay vì khẳng định danh sách đầy đủ.

Model nhận câu hỏi, lịch sử gần nhất và các đoạn tài liệu truy xuất qua Gemini API.
Lượt đầu gọi model một lần để trả lời; lượt tiếp nối thêm một lần làm rõ câu hỏi.
Các lần gọi này dùng quota của API key. Giới hạn timeout áp dụng cho từng lần gọi.
Định dạng đầu ra dùng [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

`POST /api/v1/chat` yêu cầu admin key, nhận
`{"query":"Còn điều kiện đổi hàng?","history":[{"role":"user","content":"Đổi hàng trong bao lâu?"}]}`.
Có thể truyền thêm `categories`, `top_k` như search. Response có `answer`, `status`,
`sources` (kèm `citation`), `retrieval_query`, `context`, `model`, `elapsed_ms`.
`/api/v1/knowledge/search` vẫn giữ hợp đồng cũ cho bot đang dùng riêng.

Kiểm tra tự động: `python -m unittest tests.test_chat tests.test_service`.

| Method | URL | Quyền / chức năng |
|---|---|---|
| GET | `/health/live` | Công khai, kiểm tra process sống |
| GET | `/api/v1/health/ready` | Search/admin key; kiểm tra kết nối và bảng DB, không gọi Gemini |
| POST | `/api/v1/knowledge/search` | Search/admin key; tìm ngữ cảnh |
| POST | `/api/v1/chat` | Admin; hỏi đáp Gemini với ngữ cảnh RAG và lịch sử |
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
- Log INFO có timestamp, request_id, method, route, status và thời gian mỗi request. DEBUG bổ sung câu hỏi và metadata truy xuất như hướng dẫn bên dưới. Không ghi nội dung tài liệu, lịch sử, câu trả lời, API key, vector hay DSN. Search có thêm `elapsed_ms`; mọi response có `X-Response-Time-Ms` và `X-Request-ID`.
- Semaphore giới hạn đồng thời theo process, không phải rate limit phân tán. Production cần TLS, rate/body/time limits tại reverse proxy, backup PostgreSQL, giám sát, key rotation và kiểm thử tải. Giới hạn body ở API không thay thế giới hạn tài nguyên parser PDF; chỉ admin tin cậy được upload.

## Kiểm thử

### Theo dõi truy vấn và metadata trong terminal

Đặt `LOG_LEVEL=DEBUG` trong `.env`, khởi động lại server. Cấu hình này điều khiển
log ứng dụng RAG, độc lập với `uvicorn --log-level debug`. Đổi về `LOG_LEVEL=INFO`
khi chỉ cần xem request HTTP và lỗi. DEBUG hiển thị nội dung câu hỏi và metadata nguồn.

- `RAG CHAT INPUT`: câu hỏi người dùng, nhóm được gửi lên, số tin nhắn lịch sử và model.
- `RAG CHAT QUERY`: câu hỏi gốc và câu hỏi đã làm rõ dùng để truy xuất.
- `RAG SEARCH`: query, categories, category_scope, top_k, min_similarity, embedding_model, context_limit.
- `RAG EMBEDDING` / `RAG RETRIEVAL`: thời gian embedding/tìm DB, số chunk và nhóm tìm được.
- `RAG SOURCE`: source_key, title, category, heading, chunk_index, similarity, selected,
  citation (với chat), số ký tự đưa vào context và cờ truncated. Nguồn bị bỏ do hết
  dung lượng có selected=false, reason=context_limit. chunk_index bắt đầu từ 0.
- `RAG CONTEXT`: số chunk thực sự gửi tiếp và kích thước context.
- `RAG LLM START/DONE/FAILED`: bước rewrite hoặc answer, model, thời gian và mã lỗi nếu có.
- `RAG CHAT RESULT`: trạng thái, độ dài trả lời, nhãn nguồn sử dụng và tổng thời gian.

Mọi dòng của một HTTP request dùng chung `request_id`, cũng được trả qua header
`X-Request-ID`. Dùng mã này để phân biệt các câu hỏi chạy đồng thời.
`categories=[] category_scope="all"` nghĩa là không lọc nhóm; `categories=["store"]`
nghĩa là client yêu cầu chỉ tìm nhóm store. `category` trong `RAG SOURCE` là nhóm
thực của tài liệu. RAG không tự suy luận nhóm từ câu hỏi. Giao diện Thử RAG hiện
gửi câu hỏi và lịch sử, nên mặc định tìm tất cả nhóm. Khi bot gọi riêng API search,
chỉ có log truy xuất; các bước lập kế hoạch Facebook và trả lời tại bot vẫn nằm
trong log của dự án bot.

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
