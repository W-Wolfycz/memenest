/* 大盘视图：与「表情管理」同一个页面，用顶部标签切换。
 *
 * 只读概览 + 一键处置。所有请求都走 bridge，缩略图同样取 base64 再塞 <img>。
 * helper（$ / api / toast / guard / escapeHtml / LABELS / VIA_NAMES /
 * loadThumbInto / openModal）由 app.js 挂在 window.__memenest 上复用，
 * 不走 import——宿主重写 import 说明符的 URL 与带 ?v= 的 src 不一致，
 * import 会让 app.js 执行两遍。
 */

const shared = window.__memenest || null;

const {
  $,
  api,
  toast,
  guard,
  escapeHtml,
  LABELS,
  VIA_NAMES,
  loadThumbInto,
  openModal,
  closeModal,
} = shared || {};

let pollTimer = null;

/* ---------- 小工具 ---------- */

function fmtBytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1048576).toFixed(1)} MB`;
  return `${(size / 1073741824).toFixed(2)} GB`;
}

function stat(label, value, unit = "", tone = "") {
  const suffix = unit ? `<i>${escapeHtml(unit)}</i>` : "";
  return (
    `<div class="ov-stat ${tone}"><span>${escapeHtml(label)}</span>` +
    `<b>${escapeHtml(String(value))}</b>${suffix}</div>`
  );
}

function row(label, value, tone = "", title = "") {
  const tip = title ? ` title="${escapeHtml(title)}"` : "";
  return (
    `<div class="ov-row ${tone}"><span${tip}>${escapeHtml(label)}</span>` +
    `<b>${escapeHtml(String(value))}</b></div>`
  );
}

function originLabel(umo) {
  if (!umo) return "导入 / 未知来源";
  const parts = String(umo).split(":");
  const session = parts[parts.length - 1];
  const kind = parts.length > 1 ? parts[1] : "";
  if (kind === "GroupMessage") return `群 ${session}`;
  if (kind === "FriendMessage") return `私聊 ${session}`;
  return String(umo);
}


/* 沙箱 iframe 里 window.confirm 被拦掉（没有 allow-modals），破坏性操作自己做二次确认。 */
const pendingConfirm = { action: "", at: 0 };

function armConfirm(action, label) {
  const now = Date.now();
  if (pendingConfirm.action === action && now - pendingConfirm.at < 5000) {
    pendingConfirm.action = "";
    pendingConfirm.at = 0;
    return true;
  }
  pendingConfirm.action = action;
  pendingConfirm.at = now;
  toast(`再点一次「${label}」确认`);
  return false;
}

/* ---------- 渲染 ---------- */

function renderHealth(warnings) {
  const holder = $("ov-health");
  if (!warnings || !warnings.length) {
    holder.hidden = true;
    holder.innerHTML = "";
    return;
  }
  holder.hidden = false;
  holder.innerHTML = warnings
    .map(
      (item) =>
        `<div class="ov-alert ${escapeHtml(item.level || "info")}">` +
        `${escapeHtml(item.text)}</div>`,
    )
    .join("");
}

function renderHero(library, vision) {
  const undescribed = Number(library.undescribed) || 0;
  const limit = Number(vision.limit) || 0;
  const used = Number(vision.used) || 0;
  const parts = [
    stat("库", Number(library.total) || 0, "张"),
    stat("可用", Number(library.ready) || 0),
    stat("待审", Number(library.inbox) || 0),
    stat("无描述", undescribed, "", undescribed > 0 ? "hot" : ""),
    stat("今日识图", limit ? `${used}/${limit}` : used, "", limit && used >= limit ? "hot" : ""),
  ];
  $("ov-hero-stats").innerHTML = parts.join("");
}

function renderLibrary(library) {
  const cap = Number(library.cap) || 0;
  const total = Number(library.total) || 0;
  $("ov-lib-rows").innerHTML = [
    row("禁用", library.disabled),
    row("黑名单", library.blocked),
    row("可用占比", total ? `${Math.round(((Number(library.ready) || 0) / total) * 100)}%` : "—"),
  ].join("");
  const ratio = cap ? Math.min(1, total / cap) : 0;
  const bar = $("ov-lib-bar");
  bar.style.width = cap ? `${Math.round(ratio * 100)}%` : "0";
  bar.classList.toggle("warn", cap > 0 && ratio >= 0.9);
  $("ov-lib-cap").textContent = cap ? `容量 ${total} / ${cap}` : "不限容量";
}

function renderVision(vision) {
  const limit = Number(vision.limit) || 0;
  const used = Number(vision.used) || 0;
  const spent = limit > 0 && used >= limit;
  const noModel = Number(vision.no_model) || 0;
  $("ov-vision-rows").innerHTML = [
    row("模型", vision.available ? "可用" : "不可用", vision.available ? "ok" : "hot"),
    row("今日已用", limit ? `${used} / ${limit}` : `${used}（不限）`, spent ? "hot" : ""),
    row("缺模型入库", noModel, noModel ? "hot" : ""),
  ].join("");
  $("ov-vision-note").textContent = vision.available
    ? "预算耗尽时新图先落待审核池、不调模型，次日恢复。"
    : "没有可用的视觉模型：新收的图会以兜底类入库、没有描述。";
}

function renderVector(vector) {
  const enabled = Boolean(vector && vector.enabled);
  const covered = Number(vector && vector.covered) || 0;
  const expected = Number(vector && vector.expected) || 0;
  const missing = expected > covered;
  if (!enabled) {
    $("ov-vector-rows").innerHTML = row("语义检索", "未启用", "muted");
    $("ov-vector-note").textContent = "未启用语义检索：现在按关键词打分。";
    return;
  }
  // 只有缺向量时才给「补齐」，并把按钮放在数字前面：不满的那几个数字才是它的理由
  const fill = missing
    ? '<button class="btn small ov-inline-btn" id="ov-reindex" type="button"' +
      ' title="只给还没有向量的条目补算">补齐</button>'
    : "";
  $("ov-vector-rows").innerHTML = [
    row("状态", vector.running ? "重建中" : "就绪", vector.running ? "hot" : "ok"),
    `<div class="ov-row"><span>覆盖</span><span class="ov-row-tail">${fill}` +
      `<b class="${missing ? "hot" : ""}">${covered} / ${expected}</b></span></div>`,
    row("模型", vector.model || "—", "muted"),
  ].join("");
  $("ov-vector-note").textContent = missing
    ? "覆盖不到 100% 时，部分图只能靠关键词命中。"
    : "全部条目都有向量，检索走语义。";
  const button = $("ov-reindex");
  // 每次渲染都是新 DOM，监听得在这里重挂
  if (button) button.addEventListener("click", doReindex);
}

function renderStorage(storage) {
  const dirs = storage.dirs || {};
  const bytes = (key) => (dirs[key] || {}).bytes || 0;
  const galleryFiles = ((dirs.blobs || {}).files || 0) + ((dirs.inbox || {}).files || 0);
  $("ov-storage-rows").innerHTML = [
    row("总计", fmtBytes(storage.total_bytes)),
    row("图库", `${fmtBytes(bytes("blobs") + bytes("inbox"))} · ${galleryFiles} 个`),
    row("库文件", `${fmtBytes(storage.db)} + WAL ${fmtBytes(storage.wal)}`, "muted"),
    row("缓存 / 导入 / 临时",
      fmtBytes(bytes("cache") + bytes("imports") + bytes("tmp")), "muted"),
    row("缺失文件", storage.missing, Number(storage.missing) > 0 ? "hot" : "",
      "库里有记录、文件没了"),
    row("孤儿文件", storage.orphans, Number(storage.orphans) > 0 ? "hot" : "",
      "文件在、没有任何记录引用"),
  ].join("");
}

function renderDropMissing(missing) {
  const button = $("ov-drop-missing");
  const count = Number(missing) || 0;
  button.hidden = count <= 0;
  button.textContent = count ? `删除缺失（${count}）` : "删除缺失";
}

function renderDropOrphans(orphans) {
  const button = $("ov-orphans");
  const count = Number(orphans) || 0;
  button.hidden = count <= 0;
  button.textContent = count ? `清理孤儿（${count}）` : "清理孤儿";
}

function renderSend(send) {
  const failed = Number(send.failed) || 0;
  $("ov-send-rows").innerHTML = [
    row("尝试", send.attempts),
    row("已发", send.sent, "ok"),
    row("失败", failed, failed ? "hot" : ""),
    row("取消", send.cancelled),
  ].join("");
  const entries = Object.entries(send.rejected || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([code, count]) => `<span class="ov-badge">${escapeHtml(code)} ×${count}</span>`)
    .join("");
  $("ov-send-reject").innerHTML =
    entries || '<span class="hint">本次运行还没有被拦下的记录</span>';
}

function renderLabels(labels) {
  const values = LABELS.map(([key, name]) => [name, Number(labels[key]) || 0]);
  const peak = values.reduce((top, item) => Math.max(top, item[1]), 0) || 1;
  // 真实分布很偏（一个类占四成、其余个位数）：线性归一后小值只剩一条缝，
  // 换成开方缩放，量级关系还在，小类也看得出来。
  $("ov-label-grid").innerHTML = values
    .map(([name, count]) => {
      const width = count ? Math.max(2, Math.round(Math.sqrt(count / peak) * 100)) : 0;
      return (
        `<div class="ov-bar-row${count ? "" : " is-zero"}">` +
        `<span>${escapeHtml(name)}</span>` +
        `<div class="ov-track"><i style="width:${width}%"></i></div>` +
        `<b>${count || "—"}</b></div>`
      );
    })
    .join("");
}

function renderOrigins(origins) {
  // 同一个群在不同 BOT 下是不同 UMO，按「群/私聊 + 号码」合并，别让它重复占榜单
  const merged = new Map();
  for (const entry of origins || []) {
    const label = originLabel(entry.origin);
    merged.set(label, (merged.get(label) || 0) + (Number(entry.count) || 0));
  }
  const rows = [...merged.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([label, count]) => row(label, count, label.startsWith("导入") ? "muted" : ""))
    .join("");
  $("ov-origin-grid").innerHTML = rows || '<p class="hint">还没有来源数据</p>';
}

function renderRecentSends(sends) {
  const rows = (sends || [])
    .map(
      (send) =>
        `<div class="ov-send" data-thumb="${send.memo_id}">` +
        `<div class="ov-send-thumb">…</div>` +
        `<div class="ov-send-body"><div class="ov-send-line">` +
        `<time>${new Date(send.time * 1000).toLocaleTimeString()}</time>` +
        `<b>${escapeHtml(VIA_NAMES[send.via] || send.via)}</b>` +
        `<span class="ov-badge">${escapeHtml(send.label)}</span></div>` +
        `<p>${escapeHtml(send.caption) || "（无描述）"}</p></div></div>`,
    )
    .join("");
  $("ov-recent-sends").innerHTML =
    rows || '<p class="hint">本次运行还没有成功发送过（记录在内存里，重载后清零）</p>';
  for (const item of $("ov-recent-sends").querySelectorAll(".ov-send")) {
    loadThumbInto(item.dataset.thumb, item.querySelector(".ov-send-thumb"));
  }
}

/* ---------- 数据 ---------- */

async function loadOverview() {
  const data = await guard(() => api.get("overview"), "读取大盘");
  if (!data || !data.ok) return;
  renderHealth(data.warnings);
  renderHero(data.library || {}, data.vision || {});
  renderLibrary(data.library || {});
  renderVision(data.vision || {});
  renderVector(data.vector || {});
  renderStorage(data.storage || { dirs: {} });
  renderDropMissing((data.storage || {}).missing);
  renderDropOrphans((data.storage || {}).orphans);
  renderSend(data.send || {});
  renderLabels((data.library || {}).labels || {});
  renderOrigins((data.library || {}).origins || []);
  renderRecentSends((data.send || {}).recent || []);
  $("ov-updated").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  if (data.reanalyze && data.reanalyze.running) startPolling();
}

/* ---------- 重新识图进度 ---------- */

function startPolling() {
  if (pollTimer) return;
  $("ov-reanalyze-progress").hidden = false;
  pollTimer = setInterval(pollReanalyze, 1500);
}

async function pollReanalyze() {
  const state = await guard(() => api.get("reanalyze/state", { scope: "empty" }), "重识进度");
  if (!state || !state.ok) return;
  const total = Number(state.total) || 0;
  const done = Number(state.done) || 0;
  $("ov-reanalyze-bar").style.width = total ? `${Math.round((done / total) * 100)}%` : "0";
  $("ov-reanalyze-text").textContent =
    `已处理 ${done}/${total}，更新 ${state.filled || 0}，失败 ${state.failed || 0}` +
    (state.message ? ` · ${state.message}` : "");
  if (!state.running) {
    clearInterval(pollTimer);
    pollTimer = null;
    $("ov-reanalyze-progress").hidden = true;
    if (state.message) toast(state.message);
    await loadOverview();
  }
}

async function doReanalyze() {
  const button = $("ov-reanalyze");
  button.disabled = true;
  const res = await guard(() => api.post("reanalyze", { scope: "empty" }), "重新识图");
  button.disabled = false;
  if (!res || !res.ok) {
    toast((res && res.message) || "重新识图没能启动", true);
    return;
  }
  toast("已开始：正在给无描述的图重跑识别");
  startPolling();
}

async function openIntegrity() {
  const report = await guard(() => api.get("integrity"), "完整性体检");
  if (!report || !report.ok) return;
  const dirs = report.dirs || {};
  const dirRows = ["blobs", "inbox", "cache", "imports", "tmp"]
    .map((key) => {
      const item = dirs[key] || {};
      return row(key, `${item.files || 0} 个 / ${fmtBytes(item.bytes)}`);
    })
    .join("");
  const missing = (report.missing_sample || [])
    .map((item) => row(`#${item.id}`, "缺"))
    .join("");
  const orphans = (report.orphan_sample || [])
    .map((rel) => row(rel, "孤儿"))
    .join("");
  openModal(
    "完整性体检",
    `<p class="hint">
       库里有 ${report.rows} 条记录；缺失文件 ${report.missing_count} 条，孤儿文件 ${report.orphan_count} 个。
     </p>
     <div class="ov-actions">
       <button class="btn small danger" id="ov-modal-drop-missing" type="button"
         ${report.missing_count ? "" : "disabled"}
         title="删掉「记录在、文件没了」的条目，删完不可恢复">删除缺失记录${
           report.missing_count ? `（${report.missing_count}）` : ""
         }</button>
       <button class="btn small danger" id="ov-drop-orphans" type="button"
         ${report.orphan_count ? "" : "disabled"}
         title="删掉没有任何记录引用的图片文件">清理孤儿${
           report.orphan_count ? `（${report.orphan_count}）` : ""
         }</button>
     </div>
     <h3>目录占用</h3><div class="ov-rows">${dirRows}</div>
     <h3>缺失样例${report.missing_count > 20 ? "（前 20）" : ""}</h3>
     <div class="ov-rows">${missing || '<p class="hint">没有缺失</p>'}</div>
     <h3>孤儿样例${report.orphan_count > 20 ? "（前 20）" : ""}</h3>
     <div class="ov-rows">${orphans || '<p class="hint">没有孤儿</p>'}</div>`,
  );
  // 弹层每次都是新的 DOM，按钮监听必须在这里挂
  $("ov-modal-drop-missing").addEventListener("click", doDropMissing);
  $("ov-drop-orphans").addEventListener("click", doOrphans);
}

async function doDropMissing() {
  if (!armConfirm("drop-missing", "删除缺失")) return;
  const res = await guard(() => api.post("integrity/missing"), "删除缺失记录");
  if (!res || !res.ok) return;
  toast(`已删除 ${res.removed || 0} 条缺失记录（共检查 ${res.scanned || 0} 条）`);
  closeModal();
  await loadOverview();
}

async function doOrphans() {
  if (!armConfirm("drop-orphans", "清理孤儿")) return;
  // 不传 paths = 按当前对账结果全删；传空数组等于「什么都不删」
  const res = await guard(() => api.post("integrity/orphans"), "清理孤儿");
  if (!res || !res.ok) return;
  toast(`清理孤儿：扫描 ${res.scanned || 0}，删除 ${res.removed || 0}，失败 ${res.failed || 0}`);
  await loadOverview();
}

async function doVacuum() {
  const res = await guard(() => api.post("integrity/vacuum"), "整理数据库");
  if (!res || !res.ok) return;
  toast(`数据库整理完成，回收 ${fmtBytes(res.saved || 0)}`);
  await loadOverview();
}

async function doReindex() {
  const res = await guard(() => api.post("reindex", { full: false }), "补齐向量");
  if (!res || !res.ok) {
    toast((res && res.message) || "补齐没能启动", true);
    return;
  }
  toast("已开始补齐向量，稍后刷新看覆盖率");
}

/* ---------- 视图切换 ---------- */

const SUB_TEXT = {
  overview: "库况、识图、检索、存储、发送，一屏看完",
  manager: "收集、审核、拉黑、导入，全在这里",
  history: "发过什么、走的哪条路、有没有发出去",
};

function switchView(view) {
  const target = ["manager", "history"].includes(view) ? view : "overview";
  document.body.dataset.view = target;
  $("view-overview").hidden = target !== "overview";
  $("view-manager").hidden = target !== "manager";
  $("view-history").hidden = target !== "history";
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.view === target;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  $("page-sub").textContent = SUB_TEXT[target];
  if (target === "overview") void loadOverview();
  if (target === "history") window.__memenestHistory?.open();
}

function bindEvents() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  }
  $("ov-refresh").addEventListener("click", () => loadOverview());
  $("ov-goto-history").addEventListener("click", () => switchView("history"));
  $("ov-drop-missing").addEventListener("click", doDropMissing);
  $("ov-reanalyze").addEventListener("click", doReanalyze);
  $("ov-integrity").addEventListener("click", openIntegrity);
  $("ov-vacuum").addEventListener("click", doVacuum);
  $("ov-orphans").addEventListener("click", doOrphans);
}

async function initOverview() {
  if (!shared) {
    $("view-overview").innerHTML =
      '<p class="hint" style="padding:16px">大盘脚本没拿到公共组件，请重载插件后重试。</p>';
    return;
  }
  bindEvents();
  switchView("overview");
}

// 供 history.js 切视图用（发送历史点某个表情时要先切回表情管理）
window.__memenestView = { switch: switchView };

initOverview();
