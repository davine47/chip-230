/* 键空间：搜索 / 查看 / 编辑 / 删除，全部走 /api/*，无刷新 */
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    rows: $("rows"),
    empty: $("empty"),
    emptyText: $("emptyText"),
    summary: $("summary"),
    pattern: $("pattern"),
    refresh: $("refresh"),
    create: $("create"),
    drawer: $("drawer"),
    editorTitle: $("editorTitle"),
    fKey: $("fKey"),
    fType: $("fType"),
    fTtl: $("fTtl"),
    fValue: $("fValue"),
    fHint: $("fHint"),
    deleteBtn: $("deleteBtn"),
    saveBtn: $("saveBtn"),
    toast: $("toast"),
  };

  if (!els.rows) return; // 不是 Redis 页

  const state = { editing: null, keys: [], busy: false };

  // ---------------------------------------------------------------- 工具
  let toastTimer = 0;
  function toast(message, isError = false) {
    els.toast.textContent = message;
    els.toast.classList.toggle("toast--error", isError);
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      els.toast.hidden = true;
    }, 2600);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }

  const ttlText = (ttl) => (ttl === -1 ? "永久" : ttl === -2 ? "已过期" : `${ttl}s`);

  // ---------------------------------------------------------------- 列表
  function renderRows() {
    els.rows.replaceChildren();
    els.empty.hidden = state.keys.length > 0;

    state.keys.forEach((item) => {
      const tr = document.createElement("tr");

      const keyCell = document.createElement("td");
      keyCell.className = "cell-key";
      keyCell.textContent = item.key;

      const typeCell = document.createElement("td");
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.type;
      typeCell.append(tag);

      const ttlCell = document.createElement("td");
      ttlCell.textContent = ttlText(item.ttl);
      if (item.ttl === -1) ttlCell.classList.add("muted");

      const actionCell = document.createElement("td");
      actionCell.className = "cell-actions";
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn--ghost btn--sm";
      delBtn.textContent = "删除";
      delBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        removeKey(item.key);
      });
      actionCell.append(delBtn);

      tr.append(keyCell, typeCell, ttlCell, actionCell);
      tr.addEventListener("click", () => openEditor(item.key));
      els.rows.append(tr);
    });
  }

  async function loadKeys() {
    const pattern = els.pattern.value.trim() || "*";
    els.summary.textContent = "正在加载…";
    try {
      const data = await api(`/api/keys?pattern=${encodeURIComponent(pattern)}`);
      state.keys = data.keys;
      els.emptyText.textContent = `没有匹配 ${data.pattern} 的 Key`;
      renderRows();
      els.summary.textContent =
        `共 ${data.total} 个 Key，本次返回 ${data.returned} 个（SCAN 模式 ${data.pattern}）`;
    } catch (error) {
      state.keys = [];
      renderRows();
      els.emptyText.textContent = error.message;
      els.summary.textContent = "";
      toast(error.message, true);
    }
  }

  // ---------------------------------------------------------------- 抽屉
  function setEditorMode({ key, type, value, ttl, isNew, truncated }) {
    state.editing = { key, isNew };

    els.editorTitle.textContent = isNew ? "新建 Key" : "编辑 Key";
    els.fKey.value = key || "";
    els.fKey.disabled = !isNew;
    els.fType.textContent = type || "string";
    els.fTtl.value = ttl && ttl > 0 ? ttl : "";
    els.fValue.value = value ?? "";

    // 截断的值只是前一部分，保存回去会把后半段永久覆盖掉，所以和非 String 一样只读
    const editable = (type || "string") === "string" && !truncated;
    els.fValue.disabled = !editable;
    els.saveBtn.disabled = !editable;
    els.deleteBtn.hidden = isNew;

    if (truncated && (type || "string") === "string") {
      els.fHint.textContent = "值太大，这里只显示了前一部分，为防止截断覆盖原值已禁止编辑；要改请用 redis-cli。";
    } else if (!editable) {
      els.fHint.textContent = `非 String 类型（${type}）只读：这里只能查看和删除，改值请用对应类型的原生命令。`;
    } else {
      els.fHint.textContent = "保存会直接覆盖同名 Key 的值；TTL 留空表示永不过期。";
    }

    els.drawer.hidden = false;
    (isNew ? els.fKey : els.fValue).focus();
  }

  async function openEditor(key) {
    if (!key) {
      setEditorMode({ key: "", type: "string", value: "", ttl: -1, isNew: true });
      return;
    }
    try {
      const data = await api(`/api/key?name=${encodeURIComponent(key)}`);
      setEditorMode({ ...data, isNew: false });
    } catch (error) {
      toast(error.message, true);
    }
  }

  function closeEditor() {
    els.drawer.hidden = true;
    state.editing = null;
  }

  // ---------------------------------------------------------------- 写入
  async function save() {
    if (state.busy) return;
    const key = els.fKey.value.trim();
    if (!key) {
      toast("Key 不能为空", true);
      els.fKey.focus();
      return;
    }
    state.busy = true;
    els.saveBtn.disabled = true;
    try {
      await api("/api/key", {
        method: "PUT",
        body: JSON.stringify({
          key,
          value: els.fValue.value,
          ttl: els.fTtl.value.trim() === "" ? null : Number(els.fTtl.value),
          create: Boolean(state.editing?.isNew), // 新建时服务端发现同名 Key 会拒绝，而不是覆盖
        }),
      });
      toast(`已保存 ${key}`);
      closeEditor();
      loadKeys();
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
      els.saveBtn.disabled = false;
    }
  }

  async function removeKey(key) {
    if (!window.confirm(`确认删除「${key}」？删除后不可恢复。`)) return;
    try {
      await api(`/api/key?name=${encodeURIComponent(key)}`, { method: "DELETE" });
      toast(`已删除 ${key}`);
      closeEditor();
      loadKeys();
    } catch (error) {
      toast(error.message, true);
    }
  }

  // ---------------------------------------------------------------- 事件
  els.refresh.addEventListener("click", loadKeys);
  els.create.addEventListener("click", () => openEditor(null));
  els.saveBtn.addEventListener("click", save);
  els.deleteBtn.addEventListener("click", () => {
    if (state.editing?.key) removeKey(state.editing.key);
  });

  els.pattern.addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadKeys();
  });
  let debounce = 0;
  els.pattern.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(loadKeys, 350);
  });

  els.drawer.addEventListener("click", (event) => {
    if (event.target.hasAttribute("data-close")) closeEditor();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.drawer.hidden) closeEditor();
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !els.drawer.hidden) save();
  });

  // 支持 /redis?pattern=xxx 预填搜索、/redis?key=xxx 直接打开抽屉
  const params = new URLSearchParams(location.search);
  const initialPattern = params.get("pattern");
  if (initialPattern) els.pattern.value = initialPattern;

  loadKeys();

  const deepLink = params.get("key");
  if (deepLink) openEditor(deepLink);
})();
