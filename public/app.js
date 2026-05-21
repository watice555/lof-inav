const rowsEl = document.querySelector("#fundRows");
const stateEl = document.querySelector("#refreshState");
const dataAlertsEl = document.querySelector("#dataAlerts");
const refreshBtn = document.querySelector("#refreshBtn");
const exportCsvBtn = document.querySelector("#exportCsvBtn");
const exportPngBtn = document.querySelector("#exportPngBtn");
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
const sortableHeaders = [...document.querySelectorAll("th[data-sort-key]")];

let currentCode = null;
let currentFunds = [];
let availableTypes = [];
let selectedTypes = new Set();
let hidePausedPurchase = false;
let premiumFilterMode = "all";
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

const sortAccessors = {
  fund: (fund) => fund.code,
  type: (fund) => typeSortValue(getFundType(fund)),
  announcement: (fund) => fund.announcement?.publish_date,
  purchase_limit: (fund) => fund.purchase_limit?.sort_value,
  previous_nav: (fund) => fund.previous_nav,
  trade_price: (fund) => fund.trade_price,
  estimated_nav: (fund) => fund.estimated_nav,
  premium: (fund) => fund.premium,
  covered_weight: (fund) => fund.covered_weight,
  backtest_mae: (fund) => fund.backtest?.mae_pct,
  backtest_mae_vol: (fund) => fund.backtest?.mae_to_nav_volatility,
};

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

async function loadFunds() {
  stateEl.textContent = "刷新中...";
  const res = await fetch("/api/funds");
  const data = await res.json();
  currentFunds = data.funds;
  syncTypeFilters(currentFunds);
  renderFunds(getVisibleFunds());
  renderDataAlerts(data.data_alerts || [], data.data_alert_count || 0);
  const navRefreshTime = data.last_navs_refresh_success_at
    ? new Date(data.last_navs_refresh_success_at).toLocaleString()
    : "未刷新";
  const quoteRefreshTime = data.last_realtime_quotes_refresh_at
    ? new Date(data.last_realtime_quotes_refresh_at).toLocaleString()
    : "未刷新";
  const refreshPrefix = [
    data.navs_refreshing ? "净值刷新中" : "",
    data.quotes_refreshing ? "行情刷新中" : "",
  ]
    .filter(Boolean)
    .join("，");
  const refreshState = `净值 ${navRefreshTime} / 行情 ${quoteRefreshTime}`;
  stateEl.textContent = refreshPrefix ? `${refreshPrefix}... ${refreshState}` : refreshState;
  if (!currentCode && data.funds.length) {
    showDetails(data.funds[0].code);
  }
}

function renderDataAlerts(alerts, totalCount) {
  if (!dataAlertsEl) return;
  if (!alerts.length) {
    dataAlertsEl.hidden = true;
    dataAlertsEl.innerHTML = "";
    return;
  }
  dataAlertsEl.hidden = false;
  const hiddenCount = Math.max(0, totalCount - alerts.length);
  dataAlertsEl.innerHTML = `
    <div class="data-alert-header">
      <div>
        <h2>数据警报</h2>
        <p>${escapeHtml(totalCount)} 个基金或数据项需要检查</p>
      </div>
    </div>
    <div class="data-alert-list">
      ${alerts.map(renderDataAlert).join("")}
      ${hiddenCount ? `<div class="data-alert-more">还有 ${hiddenCount} 条未展示</div>` : ""}
    </div>
  `;
}

function renderDataAlert(alert) {
  const details = alertDetailsText(alert);
  return `
    <button class="data-alert-item" type="button" title="${escapeHtml(details)}" data-alert-code="${escapeHtml(alert.code)}">
      <span class="data-alert-code">${escapeHtml(alert.code)}</span>
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
  const getValue = sortAccessors[sortState.key];
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
    selectedTypes = new Set(availableTypes);
  } else {
    selectedTypes = new Set([...selectedTypes].filter((type) => availableTypes.includes(type)));
    availableTypes.forEach((type) => {
      if (!previousTypes.has(type)) selectedTypes.add(type);
    });
  }

  renderTypeFilters();
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
  return `筛选：${typeText} / ${purchaseText} / ${getPremiumFilterLabel()}`;
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

function renderFunds(funds) {
  if (!funds.length) {
    rowsEl.innerHTML = `
      <tr>
        <td class="empty-state" colspan="12">当前筛选下暂无基金</td>
      </tr>
    `;
    return;
  }

  rowsEl.innerHTML = funds
    .map((fund) => {
      const mae = fund.backtest && fund.backtest.mae_pct !== undefined ? pct(fund.backtest.mae_pct) : "--";
      const maeVol =
        fund.backtest && fund.backtest.mae_to_nav_volatility !== undefined
          ? fmt(fund.backtest.mae_to_nav_volatility, 2)
          : "--";
      return `
        <tr data-code="${fund.code}">
          <td>
            <div class="fund-name">${fund.name}</div>
            <div class="fund-code">${fund.code} / ${fund.nav_date || "--"}</div>
          </td>
          <td><span class="type-pill">${escapeHtml(getFundType(fund))}</span></td>
          <td>${renderAnnouncement(fund.announcement)}</td>
          <td>${renderPurchaseLimit(fund.purchase_limit)}</td>
          <td>${valueWithMeta(fmt(fund.previous_nav, 4), dateOnly(fund.nav_date))}</td>
          <td>${valueWithMeta(fmt(fund.trade_price, 3), minuteTime(fund.quote_time))}</td>
          <td>${fmt(fund.estimated_nav, 4)}</td>
          <td>${signedPct(fund.premium)}</td>
          <td>${pct(fund.covered_weight)}</td>
          <td>${mae}</td>
          <td>${maeVol}</td>
          <td>${fund.note || ""}</td>
        </tr>
      `;
    })
    .join("");
  [...rowsEl.querySelectorAll("tr")].forEach((row) => {
    row.addEventListener("click", () => showDetails(row.dataset.code));
  });
  [...rowsEl.querySelectorAll(".announce-link, .announcement-pdf-link")].forEach((link) => {
    link.addEventListener("click", (event) => event.stopPropagation());
  });
}

function renderPurchaseLimit(purchaseLimit) {
  if (!purchaseLimit || !purchaseLimit.display) return "--";
  return escapeHtml(purchaseLimit.display);
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

async function showDetails(code) {
  currentCode = code;
  const [holdingsRes, backtestRes] = await Promise.all([
    fetch(`/api/funds/${code}/holdings`),
    fetch(`/api/funds/${code}/backtest`),
  ]);
  const holdings = await holdingsRes.json();
  const backtest = await backtestRes.json();
  holdingsEl.innerHTML = renderHoldings(holdings.holdings);
  backtestEl.innerHTML = renderBacktest(backtest.rows);
}

function renderHoldings(rows) {
  if (!rows.length) return `<div class="muted">暂无持仓数据</div>`;
  return `
    <table class="mini-table">
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
  `;
}

function renderBacktest(rows) {
  if (!rows.length) return `<div class="muted">暂无回测数据</div>`;
  return `
    <table class="mini-table">
      <thead><tr><th>日期</th><th>实际净值</th><th>价格</th><th>折溢价</th><th>估算净值</th><th>误差</th><th>覆盖</th><th>数据质量</th></tr></thead>
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
            <td>${pct(row.covered_weight)}</td>
            <td>${renderBacktestQuality(row.price_diagnostics)}</td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderBacktestQuality(diagnostics) {
  if (!diagnostics) return "--";
  const assetWeight = Number(diagnostics.asset_stale_weight || 0);
  const fxWeight = Number(diagnostics.fx_stale_weight || 0);
  const missingWeight = Number(diagnostics.missing_weight || 0);
  const total = assetWeight + fxWeight + missingWeight;
  if (!total) return `<span class="quality-ok">正常</span>`;
  const details = [];
  if (assetWeight) details.push(`价格回退 ${pct(assetWeight)}`);
  if (fxWeight) details.push(`汇率回退 ${pct(fxWeight)}`);
  if (missingWeight) details.push(`缺失 ${pct(missingWeight)}`);
  const title = [
    ...(diagnostics.asset_stale || []).map(
      (item) =>
        `价格 ${item.secid} ${item.name}: ${item.previous_date}->${item.previous_price_date}, ${item.current_date}->${item.current_price_date}, 权重 ${pct(item.weight)}`
    ),
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

const exportColumns = [
  { key: "fund", title: "基金", width: 0.17, align: "left" },
  { key: "type", title: "类型", width: 0.1 },
  { key: "announcement", title: "公告", width: 0.06 },
  { key: "purchase_limit", title: "申购限额", width: 0.07 },
  { key: "previous_nav", title: "上一日净值", width: 0.07 },
  { key: "trade_price", title: "场内价格", width: 0.07 },
  { key: "estimated_nav", title: "系统估值", width: 0.07 },
  { key: "premium", title: "折溢价率", width: 0.07 },
  { key: "covered_weight", title: "覆盖仓位", width: 0.07 },
  { key: "backtest_mae", title: "回测 MAE", width: 0.07 },
  { key: "backtest_mae_vol", title: "MAE/波动", width: 0.07 },
  { key: "note", title: "备注", width: 0.11, align: "left" },
];

function getExportCell(fund, key) {
  if (key === "fund") return [fund.name || "--", `${fund.code || "--"} / ${fund.nav_date || "--"}`];
  if (key === "type") return [getFundType(fund)];
  if (key === "announcement") return [fund.announcement?.publish_date || ""];
  if (key === "purchase_limit") return [fund.purchase_limit?.display || "--"];
  if (key === "previous_nav") return [fmt(fund.previous_nav, 4), dateOnly(fund.nav_date)];
  if (key === "trade_price") return [fmt(fund.trade_price, 3), minuteTime(fund.quote_time)];
  if (key === "estimated_nav") return [fmt(fund.estimated_nav, 4)];
  if (key === "premium") return [pct(fund.premium)];
  if (key === "covered_weight") return [pct(fund.covered_weight)];
  if (key === "backtest_mae") return [fund.backtest?.mae_pct !== undefined ? pct(fund.backtest.mae_pct) : "--"];
  if (key === "backtest_mae_vol") {
    return [fund.backtest?.mae_to_nav_volatility !== undefined ? fmt(fund.backtest.mae_to_nav_volatility, 2) : "--"];
  }
  if (key === "note") return [fund.note || ""];
  return [""];
}

function csvEscape(value) {
  const text = String(value ?? "");
  if (/[",\r\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function exportCsv() {
  if (!currentFunds.length) return;

  const funds = getVisibleFunds();
  const header = exportColumns.map((column) => csvEscape(column.title)).join(",");
  const rows = funds.map((fund) =>
    exportColumns
      .map((column) => csvEscape(getExportCell(fund, column.key).filter(Boolean).join(" ")))
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

function getExportColumnWidths(width) {
  const colWidths = exportColumns.map((column) => Math.round(column.width * width));
  colWidths[colWidths.length - 1] += width - colWidths.reduce((sum, value) => sum + value, 0);
  return colWidths;
}

function getExportRowHeight(context, fund, colWidths) {
  const maxLineCount = exportColumns.reduce((maxLines, column, index) => {
    const colWidth = colWidths[index];
    const rawLines = getExportCell(fund, column.key);
    const lines =
      column.key === "note"
        ? wrapCanvasText(context, fund.note || "", colWidth - 16)
        : rawLines.flatMap((line) => wrapCanvasText(context, line, colWidth - 16));
    return Math.max(maxLines, lines.length);
  }, 1);
  return Math.max(52, 18 + maxLineCount * 17);
}

function drawExportTable(context, funds, x, y, width) {
  const lineColor = "#d9dee7";
  const headerHeight = 42;
  const emptyHeight = 72;
  const colWidths = getExportColumnWidths(width);

  context.save();
  context.strokeStyle = lineColor;
  context.lineWidth = 1;
  context.fillStyle = "#eef2f6";
  context.fillRect(x, y, width, headerHeight);
  context.strokeRect(x, y, width, headerHeight);
  context.font = "700 12px 'Microsoft YaHei', 'Segoe UI', sans-serif";
  context.fillStyle = "#415064";

  let currentX = x;
  exportColumns.forEach((column, index) => {
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
    const rowHeight = getExportRowHeight(context, fund, colWidths);
    context.fillStyle = "#fff";
    context.fillRect(x, currentY, width, rowHeight);
    context.strokeStyle = lineColor;
    context.strokeRect(x, currentY, width, rowHeight);

    currentX = x;
    exportColumns.forEach((column, index) => {
      const colWidth = colWidths[index];
      const rawLines = getExportCell(fund, column.key);
      const lines =
        column.key === "note"
          ? wrapCanvasText(context, fund.note || "", colWidth - 16)
          : rawLines.flatMap((line) => wrapCanvasText(context, line, colWidth - 16));
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
    const scale = 2;
    const width = 1440;
    const margin = 24;
    const contentWidth = width - margin * 2;
    const disclaimerText = document.querySelector(".disclaimer p")?.textContent || "";
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");

    context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";
    const disclaimerLines = wrapCanvasText(context, disclaimerText, contentWidth - 32);
    const colWidths = getExportColumnWidths(contentWidth);
    const rowHeights = funds.map((fund) => {
      context.font = "13px 'Microsoft YaHei', 'Segoe UI', sans-serif";
      return getExportRowHeight(context, fund, colWidths);
    });
    const tableHeight = 42 + (funds.length ? rowHeights.reduce((sum, value) => sum + value, 0) : 72);
    const disclaimerHeight = 52 + disclaimerLines.length * 22;
    const height = margin + 78 + 18 + tableHeight + 16 + disclaimerHeight + margin;

    canvas.width = width * scale;
    canvas.height = height * scale;
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
dataAlertsEl?.addEventListener("click", (event) => {
  const item = event.target.closest("[data-alert-code]");
  if (!item) return;
  showDetails(item.dataset.alertCode);
  document.querySelector(".detail-grid")?.scrollIntoView({ behavior: "smooth", block: "start" });
});
exportCsvBtn.addEventListener("click", exportCsv);
exportPngBtn.addEventListener("click", exportPng);
selectAllTypesBtn.addEventListener("click", selectAllTypes);
clearAllTypesBtn.addEventListener("click", clearAllTypes);
showAllPurchaseLimitsBtn.addEventListener("click", () => setPurchaseLimitFilter(false));
hidePausedPurchaseBtn.addEventListener("click", () => setPurchaseLimitFilter(true));
premiumAboveInput.addEventListener("input", setPremiumThreshold);
discountBelowInput.addEventListener("input", setPremiumThreshold);
showAllPremiumsBtn.addEventListener("click", () => setPremiumFilter("all"));
showPremiumAboveBtn.addEventListener("click", () => setPremiumFilter("premiumAbove"));
showDiscountBelowBtn.addEventListener("click", () => setPremiumFilter("discountBelow"));
sortableHeaders.forEach((header) => {
  header.addEventListener("click", () => cycleSort(header.dataset.sortKey));
});
updateSortHeaders();
renderPurchaseLimitFilter();
renderPremiumFilter();
loadFunds();
setInterval(loadFunds, 60_000);
