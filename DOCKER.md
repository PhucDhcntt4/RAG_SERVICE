# Hướng dẫn triển khai RAG Service bằng Docker trên Windows

Gói này chạy API, giao diện, Product Sync Worker và PostgreSQL trong Docker Compose; Qdrant chạy trên máy Windows và được container truy cập qua `host.docker.internal:6333`. PostgreSQL quản lý loại tài liệu/nhóm và trạng thái job, còn Qdrant lưu chunk/vector. Container đọc `.env` ở thư mục dự án. Xem thêm [POSTGRES_TAXONOMY.md](POSTGRES_TAXONOMY.md). [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/networking-how-tos/)

## 1. Chuẩn bị

- Mở Docker Desktop và dùng Linux containers.
- Giữ Qdrant trên Windows hoạt động tại cổng `6333`.
- Giữ `.env` đầy đủ `QDRANT_URL`, `QDRANT_COLLECTION`, `SEARCH_API_KEY`, `ADMIN_API_KEY`, `GEMINI_API_KEY`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
- Thư mục `knowlegde` chứa file gốc của tài liệu đã upload.
- Nếu Uvicorn đang chạy trực tiếp ở cổng 8000, dừng bằng Ctrl+C trong cửa sổ chạy service trước khi bật container ở cùng cổng.

Mở PowerShell:

```powershell
cd "D:\ĐÔNG HẢI\DATA\RAG_Service"
```

## 2. Build và khởi chạy lần đầu

```powershell
docker compose build
docker compose up -d
docker compose ps
```

Sau đó mở `http://127.0.0.1:8000/`, nhập admin key như khi chạy Python. Swagger ở `/docs`.

Khi hiển thị `healthy`, tiến trình API cùng kết nối PostgreSQL và Qdrant đã được kiểm tra; healthcheck không gọi Gemini. `up -d` chạy nền nên có thể đóng cửa sổ PowerShell. [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)

Kiểm tra readiness bằng search key mà không ghi key vào lịch sử lệnh:

```powershell
$searchKey = Read-Host "SEARCH_API_KEY"
$headers = @{ Authorization = "Bearer $searchKey" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health/ready" -Headers $headers
```

Kết quả hợp lệ có `status=ready`, `vector_provider=qdrant`, tên collection và số chunk trong BM25.

## 3. Bật, tắt và theo dõi

| Công việc | Lệnh |
|---|---|
| Bật server bằng image đã build | `docker compose up -d --no-build --pull never` |
| Tắt server | `docker compose stop` |
| Khởi động lại | `docker compose restart` |
| Xem trạng thái | `docker compose ps` |
| Xem log trực tiếp | `docker compose logs -f --tail=100 rag` |
| Xem log đồng bộ sản phẩm | `docker compose logs -f --tail=100 product-worker` |
| Xóa container và network của dự án | `docker compose down` |

Ctrl+C khi đang xem log chỉ dừng việc theo dõi log. Có thể dùng các nút Start/Stop của nhóm `donghai-rag-service` trong Docker Desktop.

`restart: unless-stopped` giúp container khởi động lại nếu tiến trình bị lỗi, và chạy lại khi Docker khởi động trừ khi đã được dừng thủ công. Healthcheck báo unhealthy không tự khởi động lại container. [Compose service settings](https://docs.docker.com/reference/compose-file/services/)

## 4. Tài liệu và cấu hình được lưu ở đâu?

- `.env` được mount chỉ đọc vào container; không nhúng vào image.
- `knowlegde` được bind mount vào `/app/knowlegde`, nên file upload vẫn xuất hiện ngay trong thư mục Windows, giữ tên/đuôi như hiện tại.
- `storage` được bind mount vào Product Sync Worker để ảnh sản phẩm đã tải không mất khi tạo lại container.
- Qdrant ở ngoài container, nên `stop`, `down` hoặc build lại image không xóa collection Qdrant.
- PostgreSQL dùng volume `rag_postgres_data`; `docker compose down` không xóa volume. Không thêm `-v` nếu chưa sao lưu loại tài liệu và nhóm.
- `down` không xóa các file trong thư mục bind mount. Tuy nhiên, thao tác **Xóa tài liệu** trên giao diện vẫn xóa file tương ứng như trước. [Bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

Khi sao lưu, giữ cả PostgreSQL, snapshot Qdrant, `knowlegde` và `.env` riêng tư. Image không chứa các dữ liệu này.

## 5. Cập nhật

Sau khi sửa code:

```powershell
docker compose build
docker compose up -d
```

Sau khi sửa `.env`, tạo lại container để bảo đảm file cấu hình mới được mount và đọc lại:

```powershell
docker compose up -d --force-recreate
```

Không cần build lại image khi chỉ sửa `.env` hoặc thêm tài liệu.

## 6. Đổi cổng hoặc nối bot

Mặc định giao diện chỉ mở trên localhost cổng 8000. Muốn dùng cổng khác, thêm vào `.env`:

```dotenv
RAG_HTTP_PORT=8001
```

Sau đó chạy `docker compose up -d`. Truy cập `http://127.0.0.1:8001/` và cập nhật `RAG_SERVICE_URL` bên bot theo cổng mới.

Bot chạy trực tiếp trên cùng Windows dùng `http://127.0.0.1:8000`. Nếu bot cũng chạy trong Docker Desktop, có thể dùng `http://host.docker.internal:8000` khi cấu hình mạng cho phép truy cập cổng đã publish, hoặc cấu hình hai service vào cùng Docker network rồi dùng tên service. Không dùng `127.0.0.1` để trỏ từ container bot sang container RAG.

## 7. Khi có lỗi

- **`lookup registry-1.docker.io: no such host` khi build:** Docker không phân giải được tên miền Docker Hub. Nếu đã có image `donghai-rag-service:local`, bật ngay bằng `docker compose up -d --no-build --pull never`; không cần build lại mỗi lần bật server. Lệnh này không truy cập registry để lấy image, nhưng các chức năng gọi Gemini vẫn cần Internet. Khi cần build bản code mới, khôi phục kết nối DNS/Internet của Docker Desktop rồi chạy build lại.
- **Port is already allocated:** service Python cũ hoặc ứng dụng khác đang dùng cổng 8000. Dừng đúng ứng dụng đó hoặc đổi `RAG_HTTP_PORT`.
- **Cannot connect to Docker daemon:** mở Docker Desktop và đợi engine sẵn sàng.
- **Invalid RAG configuration:** kiểm tra `.env`, các API key, `QDRANT_URL`, collection và giới hạn cấu hình.
- **Unhealthy / HTTP 503:** xem log; kiểm tra Qdrant đang chạy tại cổng `6333` và container truy cập được `host.docker.internal:6333`.
- **Không ghi được file:** kiểm tra quyền/chia sẻ thư mục `knowlegde` với Docker Desktop.

Gói Compose này hướng tới Docker Desktop trên Windows. Trên Linux server, đặt `QDRANT_URL` thành địa chỉ Qdrant mà container truy cập được hoặc đưa hai service vào cùng Docker network; đồng thời bảo đảm UID 10001 có quyền ghi thư mục tài liệu.
