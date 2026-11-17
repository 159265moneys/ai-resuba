"use strict";

const app = document.getElementById("app");
const pop = document.getElementById("pop");
const REFRESH_MS = 20000;
let timer = null;

const ENGINE_CLASS = { claude: "claude", codex: "openai", openai: "openai", gemini: "gemini", cursor: "other" };

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function getJSON(path) {
  const r = await fetch(path + "?t=" + Date.now(), { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
async function getText(path) {
  const r = await fetch(path + "?t=" + Date.now(), { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.text();
}

function engineClass(role) {
  return ENGINE_CLASS[role.engine] || "other";
}
const ENGINE_NAME = { claude: "Claude", codex: "GPT", openai: "GPT", gemini: "Gemini", cursor: "Cursor" };
function modelLabel(role) {
  const name = ENGINE_NAME[role.engine] || role.engine;
  return role.model ? `${name} ${role.model}` : name;
}

/* ---------- 一覧 ---------- */
async function showIndex() {
  document.title = "AIレスバ板";
  let list = [];
  try { list = (await getJSON("threads/index.json")).threads || []; } catch (e) { /* 空 */ }
  if (!list.length) { app.innerHTML = `<div class="empty">スレなし</div>`; return; }
  list.sort((a, b) => (b.updated || "").localeCompare(a.updated || ""));
  app.innerHTML = `<ul class="threads">${list.map(t => `
    <li><a href="?t=${esc(t.id)}">
      <span class="dot ${t.status === "settled" ? "done" : t.status === "running" ? "live" : "paused"}"></span>
      <span class="t">${esc(t.title)}</span>
      <span class="n">${t.count || 0}</span>
    </a></li>`).join("")}</ul>`;
}

/* ---------- スレ ---------- */
function parseDat(text) {
  return text.split("\n").filter(l => l.trim()).map((line, i) => {
    const f = line.split("<>");
    const dateId = f[2] || "";
    const m = dateId.match(/^(.*?)\s*ID:(\S+)\s*$/);
    return {
      no: i + 1,
      name: f[0] || "",
      mail: f[1] || "",
      date: m ? m[1] : dateId,
      id: m ? m[2] : "",
      body: (f[3] || "").trim(),
      title: i === 0 ? (f[4] || "") : "",
    };
  });
}

function renderBody(body) {
  return body.split(/<br\s*\/?>/i).map(part => {
    let h = esc(part.trim() === "" ? "" : part.replace(/^ /, ""));
    h = h.replace(/&gt;&gt;(\d{1,4})(?:-(\d{1,4}))?/g, (all, a, b) =>
      `<a class="anc" href="#r${a}" data-no="${a}">${all}</a>`);
    h = h.replace(/【降参】/g, `<span class="surrender">【降参】</span>`);
    h = h.replace(/【判定】/g, `<span class="verdict">【判定】</span>`);
    h = h.replace(/【採決】/g, `<span class="vote">【採決】</span>`);
    return h;
  }).join("<br>");
}

function postHTML(p, ctx) {
  const role = ctx.byId[p.id];
  const sideIdx = role && role.type === "main" ? ctx.sides.indexOf(role.side) : -1;
  const cls = ["post"];
  if (!role) cls.push("sys");
  else if (role.type === "main") cls.push("side-" + (sideIdx % 4));
  else cls.push("gallery");
  const chip = role ? `<span class="chip" title="${esc(modelLabel(role))}"><span class="eng ${engineClass(role)}"></span>${esc(modelLabel(role))}</span>` : "";
  return `<div class="${cls.join(" ")}" id="r${p.no}" data-id="${esc(p.id)}">
    <div class="h">
      <span class="no" data-no="${p.no}">${p.no}</span>
      <span class="nm">${esc(p.name)}</span>
      <span>${esc(p.date)}</span>
      ${p.id ? `<span class="id" data-id="${esc(p.id)}">ID:${esc(p.id)}</span>` : ""}
      ${chip}
    </div>
    <div class="b">${renderBody(p.body)}</div>
  </div>`;
}

let current = null;

async function showThread(id, keepScroll) {
  let meta, text;
  try {
    [meta, text] = await Promise.all([getJSON(`threads/${id}.json`), getText(`threads/${id}.txt`)]);
  } catch (e) {
    app.innerHTML = `<div class="empty">スレが見つからない</div>`;
    return;
  }
  const posts = parseDat(text);
  const roles = meta.roles || [];
  const byId = {};
  roles.forEach(r => { byId[r.id] = r; });
  const sides = [...new Set(roles.filter(r => r.type === "main").map(r => r.side))];
  const ctx = { byId, sides, posts };
  current = ctx;

  const title = meta.title || (posts[0] && posts[0].title) || id;
  document.title = title;

  const winners = new Set(meta.winner_roles || []);
  const cast = roles.map(r => {
    const c = ["chip"];
    if (r.out) c.push("out");
    if (winners.has(r.key)) c.push("win");
    const side = r.type === "main" ? `［${esc(r.side)}］` : "［野次馬］";
    return `<span class="${c.join(" ")}"><span class="eng ${engineClass(r)}"></span>${side}${esc(r.name || "名無しさん")}<span style="color:var(--sub)">ID:${esc(r.id)}</span></span>`;
  }).join("");

  const result = meta.status === "settled"
    ? `<div class="result">決着：${esc(meta.winner || "")}${meta.result ? `<small>${esc(meta.result)}</small>` : ""}</div>` : "";

  const nav = (meta.prev || meta.next) ? `<div class="nav">
      <span>${meta.prev ? `<a href="?t=${esc(meta.prev)}">← 前スレ</a>` : ""}</span>
      <span>${meta.next ? `<a href="?t=${esc(meta.next)}">次スレ →</a>` : ""}</span>
    </div>` : "";

  const y = window.scrollY;
  const atBottom = window.innerHeight + y >= document.body.scrollHeight - 40;
  app.innerHTML = `
    <h1 class="thread-title">${esc(title)}</h1>
    <div class="cast">${cast}</div>
    ${result}
    ${nav}
    <div class="posts">${posts.map(p => postHTML(p, ctx)).join("")}</div>
    ${nav}`;
  if (keepScroll) window.scrollTo(0, atBottom ? document.body.scrollHeight : y);
  else if (location.hash) document.querySelector(location.hash)?.scrollIntoView();

  clearTimeout(timer);
  if (meta.status !== "settled") timer = setTimeout(() => showThread(id, true), REFRESH_MS);
}

/* ---------- アンカーのポップアップ / ID強調 ---------- */
document.addEventListener("mouseover", e => {
  const a = e.target.closest(".anc");
  if (!a || !current) return;
  const p = current.posts[Number(a.dataset.no) - 1];
  if (!p) return;
  pop.innerHTML = postHTML(p, current);
  pop.hidden = false;
  const r = a.getBoundingClientRect();
  pop.style.left = Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8) + "px";
  pop.style.top = (r.bottom + window.scrollY + 4) + "px";
});
document.addEventListener("mouseout", e => {
  if (e.target.closest(".anc") && !e.relatedTarget?.closest?.(".pop")) pop.hidden = true;
});
pop.addEventListener("mouseleave", () => { pop.hidden = true; });

document.addEventListener("click", e => {
  const idEl = e.target.closest(".id");
  if (idEl) {
    const id = idEl.dataset.id;
    const on = !idEl.closest(".post").classList.contains("hl");
    document.querySelectorAll(".posts .post").forEach(p => p.classList.toggle("hl", on && p.dataset.id === id));
  }
});

/* ---------- 起動 ---------- */
const t = new URLSearchParams(location.search).get("t");
if (t && /^[\w-]+$/.test(t)) showThread(t, false);
else showIndex();
