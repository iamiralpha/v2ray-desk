"use strict";
const BASE = document.querySelector('meta[name="app-base"]').content;
const route = (path) => BASE + path;
const EN = document.documentElement.lang === "en";
const L = (fa, en) => EN ? en : fa;
const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const num = new Intl.NumberFormat(EN ? "en-US" : "fa-IR");
const names = new Intl.DisplayNames([EN ? "en" : "fa"], {type: "region"});
let records = [], selected = new Set(), activeCountry = "", page = 1, running = false;
let fetching = false, pendingDelete = null, toastTimer;
let configsCursor = null, filterCache = null, lastSelectedId = null;
let subscriptions = [], activeSubscription = "", subscriptionSync = {running:false, total:0, done:0};
let availableNodes = [];
const countryNames = new Map();
const perPage = 50;
const statusNames = {pending: L("تست‌نشده", "Not tested"), healthy: L("دارای پاسخ", "Responding"), failed: L("بدون پاسخ", "No response"), error: L("خطای تست", "Test error"), invalid: L("تنظیمات نامعتبر", "Invalid settings"), testing: L("در حال تست", "Testing")};
const geminiNames = {untested:L("تست‌نشده", "Not tested"),http200:L("پاسخ 200", "HTTP 200"),blocked:L("۴۰۳ ناشناس", "Anonymous HTTP 403"),limited:L("محدودیت 429", "HTTP 429"),login:L("صفحهٔ ورود", "Sign-in page"),redirect:L("انتقال", "Redirect"),unreachable:L("خطای اتصال", "Connection error"),other:L("پاسخ دیگر", "Other response")};

function updateNodes(items) {
  const next = Array.isArray(items) ? items : [];
  const changed = JSON.stringify(next) !== JSON.stringify(availableNodes);
  if (!changed) return false;
  availableNodes = next;
  const select = $("node-select"), selectedNode = select.value;
  const allNodes = node("option", L("همهٔ نودها", "All nodes")); allNodes.value = "";
  select.replaceChildren(allNodes);
  const scopes = [""];
  if (availableNodes.some(entry => entry.location === "iran")) {
    const option = node("option", L("همهٔ نودهای ایران", "All Iran nodes")); option.value = "iran";
    select.append(option); scopes.push("iran");
  }
  for (const entry of availableNodes) {
    const option = node("option", `${entry.name} · ${entry.location === "iran" ? L("ایران", "Iran") : L("سایر", "Other")}`);
    option.value = `node:${entry.id}`; select.append(option); scopes.push(option.value);
  }
  select.hidden = !availableNodes.length;
  select.value = scopes.includes(selectedNode) ? selectedNode : "";
  if (selectedNode && !scopes.includes(selectedNode)) {
    $("status-filter").value = ""; page = 1; selected.clear();
  }
  return true;
}
function syncQuickFilters() {
  const scope = $("node-select").value, hasIran = availableNodes.some(entry => entry.location === "iran");
  $("quick-main").setAttribute("aria-pressed", String(!scope && $("status-filter").value === "healthy"));
  $("quick-node").setAttribute("aria-pressed", String(scope === "iran"));
  $("quick-node").disabled = !hasIran;
  $("quick-node-label").textContent = L("کانفیگ متصل در ایران", "Connected in Iran");
  $("quick-node-hint").textContent = hasIran ? L("همهٔ نودهای ایران", "All Iran nodes") : L("هنوز نود ایرانی موجود نیست", "No Iran node available");
  $("quick-main").title = L("برای نمایش همه دوباره بزنید", "Click again to show all");
  $("quick-node").title = hasIran ? L("اتصال موفق در حداقل یکی از نودهای ایران؛ برای نمایش همه دوباره بزنید", "Working on at least one Iran node; click again to show all") : L("هنوز نود ایرانی موجود نیست", "No Iran node available");
}
function quickFilter(target) {
  if (target === "node" && !availableNodes.some(entry => entry.location === "iran")) return;
  const scope = $("node-select").value;
  const active = target === "node" ? scope === "iran" : !scope && $("status-filter").value === "healthy";
  $("node-select").value = target === "node" && !active ? "iran" : "";
  $("status-filter").value = active ? "" : "healthy";
  resetView();
}
function nodeResult(item) {
  const scope = $("node-select").value;
  const nodes = availableNodes.filter(entry => !scope || (scope === "iran" ? entry.location === "iran" : scope === `node:${entry.id}`));
  const summary = nodes.flatMap(entry => {
    const value = item.node_results?.[String(entry.id)];
    if (!value) return [];
    return [`${entry.name}: ${statusNames[value.status] || value.status}${value.status === "healthy" && value.latency_ms != null ? ` · ${num.format(Math.round(value.latency_ms))} ms` : ""}`];
  }).join(" | ");
  return summary || (item.status === "healthy" && nodes.length ? L("در انتظار تست", "Awaiting test") : "—");
}

function countryName(code) {
  if (!code) return L("نامشخص", "Unknown");
  if (countryNames.has(code)) return countryNames.get(code);
  let name; try { name = names.of(code) || code; } catch { name = code; }
  countryNames.set(code, name); return name;
}
function flag(code) {
  return /^[A-Z]{2}$/.test(code || "") ? String.fromCodePoint(...[...code].map(c => c.charCodeAt(0) + 127397)) : "◎";
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function toast(message, bad = false) {
  clearTimeout(toastTimer);
  const dialog = [...document.querySelectorAll("dialog[open]")].at(-1);
  if (dialog) {
    $("toast").hidden = true;
    let notice = dialog.querySelector(".dialog-notice");
    if (!notice) {
      notice = node("p", undefined, "dialog-notice");
      const heading = dialog.querySelector(".dialog-heading");
      if (heading) heading.after(notice); else dialog.prepend(notice);
    }
    notice.setAttribute("role", bad ? "alert" : "status");
    notice.setAttribute("aria-atomic", "true");
    notice.classList.toggle("bad-notice", bad);
    notice.textContent = message;
    dialog.scrollTop = 0;
    return;
  }
  $("toast").textContent = message;
  $("toast").classList.toggle("bad-toast", bad);
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6500);
}
function openDialog(dialog) {
  dialog.querySelector(".dialog-notice")?.remove();
  $("toast").hidden = true;
  clearTimeout(toastTimer);
  dialog.showModal();
}
async function api(url, data, method = "POST") {
  const response = await fetch(route(url), {
    method: data === undefined ? "GET" : method,
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  if (response.status === 401) { location.assign(route("/login")); throw Error(L("نشست شما تمام شده است.", "Your session has expired.")); }
  if (!response.ok) {
    let message = L("ارتباط با پنل برقرار نشد.", "Could not connect to the panel.");
    try { message = (await response.json()).error || message; } catch {}
    throw Error(message);
  }
  return response;
}
function action(fn) {
  return async (event) => {
    const button = event?.currentTarget;
    const disable = button?.tagName === "BUTTON";
    if (disable) button.disabled = true;
    try { await fn(event); }
    catch (error) { toast(error.message || L("خطای ارتباط؛ دوباره تلاش کنید.", "Connection error. Please try again."), true); }
    finally { if (disable) button.disabled = false; syncButtons(); }
  };
}
function filtered() {
  if (filterCache) return filterCache;
  const term = $("search").value.trim().toLowerCase();
  const status = $("status-filter").value;
  const gemini = $("gemini-filter").value;
  const protocol = $("protocol-filter").value;
  const nodeScope = $("node-select").value;
  const matchingNodes = availableNodes.filter(entry => nodeScope === "iran" ? entry.location === "iran" : nodeScope === `node:${entry.id}`);
  const list = records.filter(item => (!activeCountry || (item.country || "unknown") === activeCountry)
    && (!activeSubscription || (activeSubscription === "none" ? !(item.subscription_ids || []).length : (item.subscription_ids || []).includes(Number(activeSubscription))))
    && (!protocol || item.protocol === protocol)
    && (!status || item.status === status)
    && (!nodeScope || (item.status === "healthy" && matchingNodes.some(entry => item.node_results?.[String(entry.id)]?.status === "healthy")))
    && (!gemini || item.gemini_status === gemini)
    && (!term || `${item.name} ${item.host} ${item.protocol} ${countryName(item.country)}`.toLowerCase().includes(term)));
  const sort = $("sort").value, group = $("group").checked;
  const key = sort.replace(/-desc$/, ""), direction = sort.endsWith("-desc") ? -1 : 1;
  const delay = (item) => item.status === "healthy" && item.latency_ms !== null ? item.latency_ms : null;
  list.sort((a, b) => {
    if (group) {
      const country = (!a.country) - (!b.country) || countryName(a.country).localeCompare(countryName(b.country), EN ? "en" : "fa");
      if (country) return country;
    }
    if (key === "latency") {
      const av = delay(a), bv = delay(b);
      if (av === null && bv !== null) return 1;
      if (av !== null && bv === null) return -1;
      return (av !== null && bv !== null ? (av - bv) * direction : 0) || a.id - b.id;
    }
    if (key === "newest") return b.id - a.id;
    if (key === "index") return (a.id - b.id) * direction;
    if (key === "port") return (a.port - b.port) * direction || a.id - b.id;
    const av = key === "country" ? countryName(a.country) : String(a[key] || "");
    const bv = key === "country" ? countryName(b.country) : String(b[key] || "");
    return av.localeCompare(bv, EN ? "en" : "fa", {numeric:true}) * direction || a.id - b.id;
  });
  filterCache = list; return list;
}
function scope() {
  const list = filtered();
  return selected.size ? list.filter(row => selected.has(row.id)) : list;
}
function testScope() {
  const mode = $("test-scope").value;
  return scope().filter(row => mode === "all"
    || (mode === "pending" && row.status === "pending")
    || (mode === "failed" && ["failed", "error"].includes(row.status))
    || (mode === "pending-failed" && ["pending", "failed", "error"].includes(row.status)));
}
function syncButtons() {
  const count = filtered().length;
  $("import-open").disabled = running;
  $("empty-import").disabled = running;
  for (const input of document.querySelectorAll(".test-settings select,.test-settings input")) input.disabled = running;
  const testCount = testScope().length;
  $("test-btn").disabled = running || subscriptionSync.running || testCount === 0;
  for (const button of document.querySelectorAll(".row-test")) button.disabled = running || subscriptionSync.running;
  $("export-btn").disabled = scope().length === 0;
  $("report-btn").disabled = scope().length === 0;
  $("delete-selected").disabled = running || selected.size === 0;
  $("delete-failed").disabled = running || !filtered().some(r => r.status === "failed");
  $("select-filtered").disabled = count === 0;
  $("test-btn").textContent = L(`▷ تست (${num.format(testCount)})`, `▷ Test (${num.format(testCount)})`);
  $("export-btn").textContent = selected.size ? L("دریافت انتخاب‌شده‌ها ↓", "Export selected \u2193") : L("دریافت فهرست ↓", "Export results \u2193");
  $("selection-text").textContent = selected.size ? L(`${num.format(selected.size)} کانفیگ انتخاب شده`, `${num.format(selected.size)} configurations selected`) : L("کانفیگی انتخاب نشده", "No configurations selected");
  syncSubscriptionControls();
}
function resetView() {
  filterCache = null;
  page = 1;
  selected.clear(); lastSelectedId = null;
  render();
}
function updateSubscriptions(items) {
  if (!Array.isArray(items)) return false;
  const next = items.map(({id,name,config_count}) => ({id,name,config_count}));
  if (JSON.stringify(next) === JSON.stringify(subscriptions)) return false;
  subscriptions = next; filterCache = null;
  return true;
}
function renderSubscriptionFilter() {
  const select = $("subscription-filter");
  select.replaceChildren();
  for (const [value, text] of [["", L("همهٔ ساب‌ها", "All subscriptions")],
    ["none", L(`بدون ساب (${num.format(records.filter(row => !(row.subscription_ids || []).length).length)})`, `Without subscription (${num.format(records.filter(row => !(row.subscription_ids || []).length).length)})`)],
    ...subscriptions.map(sub => [String(sub.id), `${sub.name} (${num.format(sub.config_count)})`])]) {
    const option = node("option", text); option.value = value; select.append(option);
  }
  if (activeSubscription && activeSubscription !== "none" && !subscriptions.some(sub => String(sub.id) === activeSubscription)) {
    activeSubscription = ""; filterCache = null; page = 1;
    selected.clear(); lastSelectedId = null;
  }
  select.value = activeSubscription;
}
function updateSubscriptionProgress(progress) {
  if (progress) subscriptionSync = progress;
  const counted = subscriptionSync.total > 0 && (!subscriptionSync.running || subscriptionSync.done < subscriptionSync.total);
  const text = counted
    ? L(`دریافت ساب‌ها: ${num.format(subscriptionSync.done)} از ${num.format(subscriptionSync.total)}`, `Subscription sync: ${num.format(subscriptionSync.done)} of ${num.format(subscriptionSync.total)}`)
    : L("دریافت ساب‌ها", "Subscription sync");
  $("subscription-sync-status").hidden = !subscriptionSync.running;
  $("subscription-sync-status").textContent = text + L(" · تست پس از پایان دریافت فعال می‌شود.", " · Testing is available when syncing finishes.");
  subsProgress.hidden = !subscriptionSync.running && !subscriptionSync.total;
  subsProgress.textContent = text + (subscriptionSync.running ? L(" · در حال دریافت…", " · Downloading…") : L(" · تمام شد", " · Complete"));
  syncSubscriptionControls();
}
function renderCountries() {
  const counts = new Map();
  records.forEach(item => {
    const key = item.country || "unknown";
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  $("country-count").textContent = num.format([...counts.keys()].filter(k => k !== "unknown").length);
  $("country-filter").replaceChildren();
  const allOption = node("option", L("همهٔ کشورها", "All countries"));
  allOption.value = ""; $("country-filter").append(allOption);
  [...counts].sort((a,b) => (a[0] === "unknown") - (b[0] === "unknown") || countryName(a[0]).localeCompare(countryName(b[0]))).forEach(([code,count]) => {
    const option = node("option", `${countryName(code === "unknown" ? null : code)} (${num.format(count)})`);
    option.value = code; $("country-filter").append(option);
  });
  if (activeCountry && !counts.has(activeCountry)) {
    activeCountry = ""; filterCache = null; page = 1;
    selected.clear(); lastSelectedId = null;
  }
  $("country-filter").value = activeCountry;
  $("country-nav").replaceChildren();
  [...counts].sort((a, b) => (a[0] === "unknown") - (b[0] === "unknown") || countryName(a[0]).localeCompare(countryName(b[0]), EN ? "en" : "fa"))
    .forEach(([code, count]) => {
      const button = node("button", undefined, "nav-item country-item");
      button.classList.toggle("active", activeCountry === code);
      button.append(node("span", flag(code), "flag"), node("span", countryName(code === "unknown" ? null : code)), node("b", num.format(count)));
      button.addEventListener("click", () => { activeCountry = code; resetView(); });
      $("country-nav").append(button);
    });
  if (!counts.size) $("country-nav").append(node("p", L("بعد از اولین تست نمایش داده می‌شود.", "Countries appear after the first test."), "country-empty"));
  $("all-nav").classList.toggle("active", !activeCountry);
}
function render() {
  syncQuickFilters();
  renderCountries();
  renderSubscriptionFilter();
  const list = filtered();
  const ids = new Set(list.map(row => row.id));
  selected = new Set([...selected].filter(id => ids.has(id)));
  const pages = Math.max(1, Math.ceil(list.length / perPage));
  page = Math.min(page, pages);
  const visible = list.slice((page - 1) * perPage, page * perPage);
  $("stat-total").textContent = num.format(records.length);
  $("nav-count").textContent = num.format(records.length);
  $("stat-healthy").textContent = num.format(records.filter(r => r.status === "healthy").length);
  $("stat-failed").textContent = num.format(records.filter(r => r.status === "failed").length);
  const delays = records.filter(r => r.status === "healthy" && r.latency_ms !== null).map(r => r.latency_ms);
  $("stat-best").textContent = delays.length ? `${num.format(Math.round(Math.min(...delays)))} ms` : "—";
  $("collection-title").replaceChildren(document.createTextNode(activeCountry ? countryName(activeCountry === "unknown" ? null : activeCountry) + " " : L("همهٔ کانفیگ‌ها ", "All configurations ")), node("span", num.format(list.length)));
  $("rows").replaceChildren();
  let lastCountry = Symbol();
  for (const item of visible) {
    if ($("group").checked && item.country !== lastCountry) {
      const group = node("tr", undefined, "group-row"), cell = node("td");
      cell.colSpan = 14;
      cell.textContent = `${flag(item.country)}  ${countryName(item.country)}`;
      group.append(cell); $("rows").append(group); lastCountry = item.country;
    }
    const tr = node("tr");
    tr.classList.toggle("selected-row", selected.has(item.id));
    tr.addEventListener("dblclick", action(async event => {
      if (!running && !event.target.closest("button,input")) await openEdit(item.id);
    }));
    const checkCell = node("td", undefined, "check-cell"), check = node("input");
    check.type = "checkbox"; check.checked = selected.has(item.id);
    check.setAttribute("aria-label", L(`انتخاب ${item.name}`, `Select ${item.name}`));
    check.addEventListener("click", event => {
      const target = check.checked;
      const first = list.findIndex(row => row.id === lastSelectedId), last = list.findIndex(row => row.id === item.id);
      const range = event.shiftKey && first >= 0 ? list.slice(Math.min(first,last), Math.max(first,last) + 1) : [item];
      range.forEach(row => target ? selected.add(row.id) : selected.delete(row.id));
      lastSelectedId = item.id; render();
    });
    checkCell.append(check); tr.append(checkCell);
    tr.append(node("td", num.format((page - 1) * perPage + visible.indexOf(item) + 1), "row-number"));
    const identity = node("td", undefined, "name-cell");
    const name = node("strong", item.name, "config-name"); name.title = item.name;
    identity.append(name);
    tr.append(identity);
    const protocol = node("td"); protocol.append(node("span", item.protocol.toUpperCase(), "protocol")); tr.append(protocol);
    for (const [value, className] of [[item.host,"server-address"],[item.port,"server-port"],
      [item.transport || "—","transport"],[item.security || "—","security"]]) {
      const cell = node("td", String(value), className); cell.title = String(value); tr.append(cell);
    }
    const country = node("td"); country.append(node("span", `${flag(item.country)} ${countryName(item.country)}`, "country-cell")); tr.append(country);
    const latency = node("td");
    if (item.status === "healthy" && item.latency_ms !== null) {
      const delay = node("strong", `${num.format(Math.round(item.latency_ms))} ms`, "latency");
      delay.title = item.delay_version === 3
        ? `${item.delay_target} · v2rayN min · ${L("نمونه‌ها", "Samples")}: ${JSON.parse(item.delay_samples || "[]").join(", ")} ms · HTTP ${JSON.parse(item.delay_codes || "[]").join(", ")}`
        : L("روش اندازه‌گیری قبلی؛ برای مقایسه دوباره تست کنید.", "Previous measurement method. Retest before comparing.");
      latency.append(delay);
      if (item.delay_version !== 3) latency.append(node("small", L("اندازه‌گیری قبلی", "Previous measurement"), "legacy-delay"));
    } else latency.append(node("span", "—", "muted"));
    tr.append(latency);
    const status = node("td");
    const badge = node("span", statusNames[item.status] || item.status, `status ${item.status}`);
    if (item.error) badge.title = item.error;
    status.append(badge);
    if (item.error) {
      const info = node("button", "ⓘ", "info-button");
      info.title = item.error;
      info.setAttribute("aria-label", L("نمایش علت خطا", "Show error details"));
      info.addEventListener("click", () => toast(item.error, true));
      status.append(info);
    }
    tr.append(status);
    const geminiCell = node("td");
    const geminiBadge = node("span", geminiNames[item.gemini_status] || L("تست‌نشده", "Not tested"), "status " + ({http200:"healthy",blocked:"failed",limited:"error",login:"pending",unreachable:"error"}[item.gemini_status] || "pending"));
    geminiBadge.title = item.gemini_code ? L(`HTTP ${item.gemini_code}؛ درخواست بدون ورود به گوگل است و وضعیت حساب شما را مشخص نمی‌کند.`, `HTTP ${item.gemini_code}; this anonymous request cannot determine your account access.`) : L("تست درخواست HTTPS به gemini.google.com/app از داخل همین پروکسی", "HTTPS request to gemini.google.com/app through this proxy");
    geminiCell.append(geminiBadge); tr.append(geminiCell);
    const remote = node("td", nodeResult(item), "node-result-cell");
    const remoteData = item.node_results?.[$("node-select").value.slice(5)];
    if (remoteData?.checked_at) remote.title = new Date(remoteData.checked_at).toLocaleString(EN ? "en-US" : "fa-IR");
    tr.append(remote);
    const checked = node("td", item.checked_at ? new Intl.DateTimeFormat(EN ? "en-US" : "fa-IR", {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}).format(new Date(item.checked_at)) : L("هنوز تست نشده", "Not tested yet"), "checked-date");
    tr.append(checked);
    const ops = node("td", undefined, "row-actions");
    for (const [label, icon, fn, disabled] of [
      [L("تست این کانفیگ", "Test this configuration"), "▷", () => startTest([item.id]), running || subscriptionSync.running],
      [L("ویرایش کانفیگ", "Edit configuration"), "✎", () => openEdit(item.id), running],
      [L("کپی لینک", "Copy link"), "⧉", () => copyConfig(item.id), false],
      [L("حذف کانفیگ", "Delete configuration"), "×", () => askDelete([item.id], false), running]
    ]) {
      const button = node("button", icon, icon === "▷" ? "icon-button row-test" : "icon-button");
      button.title = label; button.setAttribute("aria-label", label); button.disabled = disabled;
      button.addEventListener("click", action(fn)); ops.append(button);
    }
    tr.append(ops); $("rows").append(tr);
  }
  $("empty").hidden = list.length > 0;
  $("empty").querySelector("h3").textContent = records.length ? L("نتیجه‌ای پیدا نشد", "No matching configurations") : L("اولین اتصال را اضافه کنید", "Add your first connection");
  $("empty").querySelector("p").textContent = records.length ? L("فیلتر یا عبارت جست‌وجو را تغییر دهید.", "Try another filter or search term.") : L("لینک‌ها را بچسبانید یا فایل متنی وارد کنید؛ بعد با تست Real Delay بهترین اتصال‌ها را پیدا کنید.", "Paste your links or import a text file, then find the best connections with Real Delay.");
  $("empty-import").hidden = records.length > 0;
  $("select-page").checked = visible.length > 0 && visible.every(row => selected.has(row.id));
  $("select-page").indeterminate = visible.some(row => selected.has(row.id)) && !$("select-page").checked;
  $("page-info").textContent = list.length ? L(`${num.format((page - 1) * perPage + 1)} تا ${num.format(Math.min(page * perPage, list.length))} از ${num.format(list.length)} کانفیگ`, `${num.format((page - 1) * perPage + 1)}–${num.format(Math.min(page * perPage, list.length))} of ${num.format(list.length)} configurations`) : L("۰ کانفیگ", "0 configurations");
  $("page-label").textContent = `${num.format(page)} / ${num.format(pages)}`;
  $("prev-page").disabled = page === 1; $("next-page").disabled = page >= pages;
  for (const button of document.querySelectorAll(".sort-header")) {
    const value = $("sort").value, key = value.replace(/-desc$/, "");
    const active = button.dataset.sort === key;
    button.closest("th").setAttribute("aria-sort", active ? (value.endsWith("-desc") ? "descending" : "ascending") : "none");
    button.querySelector(".sort-indicator").textContent = active ? (value.endsWith("-desc") ? "↓" : "↑") : "↕";
  }
  syncButtons();
}
function renderJobProgress(job) {
  const metadata = job.phase === "metadata", phased = metadata || job.phase === "delay";
  let stage = L("در حال تست Real Delay", "Testing Real Delay");
  if (job.phase === "delay") stage = L("مرحلهٔ ۱ · تست Real Delay", "Stage 1 · Real Delay");
  if (metadata) {
    const checks = [];
    if (job.options?.gemini) checks.push(L("جمینای", "Gemini"));
    if (job.options?.country) checks.push(L("کشور", "country"));
    stage = L("مرحلهٔ ۲ · ", "Stage 2 · ") + (checks.join(L(" و ", " and ")) || L("بررسی‌های تکمیلی", "Additional checks"));
  }
  const done = job.phase_done ?? job.done, total = job.phase_total ?? job.total;
  const delayCompleted = metadata ? L(` · Real Delay تکمیل‌شده: ${num.format(job.done)}/${num.format(job.total)}`, ` · Real Delay completed: ${num.format(job.done)}/${num.format(job.total)}`) : "";
  const minutes = num.format(Math.ceil(job.eta / 60));
  const eta = job.eta == null ? "" : phased
    ? L(` · حدود ${minutes} دقیقه تا پایان این مرحله`, ` · about ${minutes} min left in this stage`)
    : L(` · حدود ${minutes} دقیقه باقی‌مانده`, ` · about ${minutes} min left`);
  const text = `${job.cancelled ? L("در حال توقف · ", "Stopping · ") : ""}${stage} · ${num.format(done)}/${num.format(total)}${delayCompleted} · ${L("هم‌زمان", "active")}: ${num.format(job.active)} · ${L("در صف", "queued")}: ${num.format(job.queued)}${eta}`;
  $("job-text").textContent = text;
  $("job-progress").max = Math.max(1, total); $("job-progress").value = done;
  $("job-progress").setAttribute("aria-label", stage);
  $("job-progress").setAttribute("aria-valuetext", text);
  $("cancel-test").disabled = job.cancelled;
}
async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const data = await (await api("/api/configs" + (configsCursor ? "?cursor=" + encodeURIComponent(configsCursor) : ""))).json();
    configsCursor = data.cursor;
    $("engine-version").textContent = data.engine?.version ? `Xray ${data.engine.version}` : L("Xray در دسترس نیست", "Xray unavailable");
    const wasRunning = running;
    const wasSyncing = subscriptionSync.running;
    const subscriptionsChanged = updateSubscriptions(data.subscriptions);
    const nodesChanged = updateNodes(data.nodes);
    updateSubscriptionProgress(data.subscription_sync);
    if (data.full) records = data.configs;
    else if (data.configs.length) {
      const updates = new Map(data.configs.map(row => [row.id, row]));
      records = records.map(row => updates.get(row.id) || row);
    }
    running = data.job.running;
    $("job").hidden = !running;
    const autoDeleted = Number(data.job.auto_deleted || 0);
    const deletedText = L(`${num.format(autoDeleted)} کانفیگ پس از دو شکست اتصال پیاپی خودکار حذف شد.`, `Auto-deleted ${num.format(autoDeleted)} configurations after two consecutive connection failures.`);
    $("auto-delete-result").hidden = !autoDeleted;
    $("auto-delete-result").textContent = deletedText;
    renderJobProgress(data.job);
    if (wasRunning && !running) toast(data.job.cancelled ? L("تست متوقف شد؛ موارد تست‌نشده تغییر نکردند.", "Testing stopped. Queued configurations were left unchanged.") : L("تست فهرست تمام شد.", "Testing complete.") + (autoDeleted ? " " + deletedText : ""));
    if (data.full || data.configs.length || subscriptionsChanged || nodesChanged || wasRunning !== running || wasSyncing !== subscriptionSync.running) { filterCache = null; render(); }
    else syncButtons();
    if (subsDialog.open && !subWorking) await loadSubscriptions();
  } finally { fetching = false; }
}
$("node-select").addEventListener("change", () => { $("status-filter").value = $("node-select").value ? "healthy" : ""; resetView(); });
$("quick-main").addEventListener("click", () => quickFilter("main"));
$("quick-node").addEventListener("click", () => quickFilter("node"));
const nodeDialog = $("node-dialog");
async function loadNodes() {
  const data = await (await api("/api/nodes")).json();
  if (updateNodes(data.nodes)) { filterCache = null; render(); }
  $("node-fingerprint").textContent = data.certificate_fingerprint || L("در SSH اثر انگشت گواهی را از دستور زیر بگیرید: openssl x509 -in /etc/v2ray-desk/cert.pem -noout -fingerprint -sha256", "In SSH obtain the fingerprint with: openssl x509 -in /etc/v2ray-desk/cert.pem -noout -fingerprint -sha256");
  $("node-list").replaceChildren();
  for (const entry of availableNodes) {
    const row = node("div", undefined, "node-entry");
    row.append(node("span", entry.name + (entry.last_seen ? " · " + new Date(entry.last_seen).toLocaleString(EN ? "en-US" : "fa-IR") : L(" · هنوز متصل نشده", " · not connected yet"))));
    const location = node("select");
    location.setAttribute("aria-label", L(`محل نود ${entry.name}`, `Location of ${entry.name}`));
    for (const [value, label] of [["iran", L("ایران", "Iran")], ["other", L("سایر کشورها", "Other location")]]) {
      const option = node("option", label); option.value = value; location.append(option);
    }
    location.value = entry.location || "other";
    location.addEventListener("change", action(async () => {
      location.disabled = true;
      try { await api(`/api/nodes/${entry.id}`, {location:location.value}, "PUT"); await loadNodes(); await refresh(); }
      catch (error) { location.value = entry.location || "other"; throw error; }
      finally { location.disabled = false; }
    }));
    const remove = node("button", L("حذف نود", "Remove node"), "text-button danger"); remove.type = "button";
    remove.addEventListener("click", action(async () => {
      if (!confirm(L(`نود ${entry.name} و نتایجش حذف شود؟`, `Remove node ${entry.name} and its results?`))) return;
      await api(`/api/nodes/${entry.id}`, {}, "DELETE"); await loadNodes(); await refresh();
    }));
    row.append(location, remove); $("node-list").append(row);
  }
}
$("nodes-open").addEventListener("click", action(async () => { $("node-token-area").hidden = true; $("node-token").value = ""; nodeDialog.showModal(); await loadNodes(); }));
$("node-close").addEventListener("click", () => { $("node-token").value = ""; nodeDialog.close(); });
nodeDialog.addEventListener("close", () => { $("node-token").value = ""; $("node-token-area").hidden = true; });
$("node-create").addEventListener("click", action(async () => {
  const name = $("node-name").value.trim();
  if (!name) throw Error(L("نام نود را وارد کنید.", "Enter a node name."));
  $("node-create").disabled = true;
  try {
    const data = await (await api("/api/nodes", {name, location:$("node-location").value || "iran"})).json();
    $("node-token").value = data.token; $("node-token-area").hidden = false; $("node-name").value = "";
    await loadNodes(); await refresh();
  } finally { $("node-create").disabled = false; }
}));
async function startTest(ids) {
  if (subscriptionSync.running) throw Error(L("تست پس از پایان دریافت ساب‌ها فعال می‌شود.", "Testing is available when subscription syncing finishes."));
  await api("/api/test", {ids, options: readTestOptions()});
  await refresh();
}
function readTestOptions() {
  return {workers:Number($("test-workers").value), samples:2,
    timeout:Number($("test-timeout").value), target:$("test-target").value,
    gemini:$("test-gemini").checked, country:$("test-country").checked,
    auto_delete_failed:$("test-auto-delete-failed").checked};
}
const testStorageKey = "v2ray-desk-test-options-v3:" + BASE;
try {
  const saved = JSON.parse(localStorage.getItem(testStorageKey) || "{}");
  for (const key of ["workers","timeout","target"]) {
    const select = $("test-" + key);
    if ([...select.options].some(o => o.value === String(saved[key]))) select.value = String(saved[key]);
  }
  for (const key of ["gemini","country","auto_delete_failed"]) if (typeof saved[key] === "boolean") $("test-" + key.replaceAll("_", "-")).checked = saved[key];
} catch {}
document.querySelectorAll(".test-settings select,.test-settings input").forEach(input => input.addEventListener("change", () => {
  try { localStorage.setItem(testStorageKey, JSON.stringify(readTestOptions())); } catch {}
}));
for (const button of document.querySelectorAll(".sort-header")) {
  const key = button.dataset.sort;
  for (const suffix of ["", "-desc"]) {
    if (![...$("sort").options].some(option => option.value === key + suffix)) {
      const option = node("option", button.childNodes[0].textContent + (suffix ? " ↓" : " ↑"));
      option.value = key + suffix; $("sort").append(option);
    }
  }
  button.addEventListener("click", () => {
    $("sort").value = $("sort").value === key ? key + "-desc" : key;
    filterCache = null; page = 1; render();
  });
}
function openImport() {
  $("import-result").hidden = true;
  openDialog($("import-dialog"));
}
async function openEdit(id) {
  const data = await (await api(`/api/configs/${id}`)).json();
  $("edit-id").value = data.id; $("edit-name").value = data.name; $("edit-uri").value = data.uri;
  openDialog($("edit-dialog"));
}
async function copyConfig(id) {
  const data = await (await api(`/api/configs/${id}`)).json();
  if (!navigator.clipboard) {
    await openEdit(id); $("edit-uri").select(); toast(L("لینک انتخاب شد؛ آن را کپی کنید.", "The link is selected. Copy it now.")); return;
  }
  await navigator.clipboard.writeText(data.uri); toast(L("لینک کپی شد.", "Link copied."));
}
function askDelete(ids, failedOnly) {
  if (!ids.length) return;
  pendingDelete = {ids, failed_only: failedOnly};
  $("confirm-message").textContent = L(`${num.format(ids.length)} کانفیگ${failedOnly ? " بدون پاسخ از نتایج فعلی" : " انتخاب‌شده"} حذف شود؟`, `Delete ${num.format(ids.length)} ${failedOnly ? "nonresponding configurations in the current results" : "selected configurations"}?`);
  openDialog($("confirm-dialog"));
}
document.querySelectorAll(".close-dialog").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
for (const id of ["import-open", "empty-import"]) $(id).addEventListener("click", openImport);
$("all-nav").addEventListener("click", () => { activeCountry = ""; resetView(); });
$("country-filter").addEventListener("change", () => { activeCountry = $("country-filter").value; resetView(); });
$("subscription-filter").addEventListener("change", () => { activeSubscription = $("subscription-filter").value; resetView(); });
for (const id of ["search", "protocol-filter", "gemini-filter", "sort", "group"]) $(id).addEventListener(id === "search" ? "input" : "change", resetView);
$("status-filter").addEventListener("change", () => { $("node-select").value = ""; resetView(); });
$("prev-page").addEventListener("click", () => { page--; render(); });
$("next-page").addEventListener("click", () => { page++; render(); });
$("select-page").addEventListener("change", () => {
  filtered().slice((page - 1) * perPage, page * perPage).forEach(row => $("select-page").checked ? selected.add(row.id) : selected.delete(row.id));
  render();
});
$("select-filtered").addEventListener("click", () => { selected = new Set(filtered().map(r => r.id)); render(); });
$("clear-selection").addEventListener("click", () => { selected.clear(); render(); });
$("test-scope").addEventListener("change", syncButtons);
$("test-btn").addEventListener("click", action(() => startTest(testScope().map(row => row.id))));
$("cancel-test").addEventListener("click", action(async () => { await api("/api/test/cancel", {}); await refresh(); }));
$("delete-selected").addEventListener("click", () => askDelete([...selected], false));
$("delete-failed").addEventListener("click", () => askDelete(filtered().filter(row => row.status === "failed").map(row => row.id), true));
$("confirm-no").addEventListener("click", () => { pendingDelete = null; $("confirm-dialog").close(); });
$("confirm-yes").addEventListener("click", action(async () => {
  if (!pendingDelete) return;
  const data = await (await api("/api/delete", pendingDelete)).json();
  pendingDelete = null; selected.clear(); $("confirm-dialog").close();
  toast(L(`${num.format(data.deleted)} کانفیگ حذف شد.`, `Deleted ${num.format(data.deleted)} configurations.`)); await refresh();
}));
$("import-file").addEventListener("change", action(async () => {
  const file = $("import-file").files[0];
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) throw Error(L("حداکثر حجم فایل ۴ مگابایت است.", "Maximum file size is 4 MB."));
  $("import-text").value = await file.text();
}));
$("import-form").addEventListener("submit", action(async event => {
  event.preventDefault();
  $("import-submit").disabled = true;
  try {
    const data = await (await api("/api/import", {text: $("import-text").value})).json();
    const result = $("import-result"); result.replaceChildren();
    result.append(node("strong", L(`${num.format(data.added)} اضافه شد · ${num.format(data.duplicates)} تکراری · ${num.format(data.errors.length)} نامعتبر`, `${num.format(data.added)} added · ${num.format(data.duplicates)} duplicate · ${num.format(data.errors.length)} invalid`)));
    if (data.errors.length) {
      const ul = node("ul");
      data.errors.forEach(item => ul.append(node("li", L(`خط ${num.format(item.line)}: ${item.error}`, `Line ${num.format(item.line)}: ${item.error}`))));
      result.append(ul);
    } else { $("import-text").value = ""; $("import-file").value = ""; }
    result.hidden = false;
    await refresh();
  } finally { $("import-submit").disabled = false; }
}));
$("edit-form").addEventListener("submit", action(async event => {
  event.preventDefault();
  await api(`/api/configs/${$("edit-id").value}`, {uri: $("edit-uri").value, name: $("edit-name").value}, "PUT");
  $("edit-dialog").close(); toast(L("تغییرات ذخیره شد؛ کانفیگ را دوباره تست کنید.", "Changes saved. Test the configuration again.")); await refresh();
}));
$("export-btn").addEventListener("click", action(async () => {
  const format = $("export-format").value;
  const response = await api("/api/export", {ids: scope().map(row => row.id), format});
  const url = URL.createObjectURL(await response.blob());
  const anchor = node("a"); anchor.href = url; anchor.download = format === "base64" ? "v2ray-configs-base64.txt" : "v2ray-configs.txt";
  document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
$("report-btn").addEventListener("click", action(async () => {
  const response = await api("/api/report", {ids: scope().map(row => row.id)});
  const url = URL.createObjectURL(await response.blob());
  const anchor = node("a"); anchor.href = url; anchor.download = "v2ray-report.csv";
  document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
$("logout").addEventListener("click", action(async () => { await api("/api/logout", {}); location.assign(route("/login")); }));
$("password-open").addEventListener("click", () => openDialog($("password-dialog")));
$("password-form").addEventListener("submit", action(async event => {
  event.preventDefault();
  if ($("new-password").value !== $("confirm-password").value) throw Error(L("رمز جدید و تکرار آن یکسان نیست.", "New passwords do not match."));
  await api("/api/password", {current: $("current-password").value, password: $("new-password").value});
  location.assign(route("/login"));
}));
action(refresh)();
setInterval(() => { if (!document.hidden) action(refresh)(); }, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) action(refresh)(); });

const subsDialog = node("dialog", undefined, "subscriptions-dialog");
subsDialog.id = "subscriptions-dialog";
const subsHeading = node("div", undefined, "dialog-heading");
subsHeading.append(node("h2", L("مدیریت ساب‌ها", "Subscriptions")));
const subsClose = node("button", "×", "icon-button");
subsClose.setAttribute("aria-label", L("بستن", "Close"));
subsClose.onclick = () => subsDialog.close(); subsHeading.append(subsClose);
const subsForm = node("form");
const subName = node("input"), subUrl = node("input"), subEnabled = node("input");
subName.id = "subscription-name"; subName.required = true; subName.maxLength = 200;
subUrl.id = "subscription-url"; subUrl.type = "url"; subUrl.required = true;
subUrl.dir = "ltr"; subUrl.maxLength = 4096; subUrl.placeholder = "https://example.com/subscription";
subEnabled.type = "checkbox"; subEnabled.checked = true;
for (const [input, title] of [[subName,L("نام ساب","Subscription name")],[subUrl,L("لینک ساب","Subscription URL")]]) {
  const label = node("label", title); label.htmlFor = input.id; subsForm.append(label,input);
}
const enabledLabel = node("label", undefined, "group-toggle");
enabledLabel.append(subEnabled, document.createTextNode(L("در به‌روزرسانی همه لحاظ شود","Include in refresh all")));
const subSubmit = node("button", L("افزودن و دریافت","Add & sync"), "primary");
subSubmit.type = "submit";
const subReset = node("button", L("فرم جدید","New subscription"), "secondary"); subReset.type = "button";
const subActions = node("div", undefined, "dialog-footer"); subActions.append(subReset,subSubmit);
subsForm.append(enabledLabel,subActions);
const subsRefreshAll = node("button",L("به‌روزرسانی ساب‌های فعال","Refresh enabled subscriptions"),"secondary");
const subsList = node("div", undefined, "subscription-list");
const subsStatus = node("p", "", "hint"); subsStatus.setAttribute("role","status");
const subsProgress = node("p", "", "hint"); subsProgress.setAttribute("role", "status"); subsProgress.hidden = true;
const presetList = node("div", undefined, "subscription-list");
subsDialog.append(subsHeading,node("p",L("لینک مستقیم یا Base64؛ حداکثر ۲۰۰۰ کانفیگ در هر ساب. کانفیگ‌های دستی و ویرایش‌شده هنگام همگام‌سازی حفظ می‌شوند.","Plain text or Base64; up to 2,000 configurations per subscription. Manual and edited configurations are preserved during sync."),"muted"),subsProgress,
  node("h3", L("منابع آماده", "Built-in sources")),
  node("p", L("منابع آماده در اولین اجرا ثبت و منابع فعال دریافت می‌شوند. منابع حذف‌شدهٔ هر ارائه‌دهنده را می‌توانید دوباره اضافه کنید.", "Built-in sources are registered on first run and enabled sources are synced. Restore missing sources from each provider below."), "hint"),presetList,
  subsForm,subsStatus,subsRefreshAll,subsList);
document.body.append(subsDialog);
let subscriptionId = null, subWorking = false, subsListSignature = "";
function syncSubscriptionControls() {
  const busy = subWorking || running || subscriptionSync.running;
  for (const control of [subName, subUrl, subEnabled, subSubmit, subReset, subsRefreshAll]) control.disabled = busy;
  for (const button of subsList.querySelectorAll("button")) button.disabled = busy || button.dataset.syncing === "true";
  for (const button of presetList.querySelectorAll("button")) button.disabled = busy || button.dataset.missing === "0";
}
async function withSubscriptionLock(fn) {
  if (subWorking || running || subscriptionSync.running) return;
  subWorking = true; syncSubscriptionControls();
  try { await fn(); }
  finally {
    try { await loadSubscriptions(); }
    finally { subWorking = false; syncSubscriptionControls(); }
  }
}
function resetSubForm() {
  subscriptionId = null; subsForm.reset(); subEnabled.checked = true;
  subSubmit.textContent = L("افزودن و دریافت","Add & sync");
}
subReset.onclick = () => { if (!subWorking && !running && !subscriptionSync.running) resetSubForm(); };
async function loadPresetCatalog() {
  const data = await (await api("/api/subscriptions/presets")).json();
  updateSubscriptionProgress(data.subscription_sync);
  presetList.replaceChildren();
  for (const provider of data.providers || []) {
    const card = node("article", undefined, "subscription-card");
    card.append(node("strong", provider.name), node("p", L(`${num.format(provider.installed)} از ${num.format(provider.total)} منبع اضافه شده`, `${num.format(provider.installed)} of ${num.format(provider.total)} sources added`), "hint"));
    const button = node("button", provider.missing ? L(`افزودن منابع باقی‌مانده (${num.format(provider.missing)})`, `Add missing sources (${num.format(provider.missing)})`) : L("همهٔ منابع اضافه شده‌اند", "All sources added"), "secondary");
    button.dataset.missing = String(provider.missing);
    button.onclick = action(() => withSubscriptionLock(async () => {
      const result = await (await api("/api/subscriptions/presets", {provider:provider.id})).json();
      updateSubscriptionProgress(result.subscription_sync);
      subsStatus.textContent = L(`${num.format(result.added)} منبع اضافه شد؛ ${num.format(result.queued)} منبع در صف دریافت.`, `Added ${num.format(result.added)} sources; ${num.format(result.queued)} queued for sync.`);
      await loadPresetCatalog(); await refresh();
    }));
    card.append(button); presetList.append(card);
  }
  syncSubscriptionControls();
}
async function syncSubscription(id) {
  subsStatus.textContent = L("در حال دریافت ساب…","Downloading subscription…");
  const data = await (await api(`/api/subscriptions/${id}/refresh`,{})).json();
  subsStatus.textContent = L(`${data.total} کانفیگ · ${data.added} جدید · ${data.removed} حذف‌شده · ${data.invalid} پشتیبانی‌نشده`,`${data.total} configurations · ${data.added} added · ${data.removed} removed · ${data.invalid} unsupported`);
  await refresh();
}
async function loadSubscriptions() {
  const data = await (await api("/api/subscriptions")).json();
  const changed = updateSubscriptions(data.subscriptions);
  updateSubscriptionProgress(data.subscription_sync);
  if (changed) render(); else syncButtons();
  const signature = JSON.stringify(data.subscriptions);
  if (signature === subsListSignature) return data.subscriptions;
  subsListSignature = signature;
  subsList.replaceChildren();
  if (!data.subscriptions.length) subsList.append(node("p",L("هنوز سابی اضافه نشده است.","No subscriptions yet."),"muted"));
  for (const sub of data.subscriptions) {
    const card = node("article",undefined,"subscription-card");
    card.append(node("strong",sub.name),node("small",sub.host,"host"));
    const updated = sub.updated_at ? new Date(sub.updated_at).toLocaleString(EN ? "en-US":"fa-IR") : L("دریافت نشده","Never synced");
    card.append(node("p",`${num.format(sub.config_count)} ${L("کانفیگ","configurations")} · ${updated} · ${sub.enabled ? L("فعال","Enabled"):L("غیرفعال","Disabled")}`,"hint"));
    if (sub.error) card.append(node("p",sub.error,"subscription-error"));
    if (sub.invalid_count) card.append(node("p",L(`${sub.invalid_count} لینک نامعتبر یا پشتیبانی‌نشده رد شد.`,`${sub.invalid_count} invalid or unsupported links skipped.`),"hint"));
    const buttons = node("div",undefined,"actions");
    const controls = [
      [L("به‌روزرسانی","Refresh"), async () => syncSubscription(sub.id)],
      [L("ویرایش","Edit"), async () => {
        const detail = await (await api(`/api/subscriptions/${sub.id}`)).json();
        subscriptionId = sub.id; subName.value = detail.name; subUrl.value = detail.url; subEnabled.checked = !!detail.enabled;
        subSubmit.textContent = L("ذخیره و دریافت","Save & sync");
      }, subName],
      [L("حذف","Delete"), async () => {
        const choice = await chooseSubRemoval(sub.name);
        if (choice === null) return;
        await api(`/api/subscriptions/${sub.id}`,{remove_configs:choice},"DELETE");
        if (subscriptionId === sub.id) resetSubForm();
        await loadPresetCatalog(); await refresh();
      }]
    ];
    for (const [label, fn, focusAfter] of controls) {
      const button = node("button",label,"secondary");
      button.dataset.syncing = String(!!sub.syncing);
      button.disabled = subWorking || running || subscriptionSync.running || sub.syncing;
      button.onclick = action(async () => {
        await withSubscriptionLock(fn);
        if (!subWorking) focusAfter?.focus();
      });
      buttons.append(button);
    }
    card.append(buttons); subsList.append(card);
  }
  return data.subscriptions;
}
function chooseSubRemoval(name) {
  return new Promise(resolve => {
    const dialog = node("dialog");
    dialog.append(node("h2",L("حذف ساب","Delete subscription")),node("p",name));
    const label = node("label",undefined,"group-toggle"), remove = node("input"); remove.type = "checkbox";
    label.append(remove,document.createTextNode(L("کانفیگ‌های اختصاصی این ساب هم حذف شوند","Also delete configurations belonging only to this subscription")));
    dialog.append(label,node("p",L("کانفیگ‌های دستی، ویرایش‌شده یا مشترک با ساب‌های دیگر حفظ می‌شوند.","Manual, edited and shared configurations are preserved."),"hint"));
    const actions = node("div",undefined,"dialog-footer");
    for (const [text, answer] of [[L("انصراف","Cancel"),null],[L("حذف","Delete"),true]]) {
      const button = node("button",text,answer ? "danger-button":"secondary");
      button.onclick = () => { resolve(answer === null ? null : remove.checked); dialog.close(); dialog.remove(); };
      actions.append(button);
    }
    dialog.addEventListener("cancel",() => { resolve(null); dialog.remove(); });
    dialog.append(actions); document.body.append(dialog); openDialog(dialog);
  });
}
$("subscriptions-open").onclick = action(async () => { await loadSubscriptions(); await loadPresetCatalog(); openDialog(subsDialog); });
subsForm.onsubmit = action(async event => {
  event.preventDefault();
  await withSubscriptionLock(async () => {
    const targetId = subscriptionId;
    const result = await (await api(targetId ? `/api/subscriptions/${targetId}`:"/api/subscriptions",
      {name:subName.value,url:subUrl.value,enabled:subEnabled.checked},targetId ? "PUT":"POST")).json();
    subscriptionId = result.id; subSubmit.textContent = L("ذخیره و دریافت","Save & sync");
    await syncSubscription(result.id); resetSubForm();
  });
});
subsRefreshAll.onclick = action(() => withSubscriptionLock(async () => {
  let failures = 0;
  const subs = await loadSubscriptions();
  for (const sub of subs.filter(item => item.enabled)) {
    try { await syncSubscription(sub.id); } catch { failures++; }
  }
  subsStatus.textContent = L(`به‌روزرسانی تمام شد؛ ${failures} خطا.`,`Refresh complete; ${failures} failed.`);
}));

const scheduleDialog = node("dialog"); scheduleDialog.id = "automation-dialog";
const scheduleHeading = node("div", undefined, "dialog-heading");
const scheduleClose = node("button", "×", "icon-button"); scheduleClose.type = "button";
scheduleClose.setAttribute("aria-label", L("بستن", "Close")); scheduleClose.onclick = () => scheduleDialog.close();
scheduleHeading.append(node("h2", L("برنامهٔ روزانه", "Daily schedule")), scheduleClose);
const scheduleForm = node("form");
const scheduleEnabled = node("input"), scheduleTime = node("input"), scheduleWorkers = node("select"), scheduleDelete = node("input");
scheduleEnabled.type = "checkbox"; scheduleEnabled.id = "schedule-enabled";
scheduleTime.type = "time"; scheduleTime.id = "schedule-time"; scheduleTime.required = true;
scheduleWorkers.id = "schedule-workers";
scheduleDelete.type = "checkbox"; scheduleDelete.id = "schedule-auto-delete";
for (const count of [1,4,8,16,32,64,100,128]) { const option = node("option", num.format(count)); option.value = String(count); scheduleWorkers.append(option); }
for (const [input, labelText] of [[scheduleEnabled,L("اجرای روزانه فعال باشد", "Enable daily run")],
  [scheduleTime,L("زمان اجرا", "Run time")],[scheduleWorkers,L("تست هم‌زمان", "Parallel tests")],
  [scheduleDelete,L("حذف خودکار اتصال‌های ناموفق", "Auto-delete failed connections")]]) {
  const label = node("label", input.type === "checkbox" ? undefined : labelText, input.type === "checkbox" ? "group-toggle" : undefined);
  label.htmlFor = input.id;
  if (input.type === "checkbox") { label.append(input, document.createTextNode(labelText)); scheduleForm.append(label); }
  else scheduleForm.append(label, input);
}
const scheduleTimezone = node("p", "", "hint");
const scheduleStatus = node("div", undefined, "hint"); scheduleStatus.setAttribute("role", "status");
const scheduleError = node("p", "", "subscription-error"); scheduleError.setAttribute("role", "alert"); scheduleError.hidden = true;
const scheduleSubmit = node("button", L("ذخیرهٔ برنامه", "Save schedule"), "primary"); scheduleSubmit.type = "submit";
const scheduleFooter = node("div", undefined, "dialog-footer"); scheduleFooter.append(scheduleSubmit);
scheduleForm.append(node("p", L("حذف پس از دو شکست پیاپی اتصال در تست‌های کامل انجام می‌شود؛ خطای جمینای، خطای هسته و تست متوقف‌شده حساب نمی‌شوند.", "Deletion requires two consecutive connection failures in completed tests; Gemini errors, core errors and stopped tests do not count."), "hint"), scheduleTimezone, scheduleFooter);
scheduleDialog.append(scheduleHeading, node("p", L("هر روز ابتدا ساب‌های فعال به‌روز می‌شوند؛ سپس همهٔ کانفیگ‌ها با Real Delay، تشخیص کشور و بررسی جمینای تست می‌شوند.", "Each day, enabled subscriptions are refreshed, then all configurations are tested with Real Delay, country detection and Gemini checks."), "muted"), scheduleForm, scheduleStatus, scheduleError);
document.body.append(scheduleDialog);
let scheduleWorking = false;
function setScheduleWorking(value) {
  scheduleWorking = value;
  for (const control of [scheduleEnabled,scheduleTime,scheduleWorkers,scheduleDelete,scheduleSubmit,$("automation-open")]) control.disabled = value;
}
function scheduleDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(EN ? "en-US" : "fa-IR", {dateStyle:"medium", timeStyle:"short", timeZone:"Asia/Tehran"}).format(date);
}
function renderSchedule(data) {
  scheduleEnabled.checked = data.enabled === true;
  scheduleTime.value = data.time;
  const workerValue = String(data.workers);
  if (![...scheduleWorkers.options].some(option => option.value === workerValue)) {
    const option = node("option", num.format(data.workers)); option.value = workerValue; scheduleWorkers.append(option);
  }
  scheduleWorkers.value = workerValue;
  scheduleDelete.checked = data.auto_delete_failed === true;
  scheduleTimezone.textContent = L("منطقهٔ زمانی: Asia/Tehran", "Time zone: Asia/Tehran");
  const phase = {idle:L("آماده", "Ready"),refreshing:L("در حال دریافت ساب‌ها", "Refreshing subscriptions"),testing:L("در حال تست همهٔ کانفیگ‌ها", "Testing all configurations")}[data.phase] || data.phase;
  scheduleStatus.replaceChildren(node("p", L(`وضعیت: ${phase}`, `Status: ${phase}`)),
    node("p", L(`اجرای بعدی: ${scheduleDate(data.next_run)}`, `Next run: ${scheduleDate(data.next_run)}`)),
    node("p", L(`آخرین اجرا: ${scheduleDate(data.last_completed)}`, `Last run: ${scheduleDate(data.last_completed)}`)));
  if (data.last_summary) {
    const summary = data.last_summary;
    scheduleStatus.append(node("p", L(`${num.format(summary.subscriptions)} ساب · ${num.format(summary.subscription_errors)} خطای ساب · ${num.format(summary.tested)} تست · ${num.format(summary.auto_deleted)} حذف خودکار`,
      `${num.format(summary.subscriptions)} subscriptions · ${num.format(summary.subscription_errors)} subscription errors · ${num.format(summary.tested)} tested · ${num.format(summary.auto_deleted)} auto-deleted`)));
  }
  scheduleError.hidden = !data.last_error; scheduleError.textContent = data.last_error || "";
}
$("automation-open").onclick = action(async () => {
  if (scheduleWorking) return;
  setScheduleWorking(true);
  try { renderSchedule(await (await api("/api/automation")).json()); openDialog(scheduleDialog); }
  finally { setScheduleWorking(false); }
});
scheduleForm.onsubmit = action(async event => {
  event.preventDefault();
  if (scheduleWorking) return;
  setScheduleWorking(true);
  try {
    const data = await (await api("/api/automation", {enabled:scheduleEnabled.checked,time:scheduleTime.value,
      workers:Number(scheduleWorkers.value),auto_delete_failed:scheduleDelete.checked}, "PUT")).json();
    renderSchedule(data); toast(L("برنامهٔ روزانه ذخیره شد.", "Daily schedule saved."));
  } finally { setScheduleWorking(false); }
});
