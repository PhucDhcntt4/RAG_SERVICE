"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  let key = "";
  let localAdmin = false;
  let busy = false;
  let taxonomy = [];
  let searchTerm = "";
  const collapsed = new Set();

  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
    $("notice").hidden = !message;
  }

  function controls() {
    const connected = Boolean(key || localAdmin);
    for (const id of ["refresh-taxonomy", "add-doc-type", "taxo-search"])
      $(id).disabled = busy || !connected;
    $("connect-button").disabled = busy;
    for (const control of document.querySelectorAll(
      "#doc-type-form input, #doc-type-form select, #doc-type-form button, " +
        "#group-form input, #group-form select, #group-form button, " +
        ".taxo-tree button",
    ))
      control.disabled = busy || !connected;
  }

  function connection(connected) {
    $("login-panel").hidden = connected;
    $("taxo-panel").hidden = !connected;
    $("disconnect").hidden = !connected || localAdmin;
    $("connection").textContent = connected ? "Đã kết nối" : "Chưa kết nối";
    $("connection").classList.toggle("online", connected);
    controls();
  }

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(
        localAdmin ? path.replace(/^\/api\//, "/admin-api/") : path,
        {
          ...options,
          cache: "no-store",
          headers: {
            ...options.headers,
            ...(localAdmin
              ? { "X-RAG-Local-UI": "1" }
              : { Authorization: `Bearer ${key}` }),
          },
        },
      );
    } catch {
      throw new Error("Không thể kết nối RAG Service.");
    }
    let body = {};
    try {
      body = await response.json();
    } catch {
      /* API error handled below */
    }
    if (!response.ok) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).join("\n")
        : body.detail;
      const error = new Error(
        typeof detail === "string"
          ? detail
          : `Yêu cầu thất bại (${response.status}).`,
      );
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function element(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function matches(text, term) {
    return text && text.toLowerCase().includes(term);
  }

  // ---- doc-type dialog ----
  function openDocTypeDialog(item = null) {
    $("doc-type-form").reset();
    $("doc-type-error").textContent = "";
    $("doc-type-id").value = item ? item.id : "";
    $("doc-type-code").disabled = Boolean(item);
    $("doc-type-code").value = item ? item.code : "";
    $("doc-type-name").value = item ? item.name : "";
    $("doc-type-description").value = item ? item.description || "" : "";
    $("doc-type-active").checked = item ? item.is_active : true;
    $("doc-type-dialog-title").textContent = item ? "Sửa loại" : "Thêm loại";
    $("save-doc-type").textContent = item ? "Lưu loại" : "Thêm loại";
    $("doc-type-dialog").showModal();
    $("doc-type-name").focus();
  }
  $("add-doc-type").addEventListener("click", () => openDocTypeDialog());
  $("taxo-empty-add").addEventListener("click", () => openDocTypeDialog());
  $("close-doc-type").addEventListener("click", () => {
    if (!busy) $("doc-type-dialog").close();
  });
  $("cancel-doc-type").addEventListener("click", () => {
    if (!busy) $("doc-type-dialog").close();
  });
  $("doc-type-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      const identifier = $("doc-type-id").value;
      const body = identifier
        ? {
            name: $("doc-type-name").value.trim(),
            description: $("doc-type-description").value.trim(),
            is_active: $("doc-type-active").checked,
            sort_order: 0,
          }
        : {
            code: $("doc-type-code").value.trim(),
            name: $("doc-type-name").value.trim(),
            description: $("doc-type-description").value.trim(),
            sort_order: 0,
          };
      try {
        await api(
          identifier ? `/api/v1/doc-types/${identifier}` : "/api/v1/doc-types",
          {
            method: identifier ? "PATCH" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        $("doc-type-dialog").close();
        notice(
          identifier ? "Đã cập nhật loại tài liệu." : "Đã thêm loại tài liệu.",
        );
        await load();
      } catch (error) {
        $("doc-type-error").textContent = error.message;
      }
    });
  });

  // ---- group dialog ----
  function fillGroupTypeOptions(selected = "") {
    const select = $("group-doc-type");
    select.replaceChildren();
    for (const item of taxonomy) {
      const option = element(
        "option",
        "",
        item.name + (item.is_active ? "" : " (đã tắt)"),
      );
      option.value = String(item.id);
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === String(selected)))
      select.value = String(selected);
  }
  function openGroupDialog(group = null, presetTypeId = "") {
    $("group-form").reset();
    $("group-error").textContent = "";
    fillGroupTypeOptions(group ? group.doc_type_id : presetTypeId);
    $("group-id").value = group ? group.id : "";
    $("group-code").disabled = Boolean(group);
    $("group-code").value = group ? group.code : "";
    $("group-name").value = group ? group.name : "";
    $("group-description").value = group ? group.description || "" : "";
    $("group-active").checked = group ? group.is_active : true;
    $("group-dialog-title").textContent = group ? "Sửa nhóm" : "Thêm nhóm";
    $("save-group").textContent = group ? "Lưu nhóm" : "Thêm nhóm";
    $("group-dialog").showModal();
    $("group-name").focus();
  }
  $("close-group").addEventListener("click", () => {
    if (!busy) $("group-dialog").close();
  });
  $("cancel-group").addEventListener("click", () => {
    if (!busy) $("group-dialog").close();
  });
  $("group-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      const identifier = $("group-id").value;
      const body = {
        doc_type_id: Number($("group-doc-type").value),
        name: $("group-name").value.trim(),
        description: $("group-description").value.trim(),
        sort_order: 0,
        ...(identifier
          ? { is_active: $("group-active").checked }
          : { code: $("group-code").value.trim() }),
      };
      try {
        await api(
          identifier ? `/api/v1/groups/${identifier}` : "/api/v1/groups",
          {
            method: identifier ? "PATCH" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        $("group-dialog").close();
        notice(
          identifier ? "Đã cập nhật nhóm tài liệu." : "Đã thêm nhóm tài liệu.",
        );
        await load();
      } catch (error) {
        $("group-error").textContent = error.message;
      }
    });
  });

  async function remove(path, name) {
    if (!window.confirm(`Xóa “${name}”?`)) return;
    await task(async () => {
      try {
        await api(path, { method: "DELETE" });
        notice(`Đã xóa “${name}”.`);
        await load();
      } catch (error) {
        notice(error.message, true);
      }
    });
  }

  // ---- tree rendering ----
  function render() {
    const tree = $("taxo-tree");
    tree.replaceChildren();
    const term = searchTerm.trim().toLowerCase();
    let groupTotal = 0;
    let visibleTypes = 0;

    for (const item of taxonomy) {
      const itemGroups = item.groups || [];
      groupTotal += itemGroups.length;

      const typeMatches =
        !term || matches(item.name, term) || matches(item.code, term);
      const matchingGroups =
        term && !typeMatches
          ? itemGroups.filter(
              (g) => matches(g.name, term) || matches(g.code, term),
            )
          : itemGroups;
      if (term && !typeMatches && matchingGroups.length === 0) continue;
      visibleTypes++;

      const isCollapsed = !term && collapsed.has(item.id);
      const card = element(
        "div",
        `type-card${item.is_active ? "" : " inactive"}${isCollapsed ? " collapsed" : ""}`,
      );

      const head = element("div", "type-head");
      const toggle = element("button", "type-toggle", "▾");
      toggle.type = "button";
      toggle.setAttribute("aria-label", isCollapsed ? "Mở rộng" : "Thu gọn");
      toggle.addEventListener("click", () => {
        if (collapsed.has(item.id)) collapsed.delete(item.id);
        else collapsed.add(item.id);
        render();
      });
      head.append(toggle);

      const copy = element("div", "type-head-copy");
      const nameRow = element("div", "type-name-row");
      nameRow.append(element("strong", "", item.name));
      if (!item.is_active)
        nameRow.append(element("span", "badge inactive", "Đã tắt"));
      copy.append(nameRow);
      copy.append(
        element("div", "type-meta", `${item.code} · ${itemGroups.length} nhóm`),
      );
      head.append(copy);

      const headActions = element("div", "inline-actions");
      const editType = element("button", "text-button", "Sửa");
      editType.type = "button";
      editType.addEventListener("click", (event) => {
        event.stopPropagation();
        openDocTypeDialog(item);
      });
      const deleteType = element("button", "delete-button", "Xóa");
      deleteType.type = "button";
      deleteType.addEventListener("click", (event) => {
        event.stopPropagation();
        remove(`/api/v1/doc-types/${item.id}`, item.name);
      });
      headActions.append(editType, deleteType);
      head.append(headActions);
      card.append(head);

      const body = element("div", "type-body");
      if (matchingGroups.length === 0) {
        body.append(
          element("div", "group-empty", "Chưa có nhóm nào trong loại này."),
        );
      }
      for (const group of matchingGroups) {
        const row = element(
          "div",
          `group-row${group.is_active ? "" : " inactive"}`,
        );
        const rowCopy = element("div", "group-row-copy");
        rowCopy.append(element("strong", "", group.name));
        rowCopy.append(
          element(
            "small",
            "",
            `${group.code} · ${group.document_count || 0} tài liệu${group.is_active ? "" : " · đã tắt"}`,
          ),
        );
        row.append(rowCopy);
        const rowActions = element("div", "inline-actions");
        const editGroup = element("button", "text-button", "Sửa");
        editGroup.type = "button";
        editGroup.addEventListener("click", () => openGroupDialog(group));
        const deleteGroup = element("button", "delete-button", "Xóa");
        deleteGroup.type = "button";
        deleteGroup.addEventListener("click", () =>
          remove(`/api/v1/groups/${group.id}`, group.name),
        );
        rowActions.append(editGroup, deleteGroup);
        row.append(rowActions);
        body.append(row);
      }
      const addGroup = element("button", "add-group-row", "＋ Thêm nhóm");
      addGroup.type = "button";
      addGroup.addEventListener("click", () => openGroupDialog(null, item.id));
      body.append(addGroup);
      card.append(body);

      tree.append(card);
    }

    $("doc-type-count").textContent = `${taxonomy.length} loại`;
    $("group-count").textContent = `${groupTotal} nhóm`;

    const nothingAtAll = taxonomy.length === 0;
    const nothingMatches = !nothingAtAll && visibleTypes === 0;
    $("taxo-tree").hidden = nothingAtAll || nothingMatches;
    $("taxo-empty").hidden = !(nothingAtAll || nothingMatches);
    $("taxo-empty-add").hidden = nothingMatches;
    if (nothingMatches) {
      $("taxo-empty-title").textContent = "Không tìm thấy kết quả";
      $("taxo-empty-description").textContent =
        `Không có loại hoặc nhóm nào khớp với “${searchTerm.trim()}”.`;
    } else {
      $("taxo-empty-title").textContent = "Chưa có loại tài liệu nào";
      $("taxo-empty-description").textContent =
        "Thêm loại đầu tiên để bắt đầu phân loại tài liệu.";
    }
    controls();
  }

  $("taxo-search").addEventListener("input", () => {
    searchTerm = $("taxo-search").value;
    render();
  });

  async function load() {
    const data = await api("/api/v1/taxonomy");
    taxonomy = Array.isArray(data.doc_types) ? data.doc_types : [];
    render();
    connection(true);
  }

  async function task(action) {
    if (busy) return;
    busy = true;
    controls();
    try {
      await action();
    } catch (error) {
      notice(error.message, true);
      if (error.status === 401 || error.status === 403) {
        key = "";
        localAdmin = false;
        connection(false);
      }
    } finally {
      busy = false;
      controls();
    }
  }

  $("refresh-taxonomy").addEventListener("click", () =>
    task(async () => {
      notice("");
      await load();
    }),
  );
  $("toggle-key").addEventListener("click", () => {
    const visible = $("admin-key").type === "text";
    $("admin-key").type = visible ? "password" : "text";
    $("toggle-key").textContent = visible ? "Hiện" : "Ẩn";
  });
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    key = $("admin-key").value.trim();
    localAdmin = false;
    task(async () => {
      await load();
      $("admin-key").value = "";
      $("login-error").textContent = "";
    });
  });
  $("disconnect").addEventListener("click", () => {
    key = "";
    taxonomy = [];
    searchTerm = "";
    $("taxo-search").value = "";
    render();
    connection(false);
  });

  for (const id of ["doc-type-dialog", "group-dialog"])
    $(id).addEventListener("cancel", (event) => {
      if (busy) event.preventDefault();
    });

  async function connectLocal() {
    if (
      !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)
    ) {
      connection(false);
      return;
    }
    localAdmin = true;
    $("connection").textContent = "Đang kết nối…";
    await task(async () => {
      try {
        await load();
      } catch (error) {
        localAdmin = false;
        connection(false);
        $("login-error").textContent = error.message;
      }
    });
  }

  $("login-panel").hidden = true;
  connectLocal();
})();
