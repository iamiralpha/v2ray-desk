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
  click() { this.clicked = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
  }
}

const source = fs.readFileSync(path.join(__dirname, "../panel/static/app.js"), "utf8");
const blankSnapshot = {configs: [], full: true, cursor: "test:0", engine: {}, subscriptions: [],
  subscription_sync: {running:false,total:0,done:0},
  job: {running: false, cancelled: false, eta: null, total: 0, done: 0, active: 0, queued: 0, auto_deleted:0}};
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const row = (id, country) => ({id, country, name: `Config ${id}`, host: "example.com", port: 443,
  protocol: "vless", status: "pending", latency_ms: null, gemini_status: "untested", checked_at: null});

async function loadApp({savedOptions = null, lang = "en"} = {}) {
  const elements = new Map();
  const body = new Element("body");
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element(id.endsWith("-dialog") ? "dialog" : "div"));
    return elements.get(id);
  };
  get("sort").value = "index";
  get("test-scope").value = "all";
  get("export-format").value = "plain";
  for (const [id, values, value] of [["test-workers",[1,4,8,16,32,64,100,128],8],["test-timeout",[5,10,15],5],["test-target",["google","gstatic","cloudflare"],"google"]]) {
    for (const item of values) { const option = new Element("option"); option.value = String(item); get(id).append(option); }
    get(id).value = String(value);
  }
  const testControls = ["test-workers","test-scope","test-timeout","test-target","test-gemini","test-country","test-auto-delete-failed"].map(get);
  const storage = new Map();
  if (savedOptions !== null) storage.set("v2ray-desk-test-options-v3:", JSON.stringify(savedOptions));
  const created = [];
  const requests = [];
  let handler = async url => response(url.includes("/api/configs") ? blankSnapshot : {subscriptions: []});
  const context = vm.createContext({
    document: {documentElement: {lang}, body, hidden: false,
      getElementById: get, querySelector: () => ({content: ""}),
      querySelectorAll: selector => selector === "dialog[open]"
        ? [...elements.values(), ...body.querySelectorAll("dialog")].filter(element => element.tagName === "DIALOG" && element.open)
        : selector === ".test-settings select,.test-settings input" ? testControls : [],
      createElement: tag => { const element = new Element(tag); created.push(element); return element; }, createTextNode: text => Object.assign(new Element(), {_text: text}),
      addEventListener() {}},
    Intl, URL: {createObjectURL:() => "blob:test-export", revokeObjectURL() {}}, navigator: {}, location: {assign() {}},
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key,value) => storage.set(key,value)},
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: (url, options) => { requests.push({url, options}); return handler(url, options); }
  });
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  return {get, requests, created, storage, run: code => vm.runInContext(code, context),
    setHandler: fn => { handler = fn; }, set: (key, value) => { context[key] = value; }};
}

test("admin table shows the selected node result alongside the main result", async () => {
  const app = await loadApp();
  const config = {...row(1,"DE"),status:"healthy",latency_ms:91,
    node_results:{"2":{status:"failed",latency_ms:null},"3":{status:"healthy",latency_ms:55}}};
  const job = {...blankSnapshot,configs:[config],nodes:[{id:2,name:"Iran"},{id:3,name:"Other"}]};
  app.setHandler(async()=>response(job));
  await app.run("refresh()");
  assert.equal(app.get("rows").children[0].children.at(-3).textContent,"No response");
  app.get("node-select").value="3";
  app.get("node-select").listeners.change();
  assert.equal(app.get("rows").children[0].children.at(-3).textContent,"Responding · 55 ms");
});

test("node enrollment token is displayed once and cleared on close", async () => {
  const app = await loadApp();
  app.setHandler(async (url, options) => {
    if (url.endsWith("/api/nodes") && options.method === "POST") return response({node:{id:1,name:"Iran"},token:"sensitive-token"});
    if (url.endsWith("/api/nodes")) return response({nodes:[{id:1,name:"Iran",last_seen:null}],certificate_fingerprint:"abc"});
    return response(blankSnapshot);
  });
  await app.get("nodes-open").listeners.click({currentTarget:app.get("nodes-open")});
  app.get("node-name").value="Iran";
  await app.get("node-create").listeners.click({currentTarget:app.get("node-create")});
  assert.equal(app.get("node-token").value,"sensitive-token");
  assert.equal(app.get("node-token-area").hidden,false);
  app.get("node-close").listeners.click();
  assert.equal(app.get("node-token").value,"");
});

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

test("subscription and protocol filters compose with country and status and reset selection", async () => {
  const app = await loadApp();
  app.set("fixtures", [
    {...row(1,"DE"),subscription_ids:[10],status:"healthy"},
    {...row(2,"DE"),subscription_ids:[10],protocol:"vmess",status:"healthy"},
    {...row(3,"US"),subscription_ids:[10],status:"healthy"},
    {...row(4,"DE"),subscription_ids:[],status:"healthy"},
    {...row(5,"DE"),subscription_ids:[20],status:"healthy"}
  ]);
  app.run("records=fixtures; updateSubscriptions([{id:10,name:'First',config_count:3},{id:20,name:'Second',config_count:1}]); activeCountry='DE'; $('status-filter').value='healthy'; $('protocol-filter').value='vless'; selected.add(2); page=2; $('subscription-filter').value='10'; $('subscription-filter').listeners.change();");
  assert.equal(app.run("JSON.stringify(filtered().map(item=>item.id))"), "[1]");
  assert.equal(app.run("page"), 1);
  assert.equal(app.run("selected.size"), 0);
  app.run("$('subscription-filter').value='none'; $('subscription-filter').listeners.change();");
  assert.equal(app.run("JSON.stringify(scope().map(item=>item.id))"), "[4]");
  app.run("$('subscription-filter').value='10'; $('subscription-filter').listeners.change(); $('protocol-filter').value='vmess'; $('protocol-filter').listeners.change();");
  assert.equal(app.run("scope()[0].id"), 2);
});

test("unchanged row polls update source counts and removed sources reset to All", async () => {
  const app = await loadApp();
  let snapshot = {...blankSnapshot,configs:[{...row(1,"US"),subscription_ids:[10]}],subscriptions:[{id:10,name:"Source",config_count:1}]};
  app.setHandler(async () => response(snapshot));
  await app.run("refresh()");
  app.run("$('subscription-filter').value='10'; $('subscription-filter').listeners.change(); selected.add(1);");
  snapshot = {...snapshot,full:false,configs:[],subscriptions:[{id:10,name:"<img src=x>",config_count:2}]};
  await app.run("refresh()");
  assert.equal(app.get("subscription-filter").options.at(-1).textContent, "<img src=x> (2)");
  assert.equal(app.get("subscription-filter").options.at(-1).children.length, 0);
  assert.equal(app.run("activeSubscription"), "10");
  snapshot = {...snapshot,subscriptions:[]};
  await app.run("refresh()");
  assert.equal(app.get("subscription-filter").value, "");
  assert.equal(app.run("selected.size"), 0);
  assert.equal(app.get("rows").children.length, 1);
});

test("background subscription sync pauses bulk and row tests until completion", async () => {
  const app = await loadApp();
  let snapshot = {...blankSnapshot,configs:[row(1,"DE")],subscription_sync:{running:true,total:6,done:2}};
  app.setHandler(async () => response(snapshot));
  await app.run("refresh()");
  assert.equal(app.get("test-btn").disabled, true);
  assert.equal(app.get("rows").children[0].children.at(-1).children[0].disabled, true);
  assert.equal(app.get("subscription-sync-status").hidden, false);
  assert.match(app.get("subscription-sync-status").textContent, /2 of 6/);
  await assert.rejects(app.run("startTest([1])"), /syncing finishes/);
  assert.equal(app.requests.some(request => request.url === "/api/test"), false);
  snapshot = {...snapshot,full:false,configs:[],subscription_sync:{running:false,total:6,done:6}};
  await app.run("refresh()");
  assert.equal(app.get("test-btn").disabled, false);
  assert.equal(app.get("rows").children[0].children.at(-1).children[0].disabled, false);
  assert.equal(app.get("subscription-sync-status").hidden, true);
});

test("auto-delete preference defaults off, restores only booleans and persists with 128 workers", async () => {
  const defaults = await loadApp();
  assert.equal(defaults.run("readTestOptions().auto_delete_failed"), false);
  const invalid = await loadApp({savedOptions:{auto_delete_failed:"true"}});
  assert.equal(invalid.run("readTestOptions().auto_delete_failed"), false);
  const saved = await loadApp({savedOptions:{auto_delete_failed:true,workers:128}});
  assert.equal(saved.run("readTestOptions().auto_delete_failed"), true);
  assert.equal(saved.run("readTestOptions().workers"), 128);
  saved.run("$('test-auto-delete-failed').checked=false; $('test-auto-delete-failed').listeners.change();");
  const stored = JSON.parse(saved.storage.get("v2ray-desk-test-options-v3:"));
  assert.equal(stored.auto_delete_failed, false);
  assert.equal(stored.workers, 128);
});

test("completed test reports auto-deletion count and removes rows from the view", async () => {
  const app = await loadApp();
  let snapshot = {...blankSnapshot,configs:[row(1,"DE")],job:{...blankSnapshot.job,running:true,total:1}};
  app.setHandler(async () => response(snapshot));
  await app.run("refresh()");
  snapshot = {...blankSnapshot,job:{...blankSnapshot.job,total:1,done:1,auto_deleted:1}};
  await app.run("refresh()");
  assert.equal(app.get("rows").children.length, 0);
  assert.equal(app.get("auto-delete-result").hidden, false);
  assert.match(app.get("auto-delete-result").textContent, /Auto-deleted 1/);
  assert.match(app.get("toast").textContent, /Auto-deleted 1/);
});

for (const lang of ["en", "fa"]) {
  test(`two-stage progress follows the active stage and finishes only after metadata (${lang})`, async () => {
    const app = await loadApp({lang});
    let job = {...blankSnapshot.job,running:true,phase:"delay",phase_done:3,phase_total:8,
      done:3,total:8,active:2,queued:3,eta:90,options:{gemini:true,country:true}};
    app.setHandler(async () => response({...blankSnapshot,configs:[row(1,"DE")],job}));
    await app.run("refresh()");
    assert.equal(app.get("job").hidden,false);
    assert.match(app.get("job-text").textContent,lang === "en" ? /Stage 1 · Real Delay · 3\/8/ : /مرحلهٔ ۱ · تست Real Delay · ۳\/۸/);
    assert.equal(app.get("job-progress").max,8);
    assert.equal(app.get("job-progress").value,3);
    assert.equal(app.get("test-btn").disabled,true);
    assert.equal(app.get("cancel-test").disabled,false);

    job = {...job,done:8,phase_done:8,active:0,queued:0,eta:0};
    await app.run("refresh()");
    assert.equal(app.get("job").hidden,false);
    assert.equal(app.get("toast").textContent,"");
    job = {...job,phase:"metadata",phase_done:0,phase_total:3,active:2,queued:1,eta:75};
    await app.run("refresh()");
    assert.equal(app.get("job-progress").value,0);
    assert.equal(app.get("job-progress").max,3);
    assert.match(app.get("job-text").textContent,lang === "en" ? /Stage 2 · Gemini and country · 0\/3/ : /مرحلهٔ ۲ · جمینای و کشور · ۰\/۳/);
    assert.match(app.get("job-text").textContent,lang === "en" ? /Real Delay completed: 8\/8/ : /Real Delay تکمیل‌شده: ۸\/۸/);
    assert.match(app.get("job-text").textContent,lang === "en" ? /about 2 min left in this stage/ : /حدود ۲ دقیقه تا پایان این مرحله/);
    assert.equal(app.get("job-progress")["aria-valuetext"],app.get("job-text").textContent);
    assert.equal(app.get("test-btn").disabled,true);
    assert.equal(app.get("toast").textContent,"");

    job = {...job,phase_done:2,active:1,queued:0};
    await app.run("refresh()");
    assert.equal(app.get("job-progress").value,2);
    job = {...job,running:false,phase:"idle",phase_done:3,active:0,eta:null};
    await app.run("refresh()");
    assert.equal(app.get("job").hidden,true);
    assert.equal(app.get("test-btn").disabled,false);
    assert.equal(app.get("toast").textContent,lang === "en" ? "Testing complete." : "تست فهرست تمام شد.");
  });
}

test("metadata labels follow the job options and stopping keeps the current phase progress", async () => {
  for (const lang of ["en","fa"]) {
    const app = await loadApp({lang});
    let job = {...blankSnapshot.job,running:true,phase:"metadata",phase_done:2,phase_total:5,
      done:10,total:10,options:{gemini:true,country:false}};
    app.setHandler(async () => response({...blankSnapshot,job}));
    await app.run("refresh()");
    assert.equal(app.get("job-progress")["aria-label"],lang === "en" ? "Stage 2 · Gemini" : "مرحلهٔ ۲ · جمینای");
    job = {...job,cancelled:true,options:{gemini:false,country:true}};
    await app.run("refresh()");
    assert.equal(app.get("job-progress")["aria-label"],lang === "en" ? "Stage 2 · country" : "مرحلهٔ ۲ · کشور");
    assert.match(app.get("job-text").textContent,lang === "en" ? /^Stopping · Stage 2/ : /^در حال توقف · مرحلهٔ ۲/);
    assert.equal(app.get("job-progress").max,5);
    assert.equal(app.get("job-progress").value,2);
    assert.equal(app.get("job").hidden,false);
    assert.equal(app.get("cancel-test").disabled,true);
    assert.equal(app.get("toast").textContent,"");
    job = {...job,running:false,phase:"idle"};
    await app.run("refresh()");
    assert.match(app.get("toast").textContent,lang === "en" ? /^Testing stopped/ : /^تست متوقف شد/);
  }
});

test("legacy jobs without phase counters retain their Real Delay progress", async () => {
  const app = await loadApp();
  app.setHandler(async () => response({...blankSnapshot,job:{...blankSnapshot.job,running:true,
    done:4,total:9,active:2,queued:3,eta:90}}));
  await app.run("refresh()");
  assert.equal(app.get("job-progress").max,9);
  assert.equal(app.get("job-progress").value,4);
  assert.equal(app.get("job-text").textContent,"Testing Real Delay · 4/9 · active: 2 · queued: 3 · about 2 min left");
});

test("export sends the chosen format and filtered selection in visible order", async () => {
  const app = await loadApp();
  app.set("fixtures",[row(1,"DE"),row(2,"DE"),row(3,"US")]);
  app.run("records=fixtures; activeCountry='DE'; selected=new Set([2]); filterCache=null; render(); $('export-format').value='base64';");
  app.setHandler(async () => ({...response({}),blob:async()=>({})}));
  await app.run("$('export-btn').listeners.click({currentTarget:$('export-btn')})");
  const request = app.requests.find(item => item.url === "/api/export");
  assert.deepEqual(JSON.parse(request.options.body),{ids:[2],format:"base64"});
  assert.equal(app.created.find(element => element.tagName === "A").download,"v2ray-configs-base64.txt");
});

test("preset catalog uses server counts and queues only the requested provider", async () => {
  const app = await loadApp();
  let added = false;
  app.setHandler(async (url,options) => {
    const progress = {running:added,total:added ? 2 : 0,done:0};
    if (url === "/api/subscriptions/presets" && options.method === "POST") { added=true; return response({added:3,queued:2,subscription_sync:{running:true,total:2,done:0}}); }
    if (url === "/api/subscriptions/presets") return response({providers:[{id:"barry",name:"Barry",total:8,installed:added ? 8 : 5,missing:added ? 0 : 3}],subscription_sync:progress});
    if (url === "/api/subscriptions") return response({subscriptions:[],subscription_sync:progress});
    if (url.startsWith("/api/configs")) return response({...blankSnapshot,subscription_sync:progress});
    throw Error(`Unexpected request ${url}`);
  });
  await app.run("loadPresetCatalog()");
  assert.match(app.run("presetList.textContent"),/5 of 8 sources/);
  await app.run("presetList.querySelectorAll('button')[0].onclick({currentTarget:presetList.querySelectorAll('button')[0]})");
  const request=app.requests.find(item=>item.url==="/api/subscriptions/presets" && item.options.method==="POST");
  assert.deepEqual(JSON.parse(request.options.body),{provider:"barry"});
  assert.equal(app.run("presetList.querySelectorAll('button')[0].disabled"),true);
  assert.match(app.run("subsStatus.textContent"),/Added 3 sources; 2 queued/);
});

test("daily schedule loads fresh on open and saves strict settings using the server response", async () => {
  const app = await loadApp();
  let current = {enabled:true,time:"03:00",timezone:"Asia/Tehran",workers:100,auto_delete_failed:false,
    next_run:"2026-10-23T03:00:00+03:30",last_completed:null,last_error:null,phase:"idle",last_summary:null};
  app.setHandler(async (url,options) => {
    assert.equal(url,"/api/automation");
    if (options.method === "PUT") current={...current,...JSON.parse(options.body),next_run:null};
    return response(current);
  });
  await app.run("$('automation-open').onclick({currentTarget:$('automation-open')})");
  assert.equal(app.run("scheduleWorkers.value"),"100");
  assert.match(app.run("scheduleTimezone.textContent"),/Asia\/Tehran/);
  app.run("scheduleEnabled.checked=false; scheduleTime.value='04:15'; scheduleWorkers.value='128'; scheduleDelete.checked=true;");
  await app.run("scheduleForm.onsubmit({preventDefault(){},currentTarget:scheduleForm})");
  const request=app.requests.find(item=>item.url==="/api/automation" && item.options.method==="PUT");
  assert.deepEqual(JSON.parse(request.options.body),{enabled:false,time:"04:15",workers:128,auto_delete_failed:true});
  assert.match(app.run("scheduleStatus.textContent"),/Next run: —/);
  assert.equal(app.run("scheduleWorking || scheduleSubmit.disabled"),false);
  current={...current,time:"05:30",last_completed:"2026-10-22T03:20:00+03:30",last_summary:{subscriptions:6,subscription_errors:1,tested:400,auto_deleted:2}};
  app.run("scheduleDialog.close()");
  await app.run("$('automation-open').onclick({currentTarget:$('automation-open')})");
  assert.equal(app.run("scheduleTime.value"),"05:30");
  assert.match(app.run("scheduleStatus.textContent"),/Last run:/);
  assert.match(app.run("scheduleStatus.textContent"),/400 tested/);
  assert.equal(app.run("scheduleDialog.querySelector('.dialog-notice')"),undefined);
});

test("failed daily schedule saves preserve the draft and unlock controls", async () => {
  const app = await loadApp({lang:"fa"});
  app.run("openDialog(scheduleDialog); scheduleTime.value='07:00'; scheduleWorkers.value='100'");
  app.setHandler(async () => response({error:"زمان اجرا معتبر نیست."},400));
  await app.run("scheduleForm.onsubmit({preventDefault(){},currentTarget:scheduleForm})");
  assert.equal(app.run("scheduleTime.value"),"07:00");
  assert.equal(app.run("scheduleWorking || scheduleTime.disabled || scheduleSubmit.disabled"),false);
  assert.equal(app.run("scheduleDialog.querySelector('.dialog-notice').role"),"alert");
  assert.equal(app.run("scheduleDialog.querySelector('.dialog-notice').textContent"),"زمان اجرا معتبر نیست.");
});
