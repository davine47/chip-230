/* 文档库系看板（/documents、/davine）：上传 + 列表，接口地址由 data-api 指定 */
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    form: $("uploadForm"),
    file: $("fileInput"),
    dropzone: $("dropzone"),
    fileHint: $("fileHint"),
    category: $("category"),
    categoryList: $("categoryList"),
    description: $("description"),
    uploadBtn: $("uploadBtn"),
    uploadHint: $("uploadHint"),
    progress: $("progress"),
    progressFill: $("progressFill"),
    rows: $("docRows"),
    empty: $("docEmpty"),
    emptyText: $("docEmptyText"),
    listHint: $("listHint"),
    toast: $("toast"),
  };

  if (!els.form) return;

  const api = els.form.dataset.api;
  if (!api) return;

  // 写接口必须带这个头，服务端据此拒绝跨站请求（见 web/guard.py）
  const CSRF = { "X-Requested-With": "chip-230" };

  let picked = null;
  let busy = false;

  let toastTimer = 0;
  function toast(message, isError = false) {
    els.toast.textContent = message;
    els.toast.classList.toggle("toast--error", isError);
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      els.toast.hidden = true;
    }, 3000);
  }

  const humanSize = (n) => {
    if (!n && n !== 0) return "—";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  };

  const timeText = (ms) => {
    if (!ms) return "—";
    const d = new Date(Number(ms));
    const pad = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  function setFile(file) {
    picked = file || null;
    if (!file) {
      els.fileHint.textContent = "拖文件到这里，或点击选择";
      els.uploadHint.textContent = "选择文件后点上传";
      return;
    }
    els.fileHint.textContent = `${file.name}  ·  ${humanSize(file.size)}`;
    els.uploadHint.textContent = "填好分类和描述，然后上传";
  }

  // ---------------------------------------------------------------- 列表
  function renderDocs(docs, total = docs.length) {
    els.rows.replaceChildren();
    els.empty.hidden = docs.length > 0;
    els.listHint.textContent = !docs.length
      ? "暂无文档"
      : total > docs.length
        ? `共 ${total} 份，显示最新 ${docs.length} 份，按创建时间倒序`
        : `共 ${total} 份，按创建时间倒序`;

    if (els.categoryList) {
      const categories = new Set(docs.map((d) => d.category).filter(Boolean));
      els.categoryList.replaceChildren(
        ...[...categories].map((name) => {
          const option = document.createElement("option");
          option.value = name;
          return option;
        })
      );
    }

    docs.forEach((doc) => {
      const tr = document.createElement("tr");

      const nameCell = document.createElement("td");
      nameCell.className = "cell-key";
      nameCell.textContent = doc.name || "(未命名)";

      const catCell = document.createElement("td");
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = doc.category || "-";
      catCell.append(tag);

      const descCell = document.createElement("td");
      descCell.className = "muted";
      descCell.textContent = doc.describe || "-";

      const timeCell = document.createElement("td");
      timeCell.textContent = timeText(doc.create_time);

      const actions = document.createElement("td");
      actions.className = "cell-actions";

      const open = document.createElement("button");
      open.type = "button";
      open.className = "btn btn--ghost btn--sm";
      open.textContent = "打开";
      open.addEventListener("click", () => openDoc(doc.id));

      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn--ghost btn--sm";
      del.textContent = "删除";
      del.addEventListener("click", () => removeDoc(doc));

      actions.append(open, del);
      tr.append(nameCell, catCell, descCell, timeCell, actions);
      els.rows.append(tr);
    });
  }

  async function loadDocs() {
    try {
      const response = await fetch(api);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "加载失败");
      renderDocs(data.documents || [], data.total);
    } catch (error) {
      els.empty.hidden = false;
      els.emptyText.textContent = error.message;
      els.listHint.textContent = "";
      toast(error.message, true);
    }
  }

  // ---------------------------------------------------------------- 操作
  async function openDoc(id) {
    try {
      const response = await fetch(`${api}/${id}/link`, { method: "POST", headers: CSRF });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "生成链接失败");
      window.open(data.url, "_blank", "noopener");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function removeDoc(doc) {
    if (!window.confirm(`删除「${doc.name}」？索引和 OSS 上的文件都会删掉。`)) return;
    try {
      const response = await fetch(`${api}/${doc.id}`, { method: "DELETE", headers: CSRF });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "删除失败");
      toast(`已删除 ${doc.name}`);
      loadDocs();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function setProgress(percent) {
    els.progress.hidden = false;
    els.progress.classList.remove("progress--waiting");
    els.progressFill.style.width = `${Math.min(100, Math.max(0, percent))}%`;
    els.uploadHint.textContent = `上传中 ${percent}%`;
  }

  function setWaiting() {
    els.progress.hidden = false;
    els.progress.classList.add("progress--waiting");
    els.uploadHint.textContent = "上传完成，正在写入 OSS…";
  }

  function resetProgress() {
    els.progress.hidden = true;
    els.progress.classList.remove("progress--waiting");
    els.progressFill.style.width = "0%";
  }

  // fetch 拿不到上传进度，改用 XHR 的 upload.onprogress
  function postForm(url, body) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      Object.entries(CSRF).forEach(([name, value]) => xhr.setRequestHeader(name, value));
      xhr.responseType = "json";
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          setProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      xhr.upload.onload = () => setWaiting();
      xhr.onload = () => {
        const data = xhr.response || {};
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.error || `上传失败（${xhr.status}）`));
      };
      xhr.onerror = () => reject(new Error("网络中断，上传失败"));
      xhr.ontimeout = () => reject(new Error("上传超时"));
      xhr.send(body);
    });
  }

  async function upload(event) {
    event.preventDefault();
    if (busy) return;
    if (!picked) {
      toast("请先选择文件", true);
      return;
    }
    const category = els.category.value.trim();
    const description = els.description.value.trim();
    if (!category || (els.description.required && !description)) {
      toast(els.description.required ? "分类和描述都要填" : "请选择分类", true);
      return;
    }

    busy = true;
    els.uploadBtn.disabled = true;
    els.uploadHint.textContent = "上传中…";

    const body = new FormData();
    body.append("file", picked);
    body.append("category", category);
    body.append("description", description);

    try {
      const data = await postForm(`${api}/upload`, body);
      toast(`已上传 ${data.document.name}`);
      els.form.reset();
      setFile(null);
      loadDocs();
    } catch (error) {
      toast(error.message, true);
      els.uploadHint.textContent = "上传失败，可以重试";
    } finally {
      resetProgress();
      busy = false;
      els.uploadBtn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 事件
  els.file.addEventListener("change", () => setFile(els.file.files[0]));
  els.form.addEventListener("submit", upload);

  ["dragenter", "dragover"].forEach((type) => {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.add("dropzone--over");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.remove("dropzone--over");
    });
  });
  els.dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) setFile(file);
  });

  loadDocs();
})();
