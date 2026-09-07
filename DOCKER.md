# Chạy RAG Service bằng Docker trên Windows

Gói này chạy API và giao diện trong Linux container; PostgreSQL vẫn chạy trên máy Windows như hiện tại. Container đọc `.env` ở thư mục dự án. Với database đặt tại `127.0.0.1`, `localhost` hoặc `::1`, entrypoint tự đổi host thành `host.docker.internal`, giữ nguyên database, cổng, tài khoản và mật khẩu. Đây là địa chỉ Docker Desktop cung cấp để container kết nối dịch vụ trên máy chủ. [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/networking-how-tos/)

## 1. Chuẩn bị

- Mở Docker Desktop và dùng Linux containers.
- Giữ PostgreSQL trên Windows hoạt động, với pgvector và database `RAG_SERVICE` hiện có.
- Giữ `.env` đầy đủ `DATABASE_URL`, `SEARCH_API_KEY`, `ADMIN_API_KEY`, `GEMINI_API_KEY`.
- Thư mục `knowlegde` chứa tài liệu đã upload. Các file cũ còn nằm trong `storage/documents` cần chuyển trước bằng `python -m app.migrate_file_names --confirm` khi chạy Python trên Windows.
- Nếu Uvicorn đang chạy trực tiếp ở cổng 8000, dừng bằng Ctrl+C trong cửa sổ chạy service trước khi bật container ở cùng cổng.

Mở PowerShell:

```powershell
cd "D:\ĐÔNG HẢI\DATA\RAG_Service"
```

## 2. Build và khởi chạy lần đầu

```powershell
docker compose build
docker compose run --rm rag python -m app.init_db --confirm
docker compose up -d
docker compose ps
```

Lệnh init tạo/cập nhật schema được dự án hỗ trợ trong database đã cấu hình; không tạo database mới. Sau đó mở `http://127.0.0.1:8000/`, nhập admin key như khi chạy Python. Swagger ở `/docs`.

Khi hiển thị `healthy`, tiến trình và kết nối database đã được kiểm tra; healthcheck không gọi Gemini. `up -d` chạy nền nên có thể đóng cửa sổ PowerShell. [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)

## 3. Bật, tắt và theo dõi

| Công việc | Lệnh |
|---|---|
| Bật server bằng image đã build | `docker compose up -d --no-build --pull never` |
| Tắt server | `docker compose stop` |
| Khởi động lại | `docker compose restart` |
| Xem trạng thái | `docker compose ps` |
| Xem log trực tiếp | `docker compose logs -f --tail=100 rag` |
| Xóa container và network của dự án | `docker compose down` |

Ctrl+C khi đang xem log chỉ dừng việc theo dõi log. Có thể dùng các nút Start/Stop của nhóm `donghai-rag-service` trong Docker Desktop.

`restart: unless-stopped` giúp container khởi động lại nếu tiến trình bị lỗi, và chạy lại khi Docker khởi động trừ khi đã được dừng thủ công. Healthcheck báo unhealthy không tự khởi động lại container. [Compose service settings](https://docs.docker.com/reference/compose-file/services/)

## 4. Tài liệu và cấu hình được lưu ở đâu?

- `.env` được mount chỉ đọc vào container; không nhúng vào image.
- `knowlegde` được bind mount vào `/app/knowlegde`, nên file upload vẫn xuất hiện ngay trong thư mục Windows, giữ tên/đuôi như hiện tại.
- PostgreSQL ở ngoài container, nên `stop`, `down` hoặc build lại image không xóa dữ liệu PostgreSQL.
- `down` không xóa các file trong thư mục bind mount. Tuy nhiên, thao tác **Xóa tài liệu** trên giao diện vẫn xóa file tương ứng như trước. [Bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

Khi sao lưu, giữ cả database, `knowlegde` và `.env` riêng tư. Image không chứa các dữ liệu này.

## 5. Cập nhật

Sau khi sửa code:

```powershell
docker compose build
docker compose run --rm rag python -m app.init_db --confirm
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
- **Invalid RAG configuration:** kiểm tra `.env`, các key và chuỗi kết nối DB.
- **Unhealthy / HTTP 503:** xem log; kiểm tra PostgreSQL Windows đang chạy, cổng và cấu hình cho phép kết nối từ Docker Desktop. Không thay `pg_hba.conf` thành `trust` cho toàn mạng. Chỉ cho phép nguồn kết nối cần thiết nếu PostgreSQL chưa cho phép Docker truy cập.
- **Không ghi được file:** kiểm tra quyền/chia sẻ thư mục `knowlegde` với Docker Desktop.

Gói Compose này hướng tới Docker Desktop trên Windows. Trên Linux server, cần cấu hình host DB thực tế hoặc ánh xạ host gateway, đồng thời bảo đảm UID 10001 có quyền ghi thư mục tài liệu.
