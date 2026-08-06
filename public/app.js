const rowsEl = document.querySelector("#fundRows");
const stateEl = document.querySelector("#refreshState");
const refreshProgressEl = document.querySelector("#refreshProgress");
const refreshProgressBarEl = document.querySelector("#refreshProgressBar");
const refreshProgressTextEl = document.querySelector("#refreshProgressText");
const dataAlertsEl = document.querySelector("#dataAlerts");
const refreshBtn = document.querySelector("#refreshBtn");
const backtestRefreshBtn = document.querySelector("#backtestRefreshBtn");
const exportCsvBtn = document.querySelector("#exportCsvBtn");
const exportPngBtn = document.querySelector("#exportPngBtn");
const autoScrollDetailsToggle = document.querySelector("#autoScrollDetailsToggle");
const columnPickerEl = document.querySelector("#columnPicker");
const columnPickerBtn = document.querySelector("#columnPickerBtn");
const columnPickerMenu = document.querySelector("#columnPickerMenu");
const columnOptionsEl = document.querySelector("#columnOptions");
const columnPickerSummaryEl = document.querySelector("#columnPickerSummary");
const selectAllColumnsBtn = document.querySelector("#selectAllColumnsBtn");
const resetColumnsBtn = document.querySelector("#resetColumnsBtn");
const tableColsEl = document.querySelector("#fundTableCols");
const tableHeadersEl = document.querySelector("#fundTableHeaders");
const detailGridEl = document.querySelector(".detail-grid");
const holdingsEl = document.querySelector("#holdings");
const backtestEl = document.querySelector("#backtest");
const typeFiltersEl = document.querySelector("#typeFilters");
const typeFilterSummaryEl = document.querySelector("#typeFilterSummary");
const selectAllTypesBtn = document.querySelector("#selectAllTypesBtn");
const clearAllTypesBtn = document.querySelector("#clearAllTypesBtn");
const purchaseLimitFilterSummaryEl = document.querySelector("#purchaseLimitFilterSummary");
const showAllPurchaseLimitsBtn = document.querySelector("#showAllPurchaseLimitsBtn");
const hidePausedPurchaseBtn = document.querySelector("#hidePausedPurchaseBtn");
const premiumFilterSummaryEl = document.querySelector("#premiumFilterSummary");
const premiumAboveInput = document.querySelector("#premiumAboveInput");
const discountBelowInput = document.querySelector("#discountBelowInput");
const showAllPremiumsBtn = document.querySelector("#showAllPremiumsBtn");
const showPremiumAboveBtn = document.querySelector("#showPremiumAboveBtn");
const showDiscountBelowBtn = document.querySelector("#showDiscountBelowBtn");

let currentCode = null;
let currentFunds = [];
let availableTypes = [];
let selectedTypes = new Set();
let dataAlertsExpanded = false;
let lastDataAlerts = [];
let lastDataAlertCount = 0;
let hidePausedPurchase = false;
let premiumFilterMode = "all";
let autoScrollDetails = true;
let visibleColumnKeys = new Set();
let sortableHeaders = [];
let sortState = {
  key: null,
  direction: null,
};

const typeOrder = [
  "A股-指数",
  "A股-主动",
  "QDII-港股",
  "QDII-美股",
  "QDII-多市场",
  "商品-贵金属",
  "商品-原油",
  "商品-混合",
  "债券-境内债",
  "债券-美元债",
  "FOF",
];

const typeOrderRank = new Map(typeOrder.map((type, index) => [type, index]));
const defaultExcludedTypePrefixes = ["A股-"];
const COLUMN_STORAGE_KEY = "lof-inav-visible-columns-v2";
const DETAIL_SCROLL_STORAGE_KEY = "lof-inav-auto-scroll-details-v1";
const REFRESH_POLL_DELAY_MS = 3_000;
const ACTIVE_REFRESH_DELAY_MS = 60_000;
const INACTIVE_REFRESH_DELAY_MS = 30 * 60_000;
const ACTIVE_GRACE_MS = 5 * 60_000;
const ACTIVITY_EVENTS = ["click", "keydown", "mousemove", "scroll", "touchstart"];
const TABLE_RENDER_CHUNK_SIZE = 80;
const DATA_ALERT_COLLAPSED_ROWS = 5;
const DATA_ALERT_ITEMS_PER_ROW = 2;
const DATA_ALERT_COLLAPSED_LIMIT = DATA_ALERT_COLLAPSED_ROWS * DATA_ALERT_ITEMS_PER_ROW;

let refreshPollTimer = null;
let autoRefreshTimer = null;
let lastUserActivityAt = Date.now();
let lastFundsLoadAt = 0;
let loadFundsInFlight = false;
let loadFundsQueued = false;
let backtestsRefreshing = false;
let backtestRefreshInFlight = false;
let csrfToken = null;
let lastFundsPayload = null;
let detailRequestId = 0;
let detailAbortController = null;
let tableRenderToken = 0;

const tableColumns = [
  {
    key: "fund",
    title: "基金",
    width: 0.17,
    align: "left",
    className: "fund-col-name",
    required: true,
    defaultVisible: true,
    sortValue: (fund) => fund.code,
    cell: (fund) => `
      <div class="fund-name-row">
        <div class="fund-name">${escapeHtml(fund.name || "--")}</div>
        ${renderFundStatus(fund)}
      </div>
      <div class="fund-code">${escapeHtml(fund.code || "--")} / ${escapeHtml(fund.nav_date || "--")}</div>
    `,
    exportCell: (fund) => [fund.name || "--", `${fund.code || "--"} / ${fund.nav_date || "--"}`],
  },
  {
    key: "type",
    title: "类型",
    width: 0.1,
    className: "fund-col-type",
    defaultVisible: true,
    sortValue: (fund) => typeSortValue(getFundType(fund)),
    cell: (fund) => `<span class="type-pill">${escapeHtml(getFundType(fund))}</span>`,
    exportCell: (fund) => [getFundType(fund)],
  },
  {
    key: "announcement",
    title: "公告",
    width: 0.06,
    className: "fund-col-announcement",
    defaultVisible: false,
    sortValue: (fund) => fund.announcement?.publish_date,
    cell: (fund) => renderAnnouncement(fund.announcement),
    exportCell: (fund) => [fund.announcement?.publish_date || ""],
  },
  {
    key: "purchase_limit",
    title: "申购限额",
    width: 0.07,
    className: "fund-col-limit",
    defaultVisible: true,
    sortValue: (fund) => fund.purchase_limit?.sort_value,
    cell: (fund) => renderPurchaseLimit(fund.purchase_limit),
    exportCell: (fund) => [fund.purchase_limit?.display || "--"],
  },
  {
    key: "previous_nav",
    title: "上一日净值",
    width: 0.07,
    className: "fund-col-nav",
    defaultVisible: true,
    sortValue: (fund) => fund.previous_nav,
    cell: (fund) => valueWithMeta(fmt(fund.previous_nav, 4), dateOnly(fund.nav_date)),
    exportCell: (fund) => [fmt(fund.previous_nav, 4), dateOnly(fund.nav_date)],
  },
  {
    key: "trade_price",
    title: "场内价格",
    width: 0.07,
    className: "fund-col-price",
    defaultVisible: true,
    sortValue: (fund) => fund.trade_price,
    cell: (fund) => renderTradePrice(fund),
    exportCell: (fund) => [fmt(fund.trade_price, 3), minuteTime(fund.quote_time)],
  },
  {
    key: "estimated_nav",
    title: "系统估值",
    width: 0.07,
    className: "fund-col-estimate",
    defaultVisible: true,
    sortValue: (fund) => fund.estimated_nav,
    cell: (fund) => estimatedNavCell(fund),
    exportCell: (fund) => [fmt(fund.estimated_nav, 4)],
  },
  {
    key: "premium",
    title: "折溢价率",
    width: 0.07,
    className: "fund-col-premium",
    defaultVisible: true,
    sortValue: (fund) => fund.premium,
    cell: (fund) => signedPct(fund.premium),
    exportCell: (fund) => [pct(fund.premium)],
  },
  {
    key: "covered_weight",
    title: "模型 / 计价",
    width: 0.07,
    className: "fund-col-covered",
    defaultVisible: false,
    sortValue: (fund) => fund.modeled_weight,
    cell: (fund) => coverageCell(fund),
    exportCell: (fund) => [coverageText(fund)],
  },
  {
    key: "backtest_mae",
    title: "回测 MAE",
    width: 0.07,
    className: "fund-col-backtest",
    defaultVisible: false,
    sortValue: (fund) => fund.backtest?.mae_pct,
    cell: (fund) => (fund.backtest && fund.backtest.mae_pct !== undefined ? pct(fund.backtest.mae_pct) : "--"),
    exportCell: (fund) => [fund.backtest?.mae_pct !== undefined ? pct(fund.backtest.mae_pct) : "--"],
  },
  {
    key: "backtest_mae_vol",
    title: "MAE/波动",
    width: 0.07,
    className: "fund-col-backtest-vol",
    defaultVisible: false,
    sortValue: (fund) => fund.backtest?.mae_to_nav_volatility,
    cell: (fund) =>
      fund.backtest && fund.backtest.mae_to_nav_volatility !== undefined
        ? fmt(fund.backtest.mae_to_nav_volatility, 2)
        : "--",
    exportCell: (fund) => [
      fund.backtest?.mae_to_nav_volatility !== undefined ? fmt(fund.backtest.mae_to_nav_volatility, 2) : "--",
    ],
  },
  {
    key: "note",
    title: "备注",
    width: 0.1,
    align: "left",
    className: "fund-col-note",
    defaultVisible: false,
    cell: (fund) => escapeHtml(fund.note || ""),
    exportCell: (fund) => [fund.note || ""],
  },
];

function fmt(value, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return Number(value).toFixed(digits);
}

function pct(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function signedPct(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return `<span>--</span>`;
  const cls = value >= 0 ? "pos" : "neg";
  return `<span class="${cls}">${pct(value)}</span>`;
}

function navWithChange(value, previousNav) {
  const base = fmt(value, 4);
  if (!value || !previousNav) return base;
  const change = Number(value) / Number(previousNav) - 1;
  return `<span class="nav-change-value">${base} (${signedPct(change)})</span>`;
}

function dateOnly(value) {
  if (!value) return "--";
  const match = String(value).match(/^\d{4}-(\d{2})-(\d{2})$/);
  if (match) return `${match[1]}-${match[2]}`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${month}-${day}`;
}

function minuteTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${month}-${day} ${hour}:${minute}`;
}

function valueWithMeta(valueHtml, metaText) {
  return `
    <div class="value-stack">
      <div>${valueHtml}</div>
      <div class="value-meta">${escapeHtml(metaText || "--")}</div>
    </div>
  `;
}

function estimatedNavCell(fund) {
  const value = fmt(fund.estimated_nav, 4);
  const modeled = fund.modeled_weight == null ? NaN : Number(fund.modeled_weight);
  const pricedRatio = fund.priced_ratio == null ? NaN : Number(fund.priced_ratio);
  const notes = [];
  if (Number.isFinite(modeled) && modeled < 0.2) notes.push(`模型仓位 ${pct(modeled)}`);
  if (Number.isFinite(pricedRatio) && pricedRatio < 0.999) notes.push(`计价率 ${pct(pricedRatio)}`);
  return notes.length ? valueWithMeta(value, notes.join(" · ")) : value;
}

function coverageText(fund) {
  const modeled = fund.modeled_weight == null ? NaN : Number(fund.modeled_weight);
  const pricedRatio = fund.priced_ratio == null ? NaN : Number(fund.priced_ratio);
  const modeledText = Number.isFinite(modeled) ? pct(modeled) : "--";
  const pricedText = Number.isFinite(pricedRatio) ? pct(pricedRatio) : "--";
  return `模型 ${modeledText} / 计价 ${pricedText}`;
}

function coverageCell(fund) {
  const modeled = fund.modeled_weight == null ? NaN : Number(fund.modeled_weight);
  const pricedRatio = fund.priced_ratio == null ? NaN : Number(fund.priced_ratio);
  return valueWithMeta(
    Number.isFinite(modeled) ? pct(modeled) : "--",
    `计价 ${Number.isFinite(pricedRatio) ? pct(pricedRatio) : "--"}`
  );
}

function renderTradePrice(fund) {
  const priceText = fmt(fund.trade_price, 3);
  const quoteUrl = getTradeQuoteUrl(fund);
  const priceHtml = quoteUrl
    ? `<a class="quote-price-link" href="${escapeHtml(quoteUrl)}" target="_blank" rel="noreferrer" title="打开东方财富实时行情">${escapeHtml(priceText)}</a>`
    : escapeHtml(priceText);
  return valueWithMeta(priceHtml, minuteTime(fund.quote_time));
}

function getTradeQuoteUrl(fund) {
  const secid = fund.trade_secid || inferTradeSecid(fund.code);
  return secid ? `https://quote.eastmoney.com/unify/r/${encodeURIComponent(secid)}` : "";
}

function isFundError(fund) {
  return fund.status === "error";
}

function renderFundStatus(fund) {
  if (!isFundError(fund)) return "";
  const title = fund.error || "估值失败";
  return `<span class="fund-status-badge" title="${escapeHtml(title)}">估值失败</span>`;
}

function inferTradeSecid(code) {
  if (!/^\d{6}$/.test(String(code || ""))) return "";
  const market = String(code).startsWith("5") ? "1" : "0";
  return `${market}.${code}`;
}

function loadAutoScrollDetails() {
  try {
    const raw = window.localStorage.getItem(DETAIL_SCROLL_STORAGE_KEY);
    return raw === null ? true : raw === "true";
  } catch {
    return true;
  }
}

function saveAutoScrollDetails(value) {
  try {
    window.localStorage.setItem(DETAIL_SCROLL_STORAGE_KEY, String(value));
  } catch {
    // Ignore storage errors; the toggle should still work for this page load.
  }
}

function syncAutoScrollDetailsToggle() {
  if (autoScrollDetailsToggle) {
    autoScrollDetailsToggle.checked = autoScrollDetails;
  }
}

function setAutoScrollDetails(value) {
  autoScrollDetails = Boolean(value);
  saveAutoScrollDetails(autoScrollDetails);
  syncAutoScrollDetailsToggle();
}

function getDefaultColumnKeys() {
  return tableColumns.filter((column) => column.required || column.defaultVisible).map((column) => column.key);
}

function loadVisibleColumnKeys() {
  const knownKeys = new Set(tableColumns.map((column) => column.key));
  try {
    const raw = window.localStorage.getItem(COLUMN_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (Array.isArray(parsed)) {
      const keys = parsed.filter((key) => knownKeys.has(key));
      tableColumns.forEach((column) => {
        if (column.required && !keys.includes(column.key)) keys.push(column.key);
      });
      return keys.length ? keys : getDefaultColumnKeys();
    }
  } catch (error) {
    console.warn("读取表头配置失败，已使用默认配置", error);
  }
  return getDefaultColumnKeys();
}

function saveVisibleColumnKeys() {
  try {
    window.localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify([...visibleColumnKeys]));
  } catch (error) {
    console.warn("保存表头配置失败", error);
  }
}

function getVisibleColumns() {
  return tableColumns.filter((column) => visibleColumnKeys.has(column.key) || column.required);
}

function getSortAccessor(key) {
  return tableColumns.find((column) => column.key === key)?.sortValue;
}

function applyColumnVisibility(nextKeys) {
  const nextVisibleKeys = new Set(nextKeys);
  tableColumns.forEach((column) => {
    if (column.required) nextVisibleKeys.add(column.key);
  });
  visibleColumnKeys = nextVisibleKeys;
  if (sortState.key && !visibleColumnKeys.has(sortState.key)) {
    sortState = { key: null, direction: null };
  }
  saveVisibleColumnKeys();
  renderColumnControls();
  renderTableStructure();
  renderFunds(getVisibleFunds());
}

function renderColumnControls() {
  const selectedCount = getVisibleColumns().length;
  columnPickerSummaryEl.textContent = `已展示 ${selectedCount} / ${tableColumns.length} 列`;
  columnOptionsEl.innerHTML = tableColumns
    .map((column) => {
      const checked = visibleColumnKeys.has(column.key) || column.required ? "checked" : "";
      const disabled = column.required ? "disabled" : "";
      return `
        <label class="column-option">
          <input type="checkbox" value="${escapeHtml(column.key)}" ${checked} ${disabled} />
          <span>${escapeHtml(column.title)}</span>
          ${column.required ? `<span class="column-required">必选</span>` : ""}
        </label>
      `;
    })
    .join("");
  selectAllColumnsBtn.disabled = selectedCount === tableColumns.length;
  resetColumnsBtn.disabled = columnKeySetsEqual(visibleColumnKeys, new Set(getDefaultColumnKeys()));
}

function columnKeySetsEqual(left, right) {
  if (left.size !== right.size) return false;
  return [...left].every((key) => right.has(key));
}

function renderTableStructure() {
  const columns = getVisibleColumns();
  tableColsEl.innerHTML = columns.map((column) => `<col class="${escapeHtml(column.className)}" />`).join("");
  tableHeadersEl.innerHTML = columns
    .map((column) => {
      const alignClass = column.align === "left" ? " align-left" : "";
      if (!column.sortValue) {
        return `<th class="${alignClass.trim()}">${escapeHtml(column.title)}</th>`;
      }
      return `
        <th class="${alignClass.trim()}" data-sort-key="${escapeHtml(column.key)}" aria-sort="none">
          <button class="sort-button" type="button">${escapeHtml(column.title)}</button>
        </th>
      `;
    })
    .join("");
  sortableHeaders = [...tableHeadersEl.querySelectorAll("th[data-sort-key]")];
  sortableHeaders.forEach((header) => {
    header.addEventListener("click", () => cycleSort(header.dataset.sortKey));
  });
  updateSortHeaders();
}

function setColumnPickerOpen(open) {
  columnPickerMenu.hidden = !open;
  columnPickerBtn.setAttribute("aria-expanded", String(open));
}

function clearRefreshPoll() {
  if (!refreshPollTimer) return;
  window.clearTimeout(refreshPollTimer);
  refreshPollTimer = null;
}

function clearAutoRefresh() {
  if (!autoRefreshTimer) return;
  window.clearTimeout(autoRefreshTimer);
  autoRefreshTimer = null;
}

function isPageVisible() {
  return document.visibilityState !== "hidden";
}

function isPageActive(now = Date.now()) {
  return isPageVisible() && now - lastUserActivityAt <= ACTIVE_GRACE_MS;
}

function autoRefreshDelay(now = Date.now()) {
  const interval = isPageActive(now) ? ACTIVE_REFRESH_DELAY_MS : INACTIVE_REFRESH_DELAY_MS;
  const elapsed = lastFundsLoadAt ? now - lastFundsLoadAt : interval;
  return Math.max(0, interval - elapsed);
}

function scheduleAutoRefresh() {
  clearAutoRefresh();
  autoRefreshTimer = window.setTimeout(() => {
    autoRefreshTimer = null;
    if (autoRefreshDelay() <= 0) {
      loadFunds();
    } else {
      scheduleAutoRefresh();
    }
  }, autoRefreshDelay());
}

function scheduleRefreshPoll() {
  if (refreshPollTimer) return;
  refreshPollTimer = window.setTimeout(() => {
    refreshPollTimer = null;
    pollRefreshStatus();
  }, isPageActive() ? REFRESH_POLL_DELAY_MS : INACTIVE_REFRESH_DELAY_MS);
}

async function pollRefreshStatus() {
  if (loadFundsInFlight) {
    scheduleRefreshPoll();
    return;
  }
  try {
    const res = await fetch("/api/refresh-status", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const status = await res.json();
    const data = { ...(lastFundsPayload || {}), ...status };
    if (status.backtests_refreshing) backtestsRefreshing = true;
    const refreshPending = isRefreshPending(status);
    renderRefreshProgress(status.refresh_progress, refreshPending);
    renderRefreshState(data);
    backtestRefreshBtn.disabled = backtestRefreshInFlight || backtestsRefreshing;
    if (refreshPending) {
      scheduleRefreshPoll();
    } else {
      loadFunds();
    }
  } catch (error) {
    console.error("刷新状态读取失败", error);
    loadFunds();
  }
}

function markUserActive() {
  const wasActive = isPageActive();
  lastUserActivityAt = Date.now();
  if (!wasActive && isPageActive()) {
    clearAutoRefresh();
    loadFunds();
    return;
  }
  if (!autoRefreshTimer) scheduleAutoRefresh();
}

function handleVisibilityChange() {
  if (isPageVisible()) {
    lastUserActivityAt = Date.now();
    clearAutoRefresh();
    loadFunds();
    return;
  }
  scheduleAutoRefresh();
}

function isRefreshPending(data) {
  return Boolean(
    data.navs_refreshing ||
      data.reports_refreshing ||
      data.quotes_refreshing ||
      data.purchase_limits_refreshing ||
      data.backtests_refreshing
  );
}

function refreshTimeText(value, emptyText = "未刷新") {
  return value ? new Date(value).toLocaleString() : emptyText;
}

function compactRefreshTimeText(value, emptyText = "未刷新") {
  if (!value) return emptyText;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${hour}:${minute}`;
}

function compactDateText(value) {
  if (!value) return "--";
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (match) return `${match[2]}-${match[3]}`;
  return String(value);
}

function backtestDataText(status, disabled) {
  if (disabled) return "未启用";
  if (!status || status.fund_count === 0) return "无数据";
  if (status.missing_count === status.fund_count) return "无数据";

  let range = "无数据";
  if (status.oldest_latest_date && status.latest_date) {
    range =
      status.oldest_latest_date === status.latest_date
        ? status.latest_date
        : `${status.oldest_latest_date} 至 ${status.latest_date}`;
  }
  const parts = [range];
  if (status.stale_count) parts.push(`滞后 ${status.stale_count} 只`);
  if (status.missing_count) parts.push(`缺失 ${status.missing_count} 只`);
  return parts.join("，");
}

function compactBacktestDataText(status, disabled) {
  if (disabled) return "未启用";
  if (!status || status.fund_count === 0) return "无数据";
  if (status.missing_count === status.fund_count) return "无数据";

  let range = "无数据";
  if (status.oldest_latest_date && status.latest_date) {
    const oldest = compactDateText(status.oldest_latest_date);
    const latest = compactDateText(status.latest_date);
    range = oldest === latest ? latest : `${oldest}~${latest}`;
  }
  const parts = [range];
  if (status.stale_count) parts.push(`滞后${status.stale_count}`);
  if (status.missing_count) parts.push(`缺${status.missing_count}`);
  return parts.join(" ");
}

function setRefreshState(text, title = text) {
  stateEl.textContent = text;
  stateEl.title = title || text;
}

function refreshProgressPercent(progress) {
  const percent = Number(progress?.percent);
  if (Number.isFinite(percent)) return Math.max(0, Math.min(100, Math.round(percent)));
  const completed = Number(progress?.completed);
  const total = Number(progress?.total);
  if (Number.isFinite(completed) && Number.isFinite(total) && total > 0) {
    return Math.max(0, Math.min(100, Math.round((completed / total) * 100)));
  }
  return null;
}

function refreshProgressLabel(progress) {
  if (!progress) return "";
  const label = progress.label || (progress.kind === "quotes" ? "行情" : "回测");
  const percent = refreshProgressPercent(progress);
  return percent === null ? `${label}中` : `${label} ${percent}%`;
}

function renderRefreshProgress(progress, pending) {
  if (!refreshProgressEl || !refreshProgressBarEl || !refreshProgressTextEl) return;
  const percent = refreshProgressPercent(progress);
  if (!progress || (!pending && progress.status !== "running")) {
    refreshProgressEl.hidden = true;
    refreshProgressEl.classList.remove("is-indeterminate");
    refreshProgressBarEl.style.width = "0%";
    refreshProgressTextEl.textContent = "--";
    return;
  }

  refreshProgressEl.hidden = false;
  refreshProgressEl.title = [
    progress.label || "",
    progress.message || "",
    progress.elapsed_seconds ? `耗时 ${progress.elapsed_seconds}s` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  refreshProgressEl.classList.toggle("is-indeterminate", percent === null);
  refreshProgressBarEl.style.width = percent === null ? "42%" : `${percent}%`;
  refreshProgressTextEl.textContent = percent === null ? "..." : `${percent}%`;
}

function renderRefreshState(data) {
  const navRefreshTime = refreshTimeText(data.last_navs_refresh_success_at);
  const quoteRefreshTime = refreshTimeText(data.last_realtime_quotes_refresh_at);
  const reportRefreshTime = refreshTimeText(data.last_reports_refresh_success_at);
  const backtestRefreshTime = refreshTimeText(
    data.last_incremental_backtests_refresh_at,
    data.backtests_disabled ? "未启用" : "未刷新"
  );
  const backtestDataRange = backtestDataText(data.backtest_status, data.backtests_disabled);
  const progressLabel = refreshProgressLabel(data.refresh_progress);
  const refreshPrefix = [
    progressLabel && isRefreshPending(data) ? progressLabel : "",
    !progressLabel && data.navs_refreshing ? "净值刷新中" : "",
    !progressLabel && data.reports_refreshing ? "持仓检查中" : "",
    !progressLabel && data.quotes_refreshing ? "行情刷新中" : "",
    !progressLabel && data.purchase_limits_refreshing ? "申购限额刷新中" : "",
    !progressLabel && data.backtests_refreshing ? "回测刷新中" : "",
  ]
    .filter(Boolean)
    .join("，");
  const fullState = `净值 ${navRefreshTime} / 行情 ${quoteRefreshTime} / 持仓 ${reportRefreshTime} / 回测任务 ${backtestRefreshTime} / 回测数据 ${backtestDataRange}`;

  const compactParts = [
    `净值 ${compactRefreshTimeText(data.last_navs_refresh_success_at)}`,
    `行情 ${compactRefreshTimeText(data.last_realtime_quotes_refresh_at)}`,
    `回测 ${compactRefreshTimeText(data.last_incremental_backtests_refresh_at, data.backtests_disabled ? "未启用" : "未刷新")}`,
    `数据 ${compactBacktestDataText(data.backtest_status, data.backtests_disabled)}`,
  ];
  const compactState = compactParts.join(" · ");
  setRefreshState(
    refreshPrefix ? `${refreshPrefix} · ${compactState}` : compactState,
    refreshPrefix ? `${refreshPrefix}... ${fullState}` : fullState
  );
}

async function loadFunds() {
  if (loadFundsInFlight) {
    loadFundsQueued = true;
    return;
  }

  clearRefreshPoll();
  clearAutoRefresh();
  lastFundsLoadAt = Date.now();
  loadFundsInFlight = true;
  refreshBtn.disabled = true;
  backtestRefreshBtn.disabled = true;
  setRefreshState("刷新中...");

  try {
    const res = await fetch("/api/funds", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!Array.isArray(data.funds)) throw new Error("返回数据格式异常");

    if (data.csrf_token) csrfToken = data.csrf_token;
    lastFundsPayload = data;
    currentFunds = data.funds;
    const wasBacktestsRefreshing = backtestsRefreshing;
    backtestsRefreshing = Boolean(data.backtests_refreshing);
    const refreshPending = isRefreshPending(data);
    syncTypeFilters(currentFunds);
    renderFunds(getVisibleFunds());
    renderDataAlerts(data.data_alerts || [], data.data_alert_count || 0);
    renderRefreshProgress(data.refresh_progress, refreshPending);
    renderRefreshState(data);
    if (!currentCode && data.funds.length) {
      showDetails(data.funds[0].code);
    } else if (currentCode && wasBacktestsRefreshing && !backtestsRefreshing) {
      showDetails(currentCode);
    }
    if (refreshPending) {
      scheduleRefreshPoll();
    }
  } catch (error) {
    console.error("刷新基金数据失败", error);
    renderRefreshProgress(null, false);
    setRefreshState(`刷新失败：${error?.message || "未知错误"}`);
  } finally {
    loadFundsInFlight = false;
    refreshBtn.disabled = false;
    backtestRefreshBtn.disabled = backtestRefreshInFlight || backtestsRefreshing;
    if (loadFundsQueued) {
      loadFundsQueued = false;
      loadFunds();
      return;
    }
    scheduleAutoRefresh();
  }
}

async function refreshIncrementalBacktests() {
  if (backtestRefreshInFlight || backtestsRefreshing) return;

  clearRefreshPoll();
  backtestRefreshInFlight = true;
  backtestRefreshBtn.disabled = true;
  setRefreshState("启动增量回测...");

  try {
    if (!csrfToken) {
      setRefreshState("获取本地会话...");
      await loadFunds();
      if (!csrfToken) throw new Error("缺少本地会话令牌");
    }
    const res = await fetch("/api/backtests/incremental", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LOF-CSRF": csrfToken,
      },
      body: "{}",
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    backtestsRefreshing = Boolean(data.backtests_refreshing);
    renderRefreshProgress(data.refresh_progress, backtestsRefreshing);
    setRefreshState(data.message || "增量回测请求已发送");
    await loadFunds();
  } catch (error) {
    console.error("启动增量回测失败", error);
    renderRefreshProgress(null, false);
    setRefreshState(`增量回测失败：${error?.message || "未知错误"}`);
  } finally {
    backtestRefreshInFlight = false;
    backtestRefreshBtn.disabled = backtestsRefreshing;
  }
}

function renderDataAlerts(alerts, totalCount) {
  if (!dataAlertsEl) return;
  lastDataAlerts = alerts;
  lastDataAlertCount = totalCount;
  if (!alerts.length) {
    dataAlertsEl.hidden = true;
    dataAlertsEl.innerHTML = "";
    return;
  }
  dataAlertsEl.hidden = false;
  const displayedAlerts = dataAlertsExpanded ? alerts : alerts.slice(0, DATA_ALERT_COLLAPSED_LIMIT);
  const collapsedCount = Math.max(0, alerts.length - displayedAlerts.length);
  const canToggle = alerts.length > DATA_ALERT_COLLAPSED_LIMIT;
  dataAlertsEl.innerHTML = `
    <div class="data-alert-header">
      <div>
        <h2>数据警报</h2>
        <p>${escapeHtml(totalCount)} 个基金或数据项需要检查</p>
      </div>
      ${
        canToggle
          ? `<button class="data-alert-toggle" type="button" data-alert-toggle>${dataAlertsExpanded ? "收起" : `展开其余 ${collapsedCount} 条`}</button>`
          : ""
      }
    </div>
    <div class="data-alert-list">
      ${displayedAlerts.map(renderDataAlert).join("")}
    </div>
  `;
}

function renderDataAlert(alert) {
  const details = alertDetailsText(alert);
  return `
    <button class="data-alert-item" type="button" title="${escapeHtml(details)}" data-alert-code="${escapeHtml(alert.code)}">
      <span class="data-alert-code">${escapeHtml(alert.code)}</span>
      ${alert.fund_type ? `<span class="data-alert-type">${escapeHtml(alert.fund_type)}</span>` : ""}
      <span>${escapeHtml(alert.message)}</span>
    </button>
  `;
}

function alertDetailsText(alert) {
  if (Array.isArray(alert.details)) return alert.details.join("\n");
  const details = alert.details || {};
  const rows = [];
  for (const item of details.asset_stale || []) {
    rows.push(
      `价格 ${item.secid} ${item.name}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
    );
  }
  for (const item of details.asset_market_closed || []) {
    rows.push(
      `休市 ${item.secid} ${item.name}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
    );
  }
  for (const item of details.fx_stale || []) {
    rows.push(
      `汇率 ${item.fx_secid} for ${item.secid}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
    );
  }
  for (const item of details.missing || []) {
    rows.push(`缺失 ${item.reason} ${item.secid} ${item.name}, 权重 ${pct(item.weight)}`);
  }
  return rows.join("\n");
}

function getVisibleFunds() {
  const filteredFunds = currentFunds.filter(
    (fund) =>
      selectedTypes.has(getFundType(fund)) && purchaseLimitMatchesFilter(fund) && premiumMatchesFilter(fund)
  );
  if (!sortState.key || !sortState.direction) return filteredFunds;
  const getValue = getSortAccessor(sortState.key);
  if (!getValue) return filteredFunds;

  return [...filteredFunds].sort((a, b) => {
    const aValue = getValue(a);
    const bValue = getValue(b);
    const aEmpty = isEmptySortValue(aValue);
    const bEmpty = isEmptySortValue(bValue);
    if (aEmpty && bEmpty) return 0;
    if (aEmpty) return 1;
    if (bEmpty) return -1;

    const result = compareValues(aValue, bValue);
    return sortState.direction === "asc" ? result : -result;
  });
}

function purchaseLimitMatchesFilter(fund) {
  return !hidePausedPurchase || fund.purchase_limit?.display !== "暂停";
}

function premiumMatchesFilter(fund) {
  if (premiumFilterMode === "all") return true;
  const premium = Number(fund.premium);
  if (!Number.isFinite(premium)) return false;
  if (premiumFilterMode === "premiumAbove") return premium > getPremiumAboveThreshold();
  if (premiumFilterMode === "discountBelow") return premium < -getDiscountBelowThreshold();
  return true;
}

function getFundType(fund) {
  return fund.type || "其他";
}

function typeSortValue(type) {
  const rank = typeOrderRank.has(type) ? typeOrderRank.get(type) : typeOrder.length;
  return `${String(rank).padStart(2, "0")}-${type}`;
}

function syncTypeFilters(funds) {
  const nextTypes = [...new Set(funds.map(getFundType))].sort(compareTypes);
  const previousTypes = new Set(availableTypes);
  const hadTypes = availableTypes.length > 0;
  availableTypes = nextTypes;

  if (!hadTypes) {
    selectedTypes = new Set(availableTypes.filter(isDefaultSelectedType));
  } else {
    selectedTypes = new Set([...selectedTypes].filter((type) => availableTypes.includes(type)));
    availableTypes.forEach((type) => {
      if (!previousTypes.has(type)) selectedTypes.add(type);
    });
  }

  renderTypeFilters();
}

function isDefaultSelectedType(type) {
  return !defaultExcludedTypePrefixes.some((prefix) => String(type).startsWith(prefix));
}

function compareTypes(a, b) {
  const aRank = typeOrderRank.has(a) ? typeOrderRank.get(a) : typeOrder.length;
  const bRank = typeOrderRank.has(b) ? typeOrderRank.get(b) : typeOrder.length;
  if (aRank !== bRank) return aRank - bRank;
  return String(a).localeCompare(String(b), "zh-CN", {
    numeric: true,
    sensitivity: "base",
  });
}

function renderTypeFilters() {
  typeFiltersEl.innerHTML = availableTypes
    .map((type) => {
      const checked = selectedTypes.has(type) ? "checked" : "";
      const count = currentFunds.filter((fund) => getFundType(fund) === type).length;
      return `
        <label class="type-filter-option">
          <input type="checkbox" value="${escapeHtml(type)}" ${checked} />
          <span>${escapeHtml(type)}</span>
          <span class="type-filter-count">${count}</span>
        </label>
      `;
    })
    .join("");

  [...typeFiltersEl.querySelectorAll("input[type='checkbox']")].forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) {
        selectedTypes.add(input.value);
      } else {
        selectedTypes.delete(input.value);
      }
      updateTypeFilterSummary();
      renderFunds(getVisibleFunds());
    });
  });
  updateTypeFilterSummary();
}

function updateTypeFilterSummary() {
  const selectedCount = selectedTypes.size;
  const totalCount = availableTypes.length;
  typeFilterSummaryEl.textContent =
    selectedCount === totalCount
      ? `已选择全部 ${totalCount} 个类型`
      : `已选择 ${selectedCount} / ${totalCount} 个类型`;
  selectAllTypesBtn.disabled = selectedCount === totalCount;
  clearAllTypesBtn.disabled = selectedCount === 0;
}

function setPurchaseLimitFilter(nextHidePausedPurchase) {
  hidePausedPurchase = nextHidePausedPurchase;
  renderPurchaseLimitFilter();
  renderFunds(getVisibleFunds());
}

function renderPurchaseLimitFilter() {
  showAllPurchaseLimitsBtn.setAttribute("aria-pressed", String(!hidePausedPurchase));
  hidePausedPurchaseBtn.setAttribute("aria-pressed", String(hidePausedPurchase));
  purchaseLimitFilterSummaryEl.textContent = hidePausedPurchase ? "已屏蔽暂停申购基金" : "显示全部申购状态";
}

function setPremiumFilter(nextMode) {
  premiumFilterMode = nextMode;
  renderPremiumFilter();
  renderFunds(getVisibleFunds());
}

function setPremiumThreshold() {
  renderPremiumFilter();
  if (premiumFilterMode !== "all") renderFunds(getVisibleFunds());
}

function renderPremiumFilter() {
  showAllPremiumsBtn.setAttribute("aria-pressed", String(premiumFilterMode === "all"));
  showPremiumAboveBtn.setAttribute("aria-pressed", String(premiumFilterMode === "premiumAbove"));
  showDiscountBelowBtn.setAttribute("aria-pressed", String(premiumFilterMode === "discountBelow"));
  premiumFilterSummaryEl.textContent = getPremiumFilterLabel();
}

function getThresholdPct(input) {
  const value = Number(input.value);
  if (!Number.isFinite(value) || value < 0) return 0;
  return value;
}

function getPremiumAboveThreshold() {
  return getThresholdPct(premiumAboveInput) / 100;
}

function getDiscountBelowThreshold() {
  return getThresholdPct(discountBelowInput) / 100;
}

function formatThresholdPct(input) {
  return `${getThresholdPct(input).toLocaleString("zh-CN", { maximumFractionDigits: 4 })}%`;
}

function getPremiumFilterLabel() {
  if (premiumFilterMode === "premiumAbove") return `仅显示溢价 > ${formatThresholdPct(premiumAboveInput)}`;
  if (premiumFilterMode === "discountBelow") return `仅显示折价 < -${formatThresholdPct(discountBelowInput)}`;
  return "显示全部折溢价";
}

function getActiveFilterSummary() {
  const typeText =
    selectedTypes.size === availableTypes.length
      ? `全部 ${availableTypes.length} 个类型`
      : `${selectedTypes.size} / ${availableTypes.length} 个类型`;
  const purchaseText = hidePausedPurchase ? "屏蔽暂停申购" : "全部申购状态";
  const columnText = `展示列 ${getVisibleColumns().length} / ${tableColumns.length}`;
  return `筛选：${typeText} / ${purchaseText} / ${getPremiumFilterLabel()} / ${columnText}`;
}

function selectAllTypes() {
  selectedTypes = new Set(availableTypes);
  renderTypeFilters();
  renderFunds(getVisibleFunds());
}

function clearAllTypes() {
  selectedTypes = new Set();
  renderTypeFilters();
  renderFunds(getVisibleFunds());
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    };
    return entities[char];
  });
}

function isEmptySortValue(value) {
  return value === null || value === undefined || value === "";
}

function compareValues(a, b) {
  const aNum = Number(a);
  const bNum = Number(b);
  if (Number.isFinite(aNum) && Number.isFinite(bNum)) {
    return aNum - bNum;
  }

  return String(a).localeCompare(String(b), "zh-CN", {
    numeric: true,
    sensitivity: "base",
  });
}

function cycleSort(key) {
  if (sortState.key !== key) {
    sortState = { key, direction: "asc" };
  } else if (sortState.direction === "asc") {
    sortState = { key, direction: "desc" };
  } else {
    sortState = { key: null, direction: null };
  }
  updateSortHeaders();
  renderFunds(getVisibleFunds());
}

function updateSortHeaders() {
  sortableHeaders.forEach((header) => {
    const button = header.querySelector(".sort-button");
    const isActive = header.dataset.sortKey === sortState.key;
    const direction = isActive ? sortState.direction : null;
    header.setAttribute("aria-sort", direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none");
    button.dataset.sortDirection = direction || "none";
  });
}

function scheduleTableRender(callback) {
  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(callback);
    return;
  }
  setTimeout(callback, 0);
}

function renderFundRow(fund, columns) {
  const rowClass = isFundError(fund) ? ` class="fund-row-error"` : "";
  return `
    <tr data-code="${escapeHtml(fund.code)}"${rowClass}>
      ${columns
        .map((column) => {
          const alignClass = column.align === "left" ? " align-left" : "";
          return `<td class="${alignClass.trim()}">${column.cell(fund)}</td>`;
        })
        .join("")}
    </tr>
  `;
}

function appendFundRowsChunk(funds, columns, startIndex, renderToken) {
  if (renderToken !== tableRenderToken) return;
  const endIndex = Math.min(startIndex + TABLE_RENDER_CHUNK_SIZE, funds.length);
  rowsEl.insertAdjacentHTML(
    "beforeend",
    funds
      .slice(startIndex, endIndex)
      .map((fund) => renderFundRow(fund, columns))
      .join("")
  );
  if (endIndex < funds.length) {
    scheduleTableRender(() => appendFundRowsChunk(funds, columns, endIndex, renderToken));
  }
}

function renderFunds(funds) {
  const renderToken = ++tableRenderToken;
  const columns = getVisibleColumns();
  if (!funds.length) {
    rowsEl.innerHTML = `
      <tr>
        <td class="empty-state" colspan="${columns.length}">当前筛选下暂无基金</td>
      </tr>
    `;
    return;
  }

  rowsEl.innerHTML = "";
  appendFundRowsChunk(funds, columns, 0, renderToken);
}

function renderPurchaseLimit(purchaseLimit) {
  if (!purchaseLimit || !purchaseLimit.display) return "--";
  const display = escapeHtml(purchaseLimit.display);
  return purchaseLimit.stale ? valueWithMeta(display, "待更新") : display;
}

function renderAnnouncement(announcement) {
  if (!announcement || !announcement.url) return "";
  const pdfUrl = getAnnouncementPdfUrl(announcement);
  return `
    <div class="announcement-cell">
      <a class="announce-link" href="${escapeHtml(announcement.url)}" target="_blank" rel="noreferrer" title="${escapeHtml(announcement.title || "")}">
        ${escapeHtml(announcement.publish_date || "最新公告")}
      </a>
      ${
        pdfUrl
          ? `<a class="announcement-pdf-link" href="${escapeHtml(pdfUrl)}" target="_blank" rel="noreferrer" title="打开公告 PDF">pdf</a>`
          : ""
      }
    </div>
  `;
}

function getAnnouncementPdfUrl(announcement) {
  const announcementId = announcement.announcement_id || announcement.url.match(/AN\d+/)?.[0];
  if (!announcementId) return "";
  return `https://pdf.dfcfw.com/pdf/H2_${announcementId}_1.pdf`;
}

async function showDetails(code, options = {}) {
  currentCode = code;
  const requestId = ++detailRequestId;
  if (detailAbortController) detailAbortController.abort();
  detailAbortController = new AbortController();
  holdingsEl.innerHTML = `<div class="muted">加载中...</div>`;
  backtestEl.innerHTML = `<div class="muted">加载中...</div>`;
  try {
    const [holdings, backtest] = await Promise.all([
      fetchJson(`/api/funds/${code}/holdings`, { signal: detailAbortController.signal }),
      fetchJson(`/api/funds/${code}/backtest`, { signal: detailAbortController.signal }),
    ]);
    if (requestId !== detailRequestId) return;
    holdingsEl.innerHTML = renderHoldings(holdings.holdings || []);
    backtestEl.innerHTML = renderBacktest(backtest.rows || []);
    if (options.scrollToDetails) {
      scrollToDetails();
    }
  } catch (error) {
    if (error?.name === "AbortError" || requestId !== detailRequestId) return;
    console.error("加载基金详情失败", error);
    const message = escapeHtml(error?.message || "未知错误");
    holdingsEl.innerHTML = `<div class="muted">加载失败：${message}</div>`;
    backtestEl.innerHTML = `<div class="muted">加载失败：${message}</div>`;
  } finally {
    if (requestId === detailRequestId) detailAbortController = null;
  }
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, { cache: "no-store", ...options });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function scrollToDetails() {
  if (!detailGridEl) return;
  const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  detailGridEl.scrollIntoView({
    behavior: prefersReducedMotion ? "auto" : "smooth",
    block: "start",
  });
}

function renderHoldings(rows) {
  if (!rows.length) return `<div class="muted">暂无持仓数据</div>`;
  return `
    <div class="detail-table-wrap" role="region" aria-label="持仓明细表" tabindex="0">
    <table class="mini-table holdings-table">
      <colgroup>
        <col class="holding-col-asset" />
        <col class="holding-col-code" />
        <col class="holding-col-weight" />
        <col class="holding-col-price" />
        <col class="holding-col-time" />
        <col class="holding-col-source" />
      </colgroup>
      <thead><tr><th>资产</th><th>代码</th><th>权重</th><th>最新价格</th><th>价格时间</th><th>来源</th></tr></thead>
      <tbody>
        ${rows
          .map(
            (row) => `
          <tr>
            <td>${escapeHtml(row.name)}</td>
            <td>${escapeHtml(row.secid)}</td>
            <td>${pct(row.weight)}</td>
            <td>${fmt(row.quote_price, 4)}</td>
            <td><span class="value-meta">${escapeHtml(minuteTime(row.quote_time || row.quote_updated_at))}</span></td>
            <td><span class="tag">${escapeHtml(row.source)}</span></td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>
    </div>
  `;
}

function renderBacktest(rows) {
  if (!rows.length) return `<div class="muted">暂无回测数据</div>`;
  return `
    <div class="detail-table-wrap" role="region" aria-label="最近回测表" tabindex="0">
    <table class="mini-table backtest-table">
      <thead><tr><th>日期</th><th>实际净值</th><th>价格</th><th>折溢价</th><th>估算净值</th><th>误差</th><th>计价 / 模型</th><th>数据质量</th></tr></thead>
      <tbody>
        ${rows
          .slice(0, 20)
          .map(
            (row) => `
          <tr>
            <td>${row.date}</td>
            <td>${navWithChange(row.actual_nav, row.previous_nav)}</td>
            <td>${fmt(row.trade_close, 4)}</td>
            <td>${signedPct(row.close_premium)}</td>
            <td>${navWithChange(row.estimated_nav, row.previous_nav)}</td>
            <td>${signedPct(row.error_pct)}</td>
            <td>${pct(row.priced_ratio)} / ${pct(row.modeled_weight)}</td>
            <td>${renderBacktestQuality(row)}</td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>
    </div>
  `;
}

function renderBacktestQuality(row) {
  const diagnostics = row?.price_diagnostics;
  if (row?.data_quality === "low_coverage") {
    return `<span class="quality-warn">计价不足 ${pct(row.priced_ratio)}</span>`;
  }
  if (row?.data_quality === "outlier") {
    return `<span class="quality-warn">误差异常 ${signedPct(row.error_pct)}</span>`;
  }
  if (!diagnostics) return "--";
  const assetWeight = Number(diagnostics.asset_stale_weight || 0);
  const marketClosedWeight = Number(diagnostics.asset_market_closed_weight || 0);
  const fxWeight = Number(diagnostics.fx_stale_weight || 0);
  const missingWeight = Number(diagnostics.missing_weight || 0);
  const total = assetWeight + fxWeight + missingWeight;
  const marketClosedRows = (diagnostics.asset_market_closed || []).map(
    (item) =>
      `休市 ${item.secid} ${item.name}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
  );
  if (!total) {
    if (!marketClosedWeight) return `<span class="quality-ok">输入完整</span>`;
    return `<span class="quality-ok" title="${escapeHtml(marketClosedRows.join("\n"))}">休市回退 ${pct(marketClosedWeight)}</span>`;
  }
  const details = [];
  if (assetWeight) details.push(`价格回退 ${pct(assetWeight)}`);
  if (marketClosedWeight) details.push(`休市回退 ${pct(marketClosedWeight)}`);
  if (fxWeight) details.push(`汇率回退 ${pct(fxWeight)}`);
  if (missingWeight) details.push(`缺失 ${pct(missingWeight)}`);
  const title = [
    ...(diagnostics.asset_stale || []).map(
      (item) =>
        `价格 ${item.secid} ${item.name}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
    ),
    ...marketClosedRows,
    ...(diagnostics.fx_stale || []).map(
      (item) =>
        `汇率 ${item.fx_secid} for ${item.secid}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
    ),
    ...(diagnostics.missing || []).map(
      (item) => `缺失 ${item.reason} ${item.secid} ${item.name}, 权重 ${pct(item.weight)}`
    ),
  ].join("\n");
  return `<span class="quality-warn" title="${escapeHtml(title)}">${escapeHtml(details.join(" / "))}</span>`;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function getExportColumns() {
  return getVisibleColumns();
}

function csvEscape(value) {
  let text = String(value ?? "");
  if (/^[\t ]*[=+\-@]/.test(text)) text = `'${text}`;
  if (/[",\r\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function exportCsv() {
  if (!currentFunds.length) return;

  const funds = getVisibleFunds();
  const columns = getExportColumns();
  const header = columns.map((column) => csvEscape(column.title)).join(",");
  const rows = funds.map((fund) =>
    columns
      .map((column) => csvEscape(column.exportCell(fund).filter(Boolean).join(" ")))
      .join(",")
  );
  const content = `\ufeff${[header, ...rows].join("\r\n")}\r\n`;
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const timestamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
  const suffix =
    premiumFilterMode === "all"
      ? "all"
      : premiumFilterMode === "premiumAbove"
        ? `premium-gt-${formatFilenameThreshold(premiumAboveInput)}pct`
        : `discount-lt-minus-${formatFilenameThreshold(discountBelowInput)}pct`;
  downloadBlob(blob, `lof-inav-${suffix}-${timestamp}.csv`);
}

function formatFilenameThreshold(input) {
  return String(getThresholdPct(input)).replace(/\./g, "p");
}

function getExportCellColor(fund, key) {
  if (key !== "premium" || fund.premium === null || fund.premium === undefined || Number.isNaN(fund.premium)) {
    return "#17202a";
  }
  return fund.premium >= 0 ? "#c92a2a" : "#087f5b";
}

function wrapCanvasText(context, text, maxWidth) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) return [""];
  const lines = [];
  let line = "";
  for (const char of normalized) {
    const next = line + char;
    if (line && context.measureText(next).width > maxWidth) {
      lines.push(line);
      line = char;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function drawWrappedText(context, text, x, y, maxWidth, lineHeight) {
  const lines = wrapCanvasText(context, text, maxWidth);
  lines.forEach((line, index) => {
    context.fillText(line, x, y + index * lineHeight);
  });
  return lines.length * lineHeight;
}

function drawMultilineCell(context, lines, x, y, width, height, options = {}) {
  const lineHeight = options.lineHeight || 17;
  const paddingX = options.paddingX || 8;
  const align = options.align || "center";
  const colors = options.colors || [];
  const totalTextHeight = lines.length * lineHeight;
  let textY = y + (height - totalTextHeight) / 2 + lineHeight * 0.78;

  context.textAlign = align;
  context.textBaseline = "alphabetic";
  lines.forEach((line, index) => {
    context.fillStyle = colors[index] || options.color || "#17202a";
    const textX = align === "left" ? x + paddingX : x + width / 2;
    context.fillText(line, textX, textY);
    textY += lineHeight;
  });
}

function getExportColumnWidths(width, columns = getExportColumns()) {
  const totalWeight = columns.reduce((sum, column) => sum + column.width, 0) || 1;
  const colWidths = columns.map((column) => Math.round((column.width / totalWeight) * width));
  colWidths[colWidths.length - 1] += width - colWidths.reduce((sum, value) => sum + value, 0);
  return colWidths;
}

function getExportRowHeight(context, fund, colWidths, columns = getExportColumns()) {
  const maxLineCount = columns.reduce((maxLines, column, index) => {
    const colWidth = colWidths[index];
    const lines = column.exportCell(fund).flatMap((line) => wrapCanvasText(context, line, colWidth - 16));
    return Math.max(maxLines, lines.length);
  }, 1);
  return Math.max(52, 18 + maxLineCount * 17);
}

function drawExportTable(context, funds, x, y, width) {
  const lineColor = "#d9dee7";
  const headerHeight = 42;
  const emptyHeight = 72;
  const columns = getExportColumns();
  const colWidths = getExportColumnWidths(width, columns);

  context.save();
  context.strokeStyle = lineColor;
  context.lineWidth = 1;
  context.fillStyle = "#eef2f6";
  context.fillRect(x, y, width, headerHeight);
  context.strokeRect(x, y, width, headerHeight);
  context.font = "700 12px 'Microsoft YaHei', 'Segoe UI', sans-serif";
  context.fillStyle = "#415064";

  let currentX = x;
  columns.forEach((column, index) => {
    const colWidth = colWidths[index];
    drawMultilineCell(context, [column.title], currentX, y, colWidth, headerHeight, {
      align: column.align || "center",
      color: "#415064",
      lineHeight: 16,
    });
    currentX += colWidth;
    context.beginPath();
    context.moveTo(currentX, y);
    context.lineTo(currentX, y + headerHeight);
    context.stroke();
  });

  let currentY = y + headerHeight;
  context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";

  if (!funds.length) {
    context.fillStyle = "#fff";
    context.fillRect(x, currentY, width, emptyHeight);
    context.strokeRect(x, currentY, width, emptyHeight);
    drawMultilineCell(context, ["当前筛选下暂无基金"], x, currentY, width, emptyHeight, {
      color: "#627086",
      lineHeight: 18,
    });
    context.restore();
    return currentY + emptyHeight;
  }

  funds.forEach((fund) => {
    const rowHeight = getExportRowHeight(context, fund, colWidths, columns);
    context.fillStyle = "#fff";
    context.fillRect(x, currentY, width, rowHeight);
    context.strokeStyle = lineColor;
    context.strokeRect(x, currentY, width, rowHeight);

    currentX = x;
    columns.forEach((column, index) => {
      const colWidth = colWidths[index];
      const lines = column.exportCell(fund).flatMap((line) => wrapCanvasText(context, line, colWidth - 16));
      const colors = lines.map((line, lineIndex) => {
        if (column.key === "fund" && lineIndex > 0) return "#627086";
        if (column.key === "previous_nav" && lineIndex > 0) return "#627086";
        if (column.key === "trade_price" && lineIndex > 0) return "#627086";
        return getExportCellColor(fund, column.key);
      });
      drawMultilineCell(context, lines, currentX, currentY, colWidth, rowHeight, {
        align: column.align || "center",
        colors,
        lineHeight: 17,
      });
      currentX += colWidth;
      context.beginPath();
      context.moveTo(currentX, currentY);
      context.lineTo(currentX, currentY + rowHeight);
      context.stroke();
    });

    currentY += rowHeight;
  });

  context.restore();
  return currentY;
}

async function exportPng() {
  if (!currentFunds.length) return;

  const originalText = exportPngBtn.textContent;
  exportPngBtn.disabled = true;
  exportPngBtn.textContent = "导出中...";

  try {
    const funds = getVisibleFunds();
    const preferredScale = 2;
    const maxCanvasDimension = 16000;
    const maxCanvasPixels = 32_000_000;
    const width = 1440;
    const margin = 24;
    const contentWidth = width - margin * 2;
    const disclaimerText = document.querySelector(".disclaimer p")?.textContent || "";
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const columns = getExportColumns();

    context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";
    const disclaimerLines = wrapCanvasText(context, disclaimerText, contentWidth - 32);
    const colWidths = getExportColumnWidths(contentWidth, columns);
    const rowHeights = funds.map((fund) => {
      context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";
      return getExportRowHeight(context, fund, colWidths, columns);
    });
    const tableHeight = 42 + (funds.length ? rowHeights.reduce((sum, value) => sum + value, 0) : 72);
    const disclaimerHeight = 52 + disclaimerLines.length * 22;
    const height = margin + 78 + 18 + tableHeight + 16 + disclaimerHeight + margin;
    const scale = Math.min(
      preferredScale,
      maxCanvasDimension / width,
      maxCanvasDimension / height,
      Math.sqrt(maxCanvasPixels / (width * height))
    );

    canvas.width = Math.max(1, Math.floor(width * scale));
    canvas.height = Math.max(1, Math.floor(height * scale));
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.fillStyle = "#fff";
    context.fillRect(0, 0, width, height);

    context.fillStyle = "#17202a";
    context.font = "700 26px 'Microsoft YaHei', 'Segoe UI', sans-serif";
    context.textAlign = "left";
    context.fillText(document.querySelector("h1")?.textContent || "LOF iNAV", margin, margin + 26);
    context.fillStyle = "#627086";
    context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";
    context.fillText(document.querySelector(".topbar p")?.textContent || "", margin, margin + 49);
    context.fillText(getActiveFilterSummary(), margin, margin + 70);
    context.textAlign = "right";
    context.fillText(`刷新时间：${stateEl.textContent || "--"}`, width - margin, margin + 26);

    let currentY = margin + 96;
    currentY = drawExportTable(context, funds, margin, currentY, contentWidth);

    currentY += 16;
    context.strokeStyle = "#d9dee7";
    context.fillStyle = "#fff";
    context.fillRect(margin, currentY, contentWidth, disclaimerHeight);
    context.strokeRect(margin, currentY, contentWidth, disclaimerHeight);
    context.fillStyle = "#17202a";
    context.font = "700 16px 'Microsoft YaHei', 'Segoe UI', sans-serif";
    context.textAlign = "left";
    context.fillText("免责声明", margin + 16, currentY + 28);
    context.fillStyle = "#627086";
    context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";
    drawWrappedText(context, disclaimerText, margin + 16, currentY + 56, contentWidth - 32, 22);

    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("PNG 生成失败");
    const timestamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    downloadBlob(blob, `lof-inav-${timestamp}.png`);
  } finally {
    exportPngBtn.disabled = false;
    exportPngBtn.textContent = originalText;
  }
}

refreshBtn.addEventListener("click", loadFunds);
backtestRefreshBtn.addEventListener("click", refreshIncrementalBacktests);
rowsEl.addEventListener("click", (event) => {
  if (event.target.closest("a")) return;
  const row = event.target.closest("tr[data-code]");
  if (!row) return;
  showDetails(row.dataset.code, { scrollToDetails: autoScrollDetails });
});
dataAlertsEl?.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-alert-toggle]");
  if (toggle) {
    dataAlertsExpanded = !dataAlertsExpanded;
    renderDataAlerts(lastDataAlerts, lastDataAlertCount);
    return;
  }
  const item = event.target.closest("[data-alert-code]");
  if (!item) return;
  showDetails(item.dataset.alertCode, { scrollToDetails: autoScrollDetails });
});
exportCsvBtn.addEventListener("click", exportCsv);
exportPngBtn.addEventListener("click", exportPng);
autoScrollDetailsToggle.addEventListener("change", () => setAutoScrollDetails(autoScrollDetailsToggle.checked));
columnPickerBtn.addEventListener("click", () => setColumnPickerOpen(columnPickerMenu.hidden));
columnOptionsEl.addEventListener("change", (event) => {
  const input = event.target.closest("input[type='checkbox']");
  if (!input || input.disabled) return;
  const nextKeys = new Set(visibleColumnKeys);
  if (input.checked) {
    nextKeys.add(input.value);
  } else {
    nextKeys.delete(input.value);
  }
  applyColumnVisibility(nextKeys);
});
selectAllColumnsBtn.addEventListener("click", () => applyColumnVisibility(tableColumns.map((column) => column.key)));
resetColumnsBtn.addEventListener("click", () => applyColumnVisibility(getDefaultColumnKeys()));
document.addEventListener("click", (event) => {
  if (!columnPickerEl.contains(event.target)) setColumnPickerOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setColumnPickerOpen(false);
});
ACTIVITY_EVENTS.forEach((eventName) => {
  window.addEventListener(eventName, markUserActive, { passive: true });
});
document.addEventListener("visibilitychange", handleVisibilityChange);
selectAllTypesBtn.addEventListener("click", selectAllTypes);
clearAllTypesBtn.addEventListener("click", clearAllTypes);
showAllPurchaseLimitsBtn.addEventListener("click", () => setPurchaseLimitFilter(false));
hidePausedPurchaseBtn.addEventListener("click", () => setPurchaseLimitFilter(true));
premiumAboveInput.addEventListener("input", setPremiumThreshold);
discountBelowInput.addEventListener("input", setPremiumThreshold);
showAllPremiumsBtn.addEventListener("click", () => setPremiumFilter("all"));
showPremiumAboveBtn.addEventListener("click", () => setPremiumFilter("premiumAbove"));
showDiscountBelowBtn.addEventListener("click", () => setPremiumFilter("discountBelow"));
autoScrollDetails = loadAutoScrollDetails();
syncAutoScrollDetailsToggle();
visibleColumnKeys = new Set(loadVisibleColumnKeys());
renderColumnControls();
renderTableStructure();
renderPurchaseLimitFilter();
renderPremiumFilter();
loadFunds();
