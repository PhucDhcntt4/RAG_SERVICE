"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const pageSize = 20;
  let taxonomy = [],
    groups = new Map(),
    docTypes = new Map();
  let key = "",
    localAdmin = false,
    offset = 0,
    filterTypeId = "",
    filterGroupId = "",
    rows = [],
    hasNext = false,
    busy = false,
    deleting = null,
    sourceKey = "",
    updating = false;
  let previewRequest = null,
    previewVersion = 0;

  function showKey(visible) {
    $("admin-key").type = visible ? "text" : "password";
    $("toggle-key").textContent = visible ? "Ẩn" : "Hiện";
    $("toggle-key").setAttribute(
      "aria-label",
      visible ? "Ẩn admin key" : "Hiện admin key",
    );
  }
  $("toggle-key").addEventListener("click", () =>
    showKey($("admin-key").type === "password"),
  );

  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
    $("notice").hidden = !message;
  }
  function controls() {
    for (const id of [
      "add-document",
      "empty-add",
      "refresh",
      "disconnect",
      "test-rag",
      "filter-type",
      "filter-group",
    ])
      $(id).disabled = busy || !(key || localAdmin);
    $("previous").disabled = busy || !(key || localAdmin) || offset === 0;
    $("next").disabled = busy || !(key || localAdmin) || !hasNext;
    $("connect-button").disabled = busy;
    for (const button of document.querySelectorAll(
      ".delete-button, .download-button, .document-open, .classification-button",
    ))
      button.disabled = busy;
  }
  function connection(connected) {
    $("login-panel").hidden = connected;
    $("disconnect").hidden = !connected || localAdmin;
    $("connection").textContent = connected ? "Đã kết nối" : "Chưa kết nối";
    $("connection").classList.toggle("online", connected);
    controls();
  }
  function reset() {
    closePreview();
    resetChat();
    showKey(false);
    key = "";
    localAdmin = false;
    rows = [];
    taxonomy = [];
    groups = new Map();
    docTypes = new Map();
    offset = 0;
    filterTypeId = "";
    filterGroupId = "";
    const allTypes = element("option", "", "Tất cả loại");
    allTypes.value = "";
    const allGroups = element("option", "", "Tất cả nhóm");
    allGroups.value = "";
    $("filter-type").replaceChildren(allTypes);
    $("filter-group").replaceChildren(allGroups);
    hasNext = false;
    $("documents").replaceChildren();
    $("table-wrap").hidden = true;
    $("empty").hidden = false;
    $("empty-title").textContent = "Kho kiến thức của bạn bắt đầu từ đây";
    $("empty-description").textContent =
      "Kết nối để xem danh sách và thêm tài liệu cho trợ lý.";
    $("empty-add").hidden = true;
    $("page-info").textContent = "Chưa tải danh sách";
    $("page-count").textContent = "—";
    connection(false);
  }
  async function api(path, options = {}, raw = false) {
    let response;
    try {
      response = await fetch(
        localAdmin ? path.replace(/^\/api\//, "/admin-api/") : path,
        {
          ...options,
          headers: {
            ...options.headers,
            ...(localAdmin
              ? { "X-RAG-Local-UI": "1" }
              : { Authorization: `Bearer ${key}` }),
          },
          cache: "no-store",
        },
      );
    } catch {
      throw new Error(
        "Không thể kết nối service. Kiểm tra service đang chạy. Nếu vừa thêm hoặc xóa tài liệu, hãy làm mới danh sách để kiểm tra kết quả trước khi thử lại.",
      );
    }
    if (response.ok && raw) return response;
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error(
        "Service trả về kết quả không hợp lệ. Hãy làm mới danh sách để kiểm tra.",
      );
    }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        reset();
        throw new Error(
          response.status === 401
            ? "Admin key không hợp lệ. Hãy nhập lại ADMIN_API_KEY trong .env."
            : "Key này không có quyền quản lý tài liệu. Hãy dùng ADMIN_API_KEY.",
        );
      }
      const detail = Array.isArray(body.detail)
        ? body.detail.map((e) => e.msg).join("\n")
        : body.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : `Yêu cầu thất bại (${response.status}).`,
      );
    }
    return body;
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function groupName(doc) {
    return (
      groups.get(Number(doc.group_id))?.name ||
      [...groups.values()].find((item) => item.code === doc.category)?.name ||
      doc.category ||
      "—"
    );
  }
  function typeName(doc) {
    const direct = docTypes.get(Number(doc.doc_type_id));
    if (direct) return direct.name;
    const group =
      groups.get(Number(doc.group_id)) ||
      [...groups.values()].find((item) => item.code === doc.category);
    return docTypes.get(Number(group?.doc_type_id))?.name || "—";
  }
  function fillTypeOptions(select, activeOnly = true) {
    const selected = select.value;
    select.replaceChildren();
    for (const item of taxonomy) {
      if (activeOnly && !item.is_active) continue;
      const option = element("option", "", item.name);
      option.value = String(item.id);
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === selected))
      select.value = selected;
  }
  function fillGroupOptions(
    docTypeId,
    selected = "",
    select = $("document-category"),
  ) {
    select.replaceChildren();
    for (const item of groups.values()) {
      if (!item.is_active || Number(item.doc_type_id) !== Number(docTypeId))
        continue;
      const option = element("option", "", item.name);
      option.value = String(item.id);
      option.dataset.code = item.code;
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === String(selected)))
      select.value = String(selected);
  }
  function fillFilterTypeOptions() {
    const selected = filterTypeId;
    const select = $("filter-type");
    const all = element("option", "", "Tất cả loại");
    all.value = "";
    select.replaceChildren(all);
    for (const item of taxonomy) {
      if (!item.is_active) continue;
      const option = element("option", "", item.name);
      option.value = String(item.id);
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === selected))
      select.value = selected;
    else filterTypeId = "";
  }
  function fillFilterGroupOptions() {
    const selected = filterGroupId;
    const select = $("filter-group");
    const all = element("option", "", "Tất cả nhóm");
    all.value = "";
    select.replaceChildren(all);
    for (const item of groups.values()) {
      if (!item.is_active) continue;
      if (filterTypeId && Number(item.doc_type_id) !== Number(filterTypeId))
        continue;
      const option = element("option", "", item.name);
      option.value = String(item.id);
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === selected))
      select.value = selected;
    else filterGroupId = "";
  }
  async function loadTaxonomy() {
    const data = await api("/api/v1/taxonomy");
    taxonomy = Array.isArray(data.doc_types) ? data.doc_types : [];
    docTypes = new Map(taxonomy.map((item) => [Number(item.id), item]));
    groups = new Map(
      taxonomy
        .flatMap((item) => item.groups || [])
        .map((item) => [Number(item.id), item]),
    );
    fillTypeOptions($("document-type"));
    fillGroupOptions($("document-type").value);
    fillFilterTypeOptions();
    fillFilterGroupOptions();
    fillChatTypeOptions();
    fillChatGroupOptions();
    updateChatScopeSummary();
  }
  function clearPreview() {
    previewVersion += 1;
    previewRequest?.abort();
    previewRequest = null;
    $("preview-content").textContent = "";
    $("preview-content").hidden = true;
    $("preview-error").textContent = "";
    $("preview-meta").textContent = "";
    $("preview-title").textContent = "";
    $("preview-loading").hidden = true;
  }
  function closePreview() {
    clearPreview();
    if ($("preview-dialog").open) $("preview-dialog").close();
  }
  async function openPreview(doc) {
    if (busy || !(key || localAdmin)) return;
    clearPreview();
    const version = previewVersion;
    const controller = new AbortController();
    previewRequest = controller;
    $("preview-title").textContent = doc.title;
    $("preview-meta").textContent = `${typeName(doc)} · ${groupName(doc)}`;
    $("preview-loading").hidden = false;
    $("preview-dialog").showModal();
    $("close-preview").focus();
    try {
      const data = await api(`/api/v1/documents/${doc.id}`, {
        signal: controller.signal,
      });
      if (version !== previewVersion || !$("preview-dialog").open) return;
      const content =
        typeof data.source_text === "string" ? data.source_text : "";
      $("preview-title").textContent = data.title;
      $("preview-meta").textContent = [
        typeName(data),
        groupName(data),
        data.file_name,
        `${content.length.toLocaleString("vi-VN")} ký tự`,
      ]
        .filter(Boolean)
        .join(" · ");
      // Render as plain text, including HTML/Markdown from uploaded documents.
      $("preview-content").textContent =
        content || "Tài liệu chưa có nội dung văn bản.";
      $("preview-content").hidden = false;
      $("preview-content").scrollTop = 0;
      $("preview-dialog").scrollTop = 0;
    } catch (error) {
      if (!(key || localAdmin)) {
        notice(error.message, true);
        return;
      }
      if (controller.signal.aborted) return;
      if (version === previewVersion && $("preview-dialog").open)
        $("preview-error").textContent = error.message;
    } finally {
      if (version === previewVersion) {
        $("preview-loading").hidden = true;
        previewRequest = null;
      }
    }
  }
  for (const id of ["close-preview", "done-preview"])
    $(id).addEventListener("click", closePreview);
  $("preview-dialog").addEventListener("close", () => {
    if (!$("preview-dialog").open) clearPreview();
  });
  function render() {
    $("documents").replaceChildren();
    for (const doc of rows) {
      const tr = element("tr");
      const file = element("div", "document-cell");
      file.append(element("span", "file-icon", "DOC"));
      const info = element("div");
      const view = element("button", "document-name document-open", doc.title);
      view.type = "button";
      view.setAttribute("aria-label", `Xem nội dung ${doc.title}`);
      view.setAttribute("aria-haspopup", "dialog");
      view.addEventListener("click", () => openPreview(doc));
      info.append(view);
      info.append(
        element(
          "div",
          "document-meta",
          `${doc.chunk_count} đoạn · ${doc.file_name || doc.source_key}`,
        ),
      );
      if (doc.file_origin === "recovered_text")
        info.append(
          element("div", "document-meta", "TXT xuất từ văn bản đã lưu"),
        );
      file.append(info);
      const nameCell = element("td");
      nameCell.append(file);
      tr.append(nameCell);
      const docType = element("td");
      docType.append(element("span", "category", typeName(doc)));
      tr.append(docType);
      const category = element("td");
      category.append(element("span", "category", groupName(doc)));
      tr.append(category);
      const status = element("td");
      status.append(
        element(
          "span",
          `status${doc.is_active ? "" : " inactive"}`,
          doc.is_active ? "Đang sử dụng" : "Đã tạm dừng",
        ),
      );
      tr.append(status);
      const date = new Date(doc.updated_at);
      tr.append(
        element(
          "td",
          "date",
          Number.isNaN(date.getTime())
            ? "—"
            : date.toLocaleString("vi-VN", {
                day: "2-digit",
                month: "2-digit",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              }),
        ),
      );
      const actions = element("td");
      if (doc.has_local_file) {
        const download = element("button", "download-button", "Tải về");
        download.type = "button";
        download.setAttribute("aria-label", `Tải về ${doc.title}`);
        download.addEventListener("click", () =>
          task(async () => {
            try {
              const response = await api(
                `/api/v1/documents/${doc.id}/download`,
                {},
                true,
              );
              const blob = await response.blob();
              const url = URL.createObjectURL(blob);
              const link = element("a");
              link.href = url;
              link.download = doc.file_name || `document-${doc.id}.txt`;
              document.body.append(link);
              link.click();
              link.remove();
              setTimeout(() => URL.revokeObjectURL(url), 60000);
            } catch (error) {
              notice(error.message, true);
            }
          }),
        );
        actions.append(download);
      }
      const update = element("button", "download-button", "Cập nhật file");
      update.type = "button";
      update.setAttribute("aria-label", `Cập nhật file ${doc.title}`);
      update.addEventListener("click", () => openUpload(doc));
      actions.append(update);
      const classify = element("button", "classification-button", "Đổi nhóm");
      classify.type = "button";
      classify.addEventListener("click", () => openClassification(doc));
      actions.append(classify);
      const remove = element("button", "delete-button", "Xóa");
      remove.type = "button";
      remove.setAttribute("aria-label", `Xóa ${doc.title}`);
      remove.addEventListener("click", () => {
        deleting = doc;
        $("delete-name").textContent = doc.title;
        $("delete-error").textContent = "";
        $("delete-dialog").showModal();
        $("cancel-delete").focus();
      });
      actions.append(remove);
      tr.append(actions);
      $("documents").append(tr);
    }
    $("empty").hidden = rows.length > 0;
    $("empty-title").textContent = offset
      ? "Không còn tài liệu ở trang này"
      : "Chưa có tài liệu nào";
    $("empty-description").textContent =
      "Thêm tài liệu đầu tiên để xây dựng kho kiến thức cho trợ lý.";
    $("empty-add").hidden = false;
    $("table-wrap").hidden = rows.length === 0;
    $("page-count").textContent = `${rows.length} trên trang`;
    $("page-info").textContent = rows.length
      ? `Hiển thị ${offset + 1}–${offset + rows.length} · Trang ${Math.floor(offset / pageSize) + 1}`
      : "0 tài liệu trên trang";
    controls();
  }
  async function load() {
    $("loading").hidden = false;
    $("empty").hidden = true;
    $("table-wrap").hidden = true;
    try {
      await loadTaxonomy();
      const filterParams =
        (filterTypeId ? `&doc_type_id=${filterTypeId}` : "") +
        (filterGroupId ? `&group_id=${filterGroupId}` : "");
      let data = await api(
        `/api/v1/documents?limit=${pageSize + 1}&offset=${offset}${filterParams}`,
      );
      if (!data.documents.length && offset > 0) {
        offset = Math.max(0, offset - pageSize);
        data = await api(
          `/api/v1/documents?limit=${pageSize + 1}&offset=${offset}${filterParams}`,
        );
      }
      hasNext = data.documents.length > pageSize;
      rows = data.documents.slice(0, pageSize);
      connection(true);
      render();
    } catch (error) {
      $("documents").replaceChildren();
      $("page-count").textContent = "—";
      $("page-info").textContent = "Chưa tải được danh sách";
      $("empty").hidden = false;
      $("empty-title").textContent = "Chưa tải được tài liệu";
      $("empty-description").textContent = "Kiểm tra kết nối và thử lại.";
      $("empty-add").hidden = true;
      hasNext = false;
      throw error;
    } finally {
      $("loading").hidden = true;
    }
  }
  async function task(action) {
    if (busy) return;
    busy = true;
    controls();
    try {
      await action();
    } finally {
      busy = false;
      controls();
    }
  }
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      key = $("admin-key").value.trim();
      $("login-error").textContent = "";
      if (key.length < 32 || !/^[\x21-\x7e]+$/.test(key)) {
        key = "";
        $("login-error").textContent =
          "Nhập giá trị admin key, không kèm chữ Bearer hoặc khoảng trắng.";
        return;
      }
      $("connect-button").textContent = "Đang kết nối…";
      try {
        await load();
        $("admin-key").value = "";
        showKey(false);
        notice("");
      } catch (error) {
        reset();
        $("login-error").textContent = error.message;
      } finally {
        $("connect-button").textContent = "Kết nối";
      }
    });
  });
  $("disconnect").addEventListener("click", () => {
    reset();
    notice("");
    $("admin-key").focus();
  });
  for (const [id, change] of [
    ["refresh", 0],
    ["previous", -pageSize],
    ["next", pageSize],
  ]) {
    $(id).addEventListener("click", () =>
      task(async () => {
        offset = Math.max(0, offset + change);
        notice("");
        try {
          await load();
        } catch (error) {
          notice(error.message, true);
        }
      }),
    );
  }
  $("filter-type").addEventListener("change", () =>
    task(async () => {
      filterTypeId = $("filter-type").value;
      filterGroupId = "";
      fillFilterGroupOptions();
      offset = 0;
      notice("");
      try {
        await load();
      } catch (error) {
        notice(error.message, true);
      }
    }),
  );
  $("filter-group").addEventListener("change", () =>
    task(async () => {
      filterGroupId = $("filter-group").value;
      offset = 0;
      notice("");
      try {
        await load();
      } catch (error) {
        notice(error.message, true);
      }
    }),
  );
  function openUpload(doc = null) {
    $("upload-form").reset();
    $("upload-error").textContent = "";
    fillTypeOptions($("document-type"));
    $("file-label").textContent = "Chọn file hoặc kéo thả vào đây";
    $("file-description").textContent = "TXT, MD hoặc PDF có lớp văn bản";
    updating = Boolean(doc);
    sourceKey = doc ? doc.source_key : `upload/${crypto.randomUUID()}`;
    $("upload-title").textContent = updating
      ? "Cập nhật tài liệu"
      : "Thêm tài liệu";
    $("submit-upload").textContent = updating
      ? "Cập nhật tài liệu"
      : "Thêm tài liệu";
    if (doc) {
      $("document-title").value = doc.title;
      const group =
        groups.get(Number(doc.group_id)) ||
        [...groups.values()].find((item) => item.code === doc.category);
      if (group) $("document-type").value = String(group.doc_type_id);
      fillGroupOptions($("document-type").value, group?.id);
      $("file-description").textContent =
        "Chọn file để thay nội dung và tạo lại dữ liệu tìm kiếm của tài liệu này.";
    } else fillGroupOptions($("document-type").value);
    $("upload-dialog").showModal();
  }
  $("add-document").addEventListener("click", () => openUpload());
  $("empty-add").addEventListener("click", () => openUpload());
  for (const id of ["close-upload", "cancel-upload"])
    $(id).addEventListener("click", () => {
      if (!busy) $("upload-dialog").close();
    });
  $("cancel-delete").addEventListener("click", () => {
    if (!busy) $("delete-dialog").close();
  });
  for (const id of ["upload-dialog", "delete-dialog", "classification-dialog"])
    $(id).addEventListener("cancel", (event) => {
      if (busy) event.preventDefault();
    });
  $("document-type").addEventListener("change", () =>
    fillGroupOptions($("document-type").value),
  );
  function openClassification(doc) {
    $("classification-document-id").value = doc.id;
    $("classification-error").textContent = "";
    fillTypeOptions($("classification-type"));
    const group =
      groups.get(Number(doc.group_id)) ||
      [...groups.values()].find((item) => item.code === doc.category);
    if (group) $("classification-type").value = String(group.doc_type_id);
    fillGroupOptions(
      $("classification-type").value,
      group?.id,
      $("classification-group"),
    );
    $("classification-dialog").showModal();
  }
  $("classification-type").addEventListener("change", () =>
    fillGroupOptions(
      $("classification-type").value,
      "",
      $("classification-group"),
    ),
  );
  for (const id of ["close-classification", "cancel-classification"])
    $(id).addEventListener("click", () => {
      if (!busy) $("classification-dialog").close();
    });
  $("classification-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      try {
        await api(
          `/api/v1/documents/${$("classification-document-id").value}/classification`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              group_id: Number($("classification-group").value),
            }),
          },
        );
        $("classification-dialog").close();
        await load();
        notice("Đã cập nhật phân loại, không cần tạo lại embedding.");
      } catch (error) {
        $("classification-error").textContent = error.message;
      }
    });
  });
  function fileChanged() {
    const file = $("upload-file").files[0];
    if (!file) return;
    $("file-label").textContent = file.name;
    $("file-description").textContent =
      `${(file.size / 1024).toLocaleString("vi-VN", { maximumFractionDigits: 1 })} KB · Chọn để đổi file`;
    if (!$("document-title").value)
      $("document-title").value = file.name
        .replace(/\.[^.]+$/, "")
        .replace(/[_-]/g, " ")
        .slice(0, 500);
    $("upload-error").textContent = "";
  }
  $("upload-file").addEventListener("change", fileChanged);
  for (const eventName of ["dragenter", "dragover"])
    $("dropzone").addEventListener(eventName, (event) => {
      event.preventDefault();
      if (!busy) $("dropzone").classList.add("dragging");
    });
  $("dropzone").addEventListener("dragleave", () =>
    $("dropzone").classList.remove("dragging"),
  );
  $("dropzone").addEventListener("drop", (event) => {
    event.preventDefault();
    $("dropzone").classList.remove("dragging");
    if (busy) return;
    if (event.dataTransfer.files.length !== 1) {
      $("upload-error").textContent = "Vui lòng chọn một tài liệu mỗi lần.";
      return;
    }
    $("upload-file").files = event.dataTransfer.files;
    fileChanged();
  });
  function uploadBusy(value) {
    for (const control of $("upload-form").elements) control.disabled = value;
    $("close-upload").disabled = value;
    $("submit-upload").textContent = value
      ? "Đang xử lý tài liệu…"
      : updating
        ? "Cập nhật tài liệu"
        : "Thêm tài liệu";
  }
  $("upload-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      $("upload-error").textContent = "";
      const file = $("upload-file").files[0];
      if (!file || !/\.(txt|md|pdf)$/i.test(file.name) || !file.size) {
        $("upload-error").textContent =
          "Chọn file TXT, MD hoặc PDF có nội dung.";
        return;
      }
      const title = $("document-title").value.trim();
      const groupId = $("document-category").value;
      const category = groups.get(Number(groupId))?.code;
      if (!title || !groupId || !category) {
        $("upload-error").textContent = "Vui lòng điền tên và nhóm tài liệu.";
        return;
      }
      const data = new FormData();
      data.append("file", file);
      data.append("source_key", sourceKey);
      data.append("title", title);
      data.append("category", category);
      data.append("group_id", groupId);
      uploadBusy(true);
      try {
        const result = await api("/api/v1/documents/upload", {
          method: "POST",
          body: data,
        });
        $("upload-dialog").close();
        notice(
          result.warning ||
            `Đã ${updating ? "cập nhật" : "thêm"} tài liệu “${title}” và lưu file trên máy chủ.`,
          Boolean(result.warning),
        );
        offset = 0;
        try {
          await load();
        } catch (error) {
          notice(
            `Đã lưu tài liệu, nhưng chưa tải lại được danh sách. ${error.message}`,
            true,
          );
        }
      } catch (error) {
        $("upload-error").textContent = error.message;
      } finally {
        uploadBusy(false);
      }
    });
  });
  $("confirm-delete").addEventListener("click", () =>
    task(async () => {
      if (!deleting) return;
      $("delete-error").textContent = "";
      $("confirm-delete").disabled = true;
      $("cancel-delete").disabled = true;
      $("confirm-delete").textContent = "Đang xóa…";
      try {
        const result = await api(`/api/v1/documents/${deleting.id}`, {
          method: "DELETE",
        });
        $("delete-dialog").close();
        notice(
          result.warning ||
            `Đã xóa tài liệu “${deleting.title}” và file local.`,
          Boolean(result.warning),
        );
        deleting = null;
        try {
          await load();
        } catch (error) {
          notice(
            `Đã xóa tài liệu, nhưng chưa tải lại được danh sách. ${error.message}`,
            true,
          );
        }
      } catch (error) {
        $("delete-error").textContent = error.message;
      } finally {
        $("confirm-delete").disabled = false;
        $("cancel-delete").disabled = false;
        $("confirm-delete").textContent = "Xóa tài liệu";
      }
    }),
  );
  const chatEndpoint = "/api/v1/chat";
  let chatMessages = [],
    chatBusy = false,
    chatHistory = [],
    chatSession = 0;
  function fillChatTypeOptions() {
    const selected = $("chat-doc-type").value;
    const all = element("option", "", "Tất cả loại");
    all.value = "";
    $("chat-doc-type").replaceChildren(all);
    for (const item of taxonomy) {
      if (!item.is_active) continue;
      const option = element("option", "", item.name);
      option.value = String(item.id);
      $("chat-doc-type").append(option);
    }
    if (
      [...$("chat-doc-type").options].some(
        (option) => option.value === selected,
      )
    )
      $("chat-doc-type").value = selected;
  }
  function fillChatGroupOptions(resetSelection = false) {
    const selected = resetSelection ? "" : $("chat-group").value;
    const typeId = $("chat-doc-type").value;
    const all = element(
      "option",
      "",
      typeId ? "Tất cả nhóm trong loại" : "Tất cả nhóm",
    );
    all.value = "";
    $("chat-group").replaceChildren(all);
    for (const item of groups.values()) {
      if (!item.is_active) continue;
      if (typeId && Number(item.doc_type_id) !== Number(typeId)) continue;
      const option = element("option", "", item.name);
      option.value = String(item.id);
      $("chat-group").append(option);
    }
    if (
      [...$("chat-group").options].some((option) => option.value === selected)
    )
      $("chat-group").value = selected;
  }
  function currentChatScope() {
    const docTypeId = $("chat-doc-type").value;
    const groupId = $("chat-group").value;
    return {
      ...(docTypeId ? { doc_type_id: Number(docTypeId) } : {}),
      ...(groupId ? { group_ids: [Number(groupId)] } : {}),
    };
  }
  function currentChatScopeLabel() {
    const docTypeId = $("chat-doc-type").value;
    const groupId = $("chat-group").value;
    const type = docTypes.get(Number(docTypeId));
    const group = groups.get(Number(groupId));
    if (group)
      return `${type?.name || group.doc_type_name || "Loại tài liệu"} › ${group.name}`;
    if (type) return `${type.name} › Tất cả nhóm`;
    return "Tất cả tài liệu đang sử dụng";
  }
  function updateChatScopeSummary() {
    $("chat-scope-summary").textContent = currentChatScopeLabel();
  }
  function clearChatForScopeChange() {
    const hadConversation = chatMessages.length > 0 || chatHistory.length > 0;
    chatSession++;
    chatMessages = [];
    chatHistory = [];
    $("chat-error").textContent = hadConversation
      ? "Đã xóa lịch sử vì phạm vi tài liệu vừa thay đổi."
      : "";
    updateChatScopeSummary();
    renderChat();
    chatControls();
  }
  function resetChat() {
    chatSession++;
    chatMessages = [];
    chatHistory = [];
    chatBusy = false;
    $("chat-dialog").close();
    $("chat-input").value = "";
    $("chat-error").textContent = "";
    renderChat();
    chatControls();
  }
  function chatControls() {
    $("chat-input").disabled = chatBusy;
    $("chat-send").disabled = chatBusy || !$("chat-input").value.trim();
    $("chat-doc-type").disabled = chatBusy || !(key || localAdmin);
    $("chat-group").disabled = chatBusy || !(key || localAdmin);
    $("clear-chat").disabled = chatBusy || chatMessages.length === 0;
    $("close-chat").disabled = chatBusy;
    $("done-chat").disabled = chatBusy;
  }
  function renderChat() {
    $("chat-log").replaceChildren();
    if (!chatMessages.length) {
      const empty = element("div", "chat-empty");
      empty.append(
        element("div", "chat-empty-icon", "✦"),
        element(
          "p",
          "",
          "Hỏi về tài liệu nội bộ. Bạn có thể hỏi tiếp trong cùng hội thoại.",
        ),
      );
      $("chat-log").append(empty);
      return;
    }
    for (const message of chatMessages) {
      const wrap = element(
        "div",
        `chat-message ${message.role}${message.pending ? " pending" : ""}`,
      );
      const bubble = element("div", "chat-bubble");
      if (message.pending) {
        bubble.setAttribute(
          "aria-label",
          "Đang tìm tài liệu và tạo câu trả lời",
        );
        bubble.append(
          element("span", "chat-dot"),
          element("span", "chat-dot"),
          element("span", "chat-dot"),
        );
      } else {
        bubble.textContent = message.text;
      }
      let target = wrap;
      if (message.role === "assistant") {
        const row = element("div", "chat-row");
        const avatar = element("span", "chat-avatar", "✦");
        avatar.setAttribute("aria-hidden", "true");
        const col = element("div", "chat-col");
        col.append(bubble);
        row.append(avatar, col);
        wrap.append(row);
        target = col;
      } else {
        wrap.append(bubble);
      }
      if (Number.isFinite(message.elapsedMs))
        target.append(
          element(
            "div",
            "chat-sources",
            `Thời gian trả lời: ${Math.round(message.elapsedMs)} ms`,
          ),
        );
      if (message.scopeLabel)
        target.append(
          element("div", "chat-sources", `Phạm vi: ${message.scopeLabel}`),
        );
      if (message.sources?.length) {
        const sources = element("div", "chat-sources");
        for (const source of message.sources)
          sources.append(element("span", "chat-source", source));
        target.append(sources);
      }
      if (message.context || message.retrievalQuery) {
        const details = element("details", "chat-context");
        details.append(element("summary", "", "Xem nội dung truy xuất"));
        details.append(
          element("p", "", `Câu hỏi tìm kiếm: ${message.retrievalQuery}`),
        );
        details.append(
          element(
            "div",
            "chat-bubble",
            message.context || "Không tìm thấy nội dung liên quan.",
          ),
        );
        target.append(details);
      }
      $("chat-log").append(wrap);
    }
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
  }
  function closeChat() {
    if (chatBusy) return;
    if ($("chat-dialog").open) $("chat-dialog").close();
  }
  $("test-rag").addEventListener("click", () => {
    $("chat-error").textContent = "";
    $("chat-input").value = "";
    renderChat();
    chatControls();
    $("chat-dialog").showModal();
    $("chat-input").focus();
  });
  for (const id of ["close-chat", "done-chat"])
    $(id).addEventListener("click", closeChat);
  $("chat-dialog").addEventListener("cancel", (event) => {
    if (chatBusy) event.preventDefault();
  });
  $("clear-chat").addEventListener("click", () => {
    if (chatBusy) return;
    chatMessages = [];
    chatHistory = [];
    $("chat-error").textContent = "";
    renderChat();
    chatControls();
  });
  $("chat-doc-type").addEventListener("change", () => {
    if (chatBusy) return;
    fillChatGroupOptions(true);
    clearChatForScopeChange();
  });
  $("chat-group").addEventListener("change", () => {
    if (chatBusy) return;
    clearChatForScopeChange();
  });
  $("chat-input").addEventListener("input", () => {
    $("chat-input").style.height = "auto";
    $("chat-input").style.height =
      `${Math.min($("chat-input").scrollHeight, 130)}px`;
    chatControls();
  });
  $("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (chatBusy || !(key || localAdmin)) return;
    const question = $("chat-input").value.trim();
    if (!question) return;
    const session = chatSession;
    const scope = currentChatScope();
    const scopeLabel = currentChatScopeLabel();
    $("chat-error").textContent = "";
    chatMessages.push({ role: "user", text: question });
    chatMessages.push({ role: "assistant", text: "", pending: true });
    $("chat-input").value = "";
    $("chat-input").style.height = "auto";
    chatBusy = true;
    chatControls();
    renderChat();
    (async () => {
      try {
        const data = await api(chatEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: question,
            history: chatHistory,
            ...scope,
          }),
        });
        if (session !== chatSession) return;
        if (
          !["answered", "insufficient_context"].includes(data.status) ||
          typeof data.answer !== "string" ||
          !Array.isArray(data.sources)
        )
          throw new Error(
            "Câu trả lời không hợp lệ. Kiểm tra phiên bản RAG Service.",
          );
        chatHistory.push(
          { role: "user", content: question },
          { role: "assistant", content: data.answer },
        );
        while (
          chatHistory.length > 12 ||
          chatHistory.reduce((sum, m) => sum + m.content.length, 0) > 24000
        )
          chatHistory.splice(0, 2);
        chatMessages[chatMessages.length - 1] = {
          role: "assistant",
          text: data.answer,
          elapsedMs: data.elapsed_ms,
          scopeLabel,
          context: data.context,
          retrievalQuery: data.retrieval_query,
          sources: data.sources.map((source) => {
            const labels = [`[${source.citation}] ${source.title || "Nguồn"}`];
            if (source.heading) labels.push(source.heading);
            if (Number.isInteger(source.chunk_index))
              labels.push(`Đoạn ${source.chunk_index + 1}`);
            if (Number.isFinite(source.similarity))
              labels.push(`Độ tương đồng: ${source.similarity.toFixed(3)}`);
            return labels.join(" · ");
          }),
        };
      } catch (error) {
        if (session !== chatSession) {
          notice(error.message, true);
          return;
        }
        chatMessages.pop();
        if (!(key || localAdmin)) {
          $("chat-dialog").close();
          notice(error.message, true);
          return;
        }
        $("chat-error").textContent = error.message;
      } finally {
        if (session === chatSession) {
          chatBusy = false;
          chatControls();
          renderChat();
        }
      }
    })();
  });
  window.addEventListener("beforeunload", (event) => {
    if (
      busy &&
      ($("upload-dialog").open ||
        $("delete-dialog").open ||
        $("classification-dialog").open)
    ) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", () => {
    key = "";
    localAdmin = false;
    $("admin-key").value = "";
    showKey(false);
    closePreview();
    resetChat();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      reset();
      connectLocal();
    }
  });
  async function connectLocal() {
    if (
      !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)
    ) {
      $("login-panel").hidden = false;
      return;
    }
    localAdmin = true;
    $("connection").textContent = "Đang kết nối…";
    await task(async () => {
      try {
        await load();
      } catch (error) {
        if (localAdmin) {
          connection(true);
          notice(error.message, true);
        } else {
          $("login-panel").hidden = false;
          $("login-error").textContent =
            "Truy cập tự động chưa được bật. Kiểm tra RAG_LOCAL_ADMIN_ENABLED hoặc kết nối bằng key.";
        }
      }
    });
  }
  connectLocal();
})();
