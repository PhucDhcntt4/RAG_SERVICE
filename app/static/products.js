"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  let key = "";
  let localAdmin = false;
  let busy = false;
  let syncPollTimer = null;
  let writeEnabled = false;
  let workerOnline = false;
  let lastJobId = null;
  let lastJobStatus = null;
  let inventorySyncRunning = false;

  let currentCursor = null;
  let nextCursor = null;
  let searchQuery = "";

  const previousCursors = [];

  function formatNumber(value) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
      return "—";
    }

    return number.toLocaleString("vi-VN");
  }

  function formatMoney(value) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
      return "—";
    }

    return new Intl.NumberFormat("vi-VN", {
      style: "currency",
      currency: "VND",
    }).format(number);
  }

  function formatDate(value) {
    if (!value) return "—";

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
      return value;
    }

    return date.toLocaleString("vi-VN");
  }

  function element(tag, className = "", text) {
    const node = document.createElement(tag);

    if (className) {
      node.className = className;
    }

    if (text !== undefined) {
      node.textContent = text;
    }

    return node;
  }

  function setConnection(connected) {
    $("login-panel").hidden = connected;
    $("product-panel").hidden = !connected;
    $("product-stats").hidden = !connected;
    $("sync-panel").hidden = !connected;

    $("connection").textContent = connected ? "Đã kết nối" : "Chưa kết nối";

    $("connection").classList.toggle("online", connected);

    $("disconnect").hidden = !connected || localAdmin;

    if (connected) {
      startSyncPolling();
    } else {
      stopSyncPolling();
    }
  }

  function notice(message, error = false) {
    const box = $("notice");

    if (!message) {
      box.hidden = true;
      box.textContent = "";
      return;
    }

    box.textContent = message;
    box.classList.toggle("error", error);
    box.hidden = false;
  }

  function reset() {
    stopSyncPolling();
    key = "";
    localAdmin = false;

    currentCursor = null;
    nextCursor = null;
    searchQuery = "";

    previousCursors.length = 0;

    $("product-search-input").value = "";
    $("product-search-clear").hidden = true;

    $("products").replaceChildren();
    $("table-wrap").hidden = true;
    $("empty").hidden = true;

    $("catalog-count").textContent = "—";
    $("image-vector-count").textContent = "—";
    $("page-product-count").textContent = "—";
    $("ready-count").textContent = "—";

    $("page-info").textContent = "Chưa tải danh sách";

    setConnection(false);
  }

  async function api(path, options = {}) {
    const actualPath = localAdmin
      ? path.replace(/^\/api\//, "/admin-api/")
      : path;

    let response;

    try {
      response = await fetch(actualPath, {
        ...options,

        headers: {
          ...options.headers,

          ...(localAdmin
            ? {
                "X-RAG-Local-UI": "1",
              }
            : {
                Authorization: `Bearer ${key}`,
              }),
        },

        cache: "no-store",
      });
    } catch {
      throw new Error("Không thể kết nối RAG Service.");
    }

    let body;

    try {
      body = await response.json();
    } catch {
      throw new Error("Service trả về dữ liệu không hợp lệ.");
    }

    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        throw new Error("Admin key không hợp lệ hoặc không có quyền.");
      }

      throw new Error(
        typeof body.detail === "string"
          ? body.detail
          : `Yêu cầu thất bại (${response.status}).`,
      );
    }

    return body;
  }

  async function loadCollections() {
    const data = await api("/api/v1/products/collections");

    const catalog = data.catalog || {};
    const images = data.images || {};

    $("catalog-count").textContent = formatNumber(catalog.points_count);

    $("image-vector-count").textContent = formatNumber(images.points_count);

    $("collection-info").textContent =
      `${catalog.name || "Catalog"} · ` + `${images.name || "Images"}`;
  }

  function createProductImage(product) {
    const wrap = element("div", "product-list-image-wrap");

    if (!product.image_url) {
      wrap.append(element("span", "product-image-placeholder", "◇"));

      return wrap;
    }

    const image = document.createElement("img");

    image.src = product.image_url;
    image.alt = product.title || "";
    image.loading = "lazy";
    image.referrerPolicy = "no-referrer";
    image.className = "product-list-image";

    image.addEventListener("error", () => {
      wrap.replaceChildren(element("span", "product-image-placeholder", "◇"));
    });

    wrap.append(image);

    return wrap;
  }

  function renderProducts(products) {
    const tbody = $("products");

    tbody.replaceChildren();

    for (const product of products) {
      const tr = element("tr");

      // Product
      const productCell = document.createElement("td");

      const productWrap = element("div", "product-cell");

      productWrap.append(createProductImage(product));

      const productInfo = document.createElement("div");

      const title = element(
        "button",
        "document-name document-open",
        product.title || "Không có tên",
      );

      title.type = "button";

      title.addEventListener("click", () => openProduct(product.product_code));

      productInfo.append(title);

      productInfo.append(
        element("div", "document-meta", product.vendor || "—"),
      );

      productWrap.append(productInfo);
      productCell.append(productWrap);
      tr.append(productCell);

      // Product code
      tr.append(createTextCell(product.product_code || "—"));

      // Type
      const typeCell = document.createElement("td");

      typeCell.append(element("span", "category", product.product_type || "—"));

      tr.append(typeCell);

      // Colors
      tr.append(createTextCell(product.colors || "—"));

      // Embeddings
      const vectorCell = document.createElement("td");

      const vectorText =
        `${product.embedding_count ?? 0}` + ` / ${product.image_count ?? 0}`;

      const vectorBadge = element(
        "span",
        product.embedding_count === product.image_count &&
          product.image_count > 0
          ? "badge"
          : "badge inactive",
        vectorText,
      );

      vectorCell.append(vectorBadge);
      tr.append(vectorCell);

      // Status
      const statusCell = document.createElement("td");

      statusCell.append(
        element(
          "span",
          product.status === "ACTIVE" ? "status" : "status inactive",
          product.status || "—",
        ),
      );

      tr.append(statusCell);

      // AI ready
      const aiCell = document.createElement("td");

      aiCell.append(
        element(
          "span",
          product.ai_ready ? "badge" : "badge inactive",
          product.ai_ready ? "Sẵn sàng" : "Chưa sẵn sàng",
        ),
      );

      tr.append(aiCell);

      // Action
      const actionCell = document.createElement("td");

      const viewButton = element("button", "secondary small", "Xem");

      viewButton.type = "button";

      viewButton.addEventListener("click", () =>
        openProduct(product.product_code),
      );

      actionCell.append(viewButton);
      tr.append(actionCell);

      tbody.append(tr);
    }
  }

  function createTextCell(text) {
    const td = document.createElement("td");

    td.textContent = text;

    return td;
  }

  async function loadProducts() {
    busy = true;

    $("loading").hidden = false;
    $("empty").hidden = true;
    $("table-wrap").hidden = true;

    $("table-refresh").disabled = true;

    try {
      const params = new URLSearchParams();

      params.set("limit", "20");

      if (currentCursor) {
        params.set("cursor", currentCursor);
      }

      if (searchQuery) {
        params.set("q", searchQuery);
      }

      const data = await api(`/api/v1/products?${params.toString()}`);

      const products = Array.isArray(data.products) ? data.products : [];

      nextCursor = data.next_cursor || null;

      renderProducts(products);

      const readyCount = products.filter((item) => item.ai_ready).length;

      $("product-count").textContent = String(products.length);

      $("page-product-count").textContent = String(products.length);

      $("ready-count").textContent = String(readyCount);

      $("page-info").textContent = searchQuery
        ? `${products.length} kết quả cho “${searchQuery}” trên trang`
        : `${products.length} sản phẩm trên trang`;

      $("empty-title").textContent = searchQuery
        ? "Không tìm thấy sản phẩm"
        : "Chưa có sản phẩm";
      $("empty-description").textContent = searchQuery
        ? `Không có sản phẩm phù hợp với “${searchQuery}”.`
        : "Không tìm thấy dữ liệu trong product catalog collection.";

      $("table-wrap").hidden = products.length === 0;

      $("empty").hidden = products.length !== 0;

      $("previous").disabled = previousCursors.length === 0;

      $("next").disabled = !nextCursor;

      setConnection(true);
    } finally {
      busy = false;

      $("loading").hidden = true;

      $("table-refresh").disabled = false;
    }
  }

  function renderVariants(variants) {
    const tbody = $("variants");

    tbody.replaceChildren();

    $("variant-count").textContent = String(variants.length);

    for (const variant of variants) {
      const tr = document.createElement("tr");

      tr.append(createTextCell(variant.sku || "—"));

      tr.append(createTextCell(variant.color || "—"));

      tr.append(createTextCell(variant.size || "—"));

      tr.append(createTextCell(formatMoney(variant.price)));

      tr.append(createTextCell(formatNumber(variant.inventory_quantity)));

      const statusCell = document.createElement("td");

      statusCell.append(
        element(
          "span",
          variant.available ? "status" : "status inactive",
          variant.available ? "Có hàng" : "Hết hàng",
        ),
      );

      tr.append(statusCell);
      tbody.append(tr);
    }
  }

  function renderImages(images) {
    const gallery = $("product-gallery");

    gallery.replaceChildren();

    $("image-count").textContent = String(images.length);

    for (const imageData of images) {
      const card = element("div", "product-image-card");

      const image = document.createElement("img");

      image.src = imageData.source_url || "";

      image.alt = imageData.alt_text || "";

      image.loading = "lazy";

      const info = element("div", "product-image-info");

      info.append(
        element(
          "strong",
          "",
          imageData.color || `Ảnh ${imageData.image_order || ""}`,
        ),
      );

      info.append(
        element(
          "span",
          "",
          imageData.embedded ? "✓ Đã embedding" : "Chưa embedding",
        ),
      );

      card.append(image, info);

      gallery.append(card);
    }

    if (!images.length) {
      gallery.append(
        element("p", "product-empty-text", "Sản phẩm chưa có hình ảnh."),
      );
    }
  }

  async function openProduct(productCode) {
    $("detail-loading").hidden = false;
    $("product-detail").hidden = true;

    $("product-dialog").showModal();

    try {
      const data = await api(
        `/api/v1/products/${encodeURIComponent(productCode)}`,
      );

      const payload = data.product || {};

      const publicInfo = payload.public_info || {};

      const detail = payload.detail?.product || {};

      const summary = payload.summary || {};

      const images = Array.isArray(payload.images) ? payload.images : [];

      const variants = Array.isArray(payload.variants) ? payload.variants : [];

      const title =
        payload.title || publicInfo.product_name || detail.title || productCode;

      $("detail-title").textContent = title;

      $("detail-meta").textContent = [
        productCode,
        payload.vendor,
        payload.product_type,
      ]
        .filter(Boolean)
        .join(" · ");

      $("detail-code").textContent = payload.product_code || productCode;

      $("detail-vendor").textContent = payload.vendor || detail.vendor || "—";

      $("detail-type").textContent =
        payload.product_type || detail.product_type || "—";

      $("detail-material").textContent =
        payload.material || detail.material || "—";

      $("detail-status").textContent = payload.status || detail.status || "—";

      $("detail-updated").textContent = formatDate(
        payload.updated_at || summary.updated_at || detail.source_updated_at,
      );

      $("detail-description").textContent =
        publicInfo.description ||
        detail.description ||
        payload.description ||
        "Không có mô tả.";

      const mainImage = publicInfo.image_urls?.[0] || images[0]?.source_url;

      if (mainImage) {
        $("detail-image").src = mainImage;

        $("detail-image").alt = title;

        $("detail-image").hidden = false;

        $("detail-no-image").hidden = true;
      } else {
        $("detail-image").hidden = true;

        $("detail-no-image").hidden = false;
      }

      renderVariants(variants);
      renderImages(images);

      $("product-detail").hidden = false;
    } catch (error) {
      notice(error.message, true);

      $("product-dialog").close();
    } finally {
      $("detail-loading").hidden = true;
    }
  }

  function syncStatusText(status) {
    return (
      {
        pending: "Đang chờ",
        running: "Đang chạy",
        completed: "Hoàn tất",
        failed: "Thất bại",
      }[status] || "—"
    );
  }

  function syncModeText(mode) {
    return mode === "all_active" ? "Tất cả ACTIVE" : "Hiện có";
  }

  function updateSyncButtons(activeJob) {
    const blocked = Boolean(activeJob) || !writeEnabled;
    $("sync-existing").disabled = blocked;
    $("sync-all").disabled = blocked;
    $("save-sync-settings").disabled = false;
  }

  function renderSyncJob(job) {
    if (!job) {
      $("sync-job-title").textContent = "Chưa có sync job";
      $("sync-job-meta").textContent = "—";
      $("sync-job-status").textContent = "—";
      $("sync-job-status").className = "sync-status";
      $("sync-progress-bar").style.width = "0%";
      $("sync-progress-text").textContent = "0 / 0";
      $("sync-created").textContent = "0";
      $("sync-updated").textContent = "0";
      $("sync-inactivated").textContent = "0";
      $("sync-embeddings").textContent = "0";
      $("sync-failed").textContent = "0";
      $("sync-job-message").textContent = "Worker chưa chạy job.";
      return;
    }

    const selected = Number(job.selected_products || 0);
    const processed = Number(job.processed_products || 0);
    const percent =
      selected > 0
        ? Math.min(100, Math.round((processed / selected) * 100))
        : 0;

    $("sync-job-title").textContent =
      `Job #${job.id} · ${syncModeText(job.mode)}`;
    $("sync-job-meta").textContent =
      `${job.trigger_type || "manual"} · ${job.dry_run ? "dry-run" : "ghi thật"} · ${formatDate(job.created_at)}`;
    $("sync-job-status").textContent = syncStatusText(job.status);
    $("sync-job-status").className = `sync-status ${job.status || ""}`;
    $("sync-progress-bar").style.width = `${percent}%`;
    $("sync-progress-text").textContent = `${processed} / ${selected}`;
    $("sync-created").textContent = formatNumber(job.created_products || 0);
    $("sync-updated").textContent = formatNumber(job.updated_products || 0);
    $("sync-inactivated").textContent = formatNumber(
      job.inactivated_products || 0,
    );
    $("sync-embeddings").textContent = formatNumber(job.new_embeddings || 0);
    $("sync-failed").textContent = formatNumber(job.failed_products || 0);
    $("sync-job-message").textContent = job.error_message || job.message || "—";
  }

  function renderSyncHistory(jobs) {
    const tbody = $("sync-history");
    tbody.replaceChildren();

    for (const job of jobs) {
      const tr = document.createElement("tr");
      tr.append(createTextCell(`#${job.id}`));
      tr.append(createTextCell(job.trigger_type || "—"));
      tr.append(createTextCell(syncModeText(job.mode)));
      tr.append(createTextCell(syncStatusText(job.status)));
      tr.append(
        createTextCell(
          `${job.processed_products || 0} / ${job.selected_products || 0}`,
        ),
      );
      tr.append(createTextCell(String(job.failed_products || 0)));
      tr.append(
        createTextCell(
          formatDate(job.finished_at || job.started_at || job.created_at),
        ),
      );
      tbody.append(tr);
    }

    if (!jobs.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7;
      td.textContent = "Chưa có lịch sử sync.";
      tr.append(td);
      tbody.append(tr);
    }
  }

  async function loadSyncStatus() {
    const data = await api("/api/v1/products/sync/status");
    const worker = data.worker || {};
    const job = data.job || null;
    const activeJob = data.active_job || null;

    writeEnabled = Boolean(data.write_enabled);
    workerOnline = Boolean(worker.online);

    $("worker-state").textContent = workerOnline
      ? "Worker: Online"
      : "Worker: Offline";
    $("worker-state").className =
      `sync-pill ${workerOnline ? "online" : "offline"}`;

    $("write-state").textContent = writeEnabled
      ? "Qdrant write: BẬT"
      : "Qdrant write: TẮT";
    $("write-state").className =
      `sync-pill ${writeEnabled ? "online" : "blocked"}`;

    renderSyncJob(job);
    updateSyncButtons(activeJob);

    if (job) {
      const transitionedToFinal =
        lastJobId === job.id &&
        ["pending", "running"].includes(lastJobStatus) &&
        ["completed", "failed"].includes(job.status);

      lastJobId = job.id;
      lastJobStatus = job.status;

      if (transitionedToFinal) {
        await Promise.all([loadCollections(), loadProducts()]);
      }
    }
  }

  async function loadSyncSettings() {
    const data = await api("/api/v1/products/sync/settings");
    $("auto-sync-enabled").checked = Boolean(data.enabled);
    $("auto-sync-mode").value = data.sync_mode || "existing";
    $("auto-sync-time").value = String(data.sync_time || "02:00").slice(0, 5);
    $("auto-sync-timezone").value = data.timezone || "Asia/Ho_Chi_Minh";
  }

  async function loadSyncHistory() {
    const data = await api("/api/v1/products/sync/history?limit=8");
    renderSyncHistory(Array.isArray(data.jobs) ? data.jobs : []);
  }


  async function loadInventorySyncStatus() {
    const data = await api("/api/v1/products/inventory-sync/status");
    inventorySyncRunning = Boolean(data.running);

    $("inventory-sync-enabled").checked = Boolean(data.enabled);
    $("inventory-sync-interval").value = String(data.interval_hours || 6);

    const stateText = data.running
      ? "Tồn kho: Đang chạy"
      : data.run_requested
        ? "Tồn kho: Đang chờ"
        : data.enabled
          ? "Tồn kho: Đã bật"
          : "Tồn kho: Đã tắt";
    $("inventory-sync-state").textContent = stateText;
    $("inventory-sync-state").className =
      `sync-pill ${data.running || data.enabled ? "online" : "blocked"}`;

    $("inventory-sync-last").textContent =
      `Lần cuối: ${formatDate(data.last_success_at)}`;
    $("inventory-sync-next").textContent =
      `Lần tiếp theo: ${formatDate(data.next_run_at)}`;

    const stats = [
      `SP kiểm tra ${formatNumber(data.last_checked_products || 0)}`,
      `SP đổi ${formatNumber(data.last_updated_products || 0)}`,
      `variant đổi ${formatNumber(data.last_changed_variants || 0)}`,
      `thiếu ${formatNumber(data.last_missing_variants || 0)}`,
    ].join(" · ");
    $("inventory-sync-stats").textContent = data.last_error
      ? `Lỗi: ${data.last_error}`
      : stats;

    $("inventory-sync-now").disabled =
      inventorySyncRunning || !Boolean(data.write_enabled);
  }

  async function saveInventorySyncSettings() {
    try {
      const body = {
        enabled: $("inventory-sync-enabled").checked,
        interval_hours: Number($("inventory-sync-interval").value || 6),
      };
      await api("/api/v1/products/inventory-sync/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      notice("Đã lưu lịch Inventory Sync.");
      await loadInventorySyncStatus();
    } catch (error) {
      notice(error.message, true);
    }
  }

  async function runInventorySyncNow() {
    try {
      await api("/api/v1/products/inventory-sync/run", { method: "POST" });
      notice("Đã yêu cầu đồng bộ tồn kho. Worker sẽ chạy sau job Product Sync hiện tại nếu có.");
      await loadInventorySyncStatus();
    } catch (error) {
      notice(error.message, true);
    }
  }

  async function refreshSyncArea() {
    await Promise.all([loadSyncStatus(), loadSyncHistory(), loadInventorySyncStatus()]);
  }

  function startSyncPolling() {
    if (syncPollTimer) return;
    syncPollTimer = window.setInterval(() => {
      refreshSyncArea().catch(() => {});
    }, 10000);
  }

  function stopSyncPolling() {
    if (!syncPollTimer) return;
    window.clearInterval(syncPollTimer);
    syncPollTimer = null;
  }

  async function enqueueSync(mode) {
    if (mode === "all_active") {
      const ok = window.confirm(
        "Sync tất cả Shopify ACTIVE sẽ tạo/cập nhật Product RAG và có thể chạy CLIP cho ảnh mới. Tiếp tục?",
      );
      if (!ok) return;
    }

    try {
      notice("");
      const job = await api("/api/v1/products/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, dry_run: false }),
      });
      notice(`Đã tạo sync job #${job.id}. Worker sẽ xử lý ở Terminal 2.`);
      await refreshSyncArea();
    } catch (error) {
      notice(error.message, true);
    }
  }

  async function saveSyncSettings() {
    try {
      const body = {
        enabled: $("auto-sync-enabled").checked,
        sync_mode: $("auto-sync-mode").value,
        sync_time: $("auto-sync-time").value || "02:00",
        timezone: $("auto-sync-timezone").value || "Asia/Ho_Chi_Minh",
      };

      await api("/api/v1/products/sync/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      notice("Đã lưu lịch Auto Sync.");
      await loadSyncSettings();
    } catch (error) {
      notice(error.message, true);
    }
  }

  async function loadAll() {
    notice("");

    await Promise.all([
      loadCollections(),
      loadProducts(),
      loadSyncSettings(),
      loadSyncStatus(),
      loadSyncHistory(),
      loadInventorySyncStatus(),
    ]);
  }

  async function tryLocalAdmin() {
    const localHosts = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

    if (!localHosts.has(window.location.hostname)) {
      setConnection(false);
      return;
    }

    localAdmin = true;

    try {
      await loadAll();

      setConnection(true);
    } catch {
      localAdmin = false;
      setConnection(false);
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();

    key = $("admin-key").value.trim();

    localAdmin = false;

    if (!key) {
      return;
    }

    $("login-error").textContent = "";

    try {
      await loadAll();

      $("admin-key").value = "";
    } catch (error) {
      $("login-error").textContent = error.message;

      setConnection(false);
    }
  });

  $("toggle-key").addEventListener("click", () => {
    const input = $("admin-key");

    const visible = input.type === "text";

    input.type = visible ? "password" : "text";

    $("toggle-key").textContent = visible ? "Hiện" : "Ẩn";
  });

  $("table-refresh").addEventListener("click", loadAll);

  $("product-search-form").addEventListener("submit", async (event) => {
    event.preventDefault();

    searchQuery = $("product-search-input").value.trim();
    currentCursor = null;
    nextCursor = null;
    previousCursors.length = 0;
    $("product-search-clear").hidden = !searchQuery;

    await loadProducts();
  });

  $("product-search-clear").addEventListener("click", async () => {
    $("product-search-input").value = "";
    searchQuery = "";
    currentCursor = null;
    nextCursor = null;
    previousCursors.length = 0;
    $("product-search-clear").hidden = true;

    await loadProducts();
    $("product-search-input").focus();
  });

  $("sync-existing").addEventListener("click", () => enqueueSync("existing"));
  $("sync-all").addEventListener("click", () => enqueueSync("all_active"));
  $("sync-refresh").addEventListener("click", refreshSyncArea);
  $("save-sync-settings").addEventListener("click", saveSyncSettings);

  $("save-inventory-settings").addEventListener("click", saveInventorySyncSettings);
  $("inventory-sync-now").addEventListener("click", runInventorySyncNow);

  $("next").addEventListener("click", async () => {
    if (!nextCursor) return;

    previousCursors.push(currentCursor);

    currentCursor = nextCursor;

    await loadProducts();
  });

  $("previous").addEventListener("click", async () => {
    if (previousCursors.length === 0) {
      return;
    }

    currentCursor = previousCursors.pop();

    await loadProducts();
  });

  $("disconnect").addEventListener("click", reset);

  $("close-detail").addEventListener("click", () => {
    $("product-dialog").close();
  });

  $("product-dialog").addEventListener("click", (event) => {
    if (event.target === $("product-dialog")) {
      $("product-dialog").close();
    }
  });

  setConnection(false);

  tryLocalAdmin();
})();
