"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  let key = "";
  let localAdmin = false;
  let busy = false;
  let noticeTimer = null;
  let syncPollTimer = null;
  let writeEnabled = false;
  let workerOnline = false;
  let lastJobId = null;
  let lastJobStatus = null;
  let inventorySyncRunning = false;
  const historyPageSize = 5;
  let syncHistoryPage = 1;
  let syncHistoryTotalPages = 1;
  let inventoryHistoryPage = 1;
  let inventoryHistoryTotalPages = 1;
  let deltaCheckpointState = null;
  let auditKind = "product";
  let auditRunId = null;
  let auditPage = 1;
  let auditTotalPages = 1;
  const auditPageSize = 10;
  const syncPanelCollapsedKey = "rag-product-sync-panel-collapsed";

  let currentPage = 1;
  let pageSize = 30;
  let totalPages = 1;
  let totalProducts = 0;
  let searchQuery = "";
  let selectedProductType = "";
  let selectedStatus = "";

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

  function formatVietnamDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("vi-VN", {
      timeZone: "Asia/Ho_Chi_Minh",
    });
  }

  function vietnamDateTimeInput(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Ho_Chi_Minh",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(date)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
  }

  function checkpointInputIso() {
    const value = $("delta-checkpoint-input").value;
    return value ? `${value}+07:00` : "";
  }

  function updateCheckpointPreview() {
    const value = checkpointInputIso();
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) {
      $("delta-checkpoint-query-preview").textContent = "—";
      return;
    }
    const overlap = Number(deltaCheckpointState?.overlap_seconds) || 120;
    $("delta-checkpoint-query-preview").textContent = formatVietnamDate(
      new Date(date.getTime() - overlap * 1000),
    );
  }

  function setSyncPanelCollapsed(collapsed, persist = true) {
    const panel = $("sync-panel");
    const toggle = $("sync-panel-toggle");
    const isCollapsed = Boolean(collapsed);

    panel.classList.toggle("is-collapsed", isCollapsed);
    toggle.setAttribute("aria-expanded", String(!isCollapsed));
    $("sync-panel-toggle-text").textContent = isCollapsed
      ? "Mở rộng"
      : "Thu gọn";

    if (persist) {
      try {
        window.localStorage.setItem(syncPanelCollapsedKey, String(isCollapsed));
      } catch (_error) {
        // Trình duyệt có thể chặn localStorage; thao tác đóng/mở vẫn hoạt động.
      }
    }
  }

  function restoreSyncPanelState() {
    let collapsed = false;
    try {
      collapsed = window.localStorage.getItem(syncPanelCollapsedKey) === "true";
    } catch (_error) {
      collapsed = false;
    }
    setSyncPanelCollapsed(collapsed, false);
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

    if (noticeTimer) {
      window.clearTimeout(noticeTimer);
      noticeTimer = null;
    }

    if (!message) {
      box.hidden = true;
      box.textContent = "";
      return;
    }

    box.textContent = message;
    box.classList.toggle("error", error);
    box.hidden = false;

    noticeTimer = window.setTimeout(
      () => {
        box.hidden = true;
        box.textContent = "";
        noticeTimer = null;
      },
      error ? 6500 : 4000,
    );
  }

  function reset() {
    stopSyncPolling();
    key = "";
    localAdmin = false;

    currentPage = 1;
    pageSize = 30;
    totalPages = 1;
    totalProducts = 0;
    searchQuery = "";
    selectedProductType = "";
    selectedStatus = "";

    $("product-search-input").value = "";
    $("product-search-clear").hidden = true;
    $("product-type-filter").value = "";
    $("product-status-filter").value = "";
    $("product-filter-clear").hidden = true;
    $("page-size").value = "30";

    $("products").replaceChildren();
    $("table-wrap").hidden = true;
    $("empty").hidden = true;

    $("catalog-count").textContent = "—";
    $("image-vector-count").textContent = "—";
    $("page-product-count").textContent = "—";
    $("ready-count").textContent = "—";
    $("total-product-count").textContent = "—";
    $("pagination-pages").replaceChildren();

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

  function replaceSelectOptions(select, values, emptyLabel, selectedValue) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = emptyLabel;
    select.replaceChildren(empty);

    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    }

    select.value = selectedValue;
    if (select.value !== selectedValue) {
      select.value = "";
    }
  }

  function updateFilterClearButton() {
    $("product-filter-clear").hidden = !(selectedProductType || selectedStatus);
  }

  async function loadProductFilters() {
    const data = await api("/api/v1/products/filters");
    const productTypes = Array.isArray(data.product_types)
      ? data.product_types
      : [];
    const statuses = Array.isArray(data.statuses) ? data.statuses : [];

    replaceSelectOptions(
      $("product-type-filter"),
      productTypes,
      "Tất cả thể loại",
      selectedProductType,
    );
    replaceSelectOptions(
      $("product-status-filter"),
      statuses,
      "Tất cả trạng thái",
      selectedStatus,
    );
    updateFilterClearButton();
  }

  function resetProductPagination() {
    currentPage = 1;
    totalPages = 1;
  }

  function paginationItems(page, pages) {
    if (pages <= 7) {
      return Array.from({ length: pages }, (_, index) => index + 1);
    }

    const selected = new Set([1, pages]);
    let rangeStart;
    let rangeEnd;

    if (page <= 4) {
      rangeStart = 2;
      rangeEnd = 5;
    } else if (page >= pages - 3) {
      rangeStart = pages - 4;
      rangeEnd = pages - 1;
    } else {
      rangeStart = page - 2;
      rangeEnd = page + 2;
    }

    for (let value = rangeStart; value <= rangeEnd; value += 1) {
      selected.add(value);
    }

    const ordered = [...selected].sort((left, right) => left - right);
    const items = [];
    let previous = 0;

    for (const value of ordered) {
      if (previous && value - previous > 1) {
        items.push("ellipsis");
      }
      items.push(value);
      previous = value;
    }
    return items;
  }

  function renderPagination() {
    const container = $("pagination-pages");
    container.replaceChildren();

    if (totalProducts === 0) {
      $("previous").disabled = true;
      $("next").disabled = true;
      return;
    }

    for (const item of paginationItems(currentPage, totalPages)) {
      if (item === "ellipsis") {
        container.append(element("span", "pagination-ellipsis", "…"));
        continue;
      }

      const button = element(
        "button",
        item === currentPage ? "pagination-page active" : "pagination-page",
        String(item),
      );
      button.type = "button";
      button.setAttribute("aria-label", `Trang ${item}`);
      if (item === currentPage) {
        button.setAttribute("aria-current", "page");
      }
      button.addEventListener("click", async () => {
        if (busy || item === currentPage) return;
        currentPage = item;
        await loadProducts();
      });
      container.append(button);
    }

    $("previous").disabled = currentPage <= 1;
    $("next").disabled = currentPage >= totalPages;
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

      params.set("limit", String(pageSize));
      params.set("page", String(currentPage));

      if (searchQuery) {
        params.set("q", searchQuery);
      }

      if (selectedProductType) {
        params.set("product_type", selectedProductType);
      }

      if (selectedStatus) {
        params.set("status", selectedStatus);
      }

      const data = await api(`/api/v1/products?${params.toString()}`);

      const products = Array.isArray(data.products) ? data.products : [];

      currentPage = Number(data.page) || 1;
      totalPages = Math.max(1, Number(data.total_pages) || 1);
      totalProducts = Math.max(0, Number(data.total) || 0);

      renderProducts(products);

      const readyCount = products.filter((item) => item.ai_ready).length;

      $("product-count").textContent = formatNumber(totalProducts);

      $("page-product-count").textContent = String(products.length);

      $("ready-count").textContent = String(readyCount);

      $("total-product-count").textContent = formatNumber(totalProducts);

      $("page-info").textContent = searchQuery
        ? `${products.length} kết quả cho “${searchQuery}” trên trang`
        : `${products.length} sản phẩm trên trang`;

      $("empty-title").textContent = searchQuery
        ? "Không tìm thấy sản phẩm"
        : "Chưa có sản phẩm";
      $("empty-description").textContent = searchQuery
        ? `Không có sản phẩm phù hợp với “${searchQuery}”.`
        : "Không tìm thấy dữ liệu trong product catalog collection.";

      if (selectedProductType || selectedStatus) {
        const filterLabels = [
          selectedProductType && `thể loại ${selectedProductType}`,
          selectedStatus && `trạng thái ${selectedStatus}`,
        ].filter(Boolean);
        const searchLabel = searchQuery ? ` · tìm “${searchQuery}”` : "";

        $("page-info").textContent =
          `${products.length} kết quả trên trang · ` +
          `${filterLabels.join(" · ")}${searchLabel}`;
        $("empty-title").textContent = "Không tìm thấy sản phẩm";
        $("empty-description").textContent =
          "Không có sản phẩm phù hợp với bộ lọc đã chọn.";
      }

      $("table-wrap").hidden = products.length === 0;

      $("empty").hidden = products.length !== 0;

      renderPagination();

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

  function syncModeText(mode, triggerType = "manual") {
    if (mode === "all_active") return "Full Sync bảo trì";
    return "Đồng bộ sản phẩm";
  }

  function updateSyncButtons(activeJob) {
    const blocked = Boolean(activeJob) || !writeEnabled;
    $("sync-existing").disabled = blocked;
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
      `Job #${job.id} · ${syncModeText(job.mode, job.trigger_type)}`;
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
      tr.append(createTextCell(syncModeText(job.mode, job.trigger_type)));
      tr.append(createTextCell(syncStatusText(job.status)));
      tr.append(
        createTextCell(
          `${job.processed_products || 0} / ${job.selected_products || 0}`,
        ),
      );
      tr.append(createTextCell(String(job.failed_products || 0)));

      const changeCell = document.createElement("td");
      const changeCount = Number(job.change_count) || 0;
      const changeButton = element(
        "button",
        "sync-audit-open",
        changeCount ? `Xem ${formatNumber(changeCount)}` : "Không có",
      );
      changeButton.type = "button";
      changeButton.disabled = changeCount === 0;
      changeButton.addEventListener("click", () => {
        openSyncAudit("product", job.id);
      });
      changeCell.append(changeButton);
      tr.append(changeCell);

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
      td.colSpan = 8;
      td.textContent = "Chưa có lịch sử sync.";
      tr.append(td);
      tbody.append(tr);
    }
  }

  function renderHistoryPagination(kind, page, totalPages, total) {
    const isInventory = kind === "inventory";
    const info = $(
      isInventory ? "inventory-history-page-info" : "sync-history-page-info",
    );
    const previous = $(
      isInventory ? "inventory-history-previous" : "sync-history-previous",
    );
    const next = $(
      isInventory ? "inventory-history-next" : "sync-history-next",
    );

    info.textContent =
      `Trang ${formatNumber(page)} / ${formatNumber(totalPages)} · ` +
      `${formatNumber(total)} bản ghi`;
    previous.disabled = page <= 1;
    next.disabled = page >= totalPages;
  }

  const auditFieldLabels = {
    product: "Sản phẩm",
    title: "Tên sản phẩm",
    vendor: "Thương hiệu",
    product_type: "Loại sản phẩm",
    status: "Trạng thái",
    description: "Mô tả",
    material: "Chất liệu",
    sole: "Đế",
    height: "Chiều cao",
    variants: "Biến thể",
    images: "Hình ảnh",
  };

  function formatAuditValue(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "Có" : "Không";
    if (typeof value === "object") {
      return JSON.stringify(value, null, 2);
    }
    return String(value);
  }

  function renderProductAudit(rows) {
    const list = $("sync-audit-list");
    list.replaceChildren();

    for (const row of rows) {
      const card = element("article", "sync-audit-card");
      const heading = element("div", "sync-audit-card-heading");
      const identity = element("div");
      identity.append(element("strong", "", row.product_code || "—"));
      identity.append(
        element("span", "", row.product_title || "Không có tên sản phẩm"),
      );
      heading.append(identity);
      heading.append(
        element("span", "sync-audit-type", row.change_type || "UPDATED"),
      );
      card.append(heading);

      const fields = element("div", "sync-audit-fields");
      for (const [field, values] of Object.entries(row.changes || {})) {
        const item = element("div", "sync-audit-field");
        item.append(element("strong", "", auditFieldLabels[field] || field));
        const comparison = element("div", "sync-audit-comparison");
        const before = element("div");
        before.append(element("span", "", "Trước"));
        before.append(element("pre", "", formatAuditValue(values?.before)));
        const after = element("div");
        after.append(element("span", "", "Sau"));
        after.append(element("pre", "", formatAuditValue(values?.after)));
        comparison.append(before, after);
        item.append(comparison);
        fields.append(item);
      }
      card.append(fields);
      list.append(card);
    }

    if (!rows.length) {
      list.append(
        element(
          "p",
          "sync-audit-empty",
          "Lần đồng bộ này không có dữ liệu thay đổi.",
        ),
      );
    }
  }

  function renderInventoryAudit(rows) {
    const list = $("sync-audit-list");
    list.replaceChildren();

    for (const row of rows) {
      const card = element("article", "sync-audit-card inventory-audit-card");
      const heading = element("div", "sync-audit-card-heading");
      const identity = element("div");
      identity.append(element("strong", "", row.product_code || "—"));
      identity.append(
        element("span", "", row.product_title || "Không có tên sản phẩm"),
      );
      heading.append(identity);
      heading.append(
        element(
          "span",
          "sync-audit-type",
          row.variant_title || row.sku || "Variant",
        ),
      );
      card.append(heading);

      const comparison = element("div", "inventory-audit-values");
      comparison.append(
        element(
          "div",
          "",
          `Tồn kho: ${formatAuditValue(row.before_quantity)} → ${formatAuditValue(row.after_quantity)}`,
        ),
      );
      comparison.append(
        element(
          "div",
          "",
          `Có thể bán: ${formatAuditValue(row.before_available)} → ${formatAuditValue(row.after_available)}`,
        ),
      );
      if (row.sku) comparison.append(element("div", "", `SKU: ${row.sku}`));
      card.append(comparison);
      list.append(card);
    }

    if (!rows.length) {
      list.append(
        element(
          "p",
          "sync-audit-empty",
          "Lần đồng bộ này không có biến thể thay đổi.",
        ),
      );
    }
  }

  async function loadSyncAudit() {
    if (!auditRunId) return;
    const prefix =
      auditKind === "inventory"
        ? "/api/v1/products/inventory-sync/history"
        : "/api/v1/products/sync/history";
    const data = await api(
      `${prefix}/${auditRunId}/changes?page=${auditPage}&page_size=${auditPageSize}`,
    );
    auditPage = Number(data.page) || 1;
    auditTotalPages = Math.max(1, Number(data.total_pages) || 1);
    const rows = Array.isArray(data.changes) ? data.changes : [];

    if (auditKind === "inventory") renderInventoryAudit(rows);
    else renderProductAudit(rows);

    $("sync-audit-summary").textContent =
      `${formatNumber(data.total || 0)} thay đổi được lưu trong PostgreSQL.`;
    $("sync-audit-page-info").textContent =
      `Trang ${formatNumber(auditPage)} / ${formatNumber(auditTotalPages)} · ` +
      `${formatNumber(data.total || 0)} thay đổi`;
    $("sync-audit-previous").disabled = auditPage <= 1;
    $("sync-audit-next").disabled = auditPage >= auditTotalPages;
  }

  async function openSyncAudit(kind, runId) {
    auditKind = kind;
    auditRunId = Number(runId);
    auditPage = 1;
    $("sync-audit-title").textContent =
      kind === "inventory"
        ? `Chi tiết đồng bộ tồn kho #${runId}`
        : `Chi tiết đồng bộ sản phẩm #${runId}`;
    $("sync-audit-list").replaceChildren(
      element("p", "sync-audit-empty", "Đang tải thay đổi…"),
    );
    $("sync-audit-dialog").showModal();
    try {
      await loadSyncAudit();
    } catch (error) {
      $("sync-audit-list").replaceChildren(
        element("p", "sync-audit-empty error", error.message),
      );
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
    $("auto-sync-time").value = String(data.sync_time || "02:00").slice(0, 5);
    $("auto-sync-timezone").value = data.timezone || "Asia/Ho_Chi_Minh";
  }

  async function loadDeltaCheckpoint() {
    try {
      const data = await api("/api/v1/products/sync/checkpoint");
      deltaCheckpointState = data;
      $("delta-checkpoint-time").textContent = formatVietnamDate(
        data.last_success_at,
      );
      const detail = [
        `Đọc thực tế từ ${formatVietnamDate(data.query_start_at)}`,
        `${formatNumber(data.mapped_products || 0)} Shopify ID đã ánh xạ`,
      ];
      if (data.latest_adjustment?.reason) {
        detail.push(`Lần chỉnh gần nhất: ${data.latest_adjustment.reason}`);
      }
      $("delta-checkpoint-detail").textContent = detail.join(" · ");
      $("adjust-delta-checkpoint").disabled = false;
    } catch (error) {
      deltaCheckpointState = null;
      $("delta-checkpoint-time").textContent = "Delta chưa bootstrap";
      $("delta-checkpoint-detail").textContent = error.message;
      $("adjust-delta-checkpoint").disabled = true;
    }
  }

  function openDeltaCheckpointDialog() {
    if (!deltaCheckpointState?.last_success_at) return;
    $("delta-checkpoint-input").value = vietnamDateTimeInput(
      deltaCheckpointState.last_success_at,
    );
    $("delta-checkpoint-input").max = vietnamDateTimeInput(new Date());
    $("delta-checkpoint-reason").value = "";
    $("delta-checkpoint-confirm").checked = false;
    updateCheckpointPreview();
    $("delta-checkpoint-dialog").showModal();
  }

  async function saveDeltaCheckpoint(event) {
    event.preventDefault();
    const button = $("save-delta-checkpoint");
    const lastSuccessAt = checkpointInputIso();
    const reason = $("delta-checkpoint-reason").value.trim();
    const confirmed = $("delta-checkpoint-confirm").checked;

    if (!lastSuccessAt || !reason || !confirmed) {
      notice("Vui lòng nhập thời gian, lý do và xác nhận rủi ro.", true);
      return;
    }

    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Đang lưu…";
    try {
      const data = await api("/api/v1/products/sync/checkpoint", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          last_success_at: lastSuccessAt,
          reason,
          confirmed,
        }),
      });
      deltaCheckpointState = data;
      $("delta-checkpoint-dialog").close();
      await loadDeltaCheckpoint();
      notice(
        `Đã đặt Delta checkpoint về ${formatVietnamDate(data.last_success_at)}. ` +
          "Hãy chạy Delta Sync ngay để đọc lại dữ liệu.",
      );
    } catch (error) {
      notice(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  async function loadSyncHistory() {
    const data = await api(
      `/api/v1/products/sync/history?page=${syncHistoryPage}&page_size=${historyPageSize}`,
    );
    syncHistoryPage = Number(data.page) || 1;
    syncHistoryTotalPages = Math.max(1, Number(data.total_pages) || 1);
    renderSyncHistory(Array.isArray(data.jobs) ? data.jobs : []);
    renderHistoryPagination(
      "product",
      syncHistoryPage,
      syncHistoryTotalPages,
      Number(data.total) || 0,
    );
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
      data.inventory_checkpoint_at
        ? `Checkpoint ${formatDate(data.inventory_checkpoint_at)}`
        : "Checkpoint chưa khởi tạo",
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

  function renderInventorySyncHistory(runs) {
    const tbody = $("inventory-sync-history");
    tbody.replaceChildren();

    for (const run of runs) {
      const tr = document.createElement("tr");
      tr.append(createTextCell(`#${run.id}`));
      tr.append(createTextCell(run.trigger_type || "—"));
      tr.append(
        createTextCell(
          run.sync_mode === "delta"
            ? "Đồng bộ tồn kho"
            : "Đồng bộ tồn kho lần đầu",
        ),
      );
      tr.append(createTextCell(syncStatusText(run.status)));
      tr.append(createTextCell(formatNumber(run.checked_products || 0)));
      tr.append(createTextCell(formatNumber(run.checked_variants || 0)));
      tr.append(
        createTextCell(
          `${formatNumber(run.updated_products || 0)} SP · ` +
            `${formatNumber(run.changed_variants || 0)} variant`,
        ),
      );
      tr.append(
        createTextCell(
          `${formatNumber(run.missing_products || 0)} SP · ` +
            `${formatNumber(run.missing_variants || 0)} variant`,
        ),
      );

      const errorCell = createTextCell(run.error_message || "0");
      if (run.error_message) {
        errorCell.className = "inventory-history-error";
        errorCell.title = run.error_message;
      }
      tr.append(errorCell);

      const detailCell = document.createElement("td");
      const changeCount = Number(run.change_count) || 0;
      const detailButton = element(
        "button",
        "sync-audit-open",
        changeCount ? `Xem ${formatNumber(changeCount)}` : "Không có",
      );
      detailButton.type = "button";
      detailButton.disabled = changeCount === 0;
      detailButton.addEventListener("click", () => {
        openSyncAudit("inventory", run.id);
      });
      detailCell.append(detailButton);
      tr.append(detailCell);

      tr.append(createTextCell(formatDate(run.finished_at || run.started_at)));
      tbody.append(tr);
    }

    if (!runs.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 11;
      td.textContent = "Chưa có lịch sử đồng bộ tồn kho.";
      tr.append(td);
      tbody.append(tr);
    }
  }

  async function loadInventorySyncHistory() {
    const data = await api(
      `/api/v1/products/inventory-sync/history?page=${inventoryHistoryPage}&page_size=${historyPageSize}`,
    );
    inventoryHistoryPage = Number(data.page) || 1;
    inventoryHistoryTotalPages = Math.max(1, Number(data.total_pages) || 1);
    renderInventorySyncHistory(Array.isArray(data.runs) ? data.runs : []);
    renderHistoryPagination(
      "inventory",
      inventoryHistoryPage,
      inventoryHistoryTotalPages,
      Number(data.total) || 0,
    );
  }

  async function saveInventorySyncSettings() {
    const button = $("save-inventory-settings");
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Đang lưu…";
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
      notice(
        body.enabled
          ? `Đã lưu lịch đồng bộ tồn kho: chạy mỗi ${body.interval_hours} giờ.`
          : "Đã lưu lịch đồng bộ tồn kho: tự động đang tắt.",
      );
      await loadInventorySyncStatus();
    } catch (error) {
      notice(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  async function runInventorySyncNow() {
    try {
      await api("/api/v1/products/inventory-sync/run", { method: "POST" });
      notice(
        "Đã yêu cầu đồng bộ tồn kho. Worker sẽ chạy sau job Product Sync hiện tại nếu có.",
      );
      await loadInventorySyncStatus();
    } catch (error) {
      notice(error.message, true);
    }
  }

  async function refreshSyncArea() {
    await Promise.all([
      loadSyncStatus(),
      loadSyncHistory(),
      loadDeltaCheckpoint(),
      loadInventorySyncStatus(),
      loadInventorySyncHistory(),
    ]);
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
    const button = $("save-sync-settings");
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Đang lưu…";
    try {
      const body = {
        enabled: $("auto-sync-enabled").checked,
        sync_mode: "existing",
        sync_time: $("auto-sync-time").value || "02:00",
        timezone: $("auto-sync-timezone").value || "Asia/Ho_Chi_Minh",
      };

      await api("/api/v1/products/sync/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      notice(
        body.enabled
          ? `Đã lưu lịch đồng bộ lúc ${body.sync_time}.`
          : "Đã lưu lịch đồng bộ: tự động đang tắt.",
      );
      await loadSyncSettings();
    } catch (error) {
      notice(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  async function loadAll() {
    notice("");

    await Promise.all([
      loadCollections(),
      loadProductFilters(),
      loadProducts(),
      loadSyncSettings(),
      loadSyncStatus(),
      loadSyncHistory(),
      loadDeltaCheckpoint(),
      loadInventorySyncStatus(),
      loadInventorySyncHistory(),
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
    resetProductPagination();
    $("product-search-clear").hidden = !searchQuery;

    await loadProducts();
  });

  $("product-search-clear").addEventListener("click", async () => {
    $("product-search-input").value = "";
    searchQuery = "";
    resetProductPagination();
    $("product-search-clear").hidden = true;

    await loadProducts();
    $("product-search-input").focus();
  });

  async function applyProductFilters() {
    selectedProductType = $("product-type-filter").value;
    selectedStatus = $("product-status-filter").value;
    resetProductPagination();
    updateFilterClearButton();
    await loadProducts();
  }

  $("product-type-filter").addEventListener("change", applyProductFilters);
  $("product-status-filter").addEventListener("change", applyProductFilters);
  $("product-filter-clear").addEventListener("click", async () => {
    selectedProductType = "";
    selectedStatus = "";
    $("product-type-filter").value = "";
    $("product-status-filter").value = "";
    resetProductPagination();
    updateFilterClearButton();
    await loadProducts();
  });

  $("sync-existing").addEventListener("click", () => enqueueSync("existing"));
  $("sync-refresh").addEventListener("click", refreshSyncArea);
  $("save-sync-settings").addEventListener("click", saveSyncSettings);
  $("adjust-delta-checkpoint").addEventListener(
    "click",
    openDeltaCheckpointDialog,
  );
  $("delta-checkpoint-input").addEventListener(
    "input",
    updateCheckpointPreview,
  );
  $("delta-checkpoint-form").addEventListener("submit", saveDeltaCheckpoint);
  $("close-delta-checkpoint").addEventListener("click", () => {
    $("delta-checkpoint-dialog").close();
  });
  $("cancel-delta-checkpoint").addEventListener("click", () => {
    $("delta-checkpoint-dialog").close();
  });
  $("delta-checkpoint-dialog").addEventListener("click", (event) => {
    if (event.target === $("delta-checkpoint-dialog")) {
      $("delta-checkpoint-dialog").close();
    }
  });
  $("sync-panel-toggle").addEventListener("click", () => {
    setSyncPanelCollapsed(!$("sync-panel").classList.contains("is-collapsed"));
  });

  $("save-inventory-settings").addEventListener(
    "click",
    saveInventorySyncSettings,
  );
  $("inventory-sync-now").addEventListener("click", runInventorySyncNow);

  $("sync-history-previous").addEventListener("click", async () => {
    if (syncHistoryPage <= 1) return;
    syncHistoryPage -= 1;
    await loadSyncHistory();
  });
  $("sync-history-next").addEventListener("click", async () => {
    if (syncHistoryPage >= syncHistoryTotalPages) return;
    syncHistoryPage += 1;
    await loadSyncHistory();
  });
  $("inventory-history-previous").addEventListener("click", async () => {
    if (inventoryHistoryPage <= 1) return;
    inventoryHistoryPage -= 1;
    await loadInventorySyncHistory();
  });
  $("inventory-history-next").addEventListener("click", async () => {
    if (inventoryHistoryPage >= inventoryHistoryTotalPages) return;
    inventoryHistoryPage += 1;
    await loadInventorySyncHistory();
  });

  $("close-sync-audit").addEventListener("click", () => {
    $("sync-audit-dialog").close();
  });
  $("sync-audit-previous").addEventListener("click", async () => {
    if (auditPage <= 1) return;
    auditPage -= 1;
    await loadSyncAudit();
  });
  $("sync-audit-next").addEventListener("click", async () => {
    if (auditPage >= auditTotalPages) return;
    auditPage += 1;
    await loadSyncAudit();
  });
  $("sync-audit-dialog").addEventListener("click", (event) => {
    if (event.target === $("sync-audit-dialog")) {
      $("sync-audit-dialog").close();
    }
  });

  $("next").addEventListener("click", async () => {
    if (busy || currentPage >= totalPages) return;
    currentPage += 1;
    await loadProducts();
  });

  $("previous").addEventListener("click", async () => {
    if (busy || currentPage <= 1) return;
    currentPage -= 1;
    await loadProducts();
  });

  $("page-size").addEventListener("change", async () => {
    pageSize = Number($("page-size").value) || 30;
    resetProductPagination();
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

  restoreSyncPanelState();
  setConnection(false);

  tryLocalAdmin();
})();
