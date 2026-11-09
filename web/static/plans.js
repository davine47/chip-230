/* Plan 库：上传 .md + 列表 */
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    form: $("planForm"),
    file: $("planFile"),
    dropzone: $("dropzone"),
    fileHint: $("fileHint"),
    planName: $("planName"),
    traceId: $("traceId"),
    githubName: $("githubName"),
    githubBranch: $("githubBranch"),
    githubUrl: $("githubUrl"),
    traceInfo: $("traceInfo"),
    planInfo: $("planInfo"),
    uploadBtn: $("uploadBtn"),
    uploadHint: $("uploadHint"),
    rows: $("planRows"),
    empty: $("planEmpty"),
    emptyText: $("planEmptyText"),
    listHint: $("listHint"),
    toast: $("toast"),
  };

  if (!els.form) return;

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
    if (file && !file.name.toLowerCase().endsWith(".md")) {
      toast("只接受 .md 文件", true);
      els.file.value = "";
      picked = null;
      return;
    }
    picked = file || null;
    if (!file) {
      els.fileHint.textContent = "拖 .md 文件到这里，或点击选择";
      els.uploadHint.textContent = "选择 .md 文件后点上传";
      return;
    }
    els.fileHint.textContent = `${file.name}  ·  ${humanSize(file.size)}`;
    els.uploadHint.textContent = "填好 plan_info，然后上传";
    if (!els.planName.value) {
      els.planName.placeholder = file.name.replace(/\.md$/i, "");
    }
  }

  // ---------------------------------------------------------------- 列表
  function renderPlans(plans) {
    els.rows.replaceChildren();
    els.empty.hidden = plans.length > 0;
    els.listHint.textContent = plans.length
      ? `共 ${plans.length} 份，按自增 id 倒序`
      : "暂无计划";

    plans.forEach((plan) => {
      const tr = document.createElement("tr");

      const idCell = document.createElement("td");
      idCell.className = "mono muted";
      idCell.textContent = plan.id ? String(plan.id).slice(-8) : "—";
      idCell.title = String(plan.id || "");

      const nameCell = document.createElement("td");
      nameCell.className = "cell-key";
      nameCell.textContent = plan.plan_name || "(未命名)";

      const ghCell = document.createElement("td");
      ghCell.className = "cell-gh";
      if (plan.github_project_name) {
        const repo = document.createElement(plan.github_project_url ? "a" : "span");
        repo.textContent = plan.github_project_name;
        if (plan.github_project_url) {
          repo.href = plan.github_project_url;
          repo.target = "_blank";
          repo.rel = "noopener";
        }
        const branch = document.createElement("span");
        branch.className = "tag tag--branch";
        branch.textContent = plan.github_project_branch || "-";
        ghCell.append(repo, branch);
      } else {
        ghCell.className = "muted";
        ghCell.textContent = "—";
      }

      const traceCell = document.createElement("td");
      traceCell.className = "mono muted cell-nowrap";
      traceCell.textContent = plan.trace_id || "—";

      const infoCell = document.createElement("td");
      infoCell.className = "muted";
      infoCell.textContent = plan.plan_info || "—";

      const timeCell = document.createElement("td");
      timeCell.className = "cell-nowrap";
      timeCell.textContent = timeText(plan.create_time);

      const actions = document.createElement("td");
      actions.className = "cell-actions";

      const open = document.createElement("button");
      open.type = "button";
      open.className = "btn btn--ghost btn--sm";
      open.textContent = "打开";
      open.addEventListener("click", () => openPlan(plan));

      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn--ghost btn--sm";
      del.textContent = "删除";
      del.addEventListener("click", () => removePlan(plan));

      actions.append(open, del);
      tr.append(idCell, nameCell, ghCell, traceCell, infoCell, timeCell, actions);
      els.rows.append(tr);
    });
  }

  async function loadPlans() {
    try {
      const response = await fetch("/api/plans");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "加载失败");
      renderPlans(data.plans || []);
    } catch (error) {
      els.empty.hidden = false;
      els.emptyText.textContent = error.message;
      els.listHint.textContent = "";
      toast(error.message, true);
    }
  }

  // ---------------------------------------------------------------- 操作
  async function openPlan(plan) {
    try {
      const response = await fetch(`/api/plans/${plan.uuid}/${plan.id}/link`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "生成链接失败");
      window.open(data.url, "_blank", "noopener");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function removePlan(plan) {
    if (!window.confirm(`删除「${plan.plan_name}」？表和 OSS 上的文件都会删掉。`)) return;
    try {
      const response = await fetch(`/api/plans/${plan.uuid}/${plan.id}`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "删除失败");
      toast(`已删除 ${plan.plan_name}`);
      loadPlans();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function upload(event) {
    event.preventDefault();
    if (busy) return;
    if (!picked) {
      toast("请先选择 .md 文件", true);
      return;
    }
    const planInfo = els.planInfo.value.trim();
    if (!planInfo) {
      toast("plan_info 不能为空", true);
      els.planInfo.focus();
      return;
    }
    const githubName = els.githubName.value.trim();
    const githubBranch = els.githubBranch.value.trim();
    if (!githubName || !githubBranch) {
      toast("GitHub 项目和分支都要填", true);
      (!githubName ? els.githubName : els.githubBranch).focus();
      return;
    }

    busy = true;
    els.uploadBtn.disabled = true;
    els.uploadHint.textContent = "上传中…";

    const body = new FormData();
    body.append("file", picked);
    body.append("plan_name", els.planName.value.trim());
    body.append("plan_info", planInfo);
    body.append("trace_id", els.traceId.value.trim());
    body.append("trace_info", els.traceInfo.value.trim());
    body.append("github_project_name", githubName);
    body.append("github_project_branch", githubBranch);
    body.append("github_project_url", els.githubUrl.value.trim());

    try {
      const response = await fetch("/api/plans/upload", { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "上传失败");
      toast(`已上传 ${data.plan.plan_name}（id=${data.plan.id}）`);
      els.form.reset();
      setFile(null);
      loadPlans();
    } catch (error) {
      toast(error.message, true);
      els.uploadHint.textContent = "上传失败，可以重试";
    } finally {
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

  loadPlans();
})();
