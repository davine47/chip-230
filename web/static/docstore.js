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
    expireHours: $("expireHours"),
    result: $("result"),
    resultUrl: $("resultUrl"),
    resultExpiry: $("resultExpiry"),
    copyBtn: $("copyBtn"),
    openBtn: $("openBtn"),
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

  function setWaiting(text = "上传完成，正在写入 OSS…") {
    els.progress.hidden = false;
    els.progress.classList.add("progress--waiting");
    els.uploadHint.textContent = text;
  }

  // 上传成功后的临时链接：只有选了有效期的看板（davine）才返回这个字段
  function showResult(doc) {
    if (!els.result || !doc || !doc.temporary_url) return;
    els.resultUrl.value = doc.temporary_url;
    els.openBtn.href = doc.temporary_url;
    els.resultExpiry.textContent = doc.expires_at
      ? `${new Date(doc.expires_at).toLocaleString()} 前有效`
      : `${doc.expires_hours} 小时内有效`;
    els.result.hidden = false;
  }

  function hideResult() {
    if (els.result) els.result.hidden = true;
  }

  function resetProgress() {
    els.progress.hidden = true;
    els.progress.classList.remove("progress--waiting");
    els.progressFill.style.width = "0%";
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }

  // 直传：浏览器拿签名 URL 直接 PUT 到 OSS，文件不再经过看板服务器。
  // 网络层失败（status 0，多半是 bucket 没配 CORS）时 reject 一个 fallback=true 的错误，交给上层退回中转。
  function putToOss(init, file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(init.method || "PUT", init.url);
      Object.entries(init.headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value));
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          setProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(`OSS 拒绝了上传（${xhr.status}）`));
      };
      xhr.onerror = () => {
        const error = new Error("浏览器直传 OSS 失败");
        error.fallback = true;
        reject(error);
      };
      xhr.ontimeout = xhr.onerror;
      xhr.send(file);
    });
  }

  // 返回上传结果；返回 null 表示这次应该走服务器中转
  async function uploadDirect(file, category, description) {
    const payload = { filename: file.name, size: file.size, category, description };
    if (els.expireHours) payload.expire_hours = els.expireHours.value;
    const init = await postJSON(`${api}/upload-url`, payload);
    if (init.mode !== "direct") return null;
    try {
      await putToOss(init, file);
    } catch (error) {
      if (!error.fallback) throw error;
      toast("浏览器直传失败（bucket 可能没配 CORS，见 scripts/07_oss_cors.py），改由服务器中转", true);
      return null;
    }
    setWaiting("已传到 OSS，正在写索引…");
    return postJSON(`${api}/complete`, { upload_id: init.upload_id });
  }

  // 服务器中转：fetch 拿不到上传进度，改用 XHR 的 upload.onprogress
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

  if (els.copyBtn) {
    els.copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(els.resultUrl.value);
        els.copyBtn.textContent = "已复制";
        setTimeout(() => {
          els.copyBtn.textContent = "复制";
        }, 1500);
      } catch {
        els.resultUrl.select();
        toast("自动复制失败，已选中链接，按 ⌘C 复制", true);
      }
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
    hideResult();
    els.uploadBtn.disabled = true;
    els.uploadHint.textContent = "上传中…";

    try {
      let data = await uploadDirect(picked, category, description);
      if (!data) {
        resetProgress();
        const body = new FormData();
        body.append("file", picked);
        body.append("category", category);
        body.append("description", description);
        if (els.expireHours) body.append("expire_hours", els.expireHours.value);
        data = await postForm(`${api}/upload`, body);
      }
      toast(`已上传 ${data.document.name}`);
      // 顺序不能反：form.reset() 会把表单内的 #resultUrl 一起清空
      els.form.reset();
      setFile(null);
      showResult(data.document);
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
