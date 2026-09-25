# Hướng dẫn vận hành Product RAG từ Delta Sync

Tài liệu này mô tả quy trình chính thức để đồng bộ Shopify vào Product RAG sau khi hệ thống đã chuyển sang Qdrant, PostgreSQL checkpoint và worker chạy nền.

## 1. Các chế độ đồng bộ

| Chức năng trên giao diện | Cách xử lý | Khi sử dụng |
|---|---|---|
| Chạy Delta Sync ngay | Chỉ xử lý sản phẩm Shopify thay đổi từ checkpoint | Vận hành hằng ngày hoặc kiểm tra ngay thay đổi mới |
| Lịch `Delta thay đổi` | Worker tự chạy Delta mỗi ngày | Chế độ vận hành chính |
| Đồng bộ tất cả sản phẩm ACTIVE | Full Sync/Full Reconcile | Khởi tạo lần đầu, phục hồi hoặc đối soát toàn bộ |
| Sync tồn kho ngay | Quét tồn kho ACTIVE, cập nhật catalog Qdrant | Kiểm tra tồn kho thủ công |
| Lịch tồn kho | Chạy Inventory Sync theo chu kỳ giờ | Cập nhật tồn kho định kỳ |

Không dùng Full Sync cho mọi lần chạy hằng ngày vì Full Sync phải đọc và đối soát toàn bộ sản phẩm, hình ảnh và vector.

## 2. Cấu hình bắt buộc

Các giá trị liên quan trong `.env`:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE

QDRANT_URL=http://127.0.0.1:6333
QDRANT_API_KEY=

PRODUCT_SYNC_WRITE_ENABLED=true
PRODUCT_SYNC_POLL_SECONDS=5

INVENTORY_SYNC_ENABLED=false
INVENTORY_SYNC_INTERVAL_HOURS=6
```

- `DATABASE_URL`: PostgreSQL lưu cấu hình, checkpoint, job và lịch sử thay đổi.
- `QDRANT_URL`: địa chỉ Qdrant Product RAG.
- `QDRANT_API_KEY`: chỉ khai báo khi Qdrant bật xác thực.
- `PRODUCT_SYNC_WRITE_ENABLED=true`: cho phép worker ghi vào Qdrant.
- `PRODUCT_SYNC_POLL_SECONDS`: số giây worker chờ giữa các lần kiểm tra job.
- Hai biến Inventory là giá trị khởi tạo. Sau đó có thể điều chỉnh trên giao diện.

Không đưa mật khẩu, API key thật vào Git.

## 3. Quy trình cho dự án hiện tại đã Full Sync

Qdrant của dự án hiện tại đã có dữ liệu từ Full Sync. Không chạy Full Sync lại chỉ để bật Delta. Việc cần làm là bảo đảm PostgreSQL có bảng metadata và checkpoint Delta.

### Bước 1: Cài thư viện

```cmd
cd /d "C:\D\ĐÔNG HẢI\DATA\RAG_Service"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Bước 2: Tạo bảng PostgreSQL

Database trong `DATABASE_URL` phải tồn tại trước. Sau đó chạy:

```cmd
.\.venv\Scripts\python.exe -m app.init_db --confirm
```

Lệnh này không xóa vector Qdrant. Lệnh tạo hoặc bổ sung các bảng metadata cần thiết.

### Bước 3: Kiểm tra checkpoint Delta

Chạy trong PostgreSQL:

```sql
SELECT *
FROM rag_metadata.product_delta_state
WHERE sync_name = 'shopify_product_delta';
```

- Nếu đã có một dòng: Delta đã bootstrap. Không chạy bootstrap lại.
- Nếu chưa có dòng: Qdrant đã Full Sync nên có thể bootstrap ngay một lần.

```cmd
.\.venv\Scripts\python.exe -m app.product_sync.delta_sync --bootstrap
```

Bootstrap thực hiện hai việc:

- Ghi checkpoint `last_success_at` vào PostgreSQL.
- Tạo bản đồ `Shopify product ID → product_code` để phát hiện cả trường hợp đổi SKU.

Không chạy bootstrap hằng ngày. Bootstrap lại sẽ đưa checkpoint tới thời điểm hiện tại và có thể bỏ qua thay đổi chưa được đồng bộ trước đó.

### Bước 4: Khởi động API

```cmd
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Bước 5: Khởi động worker ở terminal riêng

```cmd
.\.venv\Scripts\python.exe -m app.product_sync.worker
```

Chỉ chạy một Product Sync Worker. PostgreSQL advisory lock sẽ từ chối worker thứ hai.

### Bước 6: Bật lịch Delta

Trên giao diện:

1. Bật **Đồng bộ sản phẩm hằng ngày**.
2. Chọn phạm vi **Delta thay đổi**.
3. Chọn giờ chạy và lưu lịch.

Sau bước này worker tự tạo tối đa một scheduled job cho mỗi ngày.

### Nếu Qdrant hoàn toàn mới hoặc trống

Chỉ trong trường hợp Qdrant chưa có baseline:

1. Tạm thời chưa bật lịch Delta.
2. Khởi động API và worker.
3. Nhấn **Đồng bộ tất cả sản phẩm ACTIVE**.
4. Chờ Full Sync hoàn tất và kiểm tra lỗi.
5. Chạy `delta_sync --bootstrap` đúng một lần.
6. Bật lịch Delta.

Đây không phải trường hợp của dự án hiện tại.

## 4. Delta Sync hoạt động như thế nào

Khi bấm **Chạy Delta Sync ngay** hoặc lịch tự động tới giờ:

1. API tạo một Product Sync job có `mode=existing`.
2. Worker nhận job.
3. Worker đọc `last_success_at` trong PostgreSQL.
4. Khoảng truy vấn được lùi 120 giây để tránh mất dữ liệu ở biên thời gian.
5. Shopify trả về những product đã thay đổi trong khoảng đó.
6. Hệ thống xác định các SKU bị ảnh hưởng, bao gồm cả SKU cũ và SKU mới.
7. Mỗi SKU được so sánh bằng fingerprint:
   - Không thay đổi: `SKIP_UNCHANGED`.
   - Chỉ metadata thay đổi: `CATALOG_ONLY`.
   - Sản phẩm hoặc hình ảnh thay đổi: `FULL_RECONCILE`.
   - Sản phẩm mới: `CREATE_FULL`.
   - Không còn ACTIVE: cập nhật đúng trạng thái Shopify và dọn vector ảnh được quản lý.
8. Qdrant được cập nhật.
9. Chi tiết thay đổi được ghi vào PostgreSQL.
10. Chỉ khi tất cả bước thành công, checkpoint mới được cập nhật.

Nếu một SKU lỗi, Delta Sync không cập nhật checkpoint. Lần chạy sau sẽ đọc lại khoảng thời gian cũ. Audit dùng khóa duy nhất theo `job_id + product_code` để tránh trùng trong cùng job.

Chạy Delta liên tiếp là an toàn. Do có overlap 120 giây, Shopify có thể trả lại sản phẩm cũ nhưng fingerprint sẽ loại bỏ dữ liệu không đổi.

### Điều chỉnh checkpoint trên giao diện

Trong cụm **Đồng bộ sản phẩm hằng ngày**, thẻ **Delta checkpoint** hiển thị:

- Thời điểm checkpoint hiện tại.
- Thời điểm truy vấn thực tế sau khi trừ overlap 120 giây.
- Số Shopify ID đang được ánh xạ.
- Lý do điều chỉnh gần nhất, nếu có.

Để đọc lại dữ liệu từ một thời điểm cũ:

1. Bảo đảm không có Product Sync hoặc Inventory Sync đang chạy/chờ chạy.
2. Nhấn **Điều chỉnh checkpoint**.
3. Nhập thời điểm theo múi giờ `Asia/Ho_Chi_Minh`.
4. Nhập lý do điều chỉnh.
5. Đánh dấu xác nhận rủi ro.
6. Nhấn **Lưu checkpoint**.
7. Kiểm tra toast thành công rồi nhấn **Chạy Delta Sync ngay**.

Giao diện không cho đặt checkpoint trong tương lai và không tự chạy Delta sau khi lưu. Mỗi lần điều chỉnh thủ công được lưu vào:

```text
rag_metadata.product_delta_checkpoint_history
```

Việc điều chỉnh chỉ thay đổi `last_success_at`, không xóa hoặc tạo lại bảng `product_delta_product_map`.

### Checkpoint của lần Delta tiếp theo

Checkpoint không được lấy trực tiếp từ giờ cấu hình trên giao diện. Nó được lấy theo thời điểm thực tế worker bắt đầu một Delta job và chỉ được cập nhật khi job hoàn tất thành công.

Công thức:

```text
query_start = checkpoint hiện tại - 120 giây
query_end   = thời điểm Delta job thực sự bắt đầu

Nếu thành công:
checkpoint mới = query_end

Nếu thất bại:
checkpoint giữ nguyên
```

Ví dụ checkpoint hiện tại là:

```text
24/09/2026 16:46:11
```

Lịch được cấu hình chạy lúc `09:00` ngày hôm sau và worker thực tế bắt đầu lúc `09:00:07`. Khoảng truy vấn sẽ là:

```text
24/09/2026 16:44:11
→ 25/09/2026 09:00:07
```

Nếu job hoàn tất thành công, checkpoint mới là:

```text
25/09/2026 09:00:07
```

Lần chạy sau nữa sẽ bắt đầu đọc từ:

```text
25/09/2026 08:58:07
```

Nếu job thất bại, checkpoint vẫn giữ ở `24/09/2026 16:46:11`; lần sau hệ thống sẽ đọc lại khoảng dữ liệu chưa hoàn tất. Người vận hành không cần chỉnh checkpoint hằng ngày. Nút **Điều chỉnh checkpoint** chỉ dùng khi cần đọc lại dữ liệu cũ hoặc phục hồi sự cố.

## 5. Khi nào cần Full Sync

Dùng **Đồng bộ tất cả sản phẩm ACTIVE** khi:

- Khởi tạo Qdrant lần đầu.
- Collection Qdrant vừa được tạo lại.
- Nghi ngờ Qdrant thiếu sản phẩm cũ.
- Thay đổi cấu trúc payload hoặc thuật toán tạo vector.
- Muốn đối soát lại toàn bộ dữ liệu.

Delta chỉ xử lý sản phẩm có thay đổi sau checkpoint. Một sản phẩm cũ bị thiếu trong Qdrant nhưng không thay đổi trên Shopify sẽ không tự xuất hiện qua Delta.

## 6. Inventory Sync

Inventory Sync là luồng riêng với Product Delta Sync:

- Chỉ đọc sản phẩm Shopify ACTIVE.
- So sánh tồn kho và `available` theo từng variant.
- Chỉ cập nhật catalog payload trong Qdrant.
- Không tải OpenCLIP.
- Không tạo lại hoặc xóa vector ảnh.
- Ghi lịch sử variant thực sự thay đổi.

Trên giao diện có thể:

- Bật/tắt lịch tồn kho.
- Chọn chu kỳ từ 1 đến 24 giờ.
- Nhấn **Sync tồn kho ngay**.

Inventory job chỉ chạy khi không có Product Sync job đang được worker xử lý.

## 7. Lịch sử và audit

### Lịch sử Product Sync

Bảng cha:

```text
rag_metadata.product_sync_jobs
```

Chi tiết sản phẩm thay đổi:

```text
rag_metadata.product_sync_changes
```

Audit theo dõi các trường nghiệp vụ như tên, trạng thái, loại, mô tả, chất liệu, giá, variants và hình ảnh. Timestamp kỹ thuật và tồn kho không được tính vào Product Sync audit.

### Lịch sử Inventory Sync

Bảng trạng thái hiện tại:

```text
rag_metadata.product_inventory_sync_state
```

Bảng lịch sử các lần chạy:

```text
rag_metadata.product_inventory_sync_runs
```

Chi tiết variant thay đổi:

```text
rag_metadata.product_inventory_sync_changes
```

Hai bảng lịch sử trên giao diện hiển thị 5 job mỗi trang. Nút **Xem N** mở chi tiết thay đổi và phân trang 10 dòng mỗi trang.

Các job cũ được tạo trước khi có audit sẽ hiển thị **Không có** vì không thể khôi phục dữ liệu trước/sau đã không được lưu.

## 8. API lịch sử

```http
GET /api/v1/products/sync/history?page=1&page_size=5
GET /api/v1/products/sync/history/{job_id}/changes?page=1&page_size=10

GET /api/v1/products/inventory-sync/history?page=1&page_size=5
GET /api/v1/products/inventory-sync/history/{run_id}/changes?page=1&page_size=10
```

Các API này yêu cầu quyền admin giống trang Kho sản phẩm.

## 9. Câu lệnh kiểm tra PostgreSQL

Checkpoint Delta:

```sql
SELECT *
FROM rag_metadata.product_delta_state;
```

Bản đồ Shopify ID và SKU:

```sql
SELECT COUNT(*)
FROM rag_metadata.product_delta_product_map;
```

Các Product Sync job gần nhất:

```sql
SELECT id, trigger_type, mode, status,
       processed_products, failed_products,
       started_at, finished_at
FROM rag_metadata.product_sync_jobs
ORDER BY id DESC
LIMIT 10;
```

Chi tiết thay đổi của một Product Sync job:

```sql
SELECT product_code, product_title, change_type, changes
FROM rag_metadata.product_sync_changes
WHERE job_id = 123
ORDER BY id;
```

Các Inventory run gần nhất:

```sql
SELECT id, trigger_type, status,
       checked_products, changed_variants,
       started_at, finished_at
FROM rag_metadata.product_inventory_sync_runs
ORDER BY id DESC
LIMIT 10;
```

Chi tiết tồn kho của một run:

```sql
SELECT product_code, variant_title, sku,
       before_quantity, after_quantity,
       before_available, after_available
FROM rag_metadata.product_inventory_sync_changes
WHERE run_id = 123
ORDER BY id;
```

## 10. Khởi động lại sau khi cập nhật code

Phải dừng và chạy lại cả API lẫn worker vì Python process cũ không tự nạp code mới.

Terminal API:

```cmd
Ctrl + C
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Terminal worker:

```cmd
Ctrl + C
.\.venv\Scripts\python.exe -m app.product_sync.worker
```

Sau đó tải lại trình duyệt bằng `Ctrl + F5`.

## 11. Xử lý lỗi thường gặp

### Delta Sync chưa bootstrap

Thông báo:

```text
Delta Sync chưa bootstrap trong PostgreSQL
```

Đầu tiên kiểm tra xem Qdrant đã có baseline đầy đủ hay chưa:

- Dự án hiện tại đã Full Sync: chạy bootstrap một lần, không Full Sync lại.
- Qdrant mới hoặc trống: Full Sync trước, sau đó mới bootstrap.

Lệnh bootstrap:

```cmd
.\.venv\Scripts\python.exe -m app.product_sync.delta_sync --bootstrap
```

### Worker Offline

Kiểm tra terminal worker và chạy:

```cmd
.\.venv\Scripts\python.exe -m app.product_sync.worker
```

### Có worker khác giữ singleton lock

Một worker cũ vẫn đang chạy. Dừng worker cũ trước, không chạy hai worker song song.

### Job chạy nhưng không có thay đổi

Đây có thể là kết quả đúng khi:

- Không có sản phẩm Shopify thay đổi sau checkpoint.
- Sản phẩm nằm trong overlap nhưng fingerprint xác nhận nội dung không đổi.
- Full Sync xử lý nhiều sản phẩm nhưng audit chỉ lưu sản phẩm thực sự thay đổi.

### Audit không xuất hiện

Kiểm tra:

1. API và worker đã được khởi động lại bằng code mới.
2. Job được chạy sau khi tạo bảng audit.
3. Sản phẩm thực sự có thay đổi nghiệp vụ.
4. PostgreSQL có bản ghi trong bảng `product_sync_changes` hoặc `product_inventory_sync_changes`.

## 12. Quy trình vận hành chính thức

Thiết lập cho dự án hiện tại đã Full Sync:

```text
Tạo bảng PostgreSQL
→ Kiểm tra product_delta_state
→ Chỉ bootstrap nếu chưa có checkpoint
→ Bật lịch Delta
→ Bật lịch Inventory nếu cần
```

Thiết lập cho Qdrant mới hoặc trống:

```text
Tạo bảng PostgreSQL
→ Full Sync tất cả ACTIVE
→ Bootstrap Delta một lần
→ Bật lịch Delta
→ Bật lịch Inventory nếu cần
```

Vận hành hằng ngày:

```text
Worker chạy liên tục
→ Delta Sync theo lịch
→ Inventory Sync theo chu kỳ
→ Kiểm tra lịch sử và nút Xem thay đổi
```

Khôi phục khi dữ liệu có vấn đề:

```text
Chạy Full Sync tất cả ACTIVE
→ Kiểm tra hoàn tất
→ Chỉ bootstrap lại nếu chủ động muốn thiết lập một baseline Delta mới
```
