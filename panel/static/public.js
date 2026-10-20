"use strict";
const BASE = document.querySelector('meta[name="app-base"]').content;
const EN = document.documentElement.lang === "en";
const L = (fa, en) => EN ? en : fa;
const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat(EN ? "en-US" : "fa-IR");
const regions = new Intl.DisplayNames([EN ? "en" : "fa"], {type: "region"});
const statuses = {healthy:L("دارای پاسخ", "Responding"),failed:L("بدون پاسخ", "No response"),pending:L("تست‌نشده", "Not tested"),testing:L("در حال تست", "Testing"),invalid:L("تنظیمات نامعتبر", "Invalid settings"),error:L("خطای تست", "Test error")};
const gemini = {http200:"HTTP 200",blocked:"HTTP 403",limited:"HTTP 429",login:L("صفحهٔ ورود", "Sign-in page"),redirect:L("انتقال", "Redirect"),unreachable:L("خطای اتصال", "Connection error"),untested:L("تست‌نشده", "Not tested"),other:L("پاسخ دیگر", "Other response")};
let page = 1, requestNumber = 0, controller, searchTimer;

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
    try {
      await navigator.clipboard.writeText(data.uri);
      notice(L("کانفیگ کپی شد.", "Configuration copied."));
    } catch {
      $("public-copy-text").value = data.uri;
      $("public-copy-dialog").showModal();
      $("public-copy-text").focus();
      $("public-copy-text").select();
    }
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
}
function render(data) {
  page = data.page;
  $("public-total").textContent = number.format(data.stats.total);
  $("public-healthy").textContent = number.format(data.stats.healthy);
  $("public-best").textContent = data.stats.best === null ? "—" : `${number.format(Math.round(data.stats.best))} ms`;
  const country = $("public-country"), selected = country.value;
  const all = element("option", L("همهٔ کشورها", "All countries")); all.value = "";
  country.replaceChildren(all);
  for (const item of [...data.countries].sort((a,b) => countryName(a.country).localeCompare(countryName(b.country)))) {
    const option = element("option", `${countryName(item.country)} (${number.format(item.count)})`);
    option.value = item.country || "unknown"; country.append(option);
  }
  if (selected && !data.countries.some(item => (item.country || "unknown") === selected)) {
    country.value = ""; page = 1; load(); return;
  }
  country.value = selected;
  $("public-rows").replaceChildren();
  for (const item of data.configs) {
    const row = element("tr");
    const name = element("td", item.name, "config-name"); name.title = item.name;
    const delay = item.status === "healthy" && item.latency_ms !== null ? `${number.format(Math.round(item.latency_ms))} ms` : "—";
    const status = element("td"); status.append(element("span", statuses[item.status] || item.status, `status ${item.status}`));
    const date = item.checked_at ? new Date(item.checked_at).toLocaleString(EN ? "en-US" : "fa-IR", {dateStyle:"short",timeStyle:"short"}) : "—";
    row.append(element("td", number.format(item.id), "row-number"), name, element("td", item.protocol.toUpperCase()),
      element("td", countryName(item.country)), element("td", delay, "latency"), status,
      element("td", gemini[item.gemini_status] || "—"), element("td", date, "checked-date"));
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
}
async function load() {
  const current = ++requestNumber;
  controller?.abort(); controller = new AbortController();
  const params = new URLSearchParams({page: String(page), q: $("public-search").value.trim()});
  for (const key of ["country", "protocol", "status", "gemini", "sort"]) params.set(key, $("public-" + key).value);
  $("public-refresh").disabled = true;
  try {
    const data = await read("/api/public/configs?" + params, controller.signal);
    if (current === requestNumber) { render(data); notice(""); }
  } catch (error) { if (error.name !== "AbortError" && current === requestNumber) notice(error.message, true); }
  finally { if (current === requestNumber) $("public-refresh").disabled = false; }
}
for (const key of ["country", "protocol", "status", "gemini", "sort"]) {
  $("public-" + key).addEventListener("change", () => { page = 1; load(); });
}
$("public-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; load(); }, 250); });
$("public-prev").addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
$("public-next").addEventListener("click", () => { page += 1; load(); });
$("public-refresh").addEventListener("click", load);
$("public-copy-close").addEventListener("click", () => $("public-copy-dialog").close());
setInterval(() => { if (!document.hidden && !$("public-copy-dialog").open) load(); }, 15000);
load();
