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
const countryNames = new Map();
const perPage = 50;
const statusNames = {pending: L("تست‌نشده", "Not tested"), healthy: L("دارای پاسخ", "Responding"), failed: L("بدون پاسخ", "No response"), error: L("خطای تست", "Test error"), invalid: L("تنظیمات نامعتبر", "Invalid settings"), testing: L("در حال تست", "Testing")};
const geminiNames = {untested:L("تست‌نشده", "Not tested"),http200:L("پاسخ 200", "HTTP 200"),blocked:L("خطای 403", "HTTP 403"),limited:L("محدودیت 429", "HTTP 429"),login:L("صفحهٔ ورود", "Sign-in page"),redirect:L("انتقال", "Redirect"),unreachable:L("خطای اتصال", "Connection error"),other:L("پاسخ دیگر", "Other response")};

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
  const list = records.filter(item => (!activeCountry || (item.country || "unknown") === activeCountry)
    && (!status || item.status === status)
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
  $("test-btn").disabled = running || testCount === 0;
  $("export-btn").disabled = scope().length === 0;
  $("report-btn").disabled = scope().length === 0;
  $("delete-selected").disabled = running || selected.size === 0;
  $("delete-failed").disabled = running || !filtered().some(r => r.status === "failed");
  $("select-filtered").disabled = count === 0;
  $("test-btn").textContent = L(`▷ تست (${num.format(testCount)})`, `▷ Test (${num.format(testCount)})`);
  $("export-btn").textContent = selected.size ? L("دریافت انتخاب‌شده‌ها ↓", "Export selected \u2193") : L("دریافت فهرست ↓", "Export results \u2193");
  $("selection-text").textContent = selected.size ? L(`${num.format(selected.size)} کانفیگ انتخاب شده`, `${num.format(selected.size)} configurations selected`) : L("کانفیگی انتخاب نشده", "No configurations selected");
}
function resetView() {
  filterCache = null;
  page = 1;
  selected.clear(); lastSelectedId = null;
  render();
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
  renderCountries();
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
    geminiBadge.title = item.gemini_code ? L(`HTTP ${item.gemini_code}؛ پاسخ وب، بدون ورود به حساب گوگل`, `HTTP ${item.gemini_code}; web response without a Google account`) : L("تست درخواست HTTPS به gemini.google.com/app از داخل همین پروکسی", "HTTPS request to gemini.google.com/app through this proxy");
    geminiCell.append(geminiBadge); tr.append(geminiCell);
    const checked = node("td", item.checked_at ? new Intl.DateTimeFormat(EN ? "en-US" : "fa-IR", {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}).format(new Date(item.checked_at)) : L("هنوز تست نشده", "Not tested yet"), "checked-date");
    tr.append(checked);
    const ops = node("td", undefined, "row-actions");
    for (const [label, icon, fn, disabled] of [
      [L("تست این کانفیگ", "Test this configuration"), "▷", () => startTest([item.id]), running],
      [L("ویرایش کانفیگ", "Edit configuration"), "✎", () => openEdit(item.id), running],
      [L("کپی لینک", "Copy link"), "⧉", () => copyConfig(item.id), false],
      [L("حذف کانفیگ", "Delete configuration"), "×", () => askDelete([item.id], false), running]
    ]) {
      const button = node("button", icon, "icon-button");
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
async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const data = await (await api("/api/configs" + (configsCursor ? "?cursor=" + encodeURIComponent(configsCursor) : ""))).json();
    configsCursor = data.cursor;
    $("engine-version").textContent = data.engine?.version ? `Xray ${data.engine.version}` : L("Xray در دسترس نیست", "Xray unavailable");
    const wasRunning = running;
    if (data.full) records = data.configs;
    else if (data.configs.length) {
      const updates = new Map(data.configs.map(row => [row.id, row]));
      records = records.map(row => updates.get(row.id) || row);
    }
    running = data.job.running;
    $("job").hidden = !running;
    const eta = data.job.eta === null ? "" : L(` · حدود ${num.format(Math.ceil(data.job.eta / 60))} دقیقه باقی‌مانده`, ` · about ${num.format(Math.ceil(data.job.eta / 60))} min left`);
    $("job-text").textContent = `${data.job.cancelled ? L("در حال توقف", "Stopping") : L("در حال تست Real Delay", "Testing Real Delay")} · ${num.format(data.job.done)}/${num.format(data.job.total)} · ${L("هم‌زمان", "active")}: ${num.format(data.job.active)} · ${L("در صف", "queued")}: ${num.format(data.job.queued)}${eta}`;
    $("job-progress").max = Math.max(1, data.job.total); $("job-progress").value = data.job.done;
    $("cancel-test").disabled = data.job.cancelled;
    if (wasRunning && !running) toast(data.job.cancelled ? L("تست متوقف شد؛ موارد تست‌نشده تغییر نکردند.", "Testing stopped. Queued configurations were left unchanged.") : L("تست فهرست تمام شد.", "Testing complete."));
    if (data.full || data.configs.length || wasRunning !== running) { filterCache = null; render(); }
    else syncButtons();
  } finally { fetching = false; }
}
async function startTest(ids) {
  await api("/api/test", {ids, options: readTestOptions()});
  await refresh();
}
function readTestOptions() {
  return {workers:Number($("test-workers").value), samples:2,
    timeout:Number($("test-timeout").value), target:$("test-target").value,
    gemini:$("test-gemini").checked, country:$("test-country").checked};
}
const testStorageKey = "v2ray-desk-test-options-v3:" + BASE;
try {
  const saved = JSON.parse(localStorage.getItem(testStorageKey) || "{}");
  for (const key of ["workers","timeout","target"]) {
    const select = $("test-" + key);
    if ([...select.options].some(o => o.value === String(saved[key]))) select.value = String(saved[key]);
  }
  for (const key of ["gemini","country"]) if (typeof saved[key] === "boolean") $("test-" + key).checked = saved[key];
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
for (const id of ["search", "status-filter", "gemini-filter", "sort", "group"]) $(id).addEventListener(id === "search" ? "input" : "change", resetView);
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
  const response = await api("/api/export", {ids: scope().map(row => row.id)});
  const url = URL.createObjectURL(await response.blob());
  const anchor = node("a"); anchor.href = url; anchor.download = "v2ray-configs.txt";
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
subsDialog.append(subsHeading,node("p",L("لینک مستقیم یا Base64؛ حداکثر ۲۰۰۰ کانفیگ در هر ساب. کانفیگ‌های دستی و ویرایش‌شده هنگام همگام‌سازی حفظ می‌شوند.","Plain text or Base64; up to 2,000 configurations per subscription. Manual and edited configurations are preserved during sync."),"muted"),subsForm,subsStatus,subsRefreshAll,subsList);
document.body.append(subsDialog);
let subscriptionId = null, subWorking = false;
function syncSubscriptionControls() {
  for (const control of [subName, subUrl, subEnabled, subSubmit, subReset, subsRefreshAll]) control.disabled = subWorking;
  for (const button of subsList.querySelectorAll("button")) button.disabled = subWorking || button.dataset.syncing === "true";
}
async function withSubscriptionLock(fn) {
  if (subWorking) return;
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
subReset.onclick = () => { if (!subWorking) resetSubForm(); };
async function syncSubscription(id) {
  subsStatus.textContent = L("در حال دریافت ساب…","Downloading subscription…");
  const data = await (await api(`/api/subscriptions/${id}/refresh`,{})).json();
  subsStatus.textContent = L(`${data.total} کانفیگ · ${data.added} جدید · ${data.removed} حذف‌شده · ${data.invalid} پشتیبانی‌نشده`,`${data.total} configurations · ${data.added} added · ${data.removed} removed · ${data.invalid} unsupported`);
  await refresh();
}
async function loadSubscriptions() {
  const data = await (await api("/api/subscriptions")).json();
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
        await refresh();
      }]
    ];
    for (const [label, fn, focusAfter] of controls) {
      const button = node("button",label,"secondary");
      button.dataset.syncing = String(!!sub.syncing);
      button.disabled = subWorking || sub.syncing;
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
$("subscriptions-open").onclick = action(async () => { await loadSubscriptions(); openDialog(subsDialog); });
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
