# Đông Hải RAG Service

Dự án RAG độc lập với `BOT_Conversation_V2`. Nhận tài liệu → chia đoạn → tạo embedding → lưu Qdrant → tìm nội dung liên quan qua HTTP API.

Service trả **ngữ cảnh và nguồn**, không tự tư vấn/chốt đơn. Bot vẫn giữ instruction, lịch sử hội thoại, điều phối và cách trả lời khách. Không cần Redis, không dùng MCP trong bản này.

## Cài đặt trên Windows

Hướng dẫn đầy đủ để cài source Python, cấu hình `.env`, tạo collection và kiểm tra kết nối Qdrant nằm tại [SETUP_QDRANT.md](SETUP_QDRANT.md).

Nếu muốn đóng gói service bằng Docker Desktop hoặc Docker Compose, xem [DOCKER.md](DOCKER.md).

Yêu cầu Python 3.11+, Qdrant đang chạy và Gemini API key.

Trong CMD:

```bat
cd /d "D:\ĐÔNG HẢI\DATA\RAG_Service"
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Chạy lệnh tạo key hai lần; điền hai kết quả khác nhau vào `SEARCH_API_KEY` và `ADMIN_API_KEY` trong `.env`. Điền `GEMINI_API_KEY`, `QDRANT_URL` và `QDRANT_COLLECTION`. Không dùng chuỗi mẫu, không commit `.env`, không nhúng admin key vào mã nguồn giao diện hay cấu hình bot.

Ví dụ cấu hình Qdrant local:

```dotenv
QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=rag_service_compare_v1
QDRANT_TIMEOUT_SECONDS=15
```

Sau khi kiểm tra cấu hình:

```bat
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

API chỉ sử dụng Qdrant; không cần đặt biến chọn provider.

Chunk mới giữ đường dẫn heading cha–con, ví dụ `TP.HCM > Cửa hàng Hai Bà Trưng`. Đường dẫn được lưu dưới dạng `text[]`, dùng trong văn bản embedding, BM25 và nhãn nguồn của context. Migration không tự bổ sung đường dẫn hoặc tạo lại embedding cho dữ liệu cũ; cần lập chỉ mục lại tài liệu tương ứng bằng `source_key` hiện có. Chunk cũ chưa có đường dẫn vẫn dùng heading gần nhất.

TXT UTF-8 có thể dùng đề mục tự nhiên, không bắt buộc thêm `#`:

```text
I. QUY ĐỊNH CHUNG
1.1. Thời gian làm việc
a. Nhân viên chính thức
Nội dung của mục này.

II. QUẢN LÝ TÀI SẢN
2.1. Sử dụng thiết bị
Nội dung của mục tiếp theo.
```

`app/text_structure.py` nhận diện đề mục La Mã, số nhiều cấp và chữ cái khi có
mục cha. Dòng ngắn viết HOA cần dòng trống phân cách và đề mục đồng cấp khác
hoặc dàn ý đánh số tiếp theo để được nhận diện. Dòng số đơn (`1.`, `2.`), địa
chỉ bắt đầu bằng số, câu có dấu kết thúc và cảnh báo phổ biến được giữ là nội dung.
Đây là quy tắc suy luận; đề mục không rõ cấu trúc có thể chưa được nhận diện.
Nếu tài liệu có heading Markdown `#`, toàn tài liệu dùng phân cấp Markdown;
không tự suy ra thêm heading từ các danh sách bên trong. Dùng `#` để chỉ định
cấp mục rõ ràng cho các trường hợp mơ hồ.

Luồng upload TXT và thêm văn bản bằng JSON mặc định dùng pipeline
**Heading-aware → Semantic Chunking → Recursive fallback**. Heading là ranh giới
cứng; các section dài được so khoảng cách cosine giữa embedding của các cụm câu
lân cận và tách ở percentile cấu hình. Phần còn quá dài được tách lần lượt theo
đoạn, dòng, câu, dấu câu, khoảng trắng, cuối cùng mới cắt cứng. Vì vậy chunk văn
bản không vượt `RAG_CHUNK_SIZE` và không trộn hai section; ngoại lệ là một hàng
bảng PDF quá dài được giữ nguyên để không tách điều kiện khỏi giá trị.

Cấu hình trong `.env`:

```dotenv
RAG_CHUNKING_STRATEGY=adaptive
RAG_CHUNK_SIZE=1200
RAG_SEMANTIC_MIN_CHARS=300
RAG_SEMANTIC_BREAKPOINT_PERCENTILE=80
```

`RAG_SEMANTIC_MIN_CHARS` là kích thước tối thiểu trước khi chấp nhận một điểm
ngắt semantic; nếu lớn hơn cấu hình chunk, service tự giới hạn xuống dưới
`RAG_CHUNK_SIZE`. Semantic embedding chỉ chạy với section dài, sau đó embedding
tìm kiếm được tạo cho các chunk cuối cùng. Response ingest trả
`chunking_method` và `chunking_stats` để kiểm tra cách tài liệu vừa được chia.
Đặt `RAG_CHUNKING_STRATEGY=fixed` để quay lại bộ chia ký tự cũ; khi đó
`RAG_CHUNK_OVERLAP` có hiệu lực. Đổi chiến lược không tự thay dữ liệu đã lưu:
cần upload lại với cùng `source_key` để tạo lại chunk và embedding.

PDF upload dùng bộ đọc layout riêng để tạo heading và bảng trước khi đi qua
pipeline adaptive; không dùng quy tắc nhận diện heading TXT.

PDF upload đi qua `pdf_extract.py` và `pdf_tables.py` (PyMuPDF) → `pdf_structure.py` (font, vị trí,
số mục) → `pdf_chunking.py` (gom nội dung theo heading) → chia chunk → embedding
→ repository. Cấp heading được dùng trực tiếp, không chuyển PDF thành Markdown.
Nội dung ngay dưới heading cha, heading cùng dòng với nội dung và mục kéo dài
qua trang đều được giữ. `section_path` được lưu vào DB và dùng làm ngữ cảnh
embedding; chưa lưu số trang vào chunk hay bổ sung trích dẫn theo trang.

PDF không có heading rõ ràng vẫn được chia từ văn bản đọc được. Bảng có đường kẻ
được đọc bằng `find_tables(strategy="lines_strict")`: giữ từng hàng, ô gộp theo
tọa độ và ô trống; không tự điền giá trị từ hàng trước. Xem [tài liệu PyMuPDF](https://pymupdf.readthedocs.io/en/latest/page.html#Page.find_tables).
Heading phạm vi in đậm dạng `❖ ...:` được giữ để phân biệt nhóm đối tượng.
Tiêu đề nhóm cột được lặp ở các chunk; có thể kế thừa qua trang nếu biên bảng
khớp và không có nội dung/mục mới xen giữa. Đây là suy luận từ bố cục, cần đối chiếu
với file gốc. Chunk bảng không cắt giữa hàng; một hàng quá dài có thể vượt
`RAG_CHUNK_SIZE`, nhưng vẫn chịu giới hạn toàn tài liệu và embedding provider.
Số trang PDF nằm trong nội dung bảng, chưa có trường trích dẫn trang riêng.
Đã kiểm tra với bảng ABC trong sổ tay; chưa có OCR, chưa bảo đảm bảng không kẻ
khung hoặc mọi bố cục nhiều cột. Nếu có trang không đọc được chữ, upload trả lỗi kèm số trang để kiểm tra
trang trắng hoặc OCR; không âm thầm bỏ qua trang đó. Khi `LOG_LEVEL=DEBUG`, log
`RAG PDF PREPARED` hiển thị số trang, số dòng, số bảng/hàng bảng, section, chunk và thời
gian chuẩn bị. Cần upload lại PDF cũ với cùng `source_key` để áp dụng cấu trúc mới.
Trên giao diện, dùng **Cập nhật file** ở đúng tài liệu, chọn lại PDF gốc rồi bấm
**Cập nhật tài liệu**. Thao tác giữ `source_key` để thay chunk cũ; **Thêm tài liệu**
tạo mã mới và có thể làm trùng dữ liệu. Embedding phải hoàn tất trước khi thay dữ liệu cũ.

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

Luồng chat: làm rõ câu hỏi tiếp nối bằng lịch sử → BM25 và vector search → RRF hợp nhất
theo thứ hạng → mở rộng mục liên quan → Gemini
trả các ý cùng số nguồn → server kiểm tra số nguồn và gắn nhãn. Lịch sử không được
dùng làm bằng chứng thay tài liệu. Khi không tìm được dữ liệu hoặc model nhận thấy
chứng cứ không đủ, API trả `insufficient_context`. Kiểm tra mã nguồn trích dẫn không
đảm bảo mọi diễn giải của model đều đúng; vẫn cần đối chiếu nội dung truy xuất.

Với câu hỏi như “bảng tính lỗi ABC” hoặc “bảng định mức”, chat mở rộng từ kết quả
vector sang mục đánh số chứa chunk đó, gồm các nhóm đối tượng và chú thích.
Các câu hỏi chứa “chính sách”, “quy định”, “quy trình”, “phúc lợi”, “là gì”,
“bao gồm” hoặc “cách tính” cũng mở rộng theo mục. Với câu hỏi tổng quan trùng tên
một phần lớn của tài liệu, hệ thống lấy một chunk đại diện cho mỗi mục con để giữ
đủ chủ đề trong giới hạn context;
nếu kết quả nằm ở mục con như 2.1 hoặc 2.3, lấy mục cha 2 để giữ các điều kiện,
thủ tục và giấy tờ ở mục cùng cấp. Khi tên mục khớp ít nhất hai từ chủ đề trong
câu hỏi, ưu tiên các mục có bằng chứng từ khóa đó thay cho các mục chỉ gần nghĩa.
Nếu không có tên mục khớp rõ, giữ thứ tự kết quả vector. Đây là quy tắc suy luận,
không bảo đảm mọi cách diễn đạt đều chọn đúng mục; cần xem log và context khi thử.
Không mở từ đầu toàn bộ sổ tay. Số phạm vi mở rộng bị chặn bởi
`RAG_OVERVIEW_MAX_DOCUMENTS`, số chunk bởi `RAG_OVERVIEW_MAX_CHUNKS`, và context
bởi `RAG_MAX_CONTEXT_CHARS`. Log `RAG SECTION EXPANSION` cho biết các mục đã lấy.
Nếu bị giới hạn, `retrieval_coverage.truncated=true`; không đưa nửa hàng bảng vào LLM.
`selected_sections_complete` chỉ nói về các mục đã chọn, không phải toàn kho.

Chat dùng `GEMINI_API_KEY` hiện có. Có thể cấu hình trong `.env`:

```env
RAG_HYBRID_ENABLED=true
RAG_HYBRID_CANDIDATES=20
RAG_RRF_K=60
RAG_CHAT_MODEL=gemini-2.5-flash
RAG_CHAT_TIMEOUT_SECONDS=60
RAG_CHAT_MAX_STATEMENTS=40
RAG_CHAT_MAX_ANSWER_CHARS=12000
```

`RAG_HYBRID_CANDIDATES` là số ứng viên lấy riêng từ mỗi nhánh trước khi hợp nhất;
`RAG_TOP_K` là số kết quả cuối sau RRF. `RAG_RRF_K` làm mềm chênh lệch thứ hạng,
không trộn trực tiếp BM25 score với cosine similarity. Đặt `RAG_HYBRID_ENABLED=false`
để quay về vector search khi cần so sánh. Thay đổi các biến này cần khởi động lại server.

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

Các API yêu cầu header `Authorization: Bearer <API_KEY>`. Swagger/schema API công khai nhưng **dữ liệu và thao tác tài liệu đều kiểm tra key ở server**. Dùng HTTPS qua reverse proxy nếu truy cập từ máy khác. Không công khai cổng Qdrant.

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
- Qdrant cosine search lọc tài liệu đang bật, category và cấu hình embedding tương thích. Ứng dụng kết hợp BM25 với dense vector bằng RRF rồi có thể mở rộng section. Với collection nhỏ Qdrant dùng full scan; HNSW được optimizer xây khi dữ liệu vượt ngưỡng cấu hình.
- Đổi nội dung và thêm/tắt tài liệu áp dụng sau transaction thành công, không phải restart. Đổi `.env` cần restart process. Thay model/dimension cần thay adapter/schema tương ứng và lập chỉ mục lại, không chỉ đổi tên trong env.
- API v1 giữ request/response ổn định để bot không phụ thuộc code nội bộ. Thay đổi phá vỡ tương thích nên ra API v2.
- Log INFO có timestamp, request_id, method, route, status và thời gian mỗi request. DEBUG bổ sung câu hỏi và metadata truy xuất như hướng dẫn bên dưới. Không ghi nội dung tài liệu, lịch sử, câu trả lời, API key, vector hay DSN. Search có thêm `elapsed_ms`; mọi response có `X-Response-Time-Ms` và `X-Request-ID`.
- Semaphore giới hạn đồng thời theo process, không phải rate limit phân tán. Production cần TLS, rate/body/time limits tại reverse proxy, snapshot Qdrant, giám sát, key rotation và kiểm thử tải. Giới hạn body ở API không thay thế giới hạn tài nguyên parser PDF; chỉ admin tin cậy được upload.

## Kiểm thử

### Theo dõi truy vấn và metadata trong terminal

Đặt `LOG_LEVEL=DEBUG` trong `.env`, khởi động lại server. Cấu hình này điều khiển
log ứng dụng RAG, độc lập với `uvicorn --log-level debug`. Đổi về `LOG_LEVEL=INFO`
khi chỉ cần xem request HTTP và lỗi. DEBUG hiển thị nội dung câu hỏi và metadata nguồn.

- `RAG CHAT INPUT`: câu hỏi người dùng, nhóm được gửi lên, số tin nhắn lịch sử và model.
- `RAG CHAT QUERY`: câu hỏi gốc và câu hỏi đã làm rõ dùng để truy xuất.
- `RAG SEARCH`: query, categories, category_scope, top_k, min_similarity, embedding_model, context_limit.
- `RAG EMBEDDING` / `RAG RETRIEVAL`: thời gian embedding/tìm DB, số chunk và nhóm tìm được.
- `RAG EMBEDDING PROGRESS` (INFO): batch đã xong, số vector hoàn thành/tổng số.
- `RAG EMBEDDING RETRY` (WARNING): batch đang chờ thử lại và số giây chờ.
- `RAG EMBEDDING FAILED` (mức ERROR, không cần bật DEBUG): `reason`,
  `provider_code`, loại exception, model, batch bị lỗi, số vector đã xử lý và
  thời gian. Không ghi API key, văn bản tài liệu hoặc response body của Gemini.
  `rate_or_quota` cần kiểm tra giới hạn Gemini; `connection`/`timeout` cần kiểm
  tra kết nối; `invalid_key`/`blocked_key` cần kiểm tra hoặc thay key. HTTP vẫn
  trả 503 khi embedding thất bại, kèm thông báo đã lọc theo nhóm lỗi để giao diện
  hiển thị. Tham khảo [hướng dẫn lỗi Gemini](https://ai.google.dev/gemini-api/docs/troubleshooting).

Với import nhiều chunk, có thể đặt trong `.env`:

```dotenv
RAG_EMBEDDING_BATCH_SIZE=8
RAG_EMBEDDING_BATCH_INTERVAL_SECONDS=5
RAG_EMBEDDING_MAX_RETRIES=2
```

Batch size hợp lệ 1–32 (mặc định 32), khoảng nghỉ giữa batch 0–60 giây (mặc
định 0), số lần thử lại 0–3 (mặc định 0). Thiết lập trên giảm lượng dữ liệu mỗi
lần gửi và giãn các batch; không bảo đảm nằm trong mọi quota. Mức giới hạn thực
tế cần xem trong project Gemini ở AI Studio. Đây là các nhóm gửi embedding
đồng bộ, không phải dịch vụ Batch API bất đồng bộ của Google.

Khi bật retry, chỉ thử lại 429/503 của batch lỗi, giữ vector các batch trước trong
bộ nhớ. Chờ tối thiểu 5, 10, 20 giây theo số lần retry hoặc lâu hơn theo RetryInfo/
Retry-After; nếu nhà cung cấp yêu cầu trên 60 giây thì trả lỗi. Không retry khi
thông tin quota cho biết giới hạn ngày/tháng hoặc quota bằng 0. Log lỗi có thêm
`quota_windows`, `zero_quota`, `retry_after_seconds` nếu nhà cung cấp cung cấp.
Import vẫn đồng bộ và có thể mất vài phút; không bấm upload lặp lại khi đang chạy.
Chỉ lưu file/DB sau khi toàn bộ embedding thành công. Nếu request thất bại hoặc
server dừng, chưa có checkpoint để tiếp tục qua request mới; upload lại sẽ xử lý
lại các chunk. Khoảng nghỉ áp dụng từng request/process, không điều phối quota
chung với các bot hoặc dịch vụ khác cùng dùng project.
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
thực của tài liệu. API search không tự suy luận nhóm từ câu hỏi. Giao diện Thử RAG
gửi câu hỏi và lịch sử; tìm vector mặc định tìm tất cả nhóm. Khi bot gọi riêng API search,
chỉ có log truy xuất; các bước lập kế hoạch Facebook và trả lời tại bot vẫn nằm
trong log của dự án bot.

### Trả lời bằng RAG và LLM

Mọi câu hỏi trong giao diện chat, kể cả liệt kê cửa hàng, đều đi qua truy xuất
BM25 + vector → RRF → ghép context có nguồn → LLM tổng hợp câu trả lời. Nếu không có nguồn,
trả thông báo thiếu dữ liệu. Prompt tại `app/prompts/answer.txt` điều khiển cách
trình bày, mức đầy đủ và trích dẫn. Số chunk lấy về vẫn phụ thuộc `RAG_TOP_K`,
ngưỡng tương đồng và giới hạn context; LLM không thể bổ sung mục thiếu trong
nguồn truy xuất. Không cần upload hoặc tạo lại embedding khi thay đổi luồng trả lời.

Trong chat, câu hỏi tổng hợp (“liệt kê”, “danh sách”, “tổng số”, “hiện có … ở
đâu”...) dùng vector để tìm các tài liệu liên quan trước, rồi đọc thêm chunk của
các tài liệu đó qua `Repository.expand_documents`. Áp dụng chung cho mọi nhóm
tài liệu, không phân tích riêng địa chỉ hay tự tạo câu trả lời bằng code. Thứ tự
chunk được xen kẽ giữa các tài liệu để tài liệu dài không chiếm toàn bộ ngân sách.
Giữ bộ lọc category, trạng thái hoạt động, provider/model/dimension; điểm tương
đồng của chunk mở rộng vẫn được tính, nhưng không áp ngưỡng của bước tìm ban đầu.

Giới hạn mặc định: `RAG_OVERVIEW_MAX_DOCUMENTS=3` (1–5) và
`RAG_OVERVIEW_MAX_CHUNKS=100` (1–300), có thể thêm vào `.env`. Giới hạn context
vẫn áp dụng. `RAG DOCUMENT EXPANSION` ghi tài liệu được chọn, số chunk và giới
hạn; `RAG CONTEXT` ghi scope/truncated. LLM nhận `retrieval_coverage` để biết khi
nào chỉ có một phần dữ liệu. Đọc đủ tài liệu được chọn không có nghĩa đã bao phủ
toàn bộ kho. API search dành cho bot vẫn giữ top-k; phần mở rộng tự động hiện chỉ
áp dụng cho chat. Câu trả lời luôn do LLM tổng hợp nếu có nguồn.

```bat
python -m unittest discover -s tests -v
```

Test mặc định dùng mock Gemini/repository, không tốn phí API và không sửa collection Qdrant. Muốn thử end-to-end: dùng collection riêng, upload một tài liệu thử, tìm kiếm, tắt tài liệu và tìm lại; sau đó kiểm tra lỗi key và kết quả khi dịch vụ phụ thuộc không khả dụng.

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
│   ├── qdrant_repository.py # Lưu và tìm kiếm trên Qdrant
│   └── service.py       # Import và truy xuất ngữ cảnh
├── examples/bot_client.py
├── tests/test_service.py
├── .env.example
└── requirements.txt
```
