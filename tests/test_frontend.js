"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = []; this.dataset = {}; this.listeners = {};
    this.value = ""; this.checked = false; this.disabled = false;
    this._text = ""; this.classes = new Set();
    this.classList = {toggle: (name, value) => value ? this.classes.add(name) : this.classes.delete(name)};
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get options() { return this.children.filter(child => child.tagName === "OPTION"); }
  append(...children) {
    for (const child of children) { child.parentElement = this; this.children.push(child); }
  }
  prepend(child) { child.parentElement = this; this.children.unshift(child); }
  after(child) {
    child.parentElement = this.parentElement;
    this.parentElement.children.splice(this.parentElement.children.indexOf(this) + 1, 0, child);
  }
  replaceChildren(...children) { this._text = ""; this.children = []; this.append(...children); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this[name] = value; }
  matches(selector) {
    if (selector.startsWith(".")) return (this.className || "").split(" ").includes(selector.slice(1)) || this.classes.has(selector.slice(1));
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(child.matches(selector) ? [child] : []),
      ...child.querySelectorAll(selector)
    ]);
  }
  querySelector(selector) {
    let result = this.querySelectorAll(selector)[0];
    if (!result && !selector.startsWith(".")) { result = new Element(selector); this.append(result); }
    return result;
  }
  reset() {
    for (const input of this.querySelectorAll("input")) { input.value = ""; input.checked = false; }
  }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
  }
}

const source = fs.readFileSync(path.join(__dirname, "../panel/static/app.js"), "utf8");
const blankSnapshot = {configs: [], full: true, cursor: "test:0", engine: {},
  job: {running: false, cancelled: false, eta: null, total: 0, done: 0, active: 0, queued: 0}};
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const row = (id, country) => ({id, country, name: `Config ${id}`, host: "example.com", port: 443,
  protocol: "vless", status: "pending", latency_ms: null, gemini_status: "untested", checked_at: null});

async function loadApp() {
  const elements = new Map();
  const body = new Element("body");
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element(id.endsWith("-dialog") ? "dialog" : "div"));
    return elements.get(id);
  };
  get("sort").value = "index";
  get("test-scope").value = "all";
  const requests = [];
  let handler = async url => response(url.includes("/api/configs") ? blankSnapshot : {subscriptions: []});
  const context = vm.createContext({
    document: {documentElement: {lang: "en"}, body, hidden: false,
      getElementById: get, querySelector: () => ({content: ""}),
      querySelectorAll: selector => selector === "dialog[open]"
        ? [...elements.values(), ...body.querySelectorAll("dialog")].filter(element => element.tagName === "DIALOG" && element.open)
        : [],
      createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element(), {_text: text}),
      addEventListener() {}},
    Intl, URL, navigator: {}, location: {assign() {}},
    localStorage: {getItem: () => null, setItem() {}},
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: (url, options) => { requests.push({url, options}); return handler(url, options); }
  });
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  return {get, requests, run: code => vm.runInContext(code, context),
    setHandler: fn => { handler = fn; }, set: (key, value) => { context[key] = value; }};
}

test("deleting the final country record immediately restores a consistent All view", async () => {
  const app = await loadApp();
  app.set("fixtures", [row(1, "DE"), row(2, "US")]);
  app.run("records = fixtures; activeCountry = 'DE'; filterCache = null; selected.add(1); render();");
  assert.equal(app.get("rows").children.length, 1);
  app.run("records = records.filter(item => item.id !== 1); filterCache = null; render();");
  assert.equal(app.get("country-filter").value, "");
  assert.equal(app.get("collection-title").textContent, "All configurations 1");
  assert.equal(app.get("rows").children.length, 1);
  assert.equal(app.get("empty").hidden, true);
  assert.equal(app.get("selection-text").textContent, "No configurations selected");
  assert.equal(app.get("delete-selected").disabled, true);
  // An unchanged incremental poll must not revive the former cached empty list.
  app.setHandler(async () => response({...blankSnapshot, full: false}));
  await app.run("refresh()");
  assert.equal(app.get("export-btn").disabled, false);
  assert.equal(app.run("scope()[0].id"), 2);
});

test("country changes and disappearing Unknown filters use the updated rows", async () => {
  const app = await loadApp();
  app.set("fixtures", [row(1, "DE"), row(2, "US")]);
  app.run("records = fixtures; activeCountry = 'DE'; filterCache = null; render();");
  app.run("records[0].country = null; filterCache = null; render();");
  assert.equal(app.get("collection-title").textContent, "All configurations 2");
  assert.equal(app.get("rows").children.length, 2);
  app.run("activeCountry = 'unknown'; filterCache = null; render(); records[0].country = 'US'; filterCache = null; render();");
  assert.equal(app.get("country-filter").value, "");
  assert.equal(app.get("rows").children.length, 2);
});

test("saving subscriptions prevents switching the edit target until failed sync finishes", async () => {
  const app = await loadApp();
  const subscriptions = [
    {id: 1, name: "First", host: "first.example", enabled: true, config_count: 0},
    {id: 2, name: "Second", host: "second.example", enabled: true, config_count: 0}
  ];
  let releaseSave;
  const pendingSave = new Promise(resolve => { releaseSave = resolve; });
  app.setHandler(async (url, options) => {
    if (url === "/api/subscriptions") return response({subscriptions});
    if (url === "/api/subscriptions/1" && options.method === "PUT") return pendingSave;
    if (url === "/api/subscriptions/1/refresh") return response({error: "Download failed"}, 400);
    if (url === "/api/subscriptions/2") return response({name: "Second", url: "https://second.example/feed", enabled: true});
    throw Error(`Unexpected request ${url}`);
  });
  await app.run("loadSubscriptions()");
  app.run("subscriptionId = 1; subName.value = 'First'; subUrl.value = 'https://first.example/feed';");
  const saving = app.run("subsForm.onsubmit({preventDefault(){}, currentTarget: subsForm})");
  assert.equal(app.run("subName.disabled && subUrl.disabled && subReset.disabled && subsRefreshAll.disabled"), true);
  assert.equal(app.run("subsList.querySelectorAll('button').every(button => button.disabled)"), true);
  // Guard event handlers as well as disabling their controls.
  await app.run("subsList.children[1].querySelectorAll('button')[1].onclick({currentTarget: subsList.children[1].querySelectorAll('button')[1]})");
  app.run("subReset.onclick()");
  assert.equal(app.run("subscriptionId"), 1);
  assert.equal(app.run("subName.value"), "First");
  releaseSave(response({id: 1}));
  await saving;
  assert.equal(app.run("subscriptionId"), 1);
  assert.equal(app.run("subName.value"), "First");
  assert.equal(app.run("subName.disabled || subSubmit.disabled || subReset.disabled"), false);
  assert.equal(app.requests.some(request => request.url === "/api/subscriptions/2"), false);
});

test("subscription controls unlock even when reloading the list fails", async () => {
  const app = await loadApp();
  app.setHandler(async () => response({error: "Offline"}, 500));
  await app.run("subsRefreshAll.onclick({currentTarget: subsRefreshAll})");
  assert.equal(app.run("subWorking || subName.disabled || subSubmit.disabled || subReset.disabled"), false);
});

test("failed edits show an accessible error inside the dialog and clear it on reopening", async () => {
  const app = await loadApp();
  app.setHandler(async (_url, options) => options.method === "GET"
    ? response({id: 1, name: "Config", uri: "vless://example"})
    : response({error: "Unsupported protocol"}, 400));
  await app.run("openEdit(1)");
  app.run("$('edit-uri').value = 'vmess'");
  await app.run("$('edit-form').listeners.submit({preventDefault(){}, currentTarget: $('edit-form')})");
  const dialog = app.get("edit-dialog");
  const notice = dialog.querySelector(".dialog-notice");
  assert.equal(dialog.open, true);
  assert.equal(notice.textContent, "Unsupported protocol");
  assert.equal(notice.role, "alert");
  assert.equal(notice["aria-atomic"], "true");
  assert.equal(notice.parentElement, dialog);
  assert.equal(app.get("toast").hidden, true);
  app.run("$('edit-dialog').close()");
  await app.run("openEdit(1)");
  assert.equal(dialog.querySelector(".dialog-notice"), undefined);
  app.run("$('edit-dialog').close(); toast('Link copied.')");
  assert.equal(app.get("toast").textContent, "Link copied.");
  assert.equal(app.get("toast").hidden, false);
});

test("messages stay inside the foremost open dialog and use status for non-errors", async () => {
  const app = await loadApp();
  app.run("openImport(); const nested = node('dialog'); document.body.append(nested); openDialog(nested); toast('Selected for copying.');");
  assert.equal(app.run("nested.querySelector('.dialog-notice').textContent"), "Selected for copying.");
  assert.equal(app.run("nested.querySelector('.dialog-notice').role"), "status");
  assert.equal(app.get("import-dialog").querySelector(".dialog-notice"), undefined);
  assert.equal(app.get("toast").hidden, true);
});
