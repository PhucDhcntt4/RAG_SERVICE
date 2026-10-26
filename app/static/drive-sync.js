"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const historyPageSize = 10;
  const dialogPageSize = 10;
  let key = "";
  let localAdmin = false;
  let overview = null;
  let historyPage = 1;
  let historyTotalPages = 1;
  let dialogJobId = null;
  let dialogPage = 1;
  let dialogTotalPages = 1;
  let refreshTimer = null;
  let noticeTimer = null;

  const statusLabels = {
    pending: "Đang chờ",
    running: "Đang chạy",
    completed: "Hoàn tất",
    partial: "Một phần",
    failed: "Thất bại",
    discovered: "Đã phát hiện",
    processing: "Đang xử lý",
    skipped: "Bỏ qua",
    error: "Lỗi",
  };

  function element(tag, className = "", text = undefined) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("vi-VN") : "—";
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh" });
  }

  function statusPill(status, override) {
    return element(
      "span",
      `status-pill ${status || ""}`,
      override || statusLabels[status] || status || "—",
    );
  }

  function notify(message, isError = false) {
    const box = $("notice");
    if (noticeTimer) window.clearTimeout(noticeTimer);
    box.textContent = message;
    box.classList.toggle("error", isError);
    box.hidden = !message;
    if (message) {
      noticeTimer = window.setTimeout(() => {
        box.hidden = true;
      }, 6000);
    }
  }

  function errorMessage(body, status) {
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail.map((item) => item.msg).join("; ");
    }
    return `Yêu cầu thất bại (${status}).`;
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
            ? { "X-RAG-Local-UI": "1" }
            : { Authorization: `Bearer ${key}` }),
        },
        cache: "no-store",
      });
    } catch {
      throw new Error("Không thể kết nối RAG Service.");
    }
    let body = null;
    try {
      body = await response.json();
    } catch {
      throw new Error("Service trả về dữ liệu không hợp lệ.");
    }
    if (!response.ok) throw new Error(errorMessage(body, response.status));
    return body;
  }

  function setConnected(connected) {
    $("login-panel").hidden = connected;
    $("dashboard").hidden = !connected;
    $("refresh").disabled = !connected;
    $("disconnect").hidden = !connected || localAdmin;
    $("connection").textContent = connected ? "Đã kết nối" : "Chưa kết nối";
    $("connection").classList.toggle("online", connected);
    if (connected) startPolling();
    else stopPolling();
  }

  function renderHealth(data) {
    const worker = data.worker || {};
    const online = Boolean(worker.online);
    $("worker-symbol").classList.toggle("offline", !online);
    $("worker-title").textContent = online ? "Worker đang hoạt động" : "Worker đang ngoại tuyến";
    $("worker-detail").textContent = online
      ? worker.current_job_id
        ? `Đang xử lý job #${worker.current_job_id}`
        : `Sẵn sàng nhận job · Instance ${String(worker.instance_id || "").slice(0, 8)}`
      : data.enabled
        ? "Hãy kiểm tra Terminal chạy app.drive_sync.worker"
        : "GOOGLE_DRIVE_SYNC_ENABLED đang tắt";
    $("source-count").textContent = `${data.sources.length} / ${data.configured_folder_count}`;
    const active = data.sources.filter((source) =>
      ["pending", "running"].includes(source.latest_job_status),
    ).length;
    $("active-job-count").textContent = formatNumber(active);
    $("heartbeat-at").textContent = formatDate(worker.heartbeat_at);
    $("poll-minutes").value = data.settings?.poll_minutes || 1;
  }

  function metaItem(label, value) {
    const box = element("div");
    box.append(element("span", "", label), element("strong", "", value));
    return box;
  }

  function renderSources(sources) {
    const grid = $("source-grid");
    grid.replaceChildren();
    $("source-empty").hidden = sources.length > 0;

    for (const source of sources) {
      const card = element("article", `source-card${source.enabled ? "" : " is-disabled"}`);
      card.dataset.sourceId = source.id;

      const top = element("div", "source-top");
      const department = element("div", "department");
      const mark = element("div", "department-mark", source.department_code || "—");
      const identity = element("div");
      identity.append(
        element("h3", "", source.department_name || source.folder_name),
        element("p", "", `${source.department_code} · ${source.folder_name}`),
      );
      department.append(mark, identity);
      top.append(
        department,
        statusPill(source.enabled ? "completed" : "disabled", source.enabled ? "Đang bật" : "Tạm dừng"),
      );

      const stats = element("div", "source-stats");
      for (const [label, value] of [
        ["TÀI LIỆU", source.total_documents],
        ["ĐANG ACTIVE", source.active_documents],
        ["LỖI", source.error_documents],
      ]) {
        const item = element("div");
        item.append(element("span", "", label), element("strong", "", formatNumber(value)));
        stats.append(item);
      }

      const meta = element("div", "source-meta");
      meta.append(
        metaItem("Lần quét cuối", formatDate(source.last_scan_at)),
        metaItem("Job gần nhất", source.latest_job_id ? `#${source.latest_job_id} · ${statusLabels[source.latest_job_status] || source.latest_job_status}` : "—"),
        metaItem("Thành công cuối", formatDate(source.last_success_at)),
        metaItem("Folder ID", source.folder_id),
      );
      if (source.last_error) meta.append(element("div", "source-error", source.last_error));

      const controls = element("div", "source-controls");
      const toggleLabel = element("label", "toggle-control");
      const toggle = element("input");
      toggle.type = "checkbox";
      toggle.checked = source.enabled;
      toggle.dataset.role = "enabled";
      toggleLabel.append(toggle, document.createTextNode(" BẬT TỰ ĐỘNG"));

      const buttons = element("div");
      const save = element("button", "secondary", "Lưu");
      save.type = "button";
      save.dataset.action = "save";
      const run = element("button", "primary", "Đồng bộ ngay");
      run.type = "button";
      run.dataset.action = "run";
      run.disabled = !source.enabled;
      buttons.style.display = "flex";
      buttons.style.gap = "8px";
      buttons.append(save, run);
      controls.append(toggleLabel, buttons);

      card.append(top, stats, meta, controls);
      grid.append(card);
    }
  }

  function populateSourceFilter(sources) {
    const select = $("history-source");
    const current = select.value;
    select.replaceChildren(new Option("Tất cả phòng ban", ""));
    for (const source of sources) {
      select.append(new Option(`${source.department_code} · ${source.department_name}`, source.id));
    }
    if ([...select.options].some((option) => option.value === current)) select.value = current;
  }

  function renderHistory(data) {
    const body = $("history-body");
    body.replaceChildren();
    $("history-empty").hidden = data.items.length > 0;
    for (const job of data.items) {
      const row = element("tr");
      const statusCell = element("td");
      statusCell.append(statusPill(job.status));
      const result = element("td", "job-result");
      result.append(
        element("strong", "", `${formatNumber(job.imported_files)} import · ${formatNumber(job.skipped_files)} bỏ qua`),
        element("small", "", `${formatNumber(job.discovered_files)} phát hiện · ${formatNumber(job.failed_files)} lỗi`),
      );
      const action = element("td");
      const detail = element("button", "secondary detail-button", "Xem chi tiết");
      detail.type = "button";
      detail.dataset.jobId = job.id;
      action.append(detail);
      row.append(
        element("td", "", `#${job.id}`),
        element("td", "", job.department_code),
        element("td", "", job.trigger_type === "scheduled" ? "Tự động" : "Thủ công"),
        statusCell,
        result,
        element("td", "", formatDate(job.started_at || job.created_at)),
        action,
      );
      body.append(row);
    }
    historyPage = data.page;
    historyTotalPages = data.total_pages;
    $("history-page-info").textContent = `Trang ${data.page} / ${data.total_pages} · ${formatNumber(data.total)} job`;
    $("history-prev").disabled = data.page <= 1;
    $("history-next").disabled = data.page >= data.total_pages;
  }

  async function loadOverview() {
    const data = await api("/api/v1/drive-sync/overview");
    overview = data;
    renderHealth(data);
    renderSources(data.sources || []);
    populateSourceFilter(data.sources || []);
  }

  async function loadHistory() {
    const params = new URLSearchParams({ page: historyPage, page_size: historyPageSize });
    if ($("history-source").value) params.set("source_id", $("history-source").value);
    if ($("history-status").value) params.set("status", $("history-status").value);
    const data = await api(`/api/v1/drive-sync/jobs?${params}`);
    renderHistory(data);
  }

  async function loadAll({ silent = false } = {}) {
    try {
      await Promise.all([loadOverview(), loadHistory()]);
      setConnected(true);
    } catch (error) {
      if (!silent) notify(error.message, true);
      throw error;
    }
  }

  async function saveSource(card, button) {
    const sourceId = Number(card.dataset.sourceId);
    const enabled = card.querySelector('[data-role="enabled"]').checked;
    button.disabled = true;
    try {
      await api(`/api/v1/drive-sync/sources/${sourceId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      notify("Đã lưu cấu hình nguồn Google Drive.");
      await loadOverview();
    } catch (error) {
      notify(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function saveSchedule() {
    const button = $("save-schedule");
    const pollMinutes = Number($("poll-minutes").value);
    if (!Number.isInteger(pollMinutes) || pollMinutes < 1 || pollMinutes > 1440) {
      notify("Chu kỳ quét phải từ 1 đến 1.440 phút.", true);
      return;
    }
    button.disabled = true;
    try {
      const settings = await api("/api/v1/drive-sync/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ poll_minutes: pollMinutes }),
      });
      $("poll-minutes").value = settings.poll_minutes;
      notify(`Đã áp dụng chu kỳ ${settings.poll_minutes} phút cho tất cả nguồn đồng bộ.`);
    } catch (error) {
      notify(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function runSource(card, button) {
    const sourceId = Number(card.dataset.sourceId);
    button.disabled = true;
    try {
      const job = await api(`/api/v1/drive-sync/sources/${sourceId}/run`, { method: "POST" });
      notify(
        job.status === "running"
          ? `Nguồn này đang được xử lý trong job #${job.id}.`
          : `Job #${job.id} đã được đưa vào hàng đợi.`,
      );
      historyPage = 1;
      await Promise.all([loadOverview(), loadHistory()]);
    } catch (error) {
      notify(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function summaryChip(label, value) {
    return element("span", "summary-chip", `${label}: ${value}`);
  }

  function renderDialogFiles(data) {
    const list = $("dialog-files");
    list.replaceChildren();
    $("dialog-empty").hidden = data.items.length > 0;
    for (const file of data.items) {
      const card = element("article", "file-card");
      const head = element("div", "file-head");
      const title = element("div");
      title.append(
        element("h3", "", file.original_file_name),
        element("p", "", file.document_code ? `${file.document_code} · V${file.document_version || "—"}` : file.drive_file_id),
      );
      head.append(title, statusPill(file.status));
      const grid = element("div", "file-grid");
      for (const [label, value] of [
        ["PHẠM VI", String(file.drive_scope || "—").toUpperCase()],
        ["HÀNH ĐỘNG", file.sync_action || "—"],
        ["THƯ MỤC ĐÍCH", file.drive_stage || "—"],
        ["ACTIVE", file.is_active ? "Có" : "Không"],
      ]) {
        const item = element("div");
        item.append(element("span", "", label), document.createTextNode(value));
        grid.append(item);
      }
      card.append(head, grid);
      const error = file.error_message || file.move_error;
      if (error) card.append(element("div", "file-error", `${file.error_code ? `${file.error_code}: ` : ""}${error}`));
      list.append(card);
    }
    dialogPage = data.page;
    dialogTotalPages = data.total_pages;
    $("dialog-page-info").textContent = `Trang ${data.page} / ${data.total_pages} · ${formatNumber(data.total)} file`;
    $("dialog-prev").disabled = data.page <= 1;
    $("dialog-next").disabled = data.page >= data.total_pages;
  }

  async function loadDialog() {
    const [job, documents] = await Promise.all([
      api(`/api/v1/drive-sync/jobs/${dialogJobId}`),
      api(`/api/v1/drive-sync/jobs/${dialogJobId}/documents?page=${dialogPage}&page_size=${dialogPageSize}`),
    ]);
    $("dialog-title").textContent = `Đồng bộ ${job.department_code} · Job #${job.id}`;
    $("dialog-subtitle").textContent = `${job.trigger_type === "scheduled" ? "Tự động" : "Thủ công"} · ${formatDate(job.started_at || job.created_at)}`;
    const summary = $("dialog-summary");
    summary.replaceChildren(
      statusPill(job.status),
      summaryChip("Phát hiện", formatNumber(job.discovered_files)),
      summaryChip("Đã kiểm tra", formatNumber(job.processed_files)),
      summaryChip("Import", formatNumber(job.imported_files)),
      summaryChip("Bỏ qua", formatNumber(job.skipped_files)),
      summaryChip("Lỗi", formatNumber(job.failed_files)),
    );
    if (job.error_message) summary.append(element("span", "summary-chip", job.error_message));
    renderDialogFiles(documents);
  }

  async function openDialog(jobId) {
    dialogJobId = jobId;
    dialogPage = 1;
    try {
      await loadDialog();
      $("job-dialog").showModal();
    } catch (error) {
      notify(error.message, true);
    }
  }

  function startPolling() {
    stopPolling();
    refreshTimer = window.setInterval(async () => {
      try { await loadAll({ silent: true }); } catch { /* giữ dữ liệu gần nhất */ }
    }, 15000);
  }

  function stopPolling() {
    if (refreshTimer) window.clearInterval(refreshTimer);
    refreshTimer = null;
  }

  async function tryLocalAdmin() {
    const localHosts = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
    if (!localHosts.has(window.location.hostname)) {
      setConnected(false);
      return;
    }
    localAdmin = true;
    try {
      await loadAll();
    } catch {
      localAdmin = false;
      setConnected(false);
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    key = $("admin-key").value.trim();
    localAdmin = false;
    $("login-error").textContent = "";
    if (!key) return;
    try {
      await loadAll();
      $("admin-key").value = "";
    } catch (error) {
      $("login-error").textContent = error.message;
      setConnected(false);
    }
  });

  $("toggle-key").addEventListener("click", () => {
    const input = $("admin-key");
    const visible = input.type === "text";
    input.type = visible ? "password" : "text";
    $("toggle-key").textContent = visible ? "Hiện" : "Ẩn";
  });
  $("disconnect").addEventListener("click", () => {
    key = "";
    localAdmin = false;
    setConnected(false);
  });
  $("refresh").addEventListener("click", async () => {
    $("refresh").disabled = true;
    try { await loadAll(); } catch (error) { notify(error.message, true); }
    finally { $("refresh").disabled = false; }
  });
  $("save-schedule").addEventListener("click", saveSchedule);
  $("source-grid").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const card = button.closest(".source-card");
    if (button.dataset.action === "save") saveSource(card, button);
    if (button.dataset.action === "run") runSource(card, button);
  });
  $("history-source").addEventListener("change", async () => { historyPage = 1; await loadHistory(); });
  $("history-status").addEventListener("change", async () => { historyPage = 1; await loadHistory(); });
  $("history-prev").addEventListener("click", async () => { if (historyPage > 1) { historyPage -= 1; await loadHistory(); } });
  $("history-next").addEventListener("click", async () => { if (historyPage < historyTotalPages) { historyPage += 1; await loadHistory(); } });
  $("history-body").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-job-id]");
    if (button) openDialog(Number(button.dataset.jobId));
  });
  $("dialog-close").addEventListener("click", () => $("job-dialog").close());
  $("dialog-prev").addEventListener("click", async () => { if (dialogPage > 1) { dialogPage -= 1; await loadDialog(); } });
  $("dialog-next").addEventListener("click", async () => { if (dialogPage < dialogTotalPages) { dialogPage += 1; await loadDialog(); } });
  $("job-dialog").addEventListener("click", (event) => {
    if (event.target === $("job-dialog")) $("job-dialog").close();
  });

  tryLocalAdmin();
})();
