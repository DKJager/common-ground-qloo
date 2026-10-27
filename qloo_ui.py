"""qloo_ui: single-file Common Ground web page, exposed as PAGE (str)."""

__all__ = ["PAGE"]

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Common Ground -- by 3Dogs Nexus, powered by Qloo</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#4d4d4d;--card:#f4f6f9;--bd:#c3cad4;--ac:#0b5cad;--act:#fff;--bar:#dfe4ea;--warn:#fff3cd;--wfg:#533f03;--err:#fde8e8;--efg:#8a1010}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--mut:#bdbdbd;--card:#1e2126;--bd:#444b55;--ac:#7ab7ff;--act:#00203f;--bar:#363c45;--warn:#3d3200;--wfg:#ffe08a;--err:#4a1717;--efg:#ffb4b4}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header,main,footer{max-width:900px;margin:0 auto;padding:12px 16px}
h1{font-size:1.4rem;margin:.3em 0}h2{font-size:1.15rem;margin:1em 0 .4em}h3{margin:.2em 0;font-size:1.05rem}
.sub{color:var(--mut);margin:0}
button{font:inherit;min-height:44px;padding:6px 14px;border-radius:8px;border:1px solid var(--ac);background:transparent;color:var(--ac);cursor:pointer}
button.primary,button[aria-pressed="true"]{background:var(--ac);color:var(--act)}
button:disabled{opacity:.5;cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--ac);outline-offset:2px}
input,select{font:inherit;width:100%;min-height:44px;padding:6px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg)}
label{display:block;font-weight:600;margin:.5em 0 .2em}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;margin:10px 0}
.grid{display:grid;grid-template-columns:1fr;gap:10px}
@media (min-width:640px){.grid{grid-template-columns:1fr 1fr}}
.bar{background:var(--bar);border-radius:6px;height:12px;overflow:hidden}
.fill{background:var(--ac);height:100%}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;border:1px solid var(--bd);font-size:.9rem}
.notice{background:var(--warn);color:var(--wfg);padding:10px;border-radius:8px;margin:8px 0}
#error-box{background:var(--err);color:var(--efg);padding:10px;border-radius:8px;margin:8px 0}
.small{font-size:.9rem;color:var(--mut)}
table{width:100%;border-collapse:collapse}th,td{border:1px solid var(--bd);padding:6px;text-align:left;vertical-align:top}
caption{caption-side:bottom;font-size:.9rem;color:var(--mut);padding:6px;text-align:left}
[hidden]{display:none!important}
</style>
</head>
<body>
<header>
<h1>Common Ground -- by 3Dogs Nexus, powered by Qloo</h1>
<p class="sub">Tell us what each person likes; we find what everyone can enjoy.</p>
</header>
<main>
<div id="error-box" role="alert" hidden></div>
<section aria-labelledby="h-kind">
<h2 id="h-kind">1. Pick a category</h2>
<div id="kind-chips" class="row" role="group" aria-label="Category"></div>
<div id="city-wrap" hidden><label for="city">City</label><input id="city" maxlength="80" autocomplete="off"></div>
<div id="price-wrap" hidden><label for="price-max">Max price (places)</label>
<select id="price-max"><option value="">Any</option><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option></select></div>
</section>
<section aria-labelledby="h-people">
<h2 id="h-people">2. Who is in the group?</h2>
<div id="people" class="grid"></div>
<p id="likes-note" class="notice" role="status" hidden></p>
<div class="row">
<button id="add-person" type="button">Add person</button>
<button id="load-example" type="button">Load example</button>
<button id="find-btn" type="button" class="primary">Find common ground</button>
</div>
</section>
<div id="partial-box" class="notice" role="status" hidden></div>
<section aria-labelledby="h-res" aria-live="polite">
<h2 id="h-res">Results</h2>
<div id="results"><p class="small">No results yet.</p></div>
<p id="learned" class="small"></p>
<div id="excluded" class="small"></div>
<h2>Why this pick</h2>
<div id="narrative" class="card">Run a search to see the reasoning.</div>
<h2>What if someone was missing?</h2>
<ul id="counterfactual"></ul>
</section>
<section aria-labelledby="h-cmp">
<h2 id="h-cmp">Compare</h2>
<button id="compare-btn" type="button">Compare: LLM-only vs Qloo-grounded</button>
<div id="baseline-partial" class="notice" role="status" hidden></div>
<div style="overflow-x:auto">
<table id="compare-table">
<caption>Group fit measured with Qloo data, not a claim of ground truth</caption>
<thead><tr><th scope="col">LLM-only picks (measured fit)</th><th scope="col">Qloo-grounded picks</th></tr></thead>
<tbody id="compare-body"><tr><td colspan="2">Not run yet.</td></tr></tbody>
<tfoot id="compare-foot"></tfoot>
</table></div>
</section>
<section aria-labelledby="h-tr">
<h2 id="h-tr">Request trace</h2>
<div id="request-trace" class="small"></div>
<p id="trace-note" class="small"></p>
</section>
</main>
<footer>
<p class="small">Qloo data is used under the hackathon licence; nothing you type is stored.
<a href="https://github.com/DKJager/common-ground-qloo">Repo</a></p>
</footer>
<script>
"use strict";
const KINDS = ["movie","tv_show","book","artist","place","destination","podcast","video_game"];
const EXAMPLE = [
  {name:"Ana", likes:["The Godfather","Heat"], dislikes:[]},
  {name:"Ben", likes:["Amelie","Paddington 2"], dislikes:[]},
  {name:"Cy", likes:["Inception"], dislikes:[]}
];
let kind = "movie", rejections = [], learned = [], lastPicks = [], uid = 0, traces = [], busy = false;
const $ = id => document.getElementById(id);

function escape(s) {
  const m = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
  return String(s).replace(/[&<>"']/g, c => m[c]);
}
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function pct(v) {
  const n = Number(v);
  if (!isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.round(n <= 1 ? n * 100 : n)));
}
function showError(msg) {
  const b = $("error-box");
  b.textContent = msg || "";
  b.hidden = !msg;
}
function showNotice(id, d) {
  const b = $(id);
  const on = !!(d && d.partial);
  b.textContent = on ? "Partial results: " + (d.note || "some data could not be fetched.") : "";
  b.hidden = !on;
}

/* ---- kind chips and people ---- */
function renderChips() {
  const box = $("kind-chips");
  box.textContent = "";
  KINDS.forEach(k => {
    const b = el("button", k.replace("_", " "));
    b.type = "button";
    b.setAttribute("aria-pressed", String(k === kind));
    b.addEventListener("click", () => { kind = k; rejections = []; learned = []; renderChips(); });
    box.appendChild(b);
  });
  $("city-wrap").hidden = !(kind === "place" || kind === "destination");
  $("price-wrap").hidden = kind !== "place";
}
function field(card, text, cls, value, max) {
  const id = "f" + (++uid);
  const l = el("label", text); l.htmlFor = id;
  const i = el("input"); i.id = id; i.className = cls; i.value = value || ""; i.maxLength = max;
  i.autocomplete = "off";
  card.appendChild(l); card.appendChild(i);
  return i;
}
function addPerson(p) {
  p = p || {name:"", likes:[], dislikes:[]};
  const card = el("div", null, "card person");
  card.appendChild(el("h3", "Person"));
  field(card, "Name", "p-name", p.name, 40);
  const lk = field(card, "Likes (comma separated, up to 5)", "p-likes", p.likes.join(", "), 600);
  field(card, "Dislikes (comma separated, optional)", "p-dis", p.dislikes.join(", "), 600);
  lk.addEventListener("input", () => collect());
  const rm = el("button", "Remove person"); rm.type = "button"; rm.className = "rm";
  rm.addEventListener("click", () => { card.remove(); syncButtons(); });
  card.appendChild(rm);
  $("people").appendChild(card);
  syncButtons();
}
function syncButtons() {
  const n = $("people").children.length;
  $("add-person").disabled = n >= 6;
  document.querySelectorAll("#people .rm").forEach(b => { b.disabled = n <= 2; });
}
function setPeople(list) {
  $("people").textContent = "";
  list.forEach(p => addPerson(p));
}
const split = s => s.split(",").map(x => x.trim()).filter(x => x);

function collect() {
  const members = [], seen = new Set();
  let err = "", over = [];
  document.querySelectorAll("#people .person").forEach(c => {
    const name = c.querySelector(".p-name").value.trim();
    let likes = split(c.querySelector(".p-likes").value);
    const dislikes = split(c.querySelector(".p-dis").value);
    if (likes.length > 5) { over.push(name || "someone"); likes = likes.slice(0, 5); }
    if (!name) err = err || "Every person needs a name.";
    else if (name.length > 40) err = err || "Names must be 40 characters or fewer.";
    else if (seen.has(name.toLowerCase())) err = err || "Names must be unique: " + name + " appears twice.";
    seen.add(name.toLowerCase());
    if (likes.length < 1) err = err || (name || "Each person") + " needs at least 1 like.";
    if (likes.some(x => x.length > 80) || dislikes.some(x => x.length > 80)) err = err || "Each like or dislike must be 80 characters or fewer.";
    if (dislikes.length > 5) err = err || (name || "Each person") + " has more than 5 dislikes.";
    members.push({name, likes, dislikes});
  });
  const note = $("likes-note");
  note.textContent = over.length ? "More than 5 likes typed for " + over.join(", ") + ": only the first 5 are used, the extra ones are ignored." : "";
  note.hidden = !over.length;
  if (!err && (members.length < 2 || members.length > 6)) err = "A group needs 2 to 6 people.";
  return {members, err};
}

/* ---- network ---- */
async function post(path, body) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 28000);
  try {
    const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), signal:ctl.signal});
    const txt = await r.text();
    let d = null, parseFailed = false;
    try { d = JSON.parse(txt); } catch (e) { parseFailed = true; }
    if (!r.ok) return {err: (d && typeof d.error === "string" && d.error) || "The service answered with HTTP " + r.status + "."};
    if (parseFailed || !d || typeof d !== "object") return {err: "The service sent a reply the page could not read."};
    return {data: d};
  } catch (e) {
    return {err: e && e.name === "AbortError" ? "The request timed out after 28 seconds. Please try again." : "Could not reach the service: " + (e && e.message ? e.message : "network error") + "."};
  } finally {
    clearTimeout(timer);
  }
}
function buildBody(members) {
  const b = {kind, members, rejections};
  const city = $("city").value.trim();
  if ((kind === "place" || kind === "destination") && city) b.city = city;
  const pm = parseInt($("price-max").value, 10);
  if (kind === "place" && pm >= 1 && pm <= 4) b.price_max = pm;
  return b;
}

/* ---- traces (no names or likes) ---- */
function loadTraces() {
  let raw = null;
  try { raw = localStorage.getItem("cg_traces"); } catch (e) {
    $("trace-note").textContent = "Browser storage is blocked; traces are kept for this visit only.";
    return traces;
  }
  try { const a = JSON.parse(raw || "[]"); return Array.isArray(a) ? a : []; } catch (e) { return []; }
}
function renderTraces() {
  const box = $("request-trace");
  box.textContent = "";
  traces.slice().reverse().forEach(x => {
    box.appendChild(el("div", [x.request_id, x.kind, "calls " + x.qloo_calls, x.elapsed_ms + " ms",
      "partial " + x.partial, x.outcome].join(" | ")));
  });
}
function addTrace(t) {
  traces = loadTraces();
  traces.push(t);
  traces = traces.slice(-20);
  try { localStorage.setItem("cg_traces", JSON.stringify(traces)); } catch (e) {
    $("trace-note").textContent = "Browser storage is blocked; traces are kept for this visit only.";
  }
  renderTraces();
}
function trace(d, outcome, path) {
  d = d || {};
  addTrace({request_id: d.request_id || null, kind: kind + (path ? " " + path : ""), qloo_calls: d.qloo_calls ?? null,
            elapsed_ms: d.elapsed_ms ?? null, partial: d.partial === true, outcome});
}

/* ---- rendering ---- */
function renderPick(p) {
  const card = el("article", null, "card");
  card.appendChild(el("h3", String(p.name) + " (" + pct(p.score) + ")"));
  const pm = p.per_member || {};
  Object.keys(pm).forEach(m => {
    card.appendChild(el("div", m + ": " + pct(pm[m]), "small"));
    const bar = el("div", null, "bar");
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", m + " score");
    bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(pct(pm[m])));
    const f = el("div", null, "fill"); f.style.width = pct(pm[m]) + "%";
    bar.appendChild(f); card.appendChild(bar);
  });
  if (p.least_happy) card.appendChild(el("p", "least happy: " + p.least_happy, "badge"));
  if (Array.isArray(p.why) && p.why.length) {
    const ul = el("ul"); p.why.forEach(w => ul.appendChild(el("li", String(w)))); card.appendChild(ul);
  }
  const row = el("div", null, "row");
  Object.keys(pm).forEach(m => {
    const b = el("button", "Not for me (" + m + ")"); b.type = "button";
    b.addEventListener("click", () => {
      rejections.push({member: m, option_id: p.id});
      const tags = Array.isArray(p.why) && p.why.length ? p.why.join(", ") : String(p.name);
      learned.push("learned: " + m + " avoids " + tags);
      run();
    });
    row.appendChild(b);
  });
  card.appendChild(row);
  return card;
}
function render(d) {
  const box = $("results"); box.textContent = "";
  const picks = Array.isArray(d.picks) ? d.picks : [];
  lastPicks = picks;
  if (!picks.length) box.appendChild(el("p", "No common ground found. Try different likes.", "small"));
  picks.forEach(p => box.appendChild(renderPick(p)));
  $("learned").textContent = learned.join(" | ");
  $("excluded").textContent = d.excluded_count ? d.excluded_count + " option(s) excluded because someone dislikes them." : "";
  $("narrative").textContent = d.narrative || "No explanation was provided.";
  const cf = $("counterfactual"); cf.textContent = "";
  const c = d.counterfactual || {};
  Object.keys(c).forEach(m => cf.appendChild(el("li", "Without " + m + " the group would pick " + c[m])));
  const un = d.unresolved || {};
  Object.keys(un).forEach(m => { if (Array.isArray(un[m]) && un[m].length) cf.appendChild(el("li", "Not recognised for " + m + ": " + un[m].join(", "))); });
  showNotice("partial-box", d);
}

async function run() {
  if (busy) return;
  showError("");
  const {members, err} = collect();
  if (err) { showError(err); return; }
  busy = true; $("find-btn").disabled = true;
  try {
    const r = await post("/api/reconcile", buildBody(members));
    if (r.err) { showError(r.err); trace(null, "error: " + r.err); return; }
    try { render(r.data); trace(r.data, r.data.partial ? "partial" : "ok"); }
    catch (e) { showError("The results could not be displayed: " + e.message); trace(r.data, "render error"); }
  } finally { busy = false; $("find-btn").disabled = false; }
}

function renderCompare(d) {
  const body = $("compare-body"); body.textContent = "";
  const fits = Array.isArray(d.llm_picks_qloo_fit) ? d.llm_picks_qloo_fit : [];
  const n = Math.max(fits.length, lastPicks.length);
  for (let i = 0; i < n; i++) {
    const tr = el("tr"), f = fits[i], p = lastPicks[i];
    tr.appendChild(el("td", f ? String(f.name) + (f.resolved && f.group_score !== null && f.group_score !== undefined
      ? " - group fit " + pct(f.group_score) : " - not recognised by Qloo") : ""));
    tr.appendChild(el("td", p ? String(p.name) + " - group fit " + pct(p.score) : ""));
    body.appendChild(tr);
  }
  if (!n) { const tr = el("tr"), td = el("td", "No picks to compare."); td.colSpan = 2; tr.appendChild(td); body.appendChild(tr); }
  const foot = $("compare-foot"); foot.textContent = "";
  const tr = el("tr");
  tr.appendChild(el("td", "Average group score: " + pct(d.llm_avg_group_score)));
  tr.appendChild(el("td", "Average group score: " + pct(d.grounded_avg_group_score)));
  foot.appendChild(tr);
  showNotice("baseline-partial", d);
}

async function compare() {
  if (busy) return;
  showError("");
  const {members, err} = collect();
  if (err) { showError(err); return; }
  if (!lastPicks.length) { await run(); if (!lastPicks.length) return; }
  busy = true; $("compare-btn").disabled = true;
  try {
    const r = await post("/api/baseline", buildBody(members));
    if (r.err) { showError(r.err); trace(null, "error: " + r.err, "baseline"); return; }
    try { renderCompare(r.data); trace(r.data, r.data.partial ? "partial" : "ok", "baseline"); }
    catch (e) { showError("The comparison could not be displayed: " + e.message); trace(r.data, "render error", "baseline"); }
  } finally { busy = false; $("compare-btn").disabled = false; }
}

/* ---- start ---- */
renderChips();
setPeople([{name:"", likes:[], dislikes:[]}, {name:"", likes:[], dislikes:[]}, {name:"", likes:[], dislikes:[]}]);
traces = loadTraces();
renderTraces();
$("add-person").addEventListener("click", () => { if ($("people").children.length < 6) addPerson(); });
$("load-example").addEventListener("click", () => {
  kind = "movie"; rejections = []; learned = []; lastPicks = [];
  renderChips(); setPeople(EXAMPLE); showError("");
});
$("find-btn").addEventListener("click", run);
$("compare-btn").addEventListener("click", compare);
</script>
</body>
</html>
"""
