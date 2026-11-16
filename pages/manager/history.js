/* 发送历史：分页读 send_log，按检索路筛选、按触发文本搜索。
 *
 * helper 由 app.js 挂在 window.__memenest 上；视图切换由 overview.js 的 switchView 负责，
 * 这里只暴露 open() 给那边调用（切到这个标签时按需加载第一页）。
 */

const historyShared = window.__memenest || null;
const { $, api, toast, guard, escapeHtml, VIA_NAMES, loadThumbInto } =
  historyShared || {};

const OUTCOME_NAMES = { sent: "已发", failed: "失败", missing: "文件缺失", cancelled: "取消" };

const historyState = { page: 1, size: 20, total: 0, via: "", query: "", loaded: false };

function historyOrigin(umo) {
  if (!umo) return "导入";
  const parts = String(umo).split(":");
  const session = parts[parts.length - 1];
  const kind = parts.length > 1 ? parts[1] : "";
  if (kind === "GroupMessage") return `群 ${session}`;
  if (kind === "FriendMessage") return `私聊 ${session}`;
  return String(umo);
}

function historyClock(ts) {
  const date = new Date(Number(ts) * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function renderHistory(items, total) {
  const rows = items
    .map(
      (item) =>
        `<div class="ov-send" data-thumb="${item.memo_id ?? ""}">` +
        `<div class="ov-send-thumb">…</div>` +
        `<div class="ov-send-body"><div class="ov-send-line">` +
        `<time>${historyClock(item.created_at)}</time>` +
        `<b>${escapeHtml(VIA_NAMES[item.via] || item.via)}</b>` +
        `<span class="ov-badge">${escapeHtml(item.label || "?")}</span>` +
        (item.outcome === "sent"
          ? ""
          : `<span class="ov-badge hot" title="${escapeHtml(item.detail || "")}">` +
            `${escapeHtml(OUTCOME_NAMES[item.outcome] || item.outcome)}` +
            `${item.detail ? ` · ${escapeHtml(item.detail)}` : ""}</span>`) +
        `<span class="ov-send-src">${escapeHtml(historyOrigin(item.umo))}</span>` +
        `</div><p>${escapeHtml(item.text) || "（无触发文本）"}</p></div></div>`,
    )
    .join("");
  $("history-list").innerHTML =
    rows ||
    '<p class="hint">还没有记录。发送成功/失败/取消都会记在这里，保留最近 ' +
      `${historyState.days || 90} 天。</p>`;
  for (const row of $("history-list").querySelectorAll(".ov-send")) {
    const thumb = row.querySelector(".ov-send-thumb");
    if (row.dataset.thumb) loadThumbInto(row.dataset.thumb, thumb);
    else thumb.textContent = "—";
  }
  const pages = Math.max(1, Math.ceil(total / historyState.size));
  $("history-page-info").textContent =
    `第 ${historyState.page} / ${pages} 页 · 共 ${total} 条`;
}

async function loadHistory(page = historyState.page) {
  const params = {
    page,
    size: historyState.size,
    via: historyState.via,
    q: historyState.query,
  };
  const data = await guard(() => api.get("sends/history", params), "读取发送历史");
  if (!data || !data.ok) return;
  historyState.loaded = true;
  historyState.page = Number(data.page) || 1;
  historyState.total = Number(data.total) || 0;
  historyState.days = Number(data.days) || 90;
  historyState.keep = Number(data.keep) || 0;
  renderHistory(data.items || [], historyState.total);
  $("history-hint").textContent =
    `保留最近 ${historyState.days} 天` +
    (historyState.keep ? ` / 最多 ${historyState.keep} 条（先到先裁）` : "");
}

function readFilters() {
  historyState.via = $("history-via").value;
  historyState.query = $("history-q").value.trim();
  historyState.size = Number($("history-size").value) || 20;
}

function bindHistory() {
  $("history-search").addEventListener("click", () => {
    readFilters();
    void loadHistory(1);
  });
  $("history-refresh").addEventListener("click", () => void loadHistory());
  $("history-q").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    readFilters();
    void loadHistory(1);
  });
  $("history-via").addEventListener("change", () => {
    readFilters();
    void loadHistory(1);
  });
  $("history-size").addEventListener("change", () => {
    readFilters();
    void loadHistory(1);
  });
  $("history-first").addEventListener("click", () => void loadHistory(1));
  $("history-prev").addEventListener("click", () =>
    void loadHistory(Math.max(1, historyState.page - 1)),
  );
  $("history-next").addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil(historyState.total / historyState.size));
    void loadHistory(Math.min(pages, historyState.page + 1));
  });
  $("history-last").addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil(historyState.total / historyState.size));
    void loadHistory(pages);
  });
}

function openHistory() {
  if (!historyState.loaded) {
    readFilters();
    void loadHistory(1);
  }
}

function initHistory() {
  if (!historyShared) {
    $("view-history").innerHTML =
      '<p class="hint" style="padding:16px">发送历史脚本没拿到公共组件，请重载插件后重试。</p>';
    return;
  }
  bindHistory();
}

window.__memenestHistory = { open: openHistory, load: loadHistory };

initHistory();
