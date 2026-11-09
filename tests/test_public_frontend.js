"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.value = ""; this.disabled = false; this.open = false; this.checked = false; this._text = "";
    this.classes = new Set();
    this.classList = {toggle:(name,value) => value ? this.classes.add(name) : this.classes.delete(name)};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child=>child.textContent).join(""); }
  set innerHTML(_value) { throw Error("Untrusted HTML must not be inserted"); }
  append(...children) { for (const child of children) { child.parentElement=this; this.children.push(child); } }
  replaceChildren(...children) { this._text = ""; this.children = []; this.append(...children); }
  querySelectorAll(selector) {
    return this.children.flatMap(child=>[
      ...(selector === "input[type=checkbox]" && child.tagName === "INPUT" && child.type === "checkbox" ? [child] : []),
      ...child.querySelectorAll(selector)
    ]);
  }
  closest(tag) { return this.tagName === tag.toUpperCase() ? this : this.parentElement?.closest(tag); }
  setAttribute(name,value) { this[name] = value; }
  addEventListener(name,handler) { this.listeners[name] = handler; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
}

const source = fs.readFileSync(path.join(__dirname,"../panel/static/public.js"),"utf8");
const row = (id,name=`Config ${id}`,country="DE") => ({id,name,country,protocol:"vless",
  status:"healthy",latency_ms:100,gemini_status:"http200",checked_at:null});
const snapshot = (configs=[],extra={}) => ({configs,total:configs.length,page:1,pages:1,
  countries:[...new Set(configs.map(item=>item.country))].map(country=>({country,count:configs.filter(item=>item.country===country).length})),
  stats:{total:configs.length,healthy:configs.length,best:configs.length ? 100 : null},...extra});
const response = (data,status=200) => ({ok:status<400,json:async()=>data});
const settle = () => new Promise(setImmediate);

async function loadPublic({lang="en",clipboard,base=""}={}) {
  const elements = new Map(), requests = [], timers = new Map();
  let timerId=0, handler=async()=>response(snapshot());
  const get = id => {
    if (!elements.has(id)) elements.set(id,new Element(id.endsWith("dialog") ? "dialog" : "div"));
    return elements.get(id);
  };
  get("public-sort").value="latency";
  const context=vm.createContext({
    document:{documentElement:{lang},hidden:false,querySelector:()=>({content:base}),getElementById:get,createElement:tag=>new Element(tag)},
    Intl,AbortController,URLSearchParams,navigator:clipboard ? {clipboard} : {},
    fetch:(url,options)=>{requests.push({url,options});return handler(url,options);},
    setTimeout:callback=>{timers.set(++timerId,callback);return timerId;},
    clearTimeout:id=>timers.delete(id),setInterval(){}
  });
  vm.runInContext(source,context);
  await settle();
  return {get,requests,timers,run:code=>vm.runInContext(code,context),set:(key,value)=>{context[key]=value;},
    setHandler:fn=>{handler=fn;},flushTimers:()=>{const pending=[...timers.values()];timers.clear();pending.forEach(callback=>callback());}};
}

test("public visitors can view the selected node result separately from the primary result", async () => {
  const app=await loadPublic();
  const first={...row(1),node_results:{"4":{status:"failed",latency_ms:null},"5":{status:"healthy",latency_ms:46}}};
  const next=snapshot([first],{nodes:[{id:4,name:"Tehran",location:"iran"},{id:5,name:"Other",location:"other"}]});
  app.setHandler(async()=>response(next));
  await app.run("load()");
  let cells=app.get("public-rows").children[0].children;
  assert.equal(cells[5].textContent,"100 ms");
  assert.equal(cells[8].textContent,"Tehran: No response | Other: Responding · 46 ms");
  app.get("public-node").value="node:5";
  await app.run("load()");
  cells=app.get("public-rows").children[0].children;
  assert.equal(cells[5].textContent,"100 ms");
  assert.equal(cells[8].textContent,"Other: Responding · 46 ms");
});

test("quick filters query all Iran nodes and can narrow to one named node", async () => {
  const app=await loadPublic();
  assert.equal(app.get("public-quick-node").disabled,true);
  const data=snapshot([row(1)],{nodes:[{id:4,name:"Tehran",location:"iran"},{id:5,name:"Shiraz",location:"iran"},{id:6,name:"Other",location:"other"}]});
  app.setHandler(async()=>response(data));
  await app.run("load()");
  assert.equal(app.get("public-quick-node").disabled,false);
  app.get("public-quick-main").listeners.click();
  await settle();
  let params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("status"),"healthy");
  assert.equal(params.has("node_id"),false);
  assert.equal(app.get("public-quick-main")["aria-pressed"],"true");
  app.get("public-quick-node").listeners.click();
  await settle();
  params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("status"),"healthy");
  assert.equal(params.get("node_region"),"iran");
  assert.equal(params.has("node_id"),false);
  assert.equal(app.get("public-quick-node")["aria-pressed"],"true");
  app.run("page=2; selected.add(1)");
  app.get("public-node").value="node:5";
  app.get("public-node").listeners.change();
  await settle();
  params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("node_id"),"5");
  assert.equal(params.get("page"),"1");
  assert.equal(app.run("selected.size"),0);
  assert.equal(app.get("public-quick-node-label").textContent,"Connected in Iran");
  app.get("public-quick-node").listeners.click();
  await settle();
  params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("node_region"),"iran");
  app.get("public-quick-node").listeners.click();
  await settle();
  params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("status"),"");
  assert.equal(params.has("node_id"),false);
  assert.equal(params.has("node_region"),false);
});

test("a Gemini 403 is shown as an anonymous response", async () => {
  const app=await loadPublic();
  app.set("fixture",snapshot([{...row(1),gemini_status:"blocked"}]));
  app.run("render(fixture)");
  assert.equal(app.get("public-rows").children[0].children[7].textContent,"Anonymous HTTP 403");
});

test("a superseded list response cannot replace newer filtered results",async()=>{
  const app=await loadPublic({base:"/desk"});
  let releaseOld,releaseNew;
  app.setHandler(url=>new Promise(resolve=>{
    if (new URL(url,"https://example.test").searchParams.get("q")==="old") releaseOld=resolve;
    else releaseNew=resolve;
  }));
  const oldRequest=app.run("$('public-search').value='old'; load()");
  const newRequest=app.run("$('public-search').value='new'; load()");
  assert.equal(app.requests.at(-2).options.signal.aborted,true);
  releaseNew(response(snapshot([row(2,"New result")])));
  await newRequest;
  releaseOld(response(snapshot([row(1,"Old result")])));
  await oldRequest;
  assert.equal(app.get("public-rows").children[0].children[2].textContent,"New result");
  assert.equal(app.run("loading"),false);
  assert.equal(app.get("public-select-page").disabled,false);
  assert.equal(app.get("public-notice").hidden,true);
  assert.match(app.requests.at(-1).url,/^\/desk\/api\/public\/configs\?/);
});

test("superseded failures neither show errors nor unlock the active request",async()=>{
  const app=await loadPublic();
  app.set("fixture",snapshot([row(1)])); app.run("render(fixture)");
  const pending=[];
  app.setHandler(()=>new Promise(resolve=>pending.push(resolve)));
  const oldRequest=app.run("load()");
  const currentRequest=app.run("load()");
  pending[0](response({},500));
  await oldRequest;
  assert.equal(app.run("loading"),true);
  assert.equal(app.get("public-select-page").disabled,true);
  assert.equal(app.get("public-rows").querySelectorAll("input[type=checkbox]")[0].disabled,true);
  assert.equal(app.get("public-notice").hidden,true);
  pending[1](response(snapshot([row(1)])));
  await currentRequest;
  assert.equal(app.run("loading"),false);
  assert.equal(app.get("public-select-page").disabled,false);
  assert.equal(app.get("public-rows").querySelectorAll("input[type=checkbox]")[0].disabled,false);
});

test("untrusted names are plain text in the row and copy label",async()=>{
  const app=await loadPublic();
  const name='<img src=x onerror="alert(1)"><script>alert(2)</script>';
  app.set("fixture",snapshot([row(1,name)]));
  app.run("render(fixture)");
  const cells=app.get("public-rows").children[0].children;
  assert.equal(cells[2].textContent,name);
  assert.equal(cells[2].children.length,0);
  assert.equal(cells[2].title,name);
  assert.equal(cells.at(-1).children[0]["aria-label"],`Copy ${name}`);
});

for (const denied of [false,true]) {
  test(`copy falls back to a selected dialog when clipboard is ${denied ? "denied" : "unavailable"}`,async()=>{
    const app=await loadPublic({lang:"fa",clipboard:denied ? {writeText:async()=>{throw Error("Not allowed");}} : undefined});
    const uri="vless://id@example.test:443#Example";
    let release;
    app.setHandler(()=>new Promise(resolve=>{release=resolve;}));
    app.set("button",new Element("button"));
    const copying=app.run("copy(42,button)");
    assert.equal(app.run("button.disabled"),true);
    release(response({uri}));
    await copying;
    assert.equal(app.requests.at(-1).url,"/api/public/configs/42");
    assert.equal(app.get("public-copy-dialog").open,true);
    assert.equal(app.get("public-copy-text").value,uri);
    assert.equal(app.get("public-copy-text").focused,true);
    assert.equal(app.get("public-copy-text").selected,true);
    assert.equal(app.run("button.disabled"),false);
    app.get("public-copy-close").listeners.click();
    assert.equal(app.get("public-copy-dialog").open,false);
  });
}

test("successful clipboard copying keeps the fallback dialog closed",async()=>{
  let copied;
  const app=await loadPublic({clipboard:{writeText:async text=>{copied=text;}}});
  app.setHandler(async()=>response({uri:"ss://sample"}));
  app.set("button",new Element("button"));
  await app.run("copy(1,button)");
  assert.equal(copied,"ss://sample");
  assert.equal(app.get("public-copy-dialog").open,false);
  assert.equal(app.get("public-notice").textContent,"Configuration copied.");
});

test("copy all snapshots every filtered result in bounded batches without altering selection",async()=>{
  let copied;
  const app=await loadPublic({clipboard:{writeText:async text=>{copied=text;}}});
  app.set("fixture",snapshot([row(1)],{total:1205,pages:25,nodes:[{id:4,name:"Tehran",location:"iran"}]}));
  app.run("render(fixture); $('public-node').value='iran'; $('public-status').value='healthy'; selected.add(1); syncSelection()");
  const ids=Array.from({length:1205},(_,i)=>i+1), chunks=[];
  app.setHandler(async url=>{
    const parsed=new URL(url,"https://example.test");
    if (parsed.pathname.endsWith("/ids")) {
      assert.equal(parsed.searchParams.get("node_region"),"iran");
      assert.equal(parsed.searchParams.get("status"),"healthy");
      return response({ids});
    }
    const batch=parsed.searchParams.get("ids").split(",").map(Number);
    chunks.push(batch);
    return response({configs:batch.map(id=>({id,uri:`vless://sample-${id}`}))});
  });
  await app.get("public-copy-all").listeners.click();
  assert.deepEqual(chunks.map(chunk=>chunk.length),[500,500,205]);
  assert.equal(copied.split("\n").length,1205);
  assert.equal(copied.split("\n")[1204],"vless://sample-1205");
  assert.equal(app.run("selected.size"),1);
  assert.equal(app.get("public-copy-all").disabled,false);
});

test("a disappeared country clears its filter and fetches page one of All",async()=>{
  const app=await loadPublic();
  app.run("$('public-country').value='DE'; page=3;");
  const start=app.requests.length;
  app.setHandler(async url=>{
    const params=new URL(url,"https://example.test").searchParams;
    return params.get("country")==="DE"
      ? response(snapshot([],{countries:[{country:"US",count:1}]}))
      : response(snapshot([row(2,"US result","US")]));
  });
  await app.run("load()");
  await settle();
  assert.equal(app.requests.length-start,2);
  const params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("country"),"");
  assert.equal(params.get("page"),"1");
  assert.equal(app.get("public-country").value,"");
  assert.equal(app.get("public-rows").children[0].children[2].textContent,"US result");
  assert.equal(app.get("public-page").textContent,"1 / 1");
  assert.equal(app.get("public-next").disabled,true);
});

test("server-clamped pagination and Unknown-country selection remain consistent",async()=>{
  const app=await loadPublic({lang:"fa"});
  app.run("$('public-country').value='unknown'; page=9;");
  app.setHandler(async()=>response(snapshot([row(1,"ناشناس",null)],{page:2,pages:2,total:51})));
  await app.run("load()");
  assert.equal(app.run("page"),2);
  assert.equal(app.get("public-country").value,"unknown");
  assert.equal(app.get("public-next").disabled,true);
  assert.equal(app.get("public-prev").disabled,false);
  assert.equal(app.get("public-page").textContent,"۲ / ۲");
  assert.match(app.get("public-rows").textContent,/نامشخص/);
});

test("search debounce sends only the final term and resets pagination",async()=>{
  const app=await loadPublic();
  const start=app.requests.length;
  app.run("page=3; $('public-search').value='f'; $('public-search').listeners.input(); $('public-search').value='final'; $('public-search').listeners.input();");
  assert.equal(app.requests.length,start);
  assert.equal(app.timers.size,1);
  app.flushTimers();
  await settle();
  const params=new URL(app.requests.at(-1).url,"https://example.test").searchParams;
  assert.equal(params.get("q"),"final");
  assert.equal(params.get("page"),"1");
  assert.equal(app.requests.length,start+1);
});

test("public selections persist across pages and clear when a filter changes",async()=>{
  const app=await loadPublic();
  app.set("fixture",snapshot([row(1),row(2)],{pages:2,total:4}));
  app.run("render(fixture); $('public-select-page').checked=true; $('public-select-page').listeners.change();");
  assert.equal(app.run("selected.size"),2);
  assert.equal(app.get("public-select-page").checked,true);
  app.set("fixture",snapshot([row(3),row(4)],{page:2,pages:2,total:4}));
  app.run("render(fixture)");
  assert.equal(app.get("public-select-page").checked,false);
  const check=app.get("public-rows").querySelectorAll("input[type=checkbox]")[0];
  check.checked=true; check.listeners.change();
  assert.equal(app.run("selected.size"),3);
  assert.equal(app.get("public-select-page").indeterminate,true);
  app.set("fixture",snapshot([row(1),row(2)],{pages:2,total:4}));
  app.run("render(fixture)");
  assert.equal(app.get("public-select-page").checked,true);
  assert.equal(app.get("public-rows").querySelectorAll("input[type=checkbox]").every(input=>input.checked),true);
  app.run("$('public-protocol').value='vmess'; $('public-protocol').listeners.change()");
  await settle();
  assert.equal(app.run("selected.size"),0);
  assert.equal(app.get("public-copy-selected").disabled,true);
});

test("public selection is disabled while a new filtered result is pending",async()=>{
  const app=await loadPublic();
  app.set("fixture",snapshot([row(1)])); app.run("render(fixture)");
  let release;
  app.setHandler(()=>new Promise(resolve=>{release=resolve;}));
  const loading=app.run("load()");
  assert.equal(app.get("public-select-page").disabled,true);
  const check=app.get("public-rows").querySelectorAll("input[type=checkbox]")[0];
  assert.equal(check.disabled,true);
  check.checked=true; check.listeners.change();
  assert.equal(app.run("selected.size"),0);
  release(response(snapshot([row(2)]))); await loading;
  assert.equal(app.get("public-select-page").disabled,false);
});

test("bulk copy preserves snapshot order, omits removed rows and supports clipboard fallback",async()=>{
  const app=await loadPublic();
  app.run("selected=new Set([2,3,1]); syncSelection()");
  app.setHandler(async()=>response({configs:[{id:1,uri:"vless://one"},{id:2,uri:"vless://two"}]}));
  await app.run("copySelected()");
  assert.equal(app.requests.at(-1).url,"/api/public/copy?ids=2,3,1");
  assert.equal(app.get("public-copy-text").value,"vless://two\nvless://one");
  assert.equal(app.get("public-copy-dialog").open,true);
  assert.match(app.get("public-copy-status").textContent,/2 configuration links ready to copy/);
  assert.match(app.get("public-copy-status").textContent,/1 configurations are no longer available/);
  assert.equal(app.run("JSON.stringify([...selected])"),"[2,1]");
  assert.equal(app.get("public-copy-selected").disabled,false);
});

test("changing filters during bulk copy never changes the copied snapshot or newer selection",async()=>{
  let copied,release;
  const app=await loadPublic({clipboard:{writeText:async text=>{copied=text;}}});
  app.setHandler(url=>url.startsWith("/api/public/copy?") ? new Promise(resolve=>{release=resolve;}) : Promise.resolve(response(snapshot([row(99)]))));
  app.run("selected=new Set([2,1]); syncSelection()");
  const copying=app.run("copySelected()");
  app.run("$('public-protocol').value='vless'; $('public-protocol').listeners.change()"); await settle();
  const check=app.get("public-rows").querySelectorAll("input[type=checkbox]")[0];
  check.checked=true; check.listeners.change();
  release(response({configs:[{id:2,uri:"ss://two"},{id:1,uri:"ss://one"}]}));
  await copying;
  assert.equal(copied,"ss://two\nss://one");
  assert.equal(app.run("JSON.stringify([...selected])"),"[99]");
});

test("bulk copying uses at most three concurrent requests of no more than 500 IDs",async()=>{
  let copied,active=0,maxActive=0;
  const pending=[];
  const app=await loadPublic({clipboard:{writeText:async text=>{copied=text;}}});
  app.run("selected=new Set(Array.from({length:1501},(_,index)=>index+1)); syncSelection()");
  app.setHandler(url=>{
    const ids=new URL(url,"https://example.test").searchParams.get("ids").split(",").map(Number);
    assert.ok(ids.length<=500);
    active++; maxActive=Math.max(maxActive,active);
    return new Promise(resolve=>pending.push(()=>{active--;resolve(response({configs:ids.map(id=>({id,uri:`ss://${id}`}))}));}));
  });
  const copying=app.run("copySelected()");
  assert.equal(pending.length,3);
  pending[2](); await settle();
  assert.equal(pending.length,4);
  pending[3](); pending[1](); pending[0]();
  await copying;
  assert.equal(maxActive,3);
  assert.equal(copied.split("\n").length,1501);
  assert.equal(copied.split("\n")[0],"ss://1");
  assert.equal(copied.split("\n").at(-1),"ss://1501");
});

test("missing selections clear after copying and failed requests preserve selection for retry",async()=>{
  const app=await loadPublic();
  app.run("selected=new Set([1]); syncSelection()");
  app.setHandler(async()=>response({},500));
  await app.run("copySelected()");
  assert.equal(app.run("selected.size"),1);
  assert.equal(app.get("public-copy-selected").disabled,false);
  app.setHandler(async()=>response({configs:[]}));
  await app.run("copySelected()");
  assert.equal(app.run("selected.size"),0);
  assert.equal(app.get("public-copy-selected").disabled,true);
  assert.equal(app.get("public-copy-dialog").open,false);
  assert.match(app.get("public-notice").textContent,/no longer available/);
});

test("a failed bulk-copy chunk stops scheduling and keeps retry locked until active requests finish",async()=>{
  let copied;
  const pending=[];
  const app=await loadPublic({clipboard:{writeText:async text=>{copied=text;}}});
  app.run("selected=new Set(Array.from({length:2000},(_,index)=>index+1)); syncSelection()");
  app.setHandler(()=>new Promise(resolve=>pending.push(resolve)));
  const copying=app.run("copySelected()");
  assert.equal(pending.length,3);
  pending[0](response({},500));
  await settle();
  assert.equal(app.run("copyingSelection"),true);
  assert.equal(app.get("public-copy-selected").disabled,true);
  await app.run("copySelected()");
  assert.equal(pending.length,3);
  pending[1](response({configs:[]}));
  await settle();
  assert.equal(pending.length,3);
  assert.equal(app.run("copyingSelection"),true);
  pending[2](response({configs:[]}));
  await copying;
  assert.equal(pending.length,3);
  assert.equal(copied,undefined);
  assert.equal(app.run("selected.size"),2000);
  assert.equal(app.run("copyingSelection"),false);
  assert.equal(app.get("public-copy-selected").disabled,false);
  assert.match(app.get("public-notice").textContent,/Could not load the data/);

  app.run("selected=new Set([1]); syncSelection()");
  app.setHandler(async()=>response({configs:[{id:1,uri:"ss://retry"}]}));
  await app.run("copySelected()");
  assert.equal(copied,"ss://retry");
});
