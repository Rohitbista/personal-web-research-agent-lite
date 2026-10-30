(() => {
const API = "http://localhost:8000";
//const API = "https://personal-web-research-agent-lite.fastapicloud.dev";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const when = d => d ? new Date(d).toLocaleString() : "";
const ACTIVE = ["queued", "running"], DONE = ["completed", "failed", "cancelled"];
const S = { token: localStorage.getItem("pwra_token"), user: null, jobs: [], cursor: null, cur: null, poll: null, q: "" };
 
if (window.DOMPurify) DOMPurify.addHook("afterSanitizeAttributes", n => {
  if (n.tagName === "A") { n.setAttribute("target", "_blank"); n.setAttribute("rel", "noopener noreferrer"); }
});
 
/* ───────── helpers ───────── */
let toastT;
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 3500); }
 
async function api(path, { method = "GET", body, blob } = {}) {
  const headers = {};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let r;
  try { r = await fetch(API + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined }); }
  catch { throw Object.assign(new Error("Can't reach the server. Check your connection and API_BASE in config.js."), { status: 0 }); }
  if (!r.ok) {
    let d = r.statusText;
    try { const j = await r.json(); d = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map(e => e.msg).join("; ") : d; } catch {}
    if (r.status === 401 && S.token && path !== "/auth/login") logout();
    throw Object.assign(new Error(d), { status: r.status });
  }
  if (r.status === 204) return null;
  return blob ? r.blob() : r.json();
}
 
function md(text) {
  if (!window.marked || !window.DOMPurify) return `<pre>${esc(text)}</pre>`;
  return DOMPurify.sanitize(marked.parse(text || ""));
}
const safeUrl = u => /^https?:\/\//i.test(u) ? u : "#";
const parseHash = () => { const [p, q = ""] = location.hash.replace(/^#\/?/, "").split("?"); return { path: p, params: new URLSearchParams(q) }; };
function go(hash) { history.replaceState(null, "", location.pathname + location.search + (hash || "")); route(); }
 
/* ───────── routing ───────── */
async function route() {
  const { path, params } = parseHash();
  if (path === "verify") return verifyScreen(params.get("token"));
  if (path === "reset") return authView("reset", "", params.get("token"));
  if (!S.token) return authView("login");
  if (!S.user) return enterApp();
  if (path.startsWith("job/")) openJob(path.slice(4)); else showNew();
}
addEventListener("hashchange", route);
 
/* ───────── auth ───────── */
function showOnly(which) { $("#auth").hidden = which !== "auth"; $("#app").hidden = which !== "app"; }
 
function authView(mode, note = "", token = "") {
  stopPoll(); showOnly("auth");
  const fields = {
    login: `<label for="e">Email</label><input id="e" type="email" autocomplete="email" required>
            <label for="p">Password</label><input id="p" type="password" autocomplete="current-password" required>
            <button type="button" class="linkbtn" data-m="forgot">Forgot your password?</button>`,
    register: `<label for="e">Email</label><input id="e" type="email" autocomplete="email" required>
            <label for="p">Password</label><input id="p" type="password" autocomplete="new-password" minlength="8" maxlength="128" required>
            <p class="hint">At least 8 characters.</p>`,
    forgot: `<p class="hint">Enter your email and we'll send a reset link.</p><label for="e">Email</label><input id="e" type="email" autocomplete="email" required>`,
    reset: `<label for="p">New password</label><input id="p" type="password" autocomplete="new-password" minlength="8" maxlength="128" required>`,
  };
  const label = { login: "Log in", register: "Create account", forgot: "Send reset link", reset: "Set new password" }[mode];
  const tabs = mode === "login" || mode === "register"
    ? `<div class="tabs" role="tablist">${[["login", "Log in"], ["register", "Sign up"]].map(([m, l]) => `<button type="button" role="tab" aria-selected="${mode === m}" data-m="${m}">${l}</button>`).join("")}</div>` : "";
  $("#auth").innerHTML = `<div class="card"><h2>Web Research Agent</h2>
    <p class="hint">Plan, run and review web research with your own LLM key.</p>${tabs}
    ${note ? `<p class="hint" role="status">${esc(note)}</p>` : ""}
    <form id="af">${fields[mode]}<p class="err" id="ae" role="alert"></p><div id="ax"></div>
    <p><button class="btn primary block" id="ab">${label}</button></p></form>
    ${mode === "forgot" || mode === "reset" ? `<button type="button" class="linkbtn" data-m="login">Back to log in</button>` : ""}</div>`;
  $("#auth").onclick = e => { const m = e.target.dataset.m; if (m) authView(m); };
  $("#af").onsubmit = async ev => {
    ev.preventDefault();
    const email = $("#e")?.value.trim(), pw = $("#p")?.value, btn = $("#ab");
    $("#ae").textContent = ""; $("#ax").innerHTML = ""; btn.disabled = true;
    try {
      if (mode === "login") {
        const r = await api("/auth/login", { method: "POST", body: { email, password: pw } });
        S.token = r.access_token; localStorage.setItem("pwra_token", S.token); S.user = null; go("");
      } else if (mode === "register") {
        const r = await api("/auth/register", { method: "POST", body: { email, password: pw } });
        authView("login", r.message);
      } else if (mode === "forgot") {
        const r = await api("/auth/forgot-password", { method: "POST", body: { email } });
        authView("login", r.message);
      } else {
        if (!token) throw new Error("This reset link is missing its token. Request a new one.");
        const r = await api("/auth/reset-password", { method: "POST", body: { token, new_password: pw } });
        history.replaceState(null, "", location.pathname); authView("login", r.message);
      }
    } catch (x) {
      $("#ae").textContent = x.message;
      if (mode === "login" && x.status === 403 && /verified/i.test(x.message)) {
        $("#ax").innerHTML = `<button type="button" class="linkbtn" id="rs">Resend verification email</button>`;
        $("#rs").onclick = async () => {
          try { const r = await api("/auth/resend-verification", { method: "POST", body: { email } }); $("#ae").textContent = ""; toast(r.message); }
          catch (y) { $("#ae").textContent = y.message; }
        };
      }
    } finally { btn.disabled = false; }
  };
}
 
async function verifyScreen(token) {
  stopPoll(); showOnly("auth");
  const card = msg => { $("#auth").innerHTML = `<div class="card"><h2>Email verification</h2><p id="vm">${esc(msg)}</p>
    <button class="btn primary block" id="vg" hidden>Go to log in</button></div>`; $("#vg").onclick = () => go(""); };
  card("Verifying your email…");
  try {
    if (!token) throw new Error("This link is missing its token. Request a new verification email from the log in page.");
    const r = await api("/auth/verify-email", { method: "POST", body: { token } });
    $("#vm").textContent = r.message;
  } catch (x) { $("#vm").textContent = x.message; }
  $("#vg").hidden = false;
}
 
function logout() {
  stopPoll(); S.token = null; S.user = null; S.jobs = []; S.cur = null;
  localStorage.removeItem("pwra_token"); $("#dlg").open && $("#dlg").close();
  history.replaceState(null, "", location.pathname + location.search); authView("login");
}
 
/* ───────── app shell ───────── */
async function enterApp() {
  try { S.user = await api("/auth/me"); } catch { return S.token ? authView("login") : null; }
  showOnly("app");
  await loadJobs(true);
  route();
  if (!S.user.has_llm_settings) openSettings(true);
}
 
async function loadJobs(reset) {
  if (reset) { S.cursor = null; S.jobs = []; }
  try {
    // Fetch up to 10 pages of 100 so follow-up threads are complete; "Load more" covers the rest.
    for (let i = 0; i < (reset ? 10 : 1); i++) {
      const p = new URLSearchParams({ limit: 100 });
      if (S.cursor) p.set("cursor", S.cursor);
      if (S.q) p.set("q", S.q);
      const r = await api("/research?" + p);
      S.jobs.push(...r.items); S.cursor = r.next_cursor;
      if (!S.cursor) break;
    }
  } catch (x) { toast(x.message); }
  renderJobs();
}
 
function tree(jobs) {
  const ids = new Set(jobs.map(j => j.job_id)), kids = {}, roots = [];
  for (const j of jobs) (j.parent_job_id && ids.has(j.parent_job_id) ? (kids[j.parent_job_id] ??= []) : roots).push(j);
  return { kids, roots };
}
function nodes(list, kids, item) {
  return list.map(j => `<li>${item(j)}${kids[j.job_id] ? `<ul class="kids">${nodes(kids[j.job_id], kids, item)}</ul>` : ""}</li>`).join("");
}
 
function renderJobs() {
  const { kids, roots } = tree(S.jobs);
  $("#jobs").innerHTML = S.jobs.length ? nodes(roots, kids, j =>
    `<div class="it ${S.cur?.job_id === j.job_id ? "on" : ""}" data-id="${j.job_id}" tabindex="0"><span class="dot ${esc(j.status)}"></span><span class="t">${esc(j.title)}</span></div>`)
    : `<li class="hint">${S.q ? "No matches." : "No research yet."}</li>`;
  $("#more").hidden = !S.cursor;
}
 
function closeNav() { document.body.classList.remove("nav"); }
$("#jobs").addEventListener("click", e => { const li = e.target.closest(".it[data-id]"); if (li) { location.hash = "#/job/" + li.dataset.id; closeNav(); } });
$("#jobs").addEventListener("keydown", e => { if (e.key === "Enter") e.target.click(); });
$("#more").onclick = () => loadJobs(false);
$("#newBtn").onclick = () => { closeNav(); if (parseHash().path) location.hash = "#/"; else showNew(); };
$("#menu").onclick = () => document.body.classList.toggle("nav");
$("#scrim").onclick = closeNav;
$("#outBtn").onclick = logout;
$("#setBtn").onclick = () => openSettings(false);
let st; $("#search").oninput = e => { clearTimeout(st); st = setTimeout(() => { S.q = e.target.value.trim(); loadJobs(true); }, 300); };
 
/* ───────── new research ───────── */
function showNew() {
  stopPoll(); S.cur = null; renderJobs();
  $("#view").innerHTML = `<div class="wrap"><h2>What do you want to research?</h2>
    <p class="hint">The agent plans search queries first. You can edit them before it starts.</p>
    <form id="nf"><label for="nq" class="sr" style="margin-top:0">Research question</label>
    <textarea id="nq" minlength="3" maxlength="1000" required placeholder="e.g. How do solid-state batteries compare with lithium-ion for electric cars?"></textarea>
    <label class="check"><input type="checkbox" id="na"> Start right away, skip the review step</label>
    <p class="err" id="ne" role="alert"></p><button class="btn primary" id="nb">Plan research</button></form></div>`;
  $("#nf").onsubmit = async e => {
    e.preventDefault(); $("#nb").disabled = true; $("#nb").textContent = "Planning…"; $("#ne").textContent = "";
    try { await createJob($("#nq").value, $("#na").checked); }
    catch (x) { $("#ne").textContent = x.message; $("#nb").disabled = false; $("#nb").textContent = "Plan research"; }
  };
}
 
async function createJob(query, auto, parent) {
  try {
    const j = await api("/research", { method: "POST", body: { query, auto_start: !!auto, parent_job_id: parent || null } });
    S.jobs.unshift(summ(j)); location.hash = "#/job/" + j.job_id; return j;
  } catch (x) { if (x.status === 400 && /LLM/i.test(x.message)) openSettings(true); throw x; }
}
const summ = j => ({ job_id: j.job_id, title: j.title, status: j.status, stage: j.stage, parent_job_id: j.parent_job_id });
 
/* ───────── job view ───────── */
function stopPoll() { clearInterval(S.poll); S.poll = null; }
 
async function openJob(id) {
  stopPoll(); closeNav();
  try { S.cur = await api("/research/" + encodeURIComponent(id)); }
  catch (x) { toast(x.message); return x.status === 404 ? (location.hash = "#/") : null; }
  renderJobs(); renderJob();
  if (ACTIVE.includes(S.cur.status)) S.poll = setInterval(pollJob, 3000);
  if (S.cur.status === "completed") loadMessages();
}
 
async function pollJob() {
  const id = S.cur?.job_id; if (!id) return stopPoll();
  try {
    const j = await api("/research/" + id);
    if (S.cur?.job_id !== id) return;
    const changed = j.status !== S.cur.status || j.stage !== S.cur.stage;
    S.cur = j;
    const i = S.jobs.findIndex(x => x.job_id === id); if (i >= 0) S.jobs[i] = summ(j);
    if (changed) { renderJobs(); renderJob(); }
    if (!ACTIVE.includes(j.status)) { stopPoll(); if (j.status === "completed") loadMessages(); }
  } catch (x) { if (x.status === 404) stopPoll(); }
}
 
function threadHtml(j) {
  const jobs = S.jobs.some(x => x.job_id === j.job_id) ? S.jobs : [summ(j), ...S.jobs];
  const byId = Object.fromEntries(jobs.map(x => [x.job_id, x]));
  let root = byId[j.job_id];
  for (let i = 0; i < 30 && root.parent_job_id && byId[root.parent_job_id]; i++) root = byId[root.parent_job_id];
  const { kids } = tree(jobs);
  if (!kids[root.job_id]) return "";
  return `<details class="panel" open><summary><strong>Research thread</strong> <span class="hint">(linked follow-ups)</span></summary>
    <ul class="thread">${nodes([root], kids, x => `<a class="tl ${x.job_id === j.job_id ? "on" : ""}" href="#/job/${x.job_id}"><span class="dot ${esc(x.status)}"></span><span>${esc(x.title)}</span></a>`)}</ul></details>`;
}
 
function renderJob() {
  const j = S.cur, active = ACTIVE.includes(j.status), planning = !DONE.includes(j.status) && !active;
  let h = `<div class="wrap"><h2>${esc(j.title)}</h2>
    <div class="row"><span class="badge">${esc(j.status)}${active && j.stage ? ` (${esc(j.stage)})` : ""}</span>
    <span class="grow"></span>
    ${active ? `<button class="btn" data-a="cancel">Cancel</button>` : ""}
    ${j.status === "completed" ? `<button class="btn" data-a="export">Download .md</button>` : ""}
    <button class="btn" data-a="rename">Rename</button><button class="btn danger" data-a="delete">Delete</button></div>
    <p class="hint">Question: ${esc(j.query)}</p>
    ${threadHtml(j) || (j.parent_job_id ? `<p class="hint"><a href="#/job/${esc(j.parent_job_id)}">Follow-up of earlier research</a></p>` : "")}`;
  if (planning) {
    h += `<div class="panel"><h3>Review the search queries</h3><p class="hint">Edit, remove or add queries, then start.</p><div id="qs">
      ${(j.queries || []).map(q => qrow(q)).join("")}</div>
      <div class="row"><button class="btn" data-a="addq">Add query</button><button class="btn primary" data-a="start">Start research</button></div>
      <p class="err" id="je" role="alert"></p></div>`;
  } else if (active) {
    h += `<div class="panel"><h3>Researching…</h3><p class="hint">This page updates by itself. You can leave and come back; the job keeps running.</p>
      ${(j.queries || []).length ? `<p class="hint">Queries: ${j.queries.map(esc).join("; ")}</p>` : ""}</div>`;
  } else if (j.status === "failed" || j.status === "cancelled") {
    h += `<div class="panel"><h3>${j.status === "failed" ? "Research failed" : "Research cancelled"}</h3>
      ${j.error ? `<p class="err">${esc(j.error)}</p>` : ""}<button class="btn primary" data-a="retry">Start over with this question</button>
      <p class="err" id="je" role="alert"></p></div>`;
  } else {
    h += `<details class="panel"><summary><strong>Search queries used (${(j.queries || []).length})</strong></summary>
      <ol class="sources" style="margin-top:10px">${(j.queries || []).map(q => `<li>${esc(q)}</li>`).join("")}</ol></details>
      <div class="panel report md">${md(j.report)}</div>
      <div class="panel"><h3>Sources (${(j.sources || []).length})</h3><ol class="sources">
      ${(j.sources || []).map(s => `<li><a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>${s.snippet ? `<small>${esc(s.snippet)}</small>` : ""}</li>`).join("")}</ol></div>
      <div class="panel"><h3>Ask a follow-up</h3><p class="hint">Answers come from this report only, with no new web search.</p>
      <div class="chat" id="chat"></div>
      <textarea id="fq" maxlength="2000" placeholder="Ask about the report…" style="min-height:70px"></textarea>
      <p class="err" id="fe" role="alert"></p><button class="btn primary" data-a="ask">Send</button></div>
      <div class="panel"><h3>Research deeper</h3>
      <p class="hint">Start a new research linked to this one. It appears under this report in your list.</p>
      <textarea id="dq" maxlength="1000" placeholder="What should the next research look into?" style="min-height:70px"></textarea>
      <label class="check"><input type="checkbox" id="da"> Start right away, skip the review step</label>
      <p class="err" id="de" role="alert"></p><button class="btn primary" data-a="child">Plan linked research</button></div>
      <p class="hint">Finished ${esc(when(j.completed_at))}</p>`;
  }
  $("#view").innerHTML = h + "</div>"; $("#view").scrollTop = 0;
}
const qrow = q => `<div class="qrow"><input class="qin" value="${esc(q)}" maxlength="300" aria-label="Search query"><button class="btn" data-a="delq" aria-label="Remove query">✕</button></div>`;
 
$("#view").addEventListener("click", async e => {
  const b = e.target.closest("[data-a]"); if (!b) return;
  const a = b.dataset.a, j = S.cur, jerr = m => { const el = $("#je"); if (el) el.textContent = m; else toast(m); };
  try {
    if (a === "delq") b.parentElement.remove();
    else if (a === "addq") { $("#qs").insertAdjacentHTML("beforeend", qrow("")); $("#qs").lastElementChild.querySelector("input").focus(); }
    else if (a === "start") {
      const queries = [...document.querySelectorAll(".qin")].map(i => i.value.trim()).filter(Boolean);
      if (!queries.length) return jerr("Keep at least one search query.");
      b.disabled = true; $("#je").textContent = "";
      try { S.cur = await api(`/research/${j.job_id}/start`, { method: "POST", body: { queries } }); }
      catch (x) { b.disabled = false; if (x.status === 400) openSettings(true); throw x; }
      syncSide(); renderJob(); S.poll = setInterval(pollJob, 3000);
    }
    else if (a === "cancel") { S.cur = await api(`/research/${j.job_id}/cancel`, { method: "POST" }); syncSide(); renderJob(); toast("Cancelling… it stops at the next checkpoint."); }
    else if (a === "retry") { b.disabled = true; try { await createJob(j.query, false, j.parent_job_id); } catch (x) { b.disabled = false; throw x; } }
    else if (a === "rename") {
      const t = prompt("New title", j.title); if (!t || !t.trim()) return;
      S.cur = await api("/research/" + j.job_id, { method: "PATCH", body: { title: t } }); syncSide(); renderJobs(); renderJob();
    }
    else if (a === "delete") {
      if (!confirm("Delete this research and its follow-up chat? This can't be undone.")) return;
      await api("/research/" + j.job_id, { method: "DELETE" });
      S.jobs = S.jobs.filter(x => x.job_id !== j.job_id); stopPoll(); S.cur = null; location.hash = "#/"; renderJobs(); toast("Deleted.");
    }
    else if (a === "export") {
      const blob = await api(`/research/${j.job_id}/export`, { blob: true });
      const u = URL.createObjectURL(blob), l = document.createElement("a");
      l.href = u; l.download = (j.title.replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-").slice(0, 60) || "report") + ".md";
      document.body.append(l); l.click(); l.remove(); setTimeout(() => URL.revokeObjectURL(u), 1000);
    }
    else if (a === "ask") await ask(b);
    else if (a === "child") {
      const q = $("#dq").value.trim(); if (q.length < 3) { $("#de").textContent = "Write at least 3 characters."; return; }
      b.disabled = true; $("#de").textContent = "";
      try { await createJob(q, $("#da").checked, j.job_id); } catch (x) { b.disabled = false; $("#de").textContent = x.message; }
    }
    else if (a === "deepen") {
      b.disabled = true; try { await createJob(b.dataset.q, false, j.job_id); } catch (x) { b.disabled = false; throw x; }
    }
  } catch (x) { jerr(x.message); }
});
 
function syncSide() { const j = S.cur, i = S.jobs.findIndex(x => x.job_id === j.job_id); if (i >= 0) S.jobs[i] = summ(j); renderJobs(); }
 
/* ───────── follow-up chat ───────── */
const bubble = m => `<div class="msg ${esc(m.role)}${m.role === "assistant" ? " md" : ""}">${m.role === "assistant" ? md(m.content) : esc(m.content)}${m.suggested_query ? `<div style="margin-top:8px"><button class="btn" data-a="deepen" data-q="${esc(m.suggested_query)}">Research this further</button><div class="hint">${esc(m.suggested_query)}</div></div>` : ""}</div>`;
 
async function loadMessages() {
  const id = S.cur?.job_id;
  try {
    const ms = await api(`/research/${id}/messages`);
    if (S.cur?.job_id === id && $("#chat")) $("#chat").innerHTML = ms.map(bubble).join("");
  } catch {}
}
 
async function ask(btn) {
  const ta = $("#fq"), q = ta.value.trim(), chat = $("#chat");
  if (!q) return;
  btn.disabled = true; $("#fe").textContent = "";
  chat.insertAdjacentHTML("beforeend", bubble({ role: "user", content: q }) + `<div class="msg assistant" id="think">Thinking…</div>`);
  ta.value = "";
  try {
    const r = await api(`/research/${S.cur.job_id}/messages`, { method: "POST", body: { question: q } });
    $("#think").outerHTML = bubble(r.assistant_message);
  } catch (x) {
    $("#think")?.remove(); ta.value = q; $("#fe").textContent = x.message;
    if (x.status === 400) openSettings(true);
  } finally { btn.disabled = false; }
}
 
/* ───────── LLM settings ───────── */
async function openSettings(firstTime) {
  const dlg = $("#dlg");
  let providers = {}, cur = null;
  try { providers = await api("/settings/llm/providers"); } catch (x) { toast(x.message); }
  try { cur = await api("/settings/llm"); } catch (x) { if (x.status !== 404) toast(x.message); }
  const names = Object.keys(providers);
  if (cur && !names.includes(cur.provider)) names.push(cur.provider);
  const same = cur && cur.planner_model === cur.researcher_model && cur.researcher_model === cur.synthesizer_model;
  dlg.innerHTML = `<div id="sf"><h2 style="margin:0 0 4px">LLM settings</h2>
    <p class="hint">${firstTime && !cur ? "Add your provider and API key to run research. " : ""}Your key is stored encrypted and never shown again.</p>
    <label for="sp">Provider</label><select id="sp">${names.map(n => `<option ${cur?.provider === n ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
    <label for="sk">API key</label><input id="sk" type="password" autocomplete="off" placeholder="${cur ? "Saved: " + esc(cur.api_key_hint) + " (leave blank to keep)" : "Paste your API key"}">
    <label for="sm">Model</label><input id="sm" list="ml" value="${esc(same ? cur.planner_model : "")}" placeholder="Used for every stage"><datalist id="ml"></datalist>
    <details ${cur && !same ? "open" : ""}><summary class="hint" style="cursor:pointer;margin-top:12px">Use different models per stage</summary>
      ${[["planner_model", "Planner"], ["researcher_model", "Researcher"], ["synthesizer_model", "Synthesizer"]].map(([k, l]) =>
        `<label for="s_${k}">${l}</label><input id="s_${k}" list="ml" value="${esc(cur && !same ? cur[k] : "")}">`).join("")}</details>
    <p class="err" id="se" role="alert"></p><div id="sr"></div>
    <div class="row" style="margin-top:16px"><button type="button" class="btn primary" id="ss">Save</button>
    <button type="button" class="btn" id="st" ${cur ? "" : "disabled"}>Test</button>
    ${cur ? `<button type="button" class="btn danger" id="sd">Remove</button>` : ""}<span class="grow"></span>
    <button type="button" class="btn ghost" id="sc">Close</button></div></div>`;
  const fillList = () => { $("#ml").innerHTML = (providers[$("#sp").value] || []).map(m => `<option value="${esc(m)}">`).join(""); };
  fillList(); $("#sp").onchange = fillList;
  $("#sc").onclick = () => dlg.close();
  const err = m => { $("#se").textContent = m; };
  $("#ss").onclick = async () => {
    err(""); const body = { provider: $("#sp").value };
    const key = $("#sk").value.trim(); if (key) body.api_key = key;
    for (const [k, id] of [["model", "sm"], ["planner_model", "s_planner_model"], ["researcher_model", "s_researcher_model"], ["synthesizer_model", "s_synthesizer_model"]]) {
      const v = $("#" + id).value.trim(); if (v) body[k] = v;
    }
    $("#ss").disabled = true;
    try { await api("/settings/llm", { method: "PUT", body }); S.user.has_llm_settings = true; dlg.close(); toast("Settings saved."); }
    catch (x) { err(x.message); } finally { $("#ss").disabled = false; }
  };
  $("#st").onclick = async () => {
    $("#st").disabled = true; $("#sr").innerHTML = `<p class="hint">Testing each stage…</p>`;
    try {
      const r = await api("/settings/llm/test", { method: "POST" });
      $("#sr").innerHTML = r.results.map(x => `<p class="${x.ok ? "hint" : "err"}">${esc(x.stage)} (${esc(x.model)}): ${x.ok ? "works" : esc(x.error || "failed")}</p>`).join("");
    } catch (x) { $("#sr").innerHTML = ""; err(x.message); } finally { $("#st").disabled = false; }
  };
  if (cur) $("#sd").onclick = async () => {
    if (!confirm("Remove your stored provider, models and API key?")) return;
    try { await api("/settings/llm", { method: "DELETE" }); S.user.has_llm_settings = false; dlg.close(); toast("Settings removed."); } catch (x) { err(x.message); }
  };
  if (!dlg.open) dlg.showModal();
}
 
route();
})();