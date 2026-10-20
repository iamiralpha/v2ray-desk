"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.value = ""; this.disabled = false; this.open = false; this._text = "";
    this.classes = new Set();
    this.classList = {toggle:(name,value) => value ? this.classes.add(name) : this.classes.delete(name)};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child=>child.textContent).join(""); }
  set innerHTML(_value) { throw Error("Untrusted HTML must not be inserted"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
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
  assert.equal(app.get("public-rows").children[0].children[1].textContent,"New result");
  assert.equal(app.get("public-refresh").disabled,false);
  assert.equal(app.get("public-notice").hidden,true);
  assert.match(app.requests.at(-1).url,/^\/desk\/api\/public\/configs\?/);
});

test("superseded failures neither show errors nor unlock the active request",async()=>{
  const app=await loadPublic();
  const pending=[];
  app.setHandler(()=>new Promise(resolve=>pending.push(resolve)));
  const oldRequest=app.run("load()");
  const currentRequest=app.run("load()");
  pending[0](response({},500));
  await oldRequest;
  assert.equal(app.get("public-refresh").disabled,true);
  assert.equal(app.get("public-notice").hidden,true);
  pending[1](response(snapshot([row(1)])));
  await currentRequest;
  assert.equal(app.get("public-refresh").disabled,false);
});

test("untrusted names are plain text in the row and copy label",async()=>{
  const app=await loadPublic();
  const name='<img src=x onerror="alert(1)"><script>alert(2)</script>';
  app.set("fixture",snapshot([row(1,name)]));
  app.run("render(fixture)");
  const cells=app.get("public-rows").children[0].children;
  assert.equal(cells[1].textContent,name);
  assert.equal(cells[1].children.length,0);
  assert.equal(cells[1].title,name);
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
  assert.equal(app.get("public-rows").children[0].children[1].textContent,"US result");
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
