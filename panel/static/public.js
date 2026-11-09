"use strict";
const BASE = document.querySelector('meta[name="app-base"]').content;
const EN = document.documentElement.lang === "en";
const L = (fa, en) => EN ? en : fa;
const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat(EN ? "en-US" : "fa-IR");
const regions = new Intl.DisplayNames([EN ? "en" : "fa"], {type: "region"});
const statuses = {healthy:L("دارای پاسخ", "Responding"),failed:L("بدون پاسخ", "No response"),pending:L("تست‌نشده", "Not tested"),testing:L("در حال تست", "Testing"),invalid:L("تنظیمات نامعتبر", "Invalid settings"),error:L("خطای تست", "Test error")};
const gemini = {http200:"HTTP 200",blocked:L("۴۰۳ ناشناس", "Anonymous HTTP 403"),limited:"HTTP 429",login:L("صفحهٔ ورود", "Sign-in page"),redirect:L("انتقال", "Redirect"),unreachable:L("خطای اتصال", "Connection error"),untested:L("تست‌نشده", "Not tested"),other:L("پاسخ دیگر", "Other response")};
let page = 1, requestNumber = 0, controller, searchTimer;
let selected = new Set(), visible = [], loading = false, copyingSelection = false, copyingAll = false, resultTotal = 0;
let publicNodes = [];

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function countryName(code) {
  if (!code) return L("نامشخص", "Unknown");
  try { return regions.of(code) || code; } catch { return code; }
}
function notice(text, bad = false) {
  $("public-notice").textContent = text;
  $("public-notice").classList.toggle("bad", bad);
  $("public-notice").hidden = !text;
}
async function read(path, signal) {
  const response = await fetch(BASE + path, {signal, headers:{"Accept":"application/json"}});
  if (!response.ok) throw Error(L("دریافت اطلاعات ناموفق بود؛ دوباره تلاش کنید.", "Could not load the data. Please try again."));
  return response.json();
}
async function copy(id, button) {
  button.disabled = true;
  try {
    const data = await read(`/api/public/configs/${id}`);
    await copyText(data.uri, 1);
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
}
async function copyText(text, count, missing = 0) {
  const suffix = missing ? L(` ${number.format(missing)} کانفیگ دیگر موجود نیست.`, ` ${number.format(missing)} configurations are no longer available.`) : "";
  try {
    await navigator.clipboard.writeText(text);
    notice((count === 1 ? L("کانفیگ کپی شد.", "Configuration copied.") : L(`${number.format(count)} کانفیگ کپی شد.`, `Copied ${number.format(count)} configurations.`)) + suffix);
  } catch {
    $("public-copy-status").textContent = L(`${number.format(count)} لینک آمادهٔ کپی است.`, `${number.format(count)} configuration links ready to copy.`) + suffix;
    $("public-copy-text").value = text;
    $("public-copy-dialog").showModal();
    $("public-copy-text").focus(); $("public-copy-text").select();
  }
}
function syncSelection() {
  $("public-selection-count").textContent = selected.size ? L(`${number.format(selected.size)} کانفیگ انتخاب شده`, `${number.format(selected.size)} configurations selected`) : L("کانفیگی انتخاب نشده", "No configurations selected");
  $("public-copy-selected").disabled = copyingSelection || copyingAll || selected.size === 0;
  $("public-copy-all").disabled = loading || copyingSelection || copyingAll || resultTotal === 0;
  $("public-clear-selection").disabled = selected.size === 0;
  $("public-select-page").disabled = loading || visible.length === 0;
  $("public-select-page").checked = visible.length > 0 && visible.every(item => selected.has(item.id));
  $("public-select-page").indeterminate = visible.some(item => selected.has(item.id)) && !$("public-select-page").checked;
  for (const check of $("public-rows").querySelectorAll("input[type=checkbox]")) {
    check.checked = selected.has(Number(check.value)); check.disabled = loading;
    check.closest("tr").classList.toggle("selected-row", check.checked);
  }
}
function clearSelection() { selected.clear(); syncSelection(); }
function syncQuickFilters() {
  const scope = $("public-node").value, hasIran = publicNodes.some(entry => entry.location === "iran");
  $("public-quick-main").setAttribute("aria-pressed", String(!scope && $("public-status").value === "healthy"));
  $("public-quick-node").setAttribute("aria-pressed", String(scope === "iran"));
  $("public-quick-node").disabled = !hasIran;
  $("public-quick-node-label").textContent = L("کانفیگ متصل در ایران", "Connected in Iran");
  $("public-quick-node-hint").textContent = hasIran ? L("همهٔ نودهای ایران", "All Iran nodes") : L("هنوز نود ایرانی موجود نیست", "No Iran node available");
  $("public-quick-main").title = L("برای نمایش همه دوباره بزنید", "Click again to show all");
  $("public-quick-node").title = hasIran ? L("اتصال موفق در حداقل یکی از نودهای ایران؛ برای نمایش همه دوباره بزنید", "Working on at least one Iran node; click again to show all") : L("هنوز نود ایرانی موجود نیست", "No Iran node available");
}
function quickFilter(target) {
  if (target === "node" && !publicNodes.some(entry => entry.location === "iran")) return;
  const scope = $("public-node").value;
  const active = target === "node" ? scope === "iran" : !scope && $("public-status").value === "healthy";
  $("public-node").value = target === "node" && !active ? "iran" : "";
  $("public-status").value = active ? "" : "healthy";
  page = 1; clearSelection(); syncQuickFilters(); load();
}
async function linksFor(ids) {
  const chunks = [];
  for (let start = 0; start < ids.length; start += 500) chunks.push(ids.slice(start,start + 500));
  let next = 0, failure;
  const results = new Array(chunks.length);
  await Promise.all(Array.from({length:Math.min(3,chunks.length)}, async () => {
    while (next < chunks.length && !failure) {
      const index = next++;
      try { results[index] = await read("/api/public/copy?ids=" + chunks[index].join(",")); }
      catch (error) { failure = failure || error; }
    }
  }));
  if (failure) throw failure;
  return new Map(results.flatMap(result => result.configs).filter(item => typeof item.uri === "string").map(item => [item.id,item.uri]));
}
async function copySelected() {
  if (copyingSelection || copyingAll || !selected.size) return;
  const ids = [...selected];
  copyingSelection = true; syncSelection();
  try {
    const links = await linksFor(ids);
    const available = ids.filter(id => links.has(id));
    for (const id of ids) if (!links.has(id)) selected.delete(id);
    syncSelection();
    if (!available.length) throw Error(L("کانفیگ‌های انتخاب‌شده دیگر موجود نیستند.", "The selected configurations are no longer available."));
    await copyText(available.map(id => links.get(id)).join("\n"), available.length, ids.length - available.length);
  } catch (error) { notice(error.message, true); }
  finally { copyingSelection = false; syncSelection(); }
}
async function copyAll() {
  if (copyingSelection || copyingAll || loading || !resultTotal) return;
  const params = filterParams();
  copyingAll = true; syncSelection();
  try {
    const data = await read("/api/public/ids?" + params);
    if (!Array.isArray(data.ids) || data.ids.length > 10000 || data.ids.some(id => !Number.isSafeInteger(id) || id < 1)) throw Error(L("فهرست کانفیگ‌ها معتبر نیست.", "Invalid configuration list."));
    const ids = data.ids, links = await linksFor(ids), available = ids.filter(id => links.has(id));
    if (!available.length) throw Error(L("کانفیگی برای کپی موجود نیست.", "No configurations are available to copy."));
    await copyText(available.map(id => links.get(id)).join("\n"), available.length, ids.length - available.length);
  } catch (error) { notice(error.message, true); }
  finally { copyingAll = false; syncSelection(); }
}
function render(data) {
  page = data.page;
  resultTotal = data.total;
  publicNodes = Array.isArray(data.nodes) ? data.nodes : [];
  const nodeSelect = $("public-node"), selectedNode = nodeSelect.value;
  const allNodes = element("option", L("همهٔ نودها", "All nodes")); allNodes.value = "";
  nodeSelect.replaceChildren(allNodes);
  const scopes = [""];
  if (publicNodes.some(entry => entry.location === "iran")) {
    const option = element("option", L("همهٔ نودهای ایران", "All Iran nodes")); option.value = "iran";
    nodeSelect.append(option); scopes.push("iran");
  }
  for (const entry of publicNodes) {
    const option = element("option", `${entry.name} · ${entry.location === "iran" ? L("ایران", "Iran") : L("سایر", "Other")}`);
    option.value = `node:${entry.id}`; nodeSelect.append(option); scopes.push(option.value);
  }
  nodeSelect.hidden = !publicNodes.length;
  nodeSelect.value = scopes.includes(selectedNode) ? selectedNode : "";
  if (selectedNode && !scopes.includes(selectedNode)) {
    $("public-status").value = ""; page = 1; clearSelection(); syncQuickFilters(); load(); return;
  }
  syncQuickFilters();
  $("public-total").textContent = number.format(data.stats.total);
  $("public-healthy").textContent = number.format(data.stats.healthy);
  $("public-best").textContent = data.stats.best === null ? "—" : `${number.format(Math.round(data.stats.best))} ms`;
  const country = $("public-country"), selectedCountry = country.value;
  const all = element("option", L("همهٔ کشورها", "All countries")); all.value = "";
  country.replaceChildren(all);
  for (const item of [...data.countries].sort((a,b) => countryName(a.country).localeCompare(countryName(b.country)))) {
    const option = element("option", `${countryName(item.country)} (${number.format(item.count)})`);
    option.value = item.country || "unknown"; country.append(option);
  }
  if (selectedCountry && !data.countries.some(item => (item.country || "unknown") === selectedCountry)) {
    country.value = ""; page = 1; clearSelection(); load(); return;
  }
  country.value = selectedCountry;
  visible = data.configs;
  $("public-rows").replaceChildren();
  for (const item of data.configs) {
    const row = element("tr");
    const checkCell = element("td", undefined, "check-cell"), check = element("input");
    check.type = "checkbox"; check.value = String(item.id);
    check.setAttribute("aria-label", L(`انتخاب ${item.name}`, `Select ${item.name}`));
    check.addEventListener("change", () => { if (loading) return; check.checked ? selected.add(item.id) : selected.delete(item.id); syncSelection(); });
    checkCell.append(check); row.append(checkCell);
    const name = element("td", item.name, "config-name"); name.title = item.name;
    const delay = item.status === "healthy" && item.latency_ms !== null ? `${number.format(Math.round(item.latency_ms))} ms` : "—";
    const status = element("td"); status.append(element("span", statuses[item.status] || item.status, `status ${item.status}`));
    const date = item.checked_at ? new Date(item.checked_at).toLocaleString(EN ? "en-US" : "fa-IR", {dateStyle:"short",timeStyle:"short"}) : "—";
    const relevantNodes = publicNodes.filter(entry => !nodeSelect.value || (nodeSelect.value === "iran" ? entry.location === "iran" : nodeSelect.value === `node:${entry.id}`));
    const remoteText = relevantNodes.flatMap(entry => {
      const remote = item.node_results?.[String(entry.id)];
      if (!remote) return [];
      return [`${entry.name}: ${statuses[remote.status] || remote.status}${remote.status === "healthy" && remote.latency_ms != null ? ` · ${number.format(Math.round(remote.latency_ms))} ms` : ""}`];
    }).join(" | ");
    const remoteCell = element("td", remoteText || (item.status === "healthy" && relevantNodes.length ? L("در انتظار تست", "Awaiting test") : "—"), "node-result-cell");
    row.append(element("td", number.format(item.id), "row-number"), name, element("td", item.protocol.toUpperCase()),
      element("td", countryName(item.country)), element("td", delay, "latency"), status,
      element("td", gemini[item.gemini_status] || "—"), remoteCell, element("td", date, "checked-date"));
    const cell = element("td"), button = element("button", L("کپی", "Copy"), "secondary copy-button");
    button.setAttribute("aria-label", L(`کپی ${item.name}`, `Copy ${item.name}`));
    button.addEventListener("click", () => copy(item.id, button)); cell.append(button); row.append(cell);
    $("public-rows").append(row);
  }
  $("public-empty").hidden = data.configs.length > 0;
  $("public-count").textContent = L(`${number.format(data.total)} کانفیگ`, `${number.format(data.total)} configurations`);
  $("public-page").textContent = `${number.format(page)} / ${number.format(data.pages)}`;
  $("public-prev").disabled = page <= 1;
  $("public-next").disabled = page >= data.pages;
  syncSelection();
}
$("public-node").addEventListener("change", () => {
  $("public-status").value = $("public-node").value ? "healthy" : "";
  page = 1; clearSelection();
  syncQuickFilters(); load();
});
$("public-quick-main").addEventListener("click", () => quickFilter("main"));
$("public-quick-node").addEventListener("click", () => quickFilter("node"));
async function load() {
  const current = ++requestNumber;
  loading = true; syncSelection();
  controller?.abort(); controller = new AbortController();
  const params = filterParams();
  params.set("page", String(page));
  try {
    const data = await read("/api/public/configs?" + params, controller.signal);
    if (current === requestNumber) { render(data); notice(""); }
  } catch (error) { if (error.name !== "AbortError" && current === requestNumber) notice(error.message, true); }
  finally { if (current === requestNumber) { loading = false; syncSelection(); } }
}
function filterParams() {
  const params = new URLSearchParams({q: $("public-search").value.trim()});
  for (const key of ["country", "protocol", "status", "gemini", "sort"]) params.set(key, $("public-" + key).value);
  const scope = $("public-node").value;
  if (scope === "iran") params.set("node_region", "iran");
  else if (scope.startsWith("node:")) params.set("node_id", scope.slice(5));
  return params;
}
for (const key of ["country", "protocol", "gemini", "sort"]) {
  $("public-" + key).addEventListener("change", () => { page = 1; clearSelection(); load(); });
}
$("public-status").addEventListener("change", () => { $("public-node").value = ""; page = 1; clearSelection(); syncQuickFilters(); load(); });
$("public-search").addEventListener("input", () => { page = 1; clearSelection(); clearTimeout(searchTimer); searchTimer = setTimeout(load, 250); });
$("public-select-page").addEventListener("change", () => { if (loading) return; visible.forEach(item => $("public-select-page").checked ? selected.add(item.id) : selected.delete(item.id)); syncSelection(); });
$("public-clear-selection").addEventListener("click", clearSelection);
$("public-copy-selected").addEventListener("click", copySelected);
$("public-copy-all").addEventListener("click", copyAll);
$("public-prev").addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
$("public-next").addEventListener("click", () => { page += 1; load(); });
$("public-copy-close").addEventListener("click", () => $("public-copy-dialog").close());
setInterval(() => { if (!document.hidden && !$("public-copy-dialog").open) load(); }, 15000);
load();
