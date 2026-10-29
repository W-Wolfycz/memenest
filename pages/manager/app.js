const bridge = window.AstrBotPluginPage;

/* 三种状态：点一下往后走一格（审核 → 可用 → 禁用 → 审核） */
const STATE_NAME = { inbox: "审核", ready: "可用", disabled: "禁用" };
const NEXT_STATE = { inbox: "ready", ready: "disabled", disabled: "ready" };
const STATE_ICON = { inbox: "pending", ready: "ok", disabled: "pause" };

/* 21 类主情绪（全部二字）：主分类与附加情绪共用这一张表 */
const LABELS = [
  ["joy", "开心"],
  ["laugh", "大笑"],
  ["smug", "得意"],
  ["kudos", "称赞"],
  ["cheer", "鼓励"],
  ["love", "心动"],
  ["shy", "害羞"],
  ["sad", "难过"],
  ["cry", "哭泣"],
  ["upset", "生气"],
  ["disgust", "嫌弃"],
  ["blank", "无语"],
  ["awkward", "尴尬"],
  ["puzzle", "疑惑"],
  ["wow", "震惊"],
  ["alarm", "害怕"],
  ["drowsy", "疲惫"],
  ["slack", "摸鱼"],
  ["refuse", "拒绝"],
  ["expect", "期待"],
  ["other", "其他"],
];

/* 内联图标：跟文字同色（currentColor），尺寸由 CSS 控制 */
const ICONS = {
  globe:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="6.1" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
    '<path d="M8 1.9v12.2M1.9 8h12.2" stroke="currentColor" stroke-width="1.05"/>' +
    '<ellipse cx="8" cy="8" rx="2.7" ry="6.1" fill="none" stroke="currentColor" ' +
    'stroke-width="1.05"/></svg>',

  ban:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="6.1" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M3.9 12.1L12.1 3.9" stroke="currentColor" stroke-width="1.4"/></svg>',
  trash:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M2.8 4.6h10.4M6.4 4.6V3.1h3.2v1.5M4.3 4.6l.6 8.3h6.2l.6-8.3" ' +
    'fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" ' +
    'stroke-linecap="round"/></svg>',
  ok:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="6.1" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M4.9 8.3l2.1 2.1 4.1-4.5" fill="none" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg>',
  lock:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<rect x="3.6" y="7" width="8.8" height="6.2" rx="1" fill="none" ' +
    'stroke="currentColor" stroke-width="1.3"/>' +
    '<path d="M5.9 7V5.4a2.1 2.1 0 0 1 4.2 0V7" fill="none" stroke="currentColor" ' +
    'stroke-width="1.3" stroke-linecap="round"/></svg>',
  user:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="5.6" r="2.5" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
    '<path d="M3.6 13c.5-2.3 2.2-3.5 4.4-3.5s3.9 1.2 4.4 3.5" fill="none" ' +
    'stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  pencil:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M10.4 3.1l2.5 2.5-7.3 7.3-2.9.4.4-2.9z" fill="none" ' +
    'stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  pause:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="6.1" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M4.9 8h6.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  pending:
    '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="6.1" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M8 4.5v4.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
    '<circle cx="8" cy="11.2" r="0.85" fill="currentColor"/></svg>',
};
const $ = (id) => document.getElementById(id);

const state = {
  page: 1,
  size: 12,
  total: 0,
  items: [],
  selected: new Set(),
  blockedPage: 1,
  blockedTotal: 0,
  personas: [],
};

const api = {
  get: (endpoint, params) => bridge.apiGet(endpoint, params),
  post: (endpoint, body) => bridge.apiPost(endpoint, body),
};

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = isError ? "toast err" : "toast";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, isError ? 4200 : 2200);
}

async function guard(fn, label) {
  try {
    return await fn();
  } catch (error) {
    const fromBody = error && error.response && error.response.data && error.response.data.message;
    toast(`${label}失败：${fromBody || (error && error.message) || error}`, true);
    return null;
  }
}

/* 页面内二次确认：iframe 没有 allow-modals，window.confirm/prompt 会被浏览器忽略。 */
let pendingConfirm = { action: "", at: 0 };
function confirmTwice(actionName, label) {
  const now = Date.now();
  if (pendingConfirm.action === actionName && now - pendingConfirm.at < 5000) {
    pendingConfirm = { action: "", at: 0 };
    return true;
  }
  pendingConfirm = { action: actionName, at: now };
  toast(`再点一次「${label}」确认`);
  return false;
}

function runWithLimit(tasks, limit = 6) {
  let cursor = 0;
  const workers = Array.from(
    { length: Math.min(limit, Math.max(1, tasks.length)) },
    async () => {
      while (cursor < tasks.length) {
        const index = cursor;
        cursor += 1;
        try {
          await tasks[index]();
        } catch (_) {
          /* 单张缩略图失败不影响整页 */
        }
      }
    },
  );
  return Promise.all(workers);
}

/* ---------- 统计 ---------- */


function updateVectorState(vector) {
  const chip = $("vector-chip");
  if (!chip) return;
  const usable = Boolean(vector && vector.enabled && vector.model);
  // 没配向量模型时，操作按钮没有意义，直接藏起来
  for (const elementId of ["reindex-fill", "reindex-full"]) {
    const button = $(elementId);
    if (button) button.hidden = !usable;
  }
  if (!usable) {
    chip.textContent = "向量 未启用";
    chip.classList.remove("warn", "ok");
    return;
  }
  if (vector.running) {
    chip.textContent = `向量 ${vector.done || 0}/${vector.expected || 0} 重建中`;
    chip.classList.add("warn");
    return;
  }
  const covered = vector.covered || 0;
  const expected = vector.expected || 0;
  chip.textContent = `向量 ${covered}/${expected}`;
  chip.classList.toggle("warn", covered < expected);
  chip.classList.toggle("ok", expected > 0 && covered >= expected);
}

function updateQuickButtons(stats) {
  const names = {
    "bulk-review": "全部审核",
    "bulk-disable": "全部禁用",
    "bulk-enable": "全部启用",
  };
  const counts = {
    "bulk-review": stats.inbox,
    "bulk-disable": stats.ready,
    "bulk-enable": stats.disabled || 0,
  };
  for (const elementId of Object.keys(names)) {
    const button = $(elementId);
    if (!button) continue;
    const value = Number(counts[elementId]) || 0;
    button.textContent = value ? `${names[elementId]} ${value}` : names[elementId];
    // 没有可转换的条目就没必要点
    button.disabled = value === 0;
  }
}

async function loadStats() {
  const data = await guard(() => api.get("stats"), "读取统计");
  if (!data || !data.ok) return;
  // 视觉模型没配的时候，新条目会全部落成「其他」——不说出来没人知道为什么
  const sub = $("page-sub");
  sub.textContent = data.model_available
    ? "收集、审核、拉黑、导入，全在这里"
    : "没有可用的视觉模型，新收的图只会落成「其他」";
  sub.classList.toggle("warn", !data.model_available);

  const chips = [
    ["总数", data.total],
    ["可用", data.ready],
    ["待审核", data.inbox],
    ["禁用", data.disabled || 0],
    ["黑名单", data.blocked],
    ["队列", data.queue_depth],
  ];
  $("chips").innerHTML = chips
    .map(([label, value]) => `<span class="chip">${label}<b>${value}</b></span>`)
    .join("");

  const options = $("filter-persona");
  const keep = options.value;
  const personas = Object.keys(data.personas || {});
  options.innerHTML =
    '<option value="">全部</option><option value="__none__">（通用）</option>' +
    personas
      .filter((key) => key)
      .map((key) => `<option value="${escapeHtml(key)}">${escapeHtml(key)}</option>`)
      .join("");
  options.value = personas.includes(keep) || keep === "__none__" ? keep : "";

  state.personas = personas.filter((key) => key);

  $("persona-options").innerHTML = personas
    .filter((key) => key)
    .map((key) => `<option value="${escapeHtml(key)}"></option>`)
    .join("");
  state.stats = data;
  updateQuickButtons(data);
  updateVectorState(data.vector);
}

/* ---------- 列表 ---------- */

function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
  });
}

function currentFilters() {
  return {
    page: state.page,
    size: state.size,
    state: $("filter-state").value,
    label: $("filter-label").value,
    persona: normalizePersonaFilter($("filter-persona").value),
    q: $("filter-q").value.trim(),
  };
}

function normalizePersonaFilter(value) {
  // "__none__" 原样交给后端筛「只看通用」，空串才表示不限。
  return value;
}

let listToken = 0;

async function loadList() {
  const mine = ++listToken;
  const params = currentFilters();
  const data = await guard(() => api.get("list", params), "读取列表");
  if (!data || !data.ok || mine !== listToken) return;
  state.items = data.items || [];
  state.total = data.total || 0;
  const pages = Math.max(1, Math.ceil(state.total / state.size));
  if (state.page > pages) {
    // 删空最后一页后回到有内容的页。
    state.page = pages;
    return loadList();
  }
  state.selected.clear();
  renderGrid();
  renderPager();
}

function buildCard(item) {
  const card = document.createElement("article");
  card.className = "card";
  card.dataset.id = String(item.id);
  const labels = [item.label_name].concat((item.labels || []).map((entry) => entry.name));

  const personaIds = item.persona_ids || [];
  const persona = item.persona_generic
    ? '<span class="badge persona">通用</span>'
    : personaIds.length
      ? personaIds
          .slice(0, 2)
          .map((pid) => `<span class="badge persona">${escapeHtml(pid)}</span>`)
          .join("") +
        (personaIds.length > 2
          ? `<span class="badge persona">+${personaIds.length - 2}</span>`
          : "")
      : '<span class="badge persona none">谁都不用</span>';
  const caption = item.caption || "（无描述）";
  const picText = (item.text_in_image || "").trim();

  // 和状态按钮一致：显示当前值，点击切到另一个值
  const limited = item.scope === "origin";
  const scopeLabel = limited ? "限来源" : "全库";
  const scopeHint = limited
    ? "当前只在这个来源会话发送，点一下放开到所有会话"
    : item.has_origin
      ? "当前所有会话可用，点一下限制为仅来源会话"
      : "这条没有来源会话，不能设为限来源";
  const scopeButton =
    `<button type="button" class="act-btn" data-act="scope" title="${scopeHint}"` +
    `${item.has_origin || limited ? "" : " disabled"}>` +
    `${limited ? ICONS.lock : ICONS.globe}<span>${scopeLabel}</span></button>`;
  card.innerHTML = `
      <label class="pick">
        <input type="checkbox" data-pick="${item.id}" aria-label="选择表情 ${item.id}" />
      </label>
    <button type="button" class="star${item.is_fav ? " on" : ""}" data-act="fav"
      title="${item.is_fav ? "取消收藏" : "收藏"}" aria-label="收藏">${item.is_fav ? "★" : "☆"}</button>
    <div class="thumb"><span class="ph">加载中</span></div>
    <div class="meta">
      <div class="row">
        <span class="badge">${escapeHtml(labels.join(" · "))}</span>
        ${item.is_fav ? '<span class="badge is-fav">收藏</span>' : ""}
        ${persona}
      </div>
      <p class="caption">${escapeHtml(caption)}</p>
      ${picText ? `<p class="pic-text" title="图上文字">图上 ${escapeHtml(picText)}</p>` : ""}
      <div class="tags">#${item.id}${item.scope === "origin" ? " · 仅来源会话" : ""}</div>
    </div>
    <div class="actions">
      ${scopeButton}
      <button type="button" class="act-btn" data-act="bind" title="绑定人格">
        ${ICONS.user}<span>人格</span></button>
      <button type="button" class="act-btn" data-act="edit" title="编辑描述与标签">
        ${ICONS.pencil}<span>编辑</span></button>
      <button type="button" class="act-btn act-ban" data-act="ban" title="删除并拉黑">
        ${ICONS.ban}<span>拉黑</span></button>
      <button type="button" class="act-btn act-del" data-act="delete" title="删除这条表情">
        ${ICONS.trash}<span>删除</span></button>
      <button type="button" class="act-btn act-state-${item.state}" data-act="state"
        title="点击切换：审核 → 可用 → 禁用 ⇄ 可用">
        ${ICONS[STATE_ICON[item.state] || "pending"]}
        <span>${STATE_NAME[item.state] || item.state}</span></button>
    </div>`;
  return card;
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  if (!state.items.length) {
    grid.innerHTML = '<div class="empty">这个筛选下还没有表情。</div>';
    syncBulk();
    return;
  }
  for (const item of state.items) grid.appendChild(buildCard(item));
  const tasks = state.items.map((item) => () => loadThumb(item.id));
  runWithLimit(tasks);
  syncBulk();
}

async function loadThumb(id) {
  const card = document.querySelector(`.card[data-id="${id}"] .thumb`);
  if (!card || card.querySelector("img")) return;
  try {
    const res = await api.get("thumb", { id });
    // 宿主 bridge 会把顶层 data 拆包；后端已改用 image 字段，这里对字符串结果兜底。
    if (typeof res === "string" || !res || !res.ok || !res.image) {
      card.innerHTML = '<span class="ph">无缩略图</span>';
      return;
    }
    const image = new Image();
    image.dataset.thumb = String(id);
    image.alt = `表情 ${id}`;
    image.src = `data:${res.mime || "image/jpeg"};base64,${res.image}`;
    card.innerHTML = "";
    card.appendChild(image);
  } catch (_) {
    card.innerHTML = '<span class="ph">缩略图失败</span>';
  }
}

function listPages() {
  return Math.max(1, Math.ceil(state.total / state.size));
}

function renderPager() {
  const pages = listPages();
  $("page-info").textContent = `第 ${state.page} / ${pages} 页 · 共 ${state.total} 条`;
  $("first").disabled = state.page <= 1;
  $("prev").disabled = state.page <= 1;
  $("next").disabled = state.page >= pages;
  $("last").disabled = state.page >= pages;
  const jump = $("page-jump");
  jump.max = String(pages);
  jump.placeholder = String(state.page);
  jump.value = "";
}

async function goToPage(target) {
  const pages = listPages();
  const page = Math.min(pages, Math.max(1, Math.trunc(Number(target)) || 1));
  if (page === state.page) return;
  state.page = page;
  await loadList();
}

/* ---------- 批量 ---------- */

function syncBulk() {
  const count = state.selected.size;
  $("bulkbar").hidden = count === 0;
  $("selected-count").textContent = String(count);
  for (const card of document.querySelectorAll(".card")) {
    const picked = state.selected.has(Number(card.dataset.id));
    card.classList.toggle("selected", picked);
    const box = card.querySelector("input[data-pick]");
    if (box) box.checked = picked;
  }
  $("select-page").checked = state.items.length > 0 && count === state.items.length;
}

const failedReason = (res) =>
  (res && res.failed && res.failed[0] && res.failed[0].reason) || "操作未生效";

const bulkActions = {
  ban: Object.assign((ids) => api.post("ban", { ids }), { label: "拉黑" }),
  delete: Object.assign((ids) => api.post("delete", { ids }), { label: "删除" }),
};

async function runBulk(actionName) {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
    const isDestructive = actionName === "delete" || actionName === "ban";
    const needsConfirm =
      !confirmTwice(actionName, bulkActions[actionName].label || actionName);
    if (isDestructive && needsConfirm) {
    return;
  }
  const runner = bulkActions[actionName];
  if (!runner) return;
  const res = await guard(() => runner(ids), "批量操作");
  if (!res || !res.ok) {
    toast((res && res.message) || "操作失败", true);
    return;
  }
  const okCount = (res.succeeded || []).length;
  const failed = res.failed || [];
  const reason = failed.length ? `：${failed[0].reason}` : "";
    const failedPart = failed.length ? `，失败 ${failed.length} 条${reason}` : "";
    toast(`完成 ${okCount} 条${failedPart}`, failed.length > 0);
  state.selected.clear();
  await Promise.all([loadStats(), loadList()]);
}

/* ---------- 单卡操作 ---------- */

async function runCardAction(card, actionName) {
  const id = Number(card.dataset.id);
  const item = state.items.find((entry) => entry.id === id);
  if (!item) return;
  if (actionName === "delete" || actionName === "ban") {
    if (!confirmTwice(`${actionName}-${id}`, actionName === "ban" ? "拉黑" : "删除")) return;
    const res = await guard(() => api.post(actionName, { ids: [id] }), "操作");
    if (res && res.ok && (res.succeeded || []).length) {
      toast(actionName === "ban" ? "已删除并拉黑" : "已删除");
      await Promise.all([loadStats(), loadList()]);
    } else if (res && res.ok) {
      toast(failedReason(res), true);
    }
    return;
  }
  if (actionName === "state") {
    const next = NEXT_STATE[item.state] || "ready";
    const res = await guard(() => api.post("state", { id, state: next }), "切换状态");
    if (!res || !res.ok) {
      toast((res && res.message) || "切换失败", true);
      return;
    }
    toast(`已改为「${STATE_NAME[next] || next}」`);
    await Promise.all([loadStats(), loadList()]);
    return;
  }
  if (actionName === "fav") {
    const res = await guard(() => api.post("fav", { ids: [id], fav: !item.is_fav }), "收藏");
    if (res && res.ok) await loadList();
    return;
  }
  if (actionName === "scope") {
    const next = item.scope === "origin" ? "any" : "origin";
    const res = await guard(() => api.post("scope", { ids: [id], scope: next }), "作用域");
    if (res && res.ok && (res.succeeded || []).length) {
      toast(next === "origin" ? "已限制为仅来源会话" : "已放开到所有会话");
      await loadList();
    } else if (res && res.ok) {
      toast(failedReason(res), true);
    }
    return;
  }
  if (actionName === "edit") {
    openEdit(id);
    return;
  }
  if (actionName === "bind") {
    // 直接给一份可选列表，不用自己敲 id
    openPersona(id);
  }
}

/* ---------- 弹层 ---------- */

function openModal(title, body) {
  const panel = $("modal-panel");
  panel.innerHTML = `
    <div class="modal-head">
      <h2>${escapeHtml(title)}</h2>
      <button type="button" class="btn small ghost" data-close="1">关闭</button>
    </div>
    <div class="modal-body">${body}</div>`;
  $("modal").hidden = false;
  const first = panel.querySelector(".modal-body input, .modal-body textarea, .modal-body select");
  if (first) first.focus();
}

function closeModal() {
  $("modal").hidden = true;
  $("modal-panel").innerHTML = "";
  delete $("modal-panel").dataset.memoId;
}

function modalNode(selector) {
  return $("modal-panel").querySelector(selector);
}

async function openPreview(id) {
  openModal(`预览 #${id}`, '<div class="preview"><span class="hint">加载原图…</span></div>');
  const res = await guard(() => api.get("preview", { id }), "加载原图");
  const box = modalNode(".preview");
  if (!box) return;
  if (!res || !res.ok || !res.image) {
    box.innerHTML = `<span class="hint">${escapeHtml((res && res.message) || "原图读取失败")}</span>`;
    return;
  }
    const mime = res.mime || "image/jpeg";
    box.innerHTML = `<img alt="表情 ${id}" src="data:${mime};base64,${res.image}" />`;
}

const MAX_EXTRA_LABELS = 3;

function renderChips(primary, selected) {
  return LABELS.map(([key, name]) => {
    const isMain = key === primary;
    const on = !isMain && selected.has(key);
    const cls = `chip-toggle${isMain ? " main" : ""}${on ? " on" : ""}`;
    const hint = isMain ? ' title="当前主分类"' : "";
      return (
        `<button type="button" class="${cls}" data-label="${key}"${hint}>` +
        `${escapeHtml(name)}</button>`
      );
  }).join("");
}

function selectedExtraLabels() {
  return Array.from(modalNode(".chip-select").querySelectorAll(".chip-toggle.on")).map(
    (chip) => chip.dataset.label,
  );
}

function openEdit(id) {
  const item = state.items.find((entry) => entry.id === id);
  if (!item) return;
  const current = item.label;
  const extra = new Set((item.labels || []).map((entry) => entry.key));
  const options = LABELS.map(
    ([key, name]) =>
      `<option value="${key}"${key === current ? " selected" : ""}>${escapeHtml(name)}</option>`,
  ).join("");
  const chips = renderChips(current, extra);
  openModal(
    `编辑 #${id}`,
    `<div class="form">
      <label class="form-row"><span>描述 · 画面里有什么</span>
          <textarea class="edit-caption" rows="2" maxlength="80"
            >${escapeHtml(item.caption || "")}</textarea
          ></label>
      <p class="form-hint">谁、什么表情、穿什么。机器人回复在说这类内容时，会优先选它。</p>
      <label class="form-row"><span>图上文字 · 图里印着的字</span>
          <textarea class="edit-pic-text" rows="2" maxlength="200"
            >${escapeHtml(item.text_in_image || "")}</textarea
          ></label>
      <p class="form-hint">气泡、贴纸、字幕、梗图配文，逐字照抄。机器人要说的回复里出现这些字、或者意思对得上时，会优先选它。</p>
      <label class="form-row"><span>使用场景 · 聊到什么话题时会用它</span>
          <textarea class="edit-scenes" rows="3"
            >${escapeHtml((item.scenes || []).join("\n"))}</textarea
          ></label>
      <p class="form-hint">
        聊到什么话题时会用它，一行一个话题词，最多 3 个，每个 2-6 字。
        只写主题不写句子，比如「变猫娘」「写代码」「需求变更」；
        别抄图上已经印着的字，那是「图上文字」的职责。
      </p>
      <label class="form-row"><span>主分类 · 最主要的情绪</span>
        <select class="edit-label">${options}</select></label>
      <div class="form-row"><span>还可以算这几种情绪（最多 4 个，含主分类共 5 类）</span>
        <div class="chip-select">${chips}</div></div>
     </div>
     <div class="modal-actions">
       <button type="button" class="btn ghost" data-close="1">取消</button>
       <button type="button" class="btn primary" data-modal="save-edit">保存</button>
     </div>`,
  );
  $("modal-panel").dataset.memoId = String(id);

  const select = modalNode(".edit-label");
  select.addEventListener("change", () => {
    const primary = select.value;
    const picked = new Set(selectedExtraLabels());
    picked.delete(primary);
    modalNode(".chip-select").innerHTML = renderChips(primary, picked);
  });
}

async function saveEdit() {
  const id = Number($("modal-panel").dataset.memoId);
  const label = modalNode(".edit-label").value;
  const labels = selectedExtraLabels().filter((key) => key !== label);
  const body = {
    id,
    caption: modalNode(".edit-caption").value.trim(),
    text_in_image: modalNode(".edit-pic-text").value.trim(),
    scenes: modalNode(".edit-scenes").value.trim(),
    labels,
    label,
  };
  const res = await guard(() => api.post("update", body), "保存");
  if (!res || !res.ok) {
    toast((res && res.message) || "保存失败", true);
    return;
  }
  closeModal();
  toast("已保存");
  await Promise.all([loadStats(), loadList()]);
}

function openPersona(id) {
  const item = state.items.find((entry) => entry.id === id);
  if (!item) return;
  const generic = Boolean(item.persona_generic);
  const current = new Set(item.persona_ids || []);
  const genericChip =
    `<button type="button" class="chip-toggle generic${generic ? " on" : ""}" ` +
    'data-persona="*" title="任何人格都能用">通用</button>';
  const chips = state.personas.length
    ? state.personas
        .map(
          (key) =>
            `<button type="button" class="chip-toggle${current.has(key) ? " on" : ""}" ` +
            `data-persona="${escapeHtml(key)}">${escapeHtml(key)}</button>`,
        )
        .join("")
    : '<span class="hint">宿主里还没有配置人格</span>';
  openModal(
    `绑定人格 #${id}`,
    `<div class="persona-generic">${genericChip}</div>
     <div class="chip-select persona-chips">${chips}</div>
     <div class="modal-actions">
       <button type="button" class="btn ghost" data-close="1">取消</button>
       <button type="button" class="btn primary" data-modal="save-persona">保存</button>
     </div>`,
  );
  $("modal-panel").dataset.memoId = String(id);
}

function pickedPersonas() {
  return Array.from(modalNode(".persona-chips").querySelectorAll(".chip-toggle.on")).map(
    (chip) => chip.dataset.persona,
  );
}

async function bindPersona(personaIds) {
  const id = Number($("modal-panel").dataset.memoId);
  const res = await guard(() => api.post("bind", { ids: [id], persona_ids: personaIds }), "绑定");
  if (!res || !res.ok) {
    toast((res && res.message) || "绑定失败", true);
    return;
  }
  closeModal();
  if (personaIds.includes("*")) toast("已改为通用");
  else if (personaIds.length) toast(`已绑定 ${personaIds.join("、")}`);
  else toast("已设为谁都不用");
  await Promise.all([loadStats(), loadList()]);
}

/* ---------- 导入 ---------- */

const MAX_IMPORT_URLS = 20;

async function doImportUrls() {
  let urls = $("import-urls").value
    .split(/\s+/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!urls.length) {
    toast("先填图片 URL", true);
    return;
  }
  if (urls.length > MAX_IMPORT_URLS) {
    toast(`单次最多 ${MAX_IMPORT_URLS} 条，只处理前 ${MAX_IMPORT_URLS} 条`, true);
    urls = urls.slice(0, MAX_IMPORT_URLS);
  }
  const params = importParams();
  const { bar, text } = importBar(urls.length);
  const tally = importTally();
  $("do-import").disabled = true;

  // 同样一条一条来：下载 → 识别 → 入库，完成再下一条
  for (let index = 0; index < urls.length; index += 1) {
    text.textContent = `下载中 ${index + 1} / ${urls.length}`;
    const res = await guard(
      () => api.post("import", { url: urls[index], ...params }),
      "导入",
    );
    countOutcome(tally, res);
    bar.value = index + 1;
  }

  $("do-import").disabled = false;
  text.textContent = `${urls.length} / ${urls.length}`;
  $("import-status").innerHTML = `<span class="hint">${importSummary(tally)}</span>`;
  $("import-urls").value = "";
  toast(importSummary(tally));
  await loadStats();
}

function importParams() {
  return {
    persona_id: $("import-persona").value.trim(),
    scope: $("import-scope").value,
    umo: $("import-umo").value.trim(),
  };
}

function importBar(total) {
  const wrap = $("import-progress-wrap");
  wrap.hidden = false;
  const bar = $("import-progress");
  bar.max = total;
  bar.value = 0;
  return { bar, text: $("import-progress-text") };
}

function importTally() {
  return { stored: 0, duplicate: 0, rejected: 0, failed: 0 };
}

function countOutcome(tally, res) {
  if (!res || !res.ok) {
    tally.failed += 1;
    return;
  }
  const outcome = res.outcome || "";
  if (outcome in tally) {
    tally[outcome] += 1;
    // 第一个「不符合」的原因带回去，否则用户不知道是私网地址还是格式不对
    if (outcome === "rejected" && res.reason && !tally.reason) {
      tally.reason = res.reason;
    }
  } else if (outcome === "skipped") {
    tally.failed += 1;
  }
}

function importSummary(tally) {
  const rejected = tally.rejected
    ? `，不符合 ${tally.rejected}${tally.reason ? `（${tally.reason}）` : ""}`
    : "";
  return (
    `入库 ${tally.stored}` +
    (tally.duplicate ? `，重复 ${tally.duplicate}` : "") +
    rejected +
    (tally.failed ? `，失败 ${tally.failed}` : "")
  );
}

async function doImportFiles(files) {
  if (!files || !files.length) return;
  const params = importParams();
  const { bar, text } = importBar(files.length);
  const tally = importTally();
  $("do-import-files").disabled = true;

  // 一次只跑一张：上传完这张、等它识别入库，再轮到下一张。
  // 这样进度条就是真实进度，也不会把后端队列塞满后丢任务。
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    const seq = `${index + 1} / ${files.length}`;
    try {
      text.textContent = `上传中 ${seq}`;
      const uploaded = await bridge.upload("import/file", file);
      if (!uploaded || !uploaded.ok || !uploaded.job) {
        tally.failed += 1;
        toast(`${file.name} 上传失败：${(uploaded && uploaded.message) || "未知原因"}`, true);
      } else {
        text.textContent = `处理中 ${seq}`;
        const res = await api.post("import/process", {
          job: uploaded.job,
          ext: uploaded.ext,
          ...params,
        });
        countOutcome(tally, res);
      }
    } catch (error) {
      tally.failed += 1;
      toast(`${file.name} 失败：${error.message || error}`, true);
    }
    bar.value = index + 1;
  }

  text.textContent = `${files.length} / ${files.length}`;
  $("import-status").innerHTML = `<span class="hint">${importSummary(tally)}</span>`;
  $("import-files").value = "";
  $("import-files-hint").textContent = "未选择文件";
  await loadStats();
}

/* ---------- 黑名单 ---------- */

function formatTime(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

async function loadBlockedThumb(img) {
  const digest = img.dataset.bthumb;
  if (!digest) return;
  const res = await guard(() => api.get("blocked/thumb", { hash: digest }), "读取缩略图");
  if (res && res.ok && res.image) {
    img.src = `data:${res.mime || "image/jpeg"};base64,${res.image}`;
  } else {
    img.replaceWith(
      Object.assign(document.createElement("span"), {
        className: "blocked-thumb blocked-thumb--none",
        textContent: "无预览",
      }),
    );
  }
}

function blockedPages() {
  return Math.max(1, Math.ceil(state.blockedTotal / 10));
}

async function goToBlockedPage(target) {
  const pages = blockedPages();
  const page = Math.min(pages, Math.max(1, Math.trunc(Number(target)) || 1));
  if (page === state.blockedPage) return;
  state.blockedPage = page;
  await loadBlocked();
}

async function loadBlocked() {
  const data = await guard(
    () => api.get("blocked", { page: state.blockedPage, size: 10 }),
    "读取黑名单",
  );
  if (!data || !data.ok) return;
  state.blockedTotal = data.total || 0;
  const list = $("blocked-list");
  if (!data.items || !data.items.length) {
    list.innerHTML = '<span class="hint">黑名单是空的。</span>';
  } else {
    list.innerHTML = data.items
      .map((item) => {
        const when = item.created_at ? formatTime(item.created_at) : "";
        // 有缩略图就按需拉一次；拉不到（老数据/文件丢了）就显示占位
        const thumb = item.has_thumb
          ? `<img class="blocked-thumb" data-bthumb="${escapeHtml(item.hash)}" alt="" />`
          : '<span class="blocked-thumb blocked-thumb--none">无预览</span>';
        return `<div class="blocked-item">
          ${thumb}
          <div class="blocked-meta">
            <code title="${escapeHtml(item.hash)}">${escapeHtml(item.hash.slice(0, 8))}…</code>
            <span class="hint">${escapeHtml(when)}</span>
          </div>
          <button class="btn small ghost" data-unban="${escapeHtml(item.hash)}">解禁</button>
        </div>`;
      })
      .join("");
    for (const img of list.querySelectorAll("img[data-bthumb]")) {
      loadBlockedThumb(img);
    }
  }
  const pages = blockedPages();
  $("blocked-page-info").textContent =
    `第 ${state.blockedPage} / ${pages} 页 · 共 ${state.blockedTotal} 条`;
  $("blocked-first").disabled = state.blockedPage <= 1;
  $("blocked-prev").disabled = state.blockedPage <= 1;
  $("blocked-next").disabled = state.blockedPage >= pages;
  $("blocked-last").disabled = state.blockedPage >= pages;
  const jump = $("blocked-page-jump");
  jump.max = String(pages);
  jump.placeholder = String(state.blockedPage);
  jump.value = "";
}

/* ---------- 事件 ---------- */

function labelName(key) {
  const found = LABELS.find(([value]) => value === key);
  return found ? found[1] : key;
}

function fillLabelSelects() {
  const options = LABELS.map(
    ([key, name]) => `<option value="${key}">${escapeHtml(name)}</option>`,
  ).join("");
  const bulk = $("bulk-label");
  if (bulk) bulk.innerHTML = '<option value="">改为分类…</option>' + options;
  const filter = $("filter-label");
  if (filter) filter.innerHTML = '<option value="">全部</option>' + options;
}

function bindEvents() {
  const quickStates = [
    ["bulk-review", "review", "全部审核"],
    ["bulk-disable", "disable", "全部禁用"],
    ["bulk-enable", "enable", "全部启用"],
  ];
  for (const [elementId, action, title] of quickStates) {
    $(elementId).addEventListener("click", async () => {
      const counts = {
        review: state.stats && state.stats.inbox,
        disable: state.stats && state.stats.ready,
        enable: state.stats && state.stats.disabled,
      };
      const count = counts[action] || 0;
      if (!count) return;
      if (action !== "review" && !confirmTwice(action, `${title}（${count} 条）`)) return;
      const res = await guard(() => api.post("state", { action }), title);
      if (!res || !res.ok) {
        toast((res && res.message) || `${title}失败`, true);
        return;
      }
      toast(`${title}：${(res.succeeded || []).length} 条`);
      state.selected.clear();
      await Promise.all([loadStats(), loadList()]);
    });
  }

  $("bulk-label").addEventListener("change", async () => {
    const label = $("bulk-label").value;
    const ids = Array.from(state.selected);
    $("bulk-label").value = "";
    if (!label || !ids.length) return;
    const res = await guard(() => api.post("label", { ids, label }), "改分类");
    if (!res || !res.ok) {
      toast((res && res.message) || "改分类失败", true);
      return;
    }
    toast(`已把 ${(res.succeeded || []).length} 条改为${labelName(label)}`);
    state.selected.clear();
    await Promise.all([loadStats(), loadList()]);
  });

  const runReindex = async (full) => {
    if (full && !confirmTwice("reindex-full", "清空全部向量并重算")) return;
    const res = await guard(() => api.post("reindex", { full }), "向量处理");
    if (!res || !res.ok) {
      toast((res && res.message) || "操作失败", true);
      return;
    }
    toast(full ? "已开始重建（清空后全部重算）" : "已开始补齐缺失向量");
    await loadStats();
  };
  $("reindex-fill").addEventListener("click", () => runReindex(false));
  $("reindex-full").addEventListener("click", () => runReindex(true));

  $("refresh").addEventListener("click", async () => {
    await Promise.all([loadStats(), loadList()]);
    toast("已刷新");
  });

  for (const id of ["filter-state", "filter-label", "filter-persona", "page-size"]) {
    $(id).addEventListener("change", async () => {
      state.page = 1;
      state.size = Number($("page-size").value) || 12;
      await loadList();
    });
  }

  let searchTimer = null;
  $("filter-q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      state.page = 1;
      await loadList();
    }, 300);
  });

  $("first").addEventListener("click", () => goToPage(1));
  $("prev").addEventListener("click", () => goToPage(state.page - 1));
  $("next").addEventListener("click", () => goToPage(state.page + 1));
  $("last").addEventListener("click", () => goToPage(listPages()));
  $("page-jump").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    goToPage(event.target.value);
  });

  $("select-page").addEventListener("change", (event) => {
    state.selected.clear();
    if (event.target.checked) {
      for (const item of state.items) state.selected.add(item.id);
    }
    syncBulk();
  });

  $("grid").addEventListener("click", async (event) => {
    // 卡片上的按钮（含右上角星标）优先，避免被「点图预览」吞掉
    const action = event.target.closest("button[data-act]");
    if (action) {
      const card = action.closest(".card");
      if (card) await runCardAction(card, action.dataset.act);
      return;
    }
    const pick = event.target.closest("input[data-pick]");
    if (pick) {
      const id = Number(pick.dataset.pick);
      if (pick.checked) state.selected.add(id);
      else state.selected.delete(id);
      syncBulk();
      return;
    }
    // 点图片本体 = 看原图
    const thumb = event.target.closest(".thumb");
    if (thumb) {
      await openPreview(Number(thumb.closest(".card").dataset.id));
    }
  });

  $("modal").addEventListener("click", async (event) => {
    if (event.target.closest("[data-close]")) {
      closeModal();
      return;
    }

    const chip = event.target.closest(".chip-toggle");
    if (chip) {
      if (chip.dataset.persona !== undefined) {
        const panel = $("modal-panel");
        const isGeneric = chip.dataset.persona === "*";
        if (chip.classList.contains("on")) {
          chip.classList.remove("on");
        } else if (isGeneric) {
          // 选通用：清掉所有具体人格
          panel
            .querySelectorAll(".persona-chips .chip-toggle.on")
            .forEach((node) => node.classList.remove("on"));
          chip.classList.add("on");
        } else {
          // 选具体人格：通用让位
          panel.querySelector('.chip-toggle[data-persona="*"]')?.classList.remove("on");
          chip.classList.add("on");
        }
        return;
      }
      if (chip.classList.contains("main")) return;
      const picked = chip.parentElement.querySelectorAll(".chip-toggle.on");
      if (!chip.classList.contains("on") && picked.length >= MAX_EXTRA_LABELS) {
        toast(`附加情绪最多选 ${MAX_EXTRA_LABELS} 个`, true);
        return;
      }
      chip.classList.toggle("on");
      return;
    }
    const action = event.target.closest("[data-modal]");
    if (!action) return;
    if (action.dataset.modal === "save-edit") {
      await saveEdit();
    } else if (action.dataset.modal === "save-persona") {
      await bindPersona(pickedPersonas());
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("modal").hidden) closeModal();
  });

  $("bulkbar").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-bulk]");
    if (button) await runBulk(button.dataset.bulk);
  });

  $("clear-selection").addEventListener("click", () => {
    state.selected.clear();
    syncBulk();
  });

  $("toggle-import").addEventListener("click", () => {
    $("import-panel").hidden = !$("import-panel").hidden;
  });
  $("do-import").addEventListener("click", doImportUrls);
  // 选文件只记下来，点「上传所选」才真正传——避免误选一张就直接开始上传
  $("import-files").addEventListener("change", (event) => {
    const files = event.target.files;
    $("import-files-hint").textContent = files.length
      ? `已选 ${files.length} 个`
      : "未选择文件";
    $("do-import-files").disabled = !files.length;
  });
  $("do-import-files").addEventListener("click", async () => {
    const input = $("import-files");
    if (!input.files || !input.files.length) return;
    $("do-import-files").disabled = true;
    await doImportFiles(input.files);
    input.value = "";
    $("import-files-hint").textContent = "未选择文件";
  });

  $("toggle-blocked").addEventListener("click", async () => {
    const panel = $("blocked-panel");
    panel.hidden = !panel.hidden;
    if (!panel.hidden) await loadBlocked();
  });
  $("blocked-list").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-unban]");
    if (!button) return;
    const res = await guard(() => api.post("unban", { hash: button.dataset.unban }), "解禁");
    if (res && res.ok) {
      toast(`已解除 ${res.removed} 条`);
      await Promise.all([loadBlocked(), loadStats()]);
    }
  });
  $("blocked-first").addEventListener("click", () => goToBlockedPage(1));
  $("blocked-prev").addEventListener("click", () => goToBlockedPage(state.blockedPage - 1));
  $("blocked-next").addEventListener("click", () => goToBlockedPage(state.blockedPage + 1));
  $("blocked-last").addEventListener("click", () => goToBlockedPage(blockedPages()));
  $("blocked-page-jump").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    goToBlockedPage(event.target.value);
  });
}

async function init() {
  if (!bridge) {
    document.body.innerHTML = '<p style="padding:24px">bridge 未加载，请从 AstrBot 插件页打开。</p>';
    return;
  }
  await bridge.ready();
  $("page-title").textContent = bridge.t("pages.manager.title", "表情窝");
  $("page-sub").textContent = bridge.t("pages.manager.description", "收集、审核、拉黑、导入，全在这里");
  state.size = Number($("page-size").value) || 12;
  fillLabelSelects();
  bindEvents();
  await Promise.all([loadStats(), loadList()]);
}

init();
