"use strict";
/* AgentChat 화면. 서버와는 WebSocket(/ws) + 몇 개의 REST(/api/*)로만 통신한다. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 저장 불가 환경 */ } },
};

const S = {
  ws: null, config: null, conversations: [], recent: [],
  conv: null,               // 열린 대화 id
  messages: [], typing: {}, running: false, round: 0, pending: 0, summary: null,
  mode: store.get("mode", "review"),
  attachments: [],          // {name, path, uploading}
  filesOpen: store.get("filesOpen", false),
  previewPath: null,
  settings: null,           // 설정 창 상태
  avatarJobs: {},           // key -> generating
  limits: {},               // {claude:{five_hour:{pct,resets_at},seven_day:{...},updated}, codex:{...}}
  models: null,             // /api/models 결과(처음 필요할 때 받음)
  theme: store.get("theme", "auto"),
  fpTab: store.get("fpTab", "artifacts"),
  showArchived: false,      // 사이드바에 보관함을 보여주는 중인지
  typingTokens: {},         // 답하는 중인 담당자별 지금까지 토큰(실시간)
  slash: [],                // CLI 가 알려준 / 명령 목록(첫 Claude 답변 후 채워짐)
  files: [],
  local: true,              // PC 에서 직접 연 화면인지(폰=false → PC 전용 버튼 숨김)
  prevRunning: {},          // 다른 대화가 끝났을 때 화면 안에서 알리려고
  contexts: {},             // 담당자별 세션 컨텍스트 {used, window, updated}
  hold: null,               // 보내기 직전 잠깐 붙잡아 둔 메시지 {conv, text, mode, files, atts, timer, left}
  sendDelay: store.get("sendDelay", 4),  // 보내기 전 Esc 로 되돌릴 수 있는 시간(초). 0 = 바로 보냄
};

const narrow = () => window.matchMedia("(max-width: 860px)").matches;

const ICON = {
  file: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  clip: '<svg viewBox="0 0 24 24" width="13" height="13"><path d="M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/></svg>',
  gear: '<svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>',
  close: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
};

// ────────────────────────────────────────────────────────── 공통 UI
function toast(msg, ms = 2600, action) {
  const t = $("toast");
  t.textContent = msg;
  if (action) {
    const b = el("button", "toast-act", esc(action.label));
    b.onclick = () => { t.classList.remove("show"); action.fn(); };
    t.appendChild(b);
    ms = Math.max(ms, 5000);
  }
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401 && !S.local) { location.reload(); throw new Error("로그인이 필요합니다"); }  // 폰: 로그인 만료 → 로그인 화면
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `요청 실패 (${r.status})`);
  return d;
}

const initial = (name) => (name || "?").trim().charAt(0).toUpperCase();
function avatarEl(p) {
  const d = el("div", "avatar");
  d.style.setProperty("--c", p?.color || "#8e8e93");
  d.title = p?.title ? `${p.name} · ${p.title}` : (p?.name || "");
  if (p?.avatar) {
    const img = new Image();
    img.src = p.avatar; img.alt = p.name || "";
    img.onerror = () => { img.remove(); d.textContent = initial(p.name); };
    d.appendChild(img);
  } else d.textContent = initial(p?.name);
  return d;
}
const agent = (k) => S.config?.agents?.[k] || { name: k, title: "", color: "#8e8e93" };
const order = () => [S.config.worker, ...S.config.reviewers];
const userName = () => S.config?.user?.name || "나";

function fmtTime(ts, withDate) {
  const d = new Date(ts * 1000), now = new Date();
  const same = d.toDateString() === now.toDateString();
  const yest = new Date(now - 864e5).toDateString() === d.toDateString();
  const hm = d.toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit" });
  if (!withDate) return same ? hm : yest ? "어제" : `${d.getMonth() + 1}. ${d.getDate()}.`;
  const day = same ? "오늘" : yest ? "어제" : d.toLocaleDateString("ko-KR", { month: "long", day: "numeric", weekday: "short" });
  return `${day} ${hm}`;
}
const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;

// 간단 마크다운: 코드블록, 인라인 코드, 굵게, 제목, 목록
function md(text) {
  const parts = String(text ?? "").split(/```/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2) { html += `<pre><code>${esc(part.replace(/^[\w+.-]*\n/, "").replace(/\n$/, ""))}</code></pre>`; return; }
    const inline = (s) => esc(s)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    let list = null, para = [], table = null;
    const flushPara = () => { if (para.length) { html += `<p>${para.map(inline).join("<br>")}</p>`; para = []; } };
    const flushList = () => { if (list) { html += `<${list.t}>${list.items.map((x) => `<li>${inline(x)}</li>`).join("")}</${list.t}>`; list = null; } };
    const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const flushTable = () => {
      if (!table) return;
      const [head, ...rows] = table;
      const num = (c) => /^-?[\d,.]+%?$/.test(c.replace(/`/g, ""));
      html += `<table class="md-table"><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
        rows.map((r) => `<tr>${r.map((c) => `<td class="${num(c) ? "num" : ""}">${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
      table = null;
    };
    part.split("\n").forEach((line) => {
      if (/^\s*\|.*\|\s*$/.test(line)) {  // 마크다운 표
        flushPara(); flushList();
        if (/^\s*\|[\s:|-]+\|\s*$/.test(line)) return;  // 구분선 |---|---|
        (table ||= []).push(cells(line));
        return;
      }
      flushTable();
      const ul = line.match(/^\s*[-*•]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/), h = line.match(/^#{1,4}\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const t = ul ? "ul" : "ol";
        if (!list || list.t !== t) { flushList(); list = { t, items: [] }; }
        list.items.push((ul || ol)[1]);
      } else if (h) { flushPara(); flushList(); html += `<p><span class="h">${inline(h[1])}</span></p>`; }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    });
    flushPara(); flushList(); flushTable();
  });
  return html || "<p></p>";
}

// ────────────────────────────────────────────────────────── 사이드바
function renderSidebar() {
  const box = $("convList");
  const q = $("search").value.trim().toLowerCase();
  box.innerHTML = "";
  const archivedN = S.conversations.filter((c) => c.archived).length;
  if (S.showArchived) {
    const back = el("div", "archive-head", `<button class="archive-link">‹ 대화 목록</button><span>보관함 ${archivedN}</span>`);
    back.querySelector("button").onclick = () => { S.showArchived = false; renderSidebar(); };
    box.appendChild(back);
  }
  const list = S.conversations.filter((c) => !!c.archived === S.showArchived)
    .filter((c) => !q || (c.title + " " + c.last + " " + c.workspace).toLowerCase().includes(q));
  if (!list.length) {
    box.appendChild(el("div", "side-empty", q ? "검색 결과가 없습니다" : S.showArchived ? "보관한 대화가 없습니다." : "대화가 없습니다.<br>오른쪽 위 ✎ 로 새 대화를 시작하세요."));
  }
  list.forEach((c) => {
    const row = el("div", "conv" + (c.id === S.conv ? " active" : ""));
    const ava = el("div", "conv-ava");
    order().forEach((k) => ava.appendChild(avatarEl(agent(k))));
    const body = el("div", "conv-body");
    body.innerHTML = `<div class="conv-row1"><div class="conv-title">${esc(c.title)}</div>
      <div class="conv-flags">${c.approvals ? `<span class="badge-n" title="승인 대기">${c.approvals}</span>` : ""}${c.running ? '<span class="dot-run" title="진행 중"></span>' : ""}</div>
      <div class="conv-time">${fmtTime(c.updated)}</div></div><div class="conv-last">${esc((c.last || "메시지 없음").replace(/[*`#>]/g, ""))}</div>`;
    const more = el("button", "conv-more", "⋯");
    more.title = "대화 메뉴";
    more.onclick = (e) => { e.stopPropagation(); convMenu(c, more); };
    row.append(ava, body, more);
    row.onclick = () => openConv(c.id);
    box.appendChild(row);
  });
  if (!S.showArchived && archivedN) {
    const link = el("button", "archive-link bottom", `보관함 ${archivedN}`);
    link.onclick = () => { S.showArchived = true; renderSidebar(); };
    box.appendChild(link);
  }
}

function convMenu(c, anchor) {
  document.querySelector(".ctx-menu")?.remove();
  const m = el("div", "ctx-menu");
  const item = (label, fn, cls) => { const it = el("div", "menu-item" + (cls ? " " + cls : ""), `<div class="mi-title">${label}</div>`); it.onclick = () => { m.remove(); fn(); }; m.appendChild(it); };
  if (c.archived) item("다시 꺼내기", () => archiveConv(c.id, false));
  else item("나가기 (보관)", () => archiveConv(c.id, true));
  (S.config.reviewers || []).forEach((k) => {
    const out = (c.excluded || []).includes(k);
    const a = agent(k);
    const who = a.engine === "codex" ? `${a.name}(Codex·OpenAI)` : a.name;
    item(out ? `${esc(who)} 다시 넣기` : `${esc(who)} 빼기 — 이 대화 자료를 보내지 않음`, () => setExcluded(c.id, k, !out));
  });
  const r = anchor.getBoundingClientRect();
  m.style.top = `${r.bottom + 4}px`;
  m.style.left = `${Math.min(r.left - 150, window.innerWidth - 220)}px`;
  document.body.appendChild(m);
  setTimeout(() => document.addEventListener("mousedown", function close(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", close); } }), 0);
}

async function setExcluded(id, key, excluded) {
  try {
    const d = await api("POST", `/api/conv/${id}/exclude`, { agent: key, excluded });
    const c = S.conversations.find((x) => x.id === id);
    if (c) c.excluded = d.summary.excluded;
    if (S.conv === id) { S.summary = { ...S.summary, excluded: d.summary.excluded }; renderHeader(); }
    renderSidebar();
  } catch (e) { toast(e.message); }
}

async function archiveConv(id, archived) {
  const c = S.conversations.find((x) => x.id === id);
  if (archived && c?.running) toast("진행 중인 작업을 멈추고 보관합니다…");
  try { await api("POST", `/api/conv/${id}/archive`, { archived }); }
  catch (e) { toast(e.message); return; }
  if (c) c.archived = archived;
  if (archived && S.conv === id) {  // 보고 있던 대화에서 나가면 다음 대화로
    const next = S.conversations.find((x) => !x.archived && x.id !== id);
    if (next) openConv(next.id, true);
    else { S.conv = null; S.summary = null; S.messages = []; store.set("conv", null); renderAll(); }
  } else if (S.conv === id) { S.summary = { ...S.summary, archived }; renderAll(); }
  renderSidebar();
  const title = c?.title || "대화";
  if (archived) toast(`"${title.slice(0, 20)}" 대화를 보관함으로 옮겼습니다`, 5000, { label: "되돌리기", fn: () => archiveConv(id, false) });
  else toast(`"${title.slice(0, 20)}" 대화를 다시 꺼냈습니다`);
}

// ────────────────────────────────────────────────────────── 헤더
function renderHeader() {
  const mem = $("members");
  mem.innerHTML = "";
  order().forEach((k) => mem.appendChild(avatarEl(agent(k))));
  const s = S.summary;
  $("chatTitle").textContent = s ? s.title : S.config.room_name;
  const sub = $("chatSub");
  sub.innerHTML = "";
  const who = el("span", "who", order().map((k) => `${esc(agent(k).name)}(${esc(agent(k).title)})<span class="model-tag">${esc(modelLabel(k, true))}</span>`).join(" · "));
  who.title = "위 사진을 누르면 담당자별 모델을 바꿀 수 있습니다";
  sub.appendChild(who);
  if (s) {
    const ws = el("span", "tag link", `📁 ${esc(s.workspace)}`);
    ws.title = "파일 패널 열기";
    ws.onclick = () => toggleFiles(true);
    sub.appendChild(ws);
    const ctx = order().filter((k) => S.contexts[k]?.used);
    if (ctx.length) {
      const chip = el("span", "tag link ctx-chip", "컨텍스트 " + ctx.map((k) => {
        const p = ctxPct(S.contexts[k]);
        return `<span class="${ctxLevel(p)}">${esc(agent(k).name)} ${p == null ? fmtTok(S.contexts[k].used) : p + "%"}</span>`;
      }).join(" · "));
      chip.title = "담당자별 세션이 들고 있는 대화 분량(컨텍스트). 눌러서 자세히";
      chip.onclick = openContextModal;
      sub.appendChild(chip);
    }
    if (s.safe_mode) sub.appendChild(el("span", "tag warn", "전역 규칙 OFF"));
    (s.excluded || []).forEach((k) => sub.appendChild(el("span", "tag warn", `${esc(agent(k).name)} 제외`)));
  }
  $("btnStop").hidden = !S.running;
  $("btnLeave").hidden = !S.conv;
  $("btnLeave").title = s?.archived ? "보관함에서 다시 꺼내기" : "대화 나가기 (보관함으로 옮김, 기록·파일은 그대로)";
  $("btnFiles").classList.toggle("active", S.filesOpen);
  document.title = (S.running ? "● " : "") + (s ? s.title : S.config.room_name);
}

// ────────────────────────────────────────────────────────── 메시지
const GROUP_GAP = 600;
function sameGroup(a, b) {
  return a && b && a.sender === b.sender && b.sender !== "system" && a.kind !== "approval" && b.kind !== "approval" && b.ts - a.ts < GROUP_GAP;
}

function renderMessage(msg, prev) {
  const frag = document.createDocumentFragment();
  if (!prev || msg.ts - prev.ts > GROUP_GAP) frag.appendChild(el("div", "time-sep", esc(fmtTime(msg.ts, true))));

  if (msg.sender === "system") {
    const cls = msg.done ? " done" : msg.escalation ? " escalation" : "";
    const s = el("div", "sys" + cls);
    s.dataset.id = msg.id;
    s.appendChild(el("span", "", esc(msg.done ? "✓ " + msg.text : msg.text)));
    frag.appendChild(s);
    return frag;
  }
  const isUser = msg.sender === "user";
  const grouped = sameGroup(prev, msg);
  const row = el("div", `row ${isUser ? "out" : "in"}${grouped ? "" : " gap first"} last`);
  row.dataset.id = msg.id;
  row.dataset.sender = msg.sender;
  row.title = new Date(msg.ts * 1000).toLocaleString("ko-KR");

  if (!isUser) {
    const slot = el("div", "avatar-slot");
    if (!grouped) slot.appendChild(avatarEl(agent(msg.sender)));
    row.appendChild(slot);
  }
  const col = el("div", "col");
  if (!isUser && !grouped) {
    const a = agent(msg.sender);
    col.appendChild(el("div", "sender", `<b>${esc(a.name)}</b><span>${esc(a.title)}</span>` +
      (msg.verdict ? `<span class="badge ${esc(msg.verdict)}">${msg.verdict === "APPROVE" ? "승인" : "수정 요청"}</span>` : "") +
      (msg.round ? `<span class="round">라운드 ${msg.round}</span>` : "")));
  } else if (!isUser && msg.verdict) {
    col.appendChild(el("div", "sender", `<span class="badge ${esc(msg.verdict)}">${msg.verdict === "APPROVE" ? "승인" : "수정 요청"}</span>` +
      (msg.round ? `<span class="round">라운드 ${msg.round}</span>` : "")));
  }
  col.appendChild(msg.kind === "approval" ? approvalCard(msg) : bubble(msg));
  if (msg.usage && !isUser) col.appendChild(tokenMeta(msg.usage));
  if (msg.changed_files?.length) {
    const cf = el("div", "meta-tokens changed", `바뀐 파일 ${msg.changed_files.length}개 · 파일 패널에서 보기`);
    cf.title = msg.changed_files.join("\n");
    cf.onclick = () => toggleFiles(true);
    col.appendChild(cf);
  }
  row.appendChild(col);
  frag.appendChild(row);
  return frag;
}

function bubble(msg) {
  const b = el("div", "bubble");
  if (msg.review) {
    const r = msg.review;
    b.classList.add("review");
    let h = `<div class="summary">${md(r.summary)}</div>`;
    if (r.requirements?.length) {
      const unmet = r.requirements.filter((q) => !q.met).length;
      h += `<details class="reqs"${unmet ? " open" : ""}><summary>지시 항목 ${r.requirements.length}개 확인${unmet ? ` · 미충족 ${unmet}` : ""}</summary>` +
        r.requirements.map((q) => `<div class="issue"><span class="tag ${q.met ? "met" : "blocker"}">${q.met ? "충족" : "미충족"}</span><div>${esc(q.item)}<div class="ev">${esc(q.evidence)}</div></div></div>`).join("") + `</details>`;
    }
    (r.issues || []).forEach((i) => {
      const blk = i.severity === "blocker";
      h += `<div class="issue"><span class="tag ${blk ? "blocker" : "minor"}">${blk ? "필수" : "제안"}</span>${i.needs_user ? '<span class="tag user" title="실무자에게 돌려보내지 않고 사용자에게 넘기는 지적">결정 필요</span>' : ""}<div>${md(i.description)}</div></div>`;
    });
    if ((r.checks || "").trim()) h += `<details><summary>확인한 것</summary><div>${esc(r.checks)}</div></details>`;
    if (r.parse_warning) h += `<div class="warnline">⚠ ${esc(r.parse_warning)}</div>`;
    b.innerHTML = h;
  } else {
    b.innerHTML = md(msg.text);
    if (msg.sender === "user" && msg.mode === "quick") b.title = "빠른 질문";
  }
  if (msg.attachments?.length) {
    const box = el("div", "attach-list");
    msg.attachments.forEach((p) => box.appendChild(el("span", "attach", `${ICON.clip}${esc(p.split("/").pop())}`)));
    b.appendChild(box);
  }
  return b;
}

function approvalCard(msg) {
  const a = msg.approval || {};
  const c = el("div", "approval");
  c.innerHTML = `<div class="ap-title">실행 허락 요청 <span class="ap-tool">${esc(a.tool)}</span></div>
    ${a.description ? `<div class="ap-desc">${esc(a.description)}</div>` : ""}
    <pre>${esc(a.detail)}</pre>`;
  if (a.status === "pending") {
    const act = el("div", "ap-actions");
    const yes = el("button", "btn primary", "허용");
    const no = el("button", "btn gray", "거부");
    yes.onclick = () => answerApproval(a.id, true, act);
    no.onclick = () => answerApproval(a.id, false, act);
    act.append(yes, no);
    c.appendChild(act);
  } else {
    const label = { allowed: "✓ 허용함", denied: "거부함", expired: "만료됨 (서버 재시작)" }[a.status] || a.status;
    c.appendChild(el("div", `ap-status ${esc(a.status)}`, label));
  }
  return c;
}

function answerApproval(id, allow, box) {
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  send({ type: "approve", conv: S.conv, id, allow });
}

function renderAll() {
  const box = $("messages");
  box.innerHTML = "";
  S.messages.forEach((m, i) => box.appendChild(renderMessage(m, S.messages[i - 1])));
  fixTails();
  renderEmpty();
  renderTyping();
  renderStatus();
  renderHeader();
  scrollBottom();
}

// 같은 사람의 연속 말풍선 중 마지막만 꼬리 모양
function fixTails() {
  const rows = [...$("messages").querySelectorAll(".row")];
  rows.forEach((r, i) => {
    const n = rows[i + 1];
    const cont = n && n.dataset.sender === r.dataset.sender && !n.classList.contains("first");
    r.classList.toggle("last", !cont);
  });
}

function appendMessage(msg) {
  const stick = nearBottom() || msg.sender === "user";
  const prev = S.messages[S.messages.length - 1];
  S.messages.push(msg);
  $("messages").appendChild(renderMessage(msg, prev));
  fixTails();
  renderEmpty();
  if (stick) scrollBottom();
  if (S.filesOpen && msg.sender !== "user") loadFiles();
}

function updateMessage(msg) {
  const i = S.messages.findIndex((m) => m.id === msg.id);
  if (i < 0) return;
  S.messages[i] = msg;
  const old = $("messages").querySelector(`[data-id="${msg.id}"]`);
  if (!old) return renderAll();
  const frag = renderMessage(msg, S.messages[i - 1]);
  const node = [...frag.childNodes].find((n) => n.dataset?.id === msg.id);
  if (node) old.replaceWith(node);
  fixTails();
}

function renderArchivedBanner() {
  let b = $("archivedBanner");
  const show = S.conv && S.summary?.archived;
  if (!show) { b?.remove(); return; }
  if (!b) {
    b = el("div", "archived-banner");
    b.id = "archivedBanner";
    $("thread").prepend(b);
  }
  b.innerHTML = "보관한 대화입니다. 메시지를 보내면 자동으로 다시 꺼내집니다. ";
  const btn = el("button", "archive-link", "지금 꺼내기");
  btn.onclick = () => archiveConv(S.conv, false);
  b.appendChild(btn);
}

function renderEmpty() {
  renderArchivedBanner();
  const e = $("empty");
  const show = !S.conv || !S.messages.length;
  e.classList.toggle("hidden", !show);
  if (!show) return;
  const big = el("div", "big-members");
  order().forEach((k) => big.appendChild(avatarEl(agent(k))));
  e.innerHTML = "";
  e.appendChild(big);
  e.appendChild(el("h2", "", S.conv ? "무엇을 맡길까요?" : `${esc(S.config.room_name)}에 오신 걸 환영합니다`));
  const who = el("div", "who");
  order().forEach((k) => who.appendChild(el("div", "", `<b>${esc(agent(k).name)}</b> ${esc(agent(k).title)}`)));
  e.appendChild(who);
  e.appendChild(el("p", "", S.conv
    ? `검토 모드에서는 ${esc(agent(S.config.worker).name)}가 작업하고 ${S.config.reviewers.map((k) => esc(agent(k).name)).join("·")}가 검토합니다.<br>빠른 질문은 ${esc(agent(S.config.worker).name)}만 바로 답하고, <b>@이름</b>으로 한 사람만 부를 수도 있어요.`
    : "왼쪽 위 ✎ 버튼으로 새 대화를 시작하세요."));
  if (!S.conv) {
    const b = el("button", "btn primary", "새 대화 시작");
    b.onclick = openNewConvModal;
    e.appendChild(b);
  }
}

function renderTyping() {
  const stick = nearBottom();
  const box = $("typing");
  box.innerHTML = "";
  Object.entries(S.typing).forEach(([k, activity]) => {
    const a = agent(k);
    const row = el("div", "row in gap first");
    const slot = el("div", "avatar-slot");
    slot.appendChild(avatarEl(a));
    const col = el("div", "col");
    const tok = S.typingTokens[k];
    col.innerHTML = `<div class="sender"><b>${esc(a.name)}</b><span>${esc(a.title)}</span>${tok ? `<span class="live-tok" title="이번 답변에서 지금까지 쓴 토큰(실시간)">${fmtTok(tok)} 토큰</span>` : ""}</div>
      <div class="typing-bubble"><span></span><span></span><span></span></div>` +
      (activity ? `<div class="activity" title="${esc(activity)}">${esc(activity)}</div>` : "");
    row.append(slot, col);
    box.appendChild(row);
  });
  if (stick) scrollBottom();
}

function renderStatus() {
  const p = [];
  if (S.running && S.round) p.push(`검토 진행 중 · 라운드 ${S.round}/${S.config.max_rounds}`);
  else if (S.running) p.push("답변 중");
  if (S.pending) p.push(`전달 대기 지시 ${S.pending}건`);
  const waiting = S.messages.filter((m) => m.kind === "approval" && m.approval?.status === "pending").length;
  if (waiting) p.push(`승인 대기 ${waiting}건`);
  $("status").textContent = p.join("  ·  ");
  $("btnStop").hidden = !S.running;
}

const nearBottom = () => { const t = $("thread"); return t.scrollHeight - t.scrollTop - t.clientHeight < 140; };
const scrollBottom = () => { const t = $("thread"); t.scrollTop = t.scrollHeight; };

// ────────────────────────────────────────────────────────── 통신
function send(obj) { if (S.ws?.readyState === WebSocket.OPEN) S.ws.send(JSON.stringify(obj)); }

function connect() {
  S.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  S.ws.onopen = () => { $("conn").classList.add("on"); sendPresence(); };
  S.ws.onclose = (ev) => {
    $("conn").classList.remove("on");
    if (ev.code === 4401) { location.reload(); return; }  // 로그인 만료 → 로그인 화면
    setTimeout(connect, 1500);
  };
  S.ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    const mine = d.conv && d.conv === S.conv;
    switch (d.type) {
      case "init":
        S.config = d.config; S.conversations = d.conversations; S.recent = d.recent_folders || [];
        S.limits = d.limits || {};
        S.slash = d.slash_commands || [];
        S.local = d.client ? !!d.client.local : true;
        document.body.classList.toggle("remote", !S.local);
        S.conversations.forEach((c) => { S.prevRunning[c.id] = !!c.running; });
        renderSidebar(); renderUsage();
        { const fromHash = (location.hash.match(/conv=([^&]+)/) || [])[1];  // 알림을 눌러 열었을 때
          if (fromHash) history.replaceState(null, "", location.pathname);
          const last = store.get("conv"); const pick = S.conversations.find((c) => c.id === (fromHash && decodeURIComponent(fromHash))) || S.conversations.find((c) => c.id === (S.conv || last)) || S.conversations[0];
          if (pick) openConv(pick.id, true); else { S.conv = null; S.messages = []; renderAll(); } }
        renderMode();
        break;
      case "snapshot":
        if (!mine) break;
        S.messages = d.messages; S.typing = d.typing || {}; S.typingTokens = d.typing_tokens || {}; S.running = d.running; S.round = d.round; S.pending = d.pending; S.summary = d.summary;
        S.contexts = d.contexts || {};
        renderAll();
        if (S.filesOpen) loadFiles();
        break;
      case "message": if (mine) { appendMessage(d.message); renderStatus(); } break;
      case "message_update": if (mine) { updateMessage(d.message); renderStatus(); } break;
      case "typing":
        if (!mine) break;
        if (d.on) { S.typing[d.agent] = d.activity || ""; if (d.tokens) S.typingTokens[d.agent] = d.tokens; }
        else { delete S.typing[d.agent]; delete S.typingTokens[d.agent]; }
        renderTyping();
        break;
      case "context":
        if (!mine) break;
        S.contexts = d.contexts || {};
        renderHeader();
        if (document.querySelector(".ctx-modal")) openContextModal();
        break;
      case "state":
        if (!mine) break;
        S.running = d.running; S.round = d.round; S.pending = d.pending;
        renderStatus(); renderHeader();
        break;
      case "conversations":
        S.conversations = d.conversations;
        notifyFinished();
        { const c = S.conversations.find((x) => x.id === S.conv); if (c) { S.summary = { ...S.summary, ...c }; renderHeader(); } }
        renderSidebar();
        break;
      case "config":
        S.config = d.config;
        renderSidebar(); renderAll(); renderMode();
        if (S.settings) refreshSettingsAvatars();
        break;
      case "slash_commands":
        S.slash = d.commands || [];
        break;
      case "usage_limits":
        S.limits = d.limits || {};
        renderUsage();
        break;
      case "avatar_status":
        S.avatarJobs[d.key] = d.status === "generating";
        if (d.status === "done") toast(`${agent(d.key).name} 사진을 새로 만들었습니다`);
        if (d.status === "failed") toast(`사진 생성 실패: ${d.message || ""}`, 5000);
        if (S.settings) renderSettingsPane();
        break;
    }
  };
}

function openConv(id, silent) {
  flushHold();  // 붙잡아 둔 메시지는 원래 대화로 보내고 넘어간다
  S.conv = id;
  store.set("conv", id);
  S.messages = []; S.typing = {}; S.running = false; S.round = 0; S.pending = 0; S.contexts = {};
  S.summary = S.conversations.find((c) => c.id === id) || null;
  S.attachments = []; renderChips();
  S.previewPath = null;
  $("app").classList.add("chat-open");
  if (narrow()) setLeftDrawer(false);
  renderSidebar(); renderAll();
  send({ type: "open", conv: id });
  if (!silent) $("input").focus();
}

// ────────────────────────────────────────────────────────── 입력창
function renderMode() {
  const b = $("modeBtn");
  b.textContent = S.mode === "quick" ? "빠른 질문 ▾" : "검토 모드 ▾";
  b.classList.toggle("quick", S.mode === "quick");
}

function toggleModeMenu(show) {
  const m = $("modeMenu");
  if (show === false || !m.classList.contains("hidden")) { m.classList.add("hidden"); return; }
  const w = agent(S.config.worker).name, rv = S.config.reviewers.map((k) => agent(k).name).join("·");
  m.innerHTML = "";
  [["review", "검토 모드", `${w}가 작업 → ${rv}가 검토 (최대 ${S.config.max_rounds}라운드)`],
   ["quick", "빠른 질문", `${w}만 바로 답변, 검토 없음`]].forEach(([k, t, s]) => {
    const it = el("div", "menu-item", `<span class="check">${S.mode === k ? "✓" : ""}</span><div><div class="mi-title">${t}</div><div class="mi-sub">${esc(s)}</div></div>`);
    it.onclick = () => { S.mode = k; store.set("mode", k); renderMode(); m.classList.add("hidden"); $("input").focus(); };
    m.appendChild(it);
  });
  m.appendChild(el("div", "menu-hint", "메시지를 <b>@이름</b>으로 시작하면 그 사람만 답합니다."));
  m.classList.remove("hidden");
}

let mentionSel = 0;
const SLASH_HINT = { rc: "앱에서는 안 됨", resume: "앱은 대화별로 자동 이어짐" };
function updateSlash() {
  const pop = $("mentionPop");
  const m = $("input").value.match(/^(@\S+\s+)?\/(\S*)$/);
  if (!m) return false;
  const q = m[2].toLowerCase();
  const list = S.slash.filter((c) => c.toLowerCase().includes(q)).sort((a, b) => (a.toLowerCase().startsWith(q) ? 0 : 1) - (b.toLowerCase().startsWith(q) ? 0 : 1) || a.localeCompare(b)).slice(0, 12);
  pop.innerHTML = "";
  if (!S.slash.length) {
    pop.appendChild(el("div", "menu-hint", "명령 목록은 Claude 담당자가 한 번 답한 뒤에 표시됩니다. 명령은 그대로 보내도 됩니다."));
  } else if (!list.length) {
    pop.appendChild(el("div", "menu-hint", `'/${esc(q)}'로 시작하는 명령이 없습니다.`));
  }
  mentionSel = Math.min(mentionSel, Math.max(0, list.length - 1));
  list.forEach((c, i) => {
    const it = el("div", "menu-item" + (i === mentionSel ? " sel" : ""), `<div><div class="mi-title">/${esc(c)}</div></div>`);
    it.dataset.cmd = (m[1] || "") + "/" + c;
    it.onclick = () => pickSlash(it.dataset.cmd);
    pop.appendChild(it);
  });
  pop.appendChild(el("div", "menu-hint", "/ 명령은 검토 없이 담당자(기본 Jake)에게 그대로 전달됩니다. @Clara /명령 처럼 사람 지정 가능. /rc·/resume 은 앱에서 쓸 수 없습니다."));
  pop.classList.remove("hidden");
  return true;
}
function pickSlash(cmd) {
  $("input").value = cmd + " ";
  $("mentionPop").classList.add("hidden");
  autosize(); $("input").focus();
}
function updateMention() {
  const pop = $("mentionPop");
  if (updateSlash()) return;
  const m = $("input").value.match(/^@(\S*)$/);
  if (!m) { pop.classList.add("hidden"); return; }
  const q = m[1].toLowerCase();
  const list = Object.entries(S.config.agents).filter(([k, a]) => !q || a.name.toLowerCase().startsWith(q) || k.startsWith(q));
  if (!list.length) { pop.classList.add("hidden"); return; }
  mentionSel = Math.min(mentionSel, list.length - 1);
  pop.innerHTML = "";
  list.forEach(([k, a], i) => {
    const it = el("div", "menu-item" + (i === mentionSel ? " sel" : ""));
    it.appendChild(avatarEl(a));
    it.appendChild(el("div", "", `<div class="mi-title">${esc(a.name)}</div><div class="mi-sub">${esc(a.title)} · 이 사람만 답합니다</div>`));
    it.onclick = () => pickMention(a.name);
    it.dataset.name = a.name;
    pop.appendChild(it);
  });
  pop.classList.remove("hidden");
}
function pickMention(name) {
  $("input").value = `@${name} `;
  $("mentionPop").classList.add("hidden");
  autosize(); $("input").focus();
}

function autosize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 180) + "px";
  t.classList.toggle("scroll", t.scrollHeight > 180);
  $("btnSend").disabled = !(t.value.trim() || S.attachments.some((a) => a.path)) || S.attachments.some((a) => a.uploading);
}

function sendMessage() {
  const t = $("input");
  const text = t.value.trim();
  const files = S.attachments.filter((a) => a.path).map((a) => a.path);
  if (!S.conv) { toast("먼저 새 대화를 만들어 주세요"); return; }
  if ((!text && !files.length) || S.attachments.some((a) => a.uploading)) return;
  flushHold();
  const msg = { type: "say", conv: S.conv, text, mode: S.mode, attachments: files };
  const delay = Number(S.sendDelay) || 0;
  if (delay > 0) {
    S.hold = { msg, atts: S.attachments.slice(), left: delay, timer: setInterval(tickHold, 1000) };
    renderHold();
  } else send(msg);
  t.value = ""; S.attachments = []; renderChips(); autosize();
  $("mentionPop").classList.add("hidden");
}

// 보내기 직전 몇 초 붙잡아 두기: 그 안에 Esc 를 누르면 입력창으로 되돌려 고칠 수 있다(서버에는 아직 안 간 상태)
function tickHold() {
  if (!S.hold) return;
  if (--S.hold.left <= 0) flushHold(); else renderHold();
}
function flushHold() {
  const h = S.hold;
  if (!h) return;
  clearInterval(h.timer);
  S.hold = null;
  send(h.msg);
  renderHold();
}
function cancelHold() {
  const h = S.hold;
  if (!h) return false;
  clearInterval(h.timer);
  S.hold = null;
  const t = $("input");
  t.value = h.msg.text + (t.value ? "\n" + t.value : "");
  S.attachments = h.atts.concat(S.attachments);
  renderChips(); autosize(); renderHold();
  t.focus();
  t.setSelectionRange(t.value.length, t.value.length);
  return true;
}
function renderHold() {
  const box = $("holdBar");
  const h = S.hold;
  box.classList.toggle("hidden", !h);
  if (!h) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  const n = h.msg.attachments.length;
  box.appendChild(el("div", "hold-text", esc(h.msg.text || "(첨부만)") + (n ? ` <span class="hold-att">${ICON.clip}${n}</span>` : "")));
  box.appendChild(el("span", "hold-left", `${h.left}초 뒤 전송`));
  const edit = el("button", "hold-btn", "수정 <kbd>Esc</kbd>");
  edit.onclick = cancelHold;
  const now = el("button", "hold-btn primary", "바로 보내기");
  now.onclick = flushHold;
  box.append(edit, now);
}

// 첨부
function renderChips() {
  const box = $("chips");
  box.innerHTML = "";
  S.attachments.forEach((a, i) => {
    const c = el("span", "chip" + (a.uploading ? " uploading" : ""), `${ICON.clip}${esc(a.name)}${a.uploading ? " · 올리는 중" : ""}`);
    const x = el("button", "", "×");
    x.title = "첨부 취소";
    x.onclick = () => { S.attachments.splice(i, 1); renderChips(); autosize(); };
    c.appendChild(x);
    box.appendChild(c);
  });
}

function readAsDataURL(file) {
  return new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = fail; r.readAsDataURL(file); });
}

async function attachFiles(files) {
  if (!S.conv) { toast("먼저 새 대화를 만들어 주세요"); return; }
  for (const f of files) {
    if (f.size > 50 * 1024 * 1024) { toast(`${f.name}: 50MB 이하만 첨부할 수 있습니다`); continue; }
    const item = { name: f.name, uploading: true };
    S.attachments.push(item); renderChips(); autosize();
    try {
      const d = await api("POST", `/api/conv/${S.conv}/upload`, { name: f.name, data: await readAsDataURL(f) });
      item.path = d.path; item.uploading = false;
    } catch (e) {
      S.attachments.splice(S.attachments.indexOf(item), 1);
      toast(`첨부 실패: ${e.message}`);
    }
    renderChips(); autosize();
  }
  if (S.filesOpen) loadFiles();
}

// ────────────────────────────────────────────────────────── 파일 패널
function toggleFiles(force) {
  S.filesOpen = force ?? !S.filesOpen;
  if (!narrow()) store.set("filesOpen", S.filesOpen);  // 폰에서는 다음에 열 때 서랍이 덮고 있지 않게 기억하지 않음
  $("filesPanel").classList.toggle("hidden", !S.filesOpen);
  $("btnFiles").classList.toggle("active", S.filesOpen);
  if (S.filesOpen && narrow()) $("app").classList.remove("drawer-left");
  syncDrawerBack();
  if (S.filesOpen) loadFiles();
}

// ────────────────────────────────────────────────────────── 폰: 서랍 · 키보드
function syncDrawerBack() {
  $("drawerBack").classList.toggle("on", narrow() && ($("app").classList.contains("drawer-left") || S.filesOpen));
}
function setLeftDrawer(on) {
  $("app").classList.toggle("drawer-left", on);
  if (on && narrow() && S.filesOpen) toggleFiles(false);
  syncDrawerBack();
}
function syncPlaceholder() {  // 폰에서는 긴 안내가 두 줄로 넘쳐서 줄임
  $("input").placeholder = narrow() ? "메시지 입력" : "메시지 입력  ·  @이름으로 한 사람만 부르기";
}
function closeDrawers() {
  setLeftDrawer(false);
  if (narrow() && S.filesOpen) toggleFiles(false);
}

// 아이폰은 키보드가 올라와도 화면 높이를 줄이지 않는다 → 키보드가 떠 있을 때만 가려진 만큼 화면 위·아래를 줄인다.
// 평소에는 CSS(position:fixed; inset:0)가 실제 화면에 맞춘다.
// 9/29 실기: 홈 화면 앱에서 visualViewport 높이를 그대로 화면 높이로 쓰니 실제 화면보다 커서 입력창 아래가 잘렸음
// → 높이 값 대신 '가려진 양'(innerHeight − 보이는 높이)만 쓴다(두 값에 같이 끼는 오차는 빼면 없어짐).
function watchViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const root = document.documentElement.style;
  const apply = () => {
    const stick = nearBottom();
    const top = Math.max(0, Math.round(vv.offsetTop));
    const hidden = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    const keyboard = window.innerHeight - vv.height > 80;
    root.setProperty("--vv-top", keyboard ? `${top}px` : "0px");
    root.setProperty("--vv-bottom", keyboard ? `${hidden}px` : "0px");
    document.body.classList.toggle("kb-open", keyboard);
    if (stick) requestAnimationFrame(scrollBottom);
  };
  window.addEventListener("resize", apply);
  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", apply);
  apply();
}

// ────────────────────────────────────────────────────────── 알림(웹 푸시) · 화면 안 알림
const PUSH = { reg: null, endpoint: null };
const pushSupported = () => window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

async function initServiceWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data?.type === "open-conv" && e.data.conv && S.conversations.some((c) => c.id === e.data.conv)) openConv(e.data.conv, true);
  });
  try {
    PUSH.reg = await navigator.serviceWorker.register("/sw.js");
    const sub = PUSH.reg.pushManager ? await PUSH.reg.pushManager.getSubscription() : null;
    PUSH.endpoint = sub?.endpoint || null;
    sendPresence();
  } catch { /* 서비스 워커를 못 쓰는 환경 */ }
}

function sendPresence() { send({ type: "presence", endpoint: PUSH.endpoint, visible: document.visibilityState === "visible" }); }

function deviceLabel() {
  const ua = navigator.userAgent;
  const dev = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows PC" : /Mac/.test(ua) ? "Mac" : "기기";
  const br = /Edg/.test(ua) ? "Edge" : /CriOS|Chrome/.test(ua) ? "Chrome" : /Safari/.test(ua) ? "Safari" : "";
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  return `${dev}${br ? " " + br : ""}${standalone ? " · 홈 화면 앱" : ""}`;
}

function b64urlToBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function enablePush() {
  if (!pushSupported()) throw new Error("이 화면에서는 알림을 켤 수 없습니다(아이폰은 홈 화면에 추가한 앱에서만 됩니다)");
  const perm = await Notification.requestPermission();  // 버튼을 누른 그 순간에 물어야 아이폰이 허락 창을 띄운다
  if (perm !== "granted") throw new Error("알림 권한이 꺼져 있습니다. 아이폰 설정 → 알림 → 작업방에서 켜 주세요.");
  if (!PUSH.reg) PUSH.reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const { public_key } = await api("GET", "/api/push/status");
  const sub = await PUSH.reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64urlToBytes(public_key) });
  await api("POST", "/api/push/subscribe", { subscription: sub.toJSON(), label: deviceLabel() });
  PUSH.endpoint = sub.endpoint;
  sendPresence();
}

async function disablePush() {
  const sub = PUSH.reg?.pushManager ? await PUSH.reg.pushManager.getSubscription() : null;
  if (sub) { await api("POST", "/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe().catch(() => {}); }
  PUSH.endpoint = null;
  sendPresence();
}

// 다른 대화가 끝나면 화면 안에서 알린다(지금 보는 대화는 채팅에 바로 보이므로 생략)
function notifyFinished() {
  S.conversations.forEach((c) => {
    const was = S.prevRunning[c.id];
    S.prevRunning[c.id] = !!c.running;
    if (was && !c.running && c.id !== S.conv) toast(`'${c.title}' 대화가 끝났습니다`, 6000, { label: "열기", fn: () => openConv(c.id) });
  });
}

async function loadFiles() {
  if (!S.conv) return;
  let d;
  try { d = await api("GET", `/api/conv/${S.conv}/files`); } catch (e) { $("fpList").innerHTML = `<div class="pv-note">${esc(e.message)}</div>`; return; }
  $("fpPath").textContent = d.workspace;
  S.files = d.files;
  renderFileTabs();
  renderFileList(d.truncated);
}

const isArtifact = (f) => f.in_conv && !f.path.startsWith("_첨부/") && !f.path.startsWith("_qa/");

function renderFileTabs() {
  const box = $("fpTabs");
  box.innerHTML = "";
  [["artifacts", "산출물", S.files.filter(isArtifact).length], ["all", "전체 파일", S.files.length]].forEach(([k, label, c]) => {
    const b = el("button", S.fpTab === k ? "on" : "", `${label}<span class="cnt">${c}</span>`);
    b.title = k === "artifacts" ? "이 대화에서 만들거나 고친 파일 (첨부·검증용 임시 파일 제외)" : "작업 폴더의 모든 파일";
    b.onclick = () => { S.fpTab = k; store.set("fpTab", k); renderFileTabs(); renderFileList(); };
    box.appendChild(b);
  });
}

function ftype(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  if (["xlsx", "xlsm", "xls", "csv", "tsv"].includes(ext)) return ["xls", ext.toUpperCase()];
  if (["docx", "doc", "hwp", "hwpx"].includes(ext)) return ["doc", ext.toUpperCase()];
  if (["pptx", "ppt"].includes(ext)) return ["ppt", ext.toUpperCase()];
  if (ext === "pdf") return ["pdf", "PDF"];
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"].includes(ext)) return ["img", ext.toUpperCase()];
  if (["py", "js", "ts", "json", "md", "txt", "html", "css", "yaml", "yml", "bat", "ps1", "sql"].includes(ext)) return ["code", ext.toUpperCase()];
  return ["etc", (ext || "FILE").slice(0, 4).toUpperCase()];
}

function renderFileList(truncated) {
  const box = $("fpList");
  box.innerHTML = "";
  const list = S.fpTab === "artifacts" ? S.files.filter(isArtifact) : S.files;
  if (!list.length) {
    box.appendChild(el("div", "pv-note", S.fpTab === "artifacts" ? "이 대화에서 만들거나 고친 파일이 아직 없습니다." : "아직 파일이 없습니다."));
    return;
  }
  const recent = Date.now() / 1000 - 15 * 60;
  const sorted = S.fpTab === "artifacts" ? list.slice().sort((a, b) => b.mtime - a.mtime) : list.slice().sort((a, b) => a.path.localeCompare(b.path));
  let dir = null;
  sorted.forEach((f) => {
    const parts = f.path.split("/");
    const fdir = parts.slice(0, -1).join("/");
    if (S.fpTab === "all" && fdir !== dir) { dir = fdir; if (fdir) box.appendChild(el("div", "fp-dir", `📁 ${esc(fdir)}`)); }
    const [cls, lab] = ftype(f.path);
    const row = el("div", "fp-file" + (f.path === S.previewPath ? " sel" : "") + (f.mtime > recent ? " fresh" : ""));
    const name = S.fpTab === "artifacts" ? f.path : parts[parts.length - 1];
    row.innerHTML = `<span class="ftype ${cls}">${esc(lab)}</span><span class="nm">${esc(name)}</span><span class="sz">${fmtSize(f.size)}</span>`;
    row.title = `${f.path}\n수정: ${new Date(f.mtime * 1000).toLocaleString("ko-KR")}\n(두 번 누르면 넓게 보기)`;
    row.dataset.path = f.path;
    row.draggable = true;  // 채팅 쪽으로 끌어다 놓으면 첨부(이미 작업 폴더 안 파일이라 다시 올리지 않음)
    row.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData(DRAG_PATH, f.path);
      e.dataTransfer.setData("text/plain", f.path);
      e.dataTransfer.effectAllowed = "copy";
    });
    row.onclick = () => previewFile(f.path);
    row.ondblclick = () => openViewer(f.path);
    box.appendChild(row);
  });
  if (truncated) box.appendChild(el("div", "pv-note", "파일이 많아 2,000개까지만 표시합니다."));
}

const ICON_EXPAND = '<svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>';
const ICON_APP = '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>';
const ICON_FOLDER = '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

const DRAG_PATH = "application/x-agentchat-path";

function attachWorkspaceFile(path) {
  if (!S.conv) return;
  if (S.attachments.some((a) => a.path === path)) { toast("이미 첨부한 파일입니다"); return; }
  S.attachments.push({ name: path.split("/").pop(), path });
  renderChips(); autosize();
  $("input").focus();
}

function fileActions(path, withExpand) {
  const box = el("div", "pv-actions");
  const mk = (icon, title, fn) => { const b = el("button", "icon-btn sm", icon); b.title = title; b.onclick = fn; box.appendChild(b); };
  mk(ICON.clip.replace('width="13" height="13"', ""), "채팅에 첨부", () => { attachWorkspaceFile(path); if (narrow()) toggleFiles(false); });
  if (withExpand) mk(ICON_EXPAND, "넓게 보기", () => openViewer(path));
  if (!S.local) {  // 폰: PC 프로그램으로 열 수 없으니 원본 파일을 새 탭으로(아이폰 미리보기·공유)
    mk(ICON_APP, "원본 파일 열기·저장", () => window.open(`/api/conv/${S.conv}/raw?path=${encodeURIComponent(path)}`, "_blank"));
    return box;
  }
  mk(ICON_APP, "기본 앱으로 열기 (엑셀·워드 등)", () => api("POST", `/api/conv/${S.conv}/open-file`, { path }).then(() => toast("기본 앱으로 여는 중…")).catch((e) => toast(e.message)));
  mk(ICON_FOLDER, "탐색기에서 파일 위치 열기", () => api("POST", `/api/conv/${S.conv}/open`, { path }).catch((e) => toast(e.message)));
  return box;
}

async function previewFile(path) {
  S.previewPath = path;
  const pv = $("fpPreview");
  pv.classList.remove("hidden");
  pv.innerHTML = "";
  const head = el("div", "pv-head", `<span class="nm" title="${esc(path)}">${esc(path)}</span>`);
  head.appendChild(fileActions(path, true));
  const x = el("button", "icon-btn sm", ICON.close);
  x.title = "미리보기 닫기";
  x.onclick = () => { pv.classList.add("hidden"); S.previewPath = null; renderFileList(); };
  head.appendChild(x);
  const body = el("div", "viewer", '<div class="pv-note"><span class="spinner"></span></div>');
  pv.append(head, body);
  document.querySelectorAll(".fp-file").forEach((r) => r.classList.toggle("sel", r.dataset.path === path));
  await renderPreviewInto(body, path);
}

async function openViewer(path) {
  const { body, foot, sheet } = modal(path.split("/").pop());
  sheet.classList.add("viewer-sheet");
  const v = el("div", "viewer", '<div class="pv-note"><span class="spinner"></span></div>');
  body.appendChild(v);
  foot.appendChild(el("div", "note", esc(path)));
  foot.appendChild(fileActions(path, false));
  await renderPreviewInto(v, path);
}

const isNum = (s) => /^-?[\d,]+(\.\d+)?%?$/.test(String(s).trim());

function gridTable(rows) {
  const tb = el("table", "grid");
  const colN = Math.max(0, ...rows.map((r) => r.length));
  const letters = (i) => { let s = ""; i++; while (i) { s = String.fromCharCode(65 + ((i - 1) % 26)) + s; i = Math.floor((i - 1) / 26); } return s; };
  let h = "<thead><tr><th class='rn'></th>" + Array.from({ length: colN }, (_, i) => `<th>${letters(i)}</th>`).join("") + "</tr></thead><tbody>";
  rows.forEach((r, ri) => {
    h += `<tr><td class="rn">${ri + 1}</td>` + Array.from({ length: colN }, (_, i) => { const v = r[i] ?? ""; return `<td class="${isNum(v) ? "num" : ""}" title="${esc(v)}">${esc(v)}</td>`; }).join("") + "</tr>";
  });
  tb.innerHTML = h + "</tbody>";
  return tb;
}

async function renderPreviewInto(box, path) {
  let d;
  try { d = await api("GET", `/api/conv/${S.conv}/file?path=${encodeURIComponent(path)}`); }
  catch (e) { box.innerHTML = `<div class="pv-note">${esc(e.message)}</div>`; return; }
  box.innerHTML = "";
  if (d.kind === "image") box.innerHTML = `<img src="${esc(d.url)}" alt="" style="max-width:100%;display:block;margin:10px auto;border-radius:8px">`;
  else if (d.kind === "pdf") box.innerHTML = `<iframe src="${esc(d.url)}"></iframe>`;
  else if (d.kind === "table") {
    const tabs = el("div", "sheet-tabs");
    const area = el("div", "");
    const show = (i) => {
      [...tabs.children].forEach((b, j) => b.classList.toggle("on", i === j));
      area.innerHTML = "";
      const s = d.sheets[i];
      if (!s.rows.length) area.appendChild(el("div", "pv-note", "빈 시트입니다."));
      else area.appendChild(gridTable(s.rows));
      if (s.truncated) area.appendChild(el("div", "v-note", "앞 200행·40열까지만 표시했습니다. 전체는 기본 앱으로 열어 주세요."));
    };
    d.sheets.forEach((s, i) => { const b = el("button", "", esc(s.name)); b.onclick = () => show(i); tabs.appendChild(b); });
    if (d.sheets.length > 1) box.appendChild(tabs);
    box.appendChild(area);
    if (d.sheets.length) show(0); else box.appendChild(el("div", "pv-note", "보이는 시트가 없습니다."));
    if (/\.xls[xm]$/i.test(path)) box.appendChild(el("div", "v-note", "수식 칸은 마지막으로 저장된 계산 결과값으로, 저장된 값이 없으면 수식으로 표시됩니다."));
  } else if (d.kind === "doc") {
    const v = el("div", "docv");
    d.blocks.forEach((b) => {
      if (b.type === "h") v.appendChild(el("h4", "", esc(b.text)));
      else if (b.type === "p") v.appendChild(el("p", "", esc(b.text)));
      else if (b.type === "table") v.appendChild(gridTable(b.rows));
    });
    if (!d.blocks.length) v.appendChild(el("div", "pv-note", "본문이 비어 있습니다."));
    box.appendChild(v);
  } else if (d.kind === "slides") {
    const v = el("div", "slidev");
    d.slides.forEach((s) => v.appendChild(el("div", "sl", `<b>슬라이드 ${s.no}</b><div>${esc(s.texts.join("\n\n")) || "(글자 없음)"}</div>`)));
    box.appendChild(v);
  } else if (d.kind === "markdown") box.appendChild(el("div", "mdv", md(d.content)));
  else if (d.kind === "text") { box.appendChild(el("pre", "", esc(d.content))); if (d.truncated) box.appendChild(el("div", "v-note", "앞부분만 표시했습니다.")); }
  else if (d.kind === "binary") box.appendChild(el("div", "pv-note", `미리보기할 수 없는 파일입니다 (${fmtSize(d.size)}). 위 버튼으로 기본 앱에서 열어 주세요.`));
  else box.appendChild(el("div", "pv-note", esc(d.message || "미리보기를 만들 수 없습니다.")));
}

// ────────────────────────────────────────────────────────── 화면 모드
const THEMES = [["auto", "자동", '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg>'],
  ["light", "라이트", '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'],
  ["dark", "다크", '<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg>']];
function applyTheme() {
  if (S.theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = S.theme;
  const t = THEMES.find((x) => x[0] === S.theme) || THEMES[0];
  $("btnTheme").innerHTML = `${t[2]}${t[1]}`;
  $("btnTheme").title = "화면 모드: 자동(윈도우 설정) → 라이트 → 다크 (눌러서 바꾸기)";
}
function cycleTheme() {
  const i = THEMES.findIndex((x) => x[0] === S.theme);
  S.theme = THEMES[(i + 1) % THEMES.length][0];
  store.set("theme", S.theme);
  applyTheme();
}

// ────────────────────────────────────────────────────────── 사용량
const fmtTok = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0);
const fmtReset = (ts) => ts ? new Date(ts * 1000).toLocaleString("ko-KR", { month: "numeric", day: "numeric", weekday: "short", hour: "numeric", minute: "2-digit" }) : "";

function tokenMeta(u) {
  const d = el("div", "meta-tokens", u.cumulative
    ? `세션 누적 ${fmtTok(u.total_tokens)} 토큰 · 이 답변만의 값 아님(합계 제외)`
    : `${fmtTok(u.total_tokens)} 토큰${u.cost_usd != null ? ` · 참고 $${u.cost_usd.toFixed(2)}` : ""}`);
  d.style.margin = "3px 0 0 13px";
  d.title = [`입력 ${fmtTok(u.input_tokens)}`, `캐시 읽기 ${fmtTok(u.cache_read_tokens)}`, `캐시 쓰기 ${fmtTok(u.cache_write_tokens)}`,
    `출력 ${fmtTok(u.output_tokens)}`, u.reasoning_tokens ? `추론 ${fmtTok(u.reasoning_tokens)}` : "",
    (u.models || []).length ? `모델 ${u.models.join(", ")}` : "",
    u.cost_usd != null ? "참고 금액 = API 정가 환산(구독이라 실제 청구 아님)" : ""].filter(Boolean).join("\n");
  return d;
}

function gauge(label, w) {
  if (!w) return `<div class="gauge"><div class="lbl"><span>${label}</span><span>–</span></div><div class="bar"><i style="width:0"></i></div></div>`;
  const pct = Math.max(0, Math.min(100, w.pct));
  const cls = pct >= 85 ? "high" : pct >= 60 ? "mid" : "";
  return `<div class="gauge" title="${label} 한도 ${pct}% 사용 · ${fmtReset(w.resets_at)} 초기화"><div class="lbl"><span>${label}</span><b>${Math.round(pct)}%</b></div><div class="bar"><i class="${cls}" style="width:${pct}%"></i></div></div>`;
}

function renderUsage() {
  const box = $("usageBox");
  const rows = [["claude", "Claude"], ["codex", "Codex"]].map(([k, name]) => {
    const l = S.limits[k];
    return `<div class="u-line"><b>${name}</b>${gauge("5시간", l?.five_hour)}${gauge("주간", l?.seven_day)}</div>`;
  });
  box.innerHTML = rows.join("") + (Object.keys(S.limits || {}).length ? "" : '<div class="u-empty">에이전트가 한 번 답하면 한도 사용률이 표시됩니다</div>');
}

function usageTotals() {
  const tot = {};
  S.messages.forEach((m) => {
    if (!m.usage || m.usage.cumulative) return;  // 세션 누적으로 잘못 기록된 예전 값은 합계에서 뺌
    const t = tot[m.sender] ||= { calls: 0, input_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, output_tokens: 0, total_tokens: 0, cost: 0, hasCost: false, models: new Set() };
    t.calls++;
    (m.usage.models || []).forEach((x) => t.models.add(x));
    ["input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "total_tokens"].forEach((k) => (t[k] += m.usage[k] || 0));
    if (m.usage.cost_usd != null) { t.cost += m.usage.cost_usd; t.hasCost = true; }
  });
  return tot;
}

function openUsageModal() {
  const { body, foot } = modal("사용량");
  const pane = el("div", "set-pane pad");
  body.appendChild(pane);
  const cards = el("div", "limit-card");
  const namesOf = (engine) => order().filter((x) => agent(x).engine === engine).map((x) => agent(x).name).join("·") || "담당자 없음";
  [["claude", "Claude (Max 구독)", namesOf("claude")], ["codex", "Codex (ChatGPT 구독)", namesOf("codex")]].forEach(([k, name, who]) => {
    const l = S.limits[k];
    const c = el("div", "lc", `<h4>${name}</h4><div class="fine">${who} · ${l?.updated ? `마지막 갱신 ${fmtReset(l.updated)}` : "아직 기록 없음"}</div>`);
    [["five_hour", "5시간 한도"], ["seven_day", "주간 한도"]].forEach(([w, lab]) => {
      const x = l?.[w];
      c.insertAdjacentHTML("beforeend", gauge(lab, x) + (x ? `<div class="fine">${fmtReset(x.resets_at)} 초기화</div>` : ""));
    });
    cards.appendChild(c);
  });
  pane.appendChild(cards);

  pane.appendChild(el("div", "group-title", S.summary ? `이 대화 사용량 — ${esc(S.summary.title)}` : "이 대화 사용량"));
  const tot = usageTotals();
  const tb = el("table", "usage-table");
  let h = "<thead><tr><th>담당자</th><th>답변 수</th><th>입력</th><th>캐시 읽기</th><th>캐시 쓰기</th><th>출력</th><th>합계</th><th>참고 금액</th></tr></thead><tbody>";
  const sum = { calls: 0, input_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, output_tokens: 0, total_tokens: 0, cost: 0 };
  order().forEach((k) => {
    const t = tot[k];
    if (!t) { h += `<tr><td>${esc(agent(k).name)}</td><td colspan="7" style="text-align:center;color:var(--faint)">기록 없음</td></tr>`; return; }
    Object.keys(sum).forEach((x) => (sum[x] += t[x] || 0));
    const used = [...t.models].map((id) => { const o = [...modelOptions("claude"), ...modelOptions("codex")].find((x) => x.id === id); return o ? o.label.replace(/^Claude /, "") : id; });
    h += `<tr><td title="실제로 쓰인 모델">${esc(agent(k).name)} ${used.map((u) => `<span class="model-tag">${esc(u)}</span>`).join("")}</td><td>${t.calls}</td><td>${fmtTok(t.input_tokens)}</td><td>${fmtTok(t.cache_read_tokens)}</td><td>${fmtTok(t.cache_write_tokens)}</td><td>${fmtTok(t.output_tokens)}</td><td>${fmtTok(t.total_tokens)}</td><td>${t.hasCost ? "$" + t.cost.toFixed(2) : "–"}</td></tr>`;
  });
  h += `<tr class="total"><td>합계</td><td>${sum.calls}</td><td>${fmtTok(sum.input_tokens)}</td><td>${fmtTok(sum.cache_read_tokens)}</td><td>${fmtTok(sum.cache_write_tokens)}</td><td>${fmtTok(sum.output_tokens)}</td><td>${fmtTok(sum.total_tokens)}</td><td>${sum.cost ? "$" + sum.cost.toFixed(2) : "–"}</td></tr></tbody>`;
  tb.innerHTML = h;
  pane.appendChild(tb);
  pane.appendChild(el("p", "fine", "· 참고 금액은 Claude CLI가 알려주는 <b>API 정가 환산값</b>입니다. 구독 요금제라 실제로 청구되는 금액이 아닙니다. Codex는 금액을 알려주지 않아 '–'로 표시합니다.<br>· Codex의 입력 토큰은 캐시 읽기를 포함한 총량입니다. 합계는 입력+출력으로 셉니다.<br>· 한도 사용률은 에이전트가 답할 때마다 새로 받아옵니다. 각 CLI가 보고한 값이며, 이 앱이 계산한 값이 아닙니다."));
  const ok = el("button", "btn primary", "닫기"); ok.onclick = closeModal;
  foot.appendChild(ok);
}

// ────────────────────────────────────────────────────────── 모델
async function loadModels() {
  if (S.models) return S.models;
  try { S.models = await api("GET", "/api/models"); } catch { S.models = { claude: { models: [], efforts: [] }, codex: { models: [], default: null } }; }
  return S.models;
}
function modelOptions(engine) {
  const m = S.models?.[engine];
  if (!m) return [];
  if (engine === "codex") {
    const def = m.default?.model ? `기본값 (${m.default.model})` : "기본값";
    return [{ id: "", label: def }, ...m.models];
  }
  return m.models;
}
function modelLabel(k, short) {
  const a = agent(k);
  const id = a.model || "";
  const found = S.models && modelOptions(a.engine).find((o) => o.id === id);
  if (!id) return a.engine === "codex" ? (S.models?.codex?.default?.model || "기본 모델") : "기본 모델";
  const lab = found ? found.label : id;
  return short ? lab.replace(/^Claude /, "") : lab;
}
function effortOptions(engine, modelId) {
  if (engine === "claude") return S.models?.claude?.efforts || [];
  const m = (S.models?.codex?.models || []).find((x) => x.id === (modelId || S.models?.codex?.default?.model));
  return m?.efforts || ["low", "medium", "high", "xhigh"];
}

function modelSelect(engine, value, onChange) {
  const sel = el("select", "model");
  const opts = modelOptions(engine);
  const known = opts.some((o) => o.id === (value || ""));
  opts.forEach((o) => { const op = el("option", "", esc(o.label)); op.value = o.id; op.title = o.note || ""; if (o.id === (value || "")) op.selected = true; sel.appendChild(op); });
  if (!known && value) { const op = el("option", "", esc(`${value} (직접 입력)`)); op.value = value; op.selected = true; sel.appendChild(op); }
  const custom = el("option", "", "직접 입력…"); custom.value = "__custom__"; sel.appendChild(custom);
  sel.onchange = () => {
    if (sel.value === "__custom__") {  // 브라우저 대화상자 대신 입력칸으로 바꿔 받음
      const inp = el("input"); inp.type = "text"; inp.placeholder = "모델 ID"; inp.value = value || "";
      sel.replaceWith(inp); inp.focus();
      inp.onchange = () => onChange(inp.value.trim());
      return;
    }
    onChange(sel.value);
  };
  return sel;
}
function effortSelect(engine, modelId, value, onChange) {
  const ef = el("select");
  ["", ...effortOptions(engine, modelId)].forEach((v) => { const o = el("option", "", v ? v : "생각 깊이: 기본"); o.value = v; if ((value || "") === v) o.selected = true; ef.appendChild(o); });
  ef.onchange = () => onChange(ef.value);
  return ef;
}

async function saveAgentModel(k, patch) {
  try {
    await api("PUT", "/api/config", { agents: { [k]: patch } });
    toast(`${agent(k).name}: 다음 답변부터 적용됩니다`);
  } catch (e) { toast(e.message, 5000); }
}

async function toggleModelPop() {
  const old = document.querySelector(".model-pop");
  if (old) { old.remove(); return; }
  await loadModels();
  const pop = el("div", "model-pop");
  pop.appendChild(el("h5", "", "담당자별 모델"));
  order().forEach((k) => {
    const a = agent(k);
    const row = el("div", "mp-row");
    row.appendChild(avatarEl(a));
    const right = el("div", "");
    right.appendChild(el("div", "nm", `${esc(a.name)}<span>${esc(a.title)} · ${a.engine === "codex" ? "Codex" : "Claude"}</span>`));
    const sels = el("div", "sels");
    const renderSels = (modelId, effort) => {
      sels.innerHTML = "";
      sels.appendChild(modelSelect(a.engine, modelId, (v) => { saveAgentModel(k, { model: v, effort: "" }); renderSels(v, ""); }));
      sels.appendChild(effortSelect(a.engine, modelId, effort, (v) => saveAgentModel(k, { effort: v })));
    };
    renderSels(a.model || "", a.effort || "");
    right.appendChild(sels);
    row.appendChild(right);
    pop.appendChild(row);
  });
  pop.appendChild(el("div", "fine", "바꾸면 진행 중인 대화에도 다음 답변부터 적용됩니다(세션 맥락은 유지). 설정 창에서도 바꿀 수 있습니다."));
  pop.addEventListener("mousedown", (e) => e.stopPropagation());
  document.querySelector(".chat-head").appendChild(pop);
  setTimeout(() => document.addEventListener("mousedown", function close(e) { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("mousedown", close); } }), 0);
}

// ────────────────────────────────────────────────────────── 컨텍스트
// 컨텍스트 = 담당자 세션이 지금 들고 있는 대화 분량(마지막 요청 크기). 창 크기를 알면 %로, 모르면 토큰 수로 보여준다.
function ctxPct(c) { return c?.used && c?.window ? Math.min(100, Math.round((c.used / c.window) * 100)) : null; }
function ctxLevel(p) { return p == null ? "" : p >= 90 ? "ctx-high" : p >= 70 ? "ctx-mid" : ""; }

function openContextModal() {
  const m = modal("컨텍스트", "ctx-modal");
  m.body.appendChild(el("div", "hint", "담당자마다 세션이 따로 있고, 답할 때마다 그 세션에 쌓인 대화 전체를 다시 읽습니다. 가득 차면 느려지고 앞 내용을 잊기 시작합니다."));
  order().forEach((k) => {
    const a = agent(k), c = S.contexts[k];
    const row = el("div", "ctx-row");
    row.appendChild(avatarEl(a));
    const right = el("div", "ctx-right");
    const p = ctxPct(c);
    right.appendChild(el("div", "nm", `${esc(a.name)}<span>${esc(a.title)} · ${a.engine === "codex" ? "Codex" : "Claude"}</span>`));
    if (!c?.used) right.appendChild(el("div", "fine", "이 대화에서 아직 답한 적이 없습니다."));
    else {
      right.appendChild(el("div", "ctx-bar", `<i class="${ctxLevel(p)}" style="width:${p ?? 0}%"></i>`));
      right.appendChild(el("div", "fine", (p == null ? `${fmtTok(c.used)} 토큰 (창 크기 확인 못 함)` : `${fmtTok(c.used)} / ${fmtTok(c.window)} 토큰 · ${p}%`) +
        (c.updated ? ` · ${fmtTime(c.updated)} 기준` : "")));
    }
    row.appendChild(right);
    if (a.engine === "claude" && c?.used) {
      const b = el("button", "btn gray", "압축");
      b.title = `${a.name} 세션을 요약·압축합니다 (/compact)`;
      b.disabled = S.running;
      b.onclick = () => { send({ type: "say", conv: S.conv, text: `@${a.name} /compact`, mode: S.mode, attachments: [] }); closeModal(); toast(`${a.name} 세션 압축을 요청했습니다`); };
      row.appendChild(b);
    }
    m.body.appendChild(row);
  });
  m.body.appendChild(el("div", "fine", "압축은 진행 중이 아닐 때만 됩니다. Codex 담당자는 가득 차면 스스로 압축합니다. 압축 뒤 수치는 그 담당자의 다음 답변 때 갱신됩니다."));
}

// ────────────────────────────────────────────────────────── 모달 공통
function modal(title, cls) {
  const root = $("modalRoot");
  root.innerHTML = "";
  const back = el("div", "modal-back");
  const sheet = el("div", "sheet" + (cls ? " " + cls : ""));
  const head = el("div", "sheet-head", `<h3>${esc(title)}</h3>`);
  const x = el("button", "icon-btn", ICON.close);
  x.onclick = closeModal;
  head.appendChild(x);
  const body = el("div", "sheet-body");
  const foot = el("div", "sheet-foot");
  sheet.append(head, body, foot);
  back.appendChild(sheet);
  back.addEventListener("mousedown", (e) => { if (e.target === back) closeModal(); });
  root.appendChild(back);
  return { body, foot, sheet };
}
function closeModal() { $("modalRoot").innerHTML = ""; S.settings = null; }

function formRow(label, control, hint, col) {
  const r = el("div", "form-row" + (col ? " col" : ""));
  const l = el("label", "", esc(label));
  r.appendChild(l);
  if (col && hint) r.appendChild(el("div", "hint", hint));
  r.appendChild(control);
  if (!col && hint) { const wrap = el("div", "", ""); wrap.style.flex = "1"; r.replaceChild(wrap, control); wrap.appendChild(control); wrap.appendChild(el("div", "hint", hint)); control.style.width = "100%"; }
  return r;
}
function input(value, type = "text", attrs = {}) {
  const i = el("input");
  i.type = type; i.value = value ?? "";
  Object.assign(i, attrs);
  return i;
}
function textarea(value, cls) { const t = el("textarea", cls || ""); t.value = value ?? ""; return t; }
function switchEl(checked) {
  const lab = el("label", "switch");
  const i = el("input"); i.type = "checkbox"; i.checked = !!checked;
  lab.append(i, el("span"));
  lab.input = i;
  return lab;
}

// ────────────────────────────────────────────────────────── 새 대화
function openNewConvModal() {
  const { body, foot } = modal("새 대화", "small");
  const pane = el("div", "set-pane pad");
  body.appendChild(pane);
  let kind = "new";
  const g = el("div", "form-group");
  const title = input("", "text", { placeholder: "비워두면 첫 메시지로 자동 지정" });
  g.appendChild(formRow("제목", title));
  pane.appendChild(g);

  pane.appendChild(el("div", "group-title", "작업 폴더"));
  const g2 = el("div", "form-group");
  const seg = el("div", "seg");
  const bNew = el("button", "on", "새 폴더 자동 생성"), bOld = el("button", "", "기존 폴더에서 작업");
  seg.append(bNew, bOld);
  const segRow = el("div", "form-row"); segRow.appendChild(seg); g2.appendChild(segRow);
  const pickRow = el("div", "form-row col hidden");
  const fp = el("div", "folder-pick");
  const path = input("", "text", { placeholder: "C:\\Users\\...\\프로젝트 폴더" });
  const browse = el("button", "btn gray", "찾아보기…");
  browse.onclick = async () => {
    browse.disabled = true; browse.textContent = "창 여는 중…";
    try { const d = await api("POST", "/api/pick-folder", { initial: path.value || null }); if (d.path) path.value = d.path; }
    catch (e) { toast(e.message); }
    browse.disabled = false; browse.textContent = "찾아보기…";
  };
  fp.append(path);
  if (S.local) fp.append(browse);  // 폴더 선택 창은 PC 화면에 뜨므로 폰에서는 경로를 직접 적거나 최근 폴더를 고른다
  pickRow.appendChild(fp);
  pickRow.appendChild(el("div", "hint", "선택한 폴더 안에서 Jake가 파일을 만들고 고칩니다. 폴더 밖 수정·삭제·설치 같은 명령은 채팅창에서 허락을 받습니다."));
  if (S.recent.length) {
    const rc = el("div", "recent");
    S.recent.forEach((p) => { const b = el("button", "", esc(p)); b.onclick = () => (path.value = p); rc.appendChild(b); });
    pickRow.appendChild(el("div", "hint", "최근 폴더"));
    pickRow.appendChild(rc);
  }
  g2.appendChild(pickRow);
  pane.appendChild(g2);
  bNew.onclick = () => { kind = "new"; bNew.classList.add("on"); bOld.classList.remove("on"); pickRow.classList.add("hidden"); };
  bOld.onclick = () => { kind = "old"; bOld.classList.add("on"); bNew.classList.remove("on"); pickRow.classList.remove("hidden"); };

  const g3 = el("div", "form-group");
  const sw = switchEl(true);
  api("GET", "/api/config").then((d) => { sw.input.checked = d.editable.settings.global_rules; }).catch(() => {});
  const r3 = el("div", "form-row", `<label>전역 규칙 적용</label><div class="hint" style="flex:1">${esc(userName())}의 전역 규칙(CLAUDE.md·AGENTS.md)·플러그인을 Claude 에이전트에 싣습니다. 끄면 역할 지침만 쓰고 더 빠르고 가볍습니다. 대화마다 고정됩니다.</div>`);
  r3.appendChild(sw);
  g3.appendChild(r3);
  pane.appendChild(g3);

  const codexKeys = (S.config.reviewers || []).filter((k) => agent(k).engine === "codex");
  let exSw = null;
  if (codexKeys.length) {
    const g4 = el("div", "form-group");
    exSw = switchEl(false);
    const names = codexKeys.map((k) => agent(k).name).join("·");
    const r4 = el("div", "form-row", `<label>${esc(names)} 제외</label><div class="hint" style="flex:1">켜면 이 대화의 자료를 Codex(OpenAI)로 보내지 않습니다. 검토는 Clara만 합니다. 대화 중에도 ⋯ 메뉴에서 바꿀 수 있습니다. (Jake·Clara 도 외부 AI(Anthropic)입니다)</div>`);
    r4.appendChild(exSw);
    g4.appendChild(r4);
    pane.appendChild(g4);
  }

  const cancel = el("button", "btn gray", "취소"); cancel.onclick = closeModal;
  const ok = el("button", "btn primary", "시작");
  ok.onclick = async () => {
    if (kind === "old" && !path.value.trim()) { toast("작업할 폴더를 골라 주세요"); return; }
    ok.disabled = true;
    try {
      const d = await api("POST", "/api/conversations", { title: title.value.trim() || null, workspace: kind === "old" ? path.value.trim() : null, global_rules: sw.input.checked, exclude: exSw?.input.checked ? codexKeys : [] });
      S.conversations = [d.summary, ...S.conversations.filter((c) => c.id !== d.id)];
      if (kind === "old" && !S.recent.includes(path.value.trim())) S.recent.unshift(path.value.trim());
      closeModal();
      openConv(d.id);
    } catch (e) { toast(e.message); ok.disabled = false; }
  };
  foot.append(cancel, ok);
  title.focus();
}

// ────────────────────────────────────────────────────────── 설정
async function openSettings(tab) {
  let d;
  try { d = await api("GET", "/api/config"); } catch (e) { toast(e.message); return; }
  await loadModels();
  const { body, foot } = modal("설정");
  S.settings = { data: d.editable, tab: tab || "general", body };
  const nav = el("div", "set-nav");
  const pane = el("div", "set-pane");
  body.append(nav, pane);
  S.settings.nav = nav; S.settings.pane = pane;
  renderSettingsNav();
  renderSettingsPane();
  const note = el("div", "note", "저장하면 이전 설정은 config/backups 에 백업됩니다. 이름·사진은 바로, 역할 지침은 새 대화부터 확실히 적용됩니다.");
  const cancel = el("button", "btn gray", "닫기"); cancel.onclick = closeModal;
  const save = el("button", "btn primary", "저장");
  save.onclick = async () => {
    const x = S.settings.data;
    const patch = { room_name: x.room_name, user_name: x.user_name, settings: x.settings, agents: {} };
    Object.entries(x.agents).forEach(([k, a]) => {
      patch.agents[k] = { name: a.name, title: a.title, color: a.color, model: a.model || "", effort: a.effort || "", prompt: a.prompt, checklist: a.checklist, not_my_job: a.not_my_job || "" };
    });
    save.disabled = true;
    try { await api("PUT", "/api/config", patch); toast("저장했습니다"); closeModal(); }
    catch (e) { toast(e.message, 5000); save.disabled = false; }
  };
  foot.append(note, cancel, save);
}

function renderSettingsNav() {
  const { nav, data } = S.settings;
  nav.innerHTML = "";
  const gen = el("div", "nav-item" + (S.settings.tab === "general" ? " active" : ""));
  gen.innerHTML = `<span class="nav-ico">${ICON.gear}</span><span class="txt">일반</span>`;
  gen.onclick = () => { S.settings.tab = "general"; renderSettingsNav(); renderSettingsPane(); };
  nav.appendChild(gen);
  const mob = el("div", "nav-item" + (S.settings.tab === "mobile" ? " active" : ""));
  mob.innerHTML = `<span class="nav-ico">${ICON_PHONE}</span><span class="txt">모바일·알림</span>`;
  mob.onclick = () => { S.settings.tab = "mobile"; renderSettingsNav(); renderSettingsPane(); };
  nav.appendChild(mob);
  Object.entries(data.agents).forEach(([k, a]) => {
    const it = el("div", "nav-item" + (S.settings.tab === k ? " active" : ""));
    it.appendChild(avatarEl({ ...agent(k), name: a.name }));
    it.appendChild(el("span", "txt", `${esc(a.name)}<div class="sub">${esc(a.title)}</div>`));
    it.onclick = () => { S.settings.tab = k; renderSettingsNav(); renderSettingsPane(); };
    nav.appendChild(it);
  });
}

const ICON_PHONE = '<svg viewBox="0 0 24 24"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg>';

// 설정 → 모바일·알림: 비밀번호(PC 에서만), 로그아웃, 이 기기 알림 켜기·시험
async function renderMobilePane(pane) {
  const [st, ps] = await Promise.all([api("GET", "/api/auth/status").catch(() => ({})), api("GET", "/api/push/status").catch(() => ({ devices: [] }))]);
  if (S.settings?.tab !== "mobile") return;
  pane.innerHTML = "";
  const rerender = () => renderMobilePane(pane);

  pane.appendChild(el("div", "group-title", "폰에서 접속"));
  const g0 = el("div", "form-group");
  g0.appendChild(el("div", "form-row", `<div class="hint" style="flex:1">폰에서는 <b>Tailscale</b>(내 기기끼리만 연결되는 사설망) 주소 <code>https://&lt;PC이름&gt;.&lt;tailnet&gt;.ts.net</code> 으로 엽니다. 인터넷에는 공개되지 않습니다.
    아이폰: Safari 로 열고 → 공유 → <b>홈 화면에 추가</b> → 그 아이콘으로 열면 앱처럼 쓰고 알림도 받을 수 있습니다(iOS 16.4 이상).
    ${S.local ? "이 PC 화면(127.0.0.1)은 로그인 없이 씁니다." : `이 기기는 로그인해서 쓰는 중입니다(${st.session_days || 30}일 유지).`}</div>`));
  pane.appendChild(g0);

  pane.appendChild(el("div", "group-title", "비밀번호"));
  const g1 = el("div", "form-group");
  if (S.local) {
    const pw1 = input("", "password", { placeholder: "8자 이상", autocomplete: "new-password" });
    const pw2 = input("", "password", { placeholder: "한 번 더", autocomplete: "new-password" });
    g1.appendChild(formRow(st.password_set ? "새 비밀번호" : "비밀번호 정하기", pw1, st.password_set ? "바꾸면 로그인해 둔 폰은 모두 다시 로그인해야 합니다." : "폰에서 들어올 때 쓰는 비밀번호입니다. PC 에는 원문을 남기지 않고 해시로만 저장합니다."));
    g1.appendChild(formRow("확인", pw2));
    const r = el("div", "form-row");
    const save = el("button", "btn primary", st.password_set ? "비밀번호 바꾸기" : "비밀번호 저장");
    save.onclick = async () => {
      if (pw1.value !== pw2.value) { toast("두 칸의 비밀번호가 다릅니다"); return; }
      save.disabled = true;
      try { await api("POST", "/api/auth/password", { password: pw1.value }); toast("비밀번호를 저장했습니다"); rerender(); }
      catch (e) { toast(e.message, 5000); save.disabled = false; }
    };
    r.appendChild(el("div", "hint", st.password_set ? "✓ 비밀번호가 정해져 있습니다" : "아직 없음 — 정하기 전에는 폰에서 들어올 수 없습니다"));
    r.firstChild.style.flex = "1";
    r.appendChild(save);
    g1.appendChild(r);
  } else {
    g1.appendChild(el("div", "form-row", '<div class="hint" style="flex:1">비밀번호는 PC 화면에서만 바꿀 수 있습니다.</div>'));
    const r = el("div", "form-row");
    const out = el("button", "btn gray", "이 기기 로그아웃");
    out.onclick = async () => { await api("POST", "/api/logout").catch(() => {}); location.reload(); };
    r.appendChild(out);
    g1.appendChild(r);
  }
  const r2 = el("div", "form-row");
  r2.appendChild(el("div", "hint", "폰을 잃어버렸거나 다른 사람이 로그인했을 수 있으면 누르세요. 모든 폰의 로그인이 끊깁니다."));
  r2.firstChild.style.flex = "1";
  const all = el("button", "btn danger", "모든 기기 로그아웃");
  all.onclick = async () => {
    try { await api("POST", "/api/auth/logout-all"); toast("모든 기기를 로그아웃했습니다"); if (!S.local) location.reload(); }
    catch (e) { toast(e.message); }
  };
  r2.appendChild(all);
  g1.appendChild(r2);
  pane.appendChild(g1);

  pane.appendChild(el("div", "group-title", "작업 완료 알림"));
  const g2 = el("div", "form-group");
  g2.appendChild(el("div", "form-row", `<div class="hint" style="flex:1">라운드가 끝나거나(완료·판단 필요·멈춤), 승인 카드가 뜨면 알립니다. 화면을 보고 있는 기기에는 보내지 않습니다.
    알림은 Apple·Google 푸시 서버를 거치므로(내용은 암호화) 문구에 대화 제목·고객사명·작업 내용을 넣지 않고 "Jake 작업 완료" 정도만 보냅니다.</div>`));
  const here = !!PUSH.endpoint && ps.devices.some((d) => d.endpoint === PUSH.endpoint);
  const r3 = el("div", "form-row");
  if (!pushSupported()) {
    r3.appendChild(el("div", "hint", S.local ? "이 PC 화면에서는 알림을 켜지 않아도 됩니다(폰에서 켜세요)." : "이 화면에서는 알림을 켤 수 없습니다. 아이폰은 Safari → 공유 → 홈 화면에 추가 → 그 아이콘으로 연 뒤 여기서 켜 주세요."));
  } else {
    r3.appendChild(el("div", "hint", here ? `✓ 이 기기(${esc(deviceLabel())})는 알림을 받습니다` : `이 기기(${esc(deviceLabel())})는 알림을 받지 않습니다`));
    const b = el("button", here ? "btn gray" : "btn primary", here ? "이 기기 알림 끄기" : "이 기기에서 알림 받기");
    b.onclick = async () => {
      b.disabled = true;
      try { if (here) await disablePush(); else { await enablePush(); toast("알림을 켰습니다. '시험 알림'으로 확인해 보세요."); } }
      catch (e) { toast(e.message, 6000); }
      rerender();
    };
    r3.appendChild(b);
    if (here) {
      const t = el("button", "btn gray", "시험 알림");
      t.onclick = async () => {
        t.disabled = true;
        try {
          const d = await api("POST", "/api/push/test", { endpoint: PUSH.endpoint });
          const r = (d.results || [])[0];
          toast(r?.ok ? "보냈습니다. 몇 초 안에 알림이 오면 정상입니다(앱을 보고 있으면 안 뜰 수 있음)." : `보내기 실패: ${r?.status || ""} ${r?.error || ""}`, 7000);
        } catch (e) { toast(e.message, 6000); }
        t.disabled = false;
      };
      r3.appendChild(t);
    }
  }
  r3.firstChild.style.flex = "1";
  g2.appendChild(r3);
  if (ps.devices.length) {
    const list = el("div", "form-row col");
    list.appendChild(el("label", "", `알림 받는 기기 ${ps.devices.length}대`));
    ps.devices.forEach((d) => {
      const row = el("div", "dev-row");
      row.appendChild(el("span", "", `${esc(d.label || "기기")}${d.endpoint === PUSH.endpoint ? " (이 기기)" : ""} · ${d.added ? fmtTime(d.added, true) : ""}`));
      const x = el("button", "archive-link", "빼기");
      x.onclick = async () => { await api("POST", "/api/push/unsubscribe", { endpoint: d.endpoint }).catch(() => {}); if (d.endpoint === PUSH.endpoint) await disablePush(); rerender(); };
      row.appendChild(x);
      list.appendChild(row);
    });
    g2.appendChild(list);
  }
  pane.appendChild(g2);
}

function refreshSettingsAvatars() { if (S.settings) { renderSettingsNav(); renderSettingsPane(); } }

function renderSettingsPane() {
  const { pane, data, tab } = S.settings;
  pane.innerHTML = "";
  if (tab === "mobile") { renderMobilePane(pane); return; }
  if (tab === "general") {
    pane.appendChild(el("div", "group-title", "기본"));
    const g = el("div", "form-group");
    const un = input(data.user_name); un.oninput = () => (data.user_name = un.value);
    const rn = input(data.room_name); rn.oninput = () => (data.room_name = rn.value);
    g.appendChild(formRow("내 호칭", un, "에이전트가 부르는 호칭입니다. 전역 규칙에 적힌 호칭보다 우선합니다."));
    g.appendChild(formRow("방 이름", rn));
    pane.appendChild(g);

    pane.appendChild(el("div", "group-title", "진행 방식"));
    const g2 = el("div", "form-group");
    const sw = switchEl(data.settings.global_rules);
    sw.input.onchange = () => (data.settings.global_rules = sw.input.checked);
    const r = el("div", "form-row", `<label>전역 규칙 적용</label><div class="hint" style="flex:1">켜면 Claude 에이전트가 전역 규칙(CLAUDE.md·AGENTS.md)·플러그인·훅을 함께 읽습니다(호출당 약 69k 토큰). 끄면 역할 지침만 씁니다. <b>새 대화부터</b> 적용됩니다.</div>`);
    r.appendChild(sw);
    g2.appendChild(r);
    const mr = input(data.settings.max_rounds, "number", { min: 1, max: 10 });
    mr.oninput = () => (data.settings.max_rounds = Number(mr.value));
    g2.appendChild(formRow("최대 라운드", mr, "두 감독이 이 횟수 안에 승인하지 않으면 쟁점을 정리해 넘깁니다."));
    const to = input(Math.round(data.settings.call_timeout_sec / 60), "number", { min: 1, max: 180 });
    to.oninput = () => (data.settings.call_timeout_sec = Number(to.value) * 60);
    g2.appendChild(formRow("호출 제한 시간(분)", to, "에이전트 한 번의 답변이 이 시간을 넘기면 중단합니다."));
    const sd = input(S.sendDelay, "number", { min: 0, max: 30 });
    sd.oninput = () => { S.sendDelay = Math.max(0, Math.min(30, Number(sd.value) || 0)); store.set("sendDelay", S.sendDelay); };
    g2.appendChild(formRow("보내기 전 대기(초)", sd, "보낸 뒤 이 시간 안에 Esc 를 누르면 입력창으로 되돌려 고칠 수 있습니다. 0이면 바로 보냅니다. 이 기기에만 저장되고 바로 적용됩니다."));
    pane.appendChild(g2);

    pane.appendChild(el("div", "group-title", "음성 입력 (입력창의 🎤)"));
    const g3 = el("div", "form-group");
    const eng = el("select");
    [["local", "이 PC 안에서 인식 (권장) — 음성이 밖으로 나가지 않음"], ["browser", "Chrome 음성 인식 — 빠르지만 음성이 Google 서버로 전송됨"]].forEach(([v, l]) => {
      const o = el("option", "", l); o.value = v; if ((data.settings.stt_engine || "local") === v) o.selected = true; eng.appendChild(o);
    });
    const mdl = el("select");
    [["small", "정확 (small) — 6초 말하면 약 10초"], ["base", "빠름 (base) — 6초 말하면 약 3초, 가끔 틀림"]].forEach(([v, l]) => {
      const o = el("option", "", l); o.value = v; if ((data.settings.stt_model || "small") === v) o.selected = true; mdl.appendChild(o);
    });
    const voc = textarea((data.settings.stt_vocab || []).join("\n"));
    const mdlRow = formRow("정확도", mdl, "PC 안에서 인식할 때만 적용. 처음 쓸 때 모델을 한 번 내려받습니다(small 약 480MB, base 약 145MB).");
    const vocRow = formRow("용어 힌트", voc, "한 줄에 하나. 자주 쓰는 회계 용어·고객사 약칭을 넣으면 덜 틀립니다(힌트가 없으면 '손익'을 '손이'로 알아듣는 등 실측 오인식이 있었음). 참여자 이름과 호칭은 자동으로 들어갑니다.", true);
    const syncRows = () => { const local = eng.value === "local"; mdlRow.style.display = local ? "" : "none"; vocRow.style.display = local ? "" : "none"; };
    eng.onchange = () => { data.settings.stt_engine = eng.value; syncRows(); };
    mdl.onchange = () => { data.settings.stt_model = mdl.value; };
    voc.oninput = () => { data.settings.stt_vocab = voc.value.split("\n"); };
    g3.appendChild(formRow("인식 방식", eng));
    g3.appendChild(mdlRow);
    g3.appendChild(vocRow);
    syncRows();
    pane.appendChild(g3);
    return;
  }

  const a = data.agents[tab];
  const pub = agent(tab);
  const top = el("div", "profile-top");
  const ava = avatarEl({ ...pub, name: a.name });
  top.appendChild(ava);
  const info = el("div", "");
  info.appendChild(el("div", "pt-name", esc(a.name)));
  info.appendChild(el("div", "pt-sub", `${esc(a.title)} · ${a.engine === "codex" ? "Codex CLI" : "Claude CLI"}`));
  const btns = el("div", "pt-btns");
  const up = el("button", "btn gray", "사진 올리기");
  const fileIn = el("input"); fileIn.type = "file"; fileIn.accept = "image/*"; fileIn.hidden = true;
  up.onclick = () => fileIn.click();
  fileIn.onchange = async () => {
    const f = fileIn.files[0]; if (!f) return;
    up.disabled = true; up.textContent = "올리는 중…";
    try { await api("POST", `/api/avatar/${tab}`, { name: f.name, data: await readAsDataURL(f) }); toast("사진을 바꿨습니다"); }
    catch (e) { toast(e.message); }
    up.disabled = false; up.textContent = "사진 올리기";
  };
  const gen = el("button", "btn gray", S.avatarJobs[tab] ? '<span class="spinner"></span> 만드는 중…' : "AI로 새로 만들기");
  gen.disabled = !!S.avatarJobs[tab];
  btns.append(up, fileIn, gen);
  info.appendChild(btns);
  const genBox = el("div", "gen-box hidden");
  const desc = input("", "text", { placeholder: "예: 30대 한국인 남성 개발자, 짧은 머리, 캐주얼 셔츠" });
  const go = el("button", "btn primary", "만들기");
  genBox.append(desc, go);
  gen.onclick = () => { genBox.classList.toggle("hidden"); desc.focus(); };
  go.onclick = async () => {
    if (!desc.value.trim()) { toast("인물 설명을 적어 주세요"); return; }
    try { await api("POST", `/api/avatar/${tab}/generate`, { description: desc.value.trim() }); S.avatarJobs[tab] = true; toast("Codex가 사진을 만드는 중입니다 (1~3분)"); renderSettingsPane(); }
    catch (e) { toast(e.message); }
  };
  info.appendChild(genBox);
  top.appendChild(info);
  pane.appendChild(top);

  pane.appendChild(el("div", "group-title", "프로필"));
  const g = el("div", "form-group");
  const nm = input(a.name); nm.oninput = () => { a.name = nm.value; };
  const ti = input(a.title); ti.oninput = () => { a.title = ti.value; };
  const co = input(a.color || "#8e8e93", "color"); co.oninput = () => { a.color = co.value; };
  g.appendChild(formRow("이름", nm));
  g.appendChild(formRow("직위", ti));
  g.appendChild(formRow("색", co, "사진이 없을 때 이니셜 배경색입니다."));
  pane.appendChild(g);

  pane.appendChild(el("div", "group-title", "모델"));
  const g2 = el("div", "form-group");
  const moWrap = el("div", ""); moWrap.style.flex = "1"; moWrap.style.display = "flex";
  const efWrap = el("div", ""); efWrap.style.flex = "1"; efWrap.style.display = "flex";
  const drawModel = () => {
    moWrap.innerHTML = ""; efWrap.innerHTML = "";
    const ms = modelSelect(a.engine, a.model || "", (v) => { a.model = v; a.effort = ""; drawModel(); });
    ms.style.flex = "1";
    moWrap.appendChild(ms);
    const es = effortSelect(a.engine, a.model || "", a.effort || "", (v) => { a.effort = v; });
    es.style.flex = "1";
    efWrap.appendChild(es);
  };
  drawModel();
  g2.appendChild(formRow("모델", moWrap, a.engine === "codex" ? "Codex 카탈로그(codex debug models)에서 고릅니다. 기본값은 ~/.codex/config.toml 설정입니다." : "진행 중인 대화에도 다음 답변부터 적용됩니다(맥락 유지)."));
  g2.appendChild(formRow("생각 깊이", efWrap, "높을수록 신중하지만 느리고 사용량이 늡니다."));
  pane.appendChild(g2);

  pane.appendChild(el("div", "group-title", "역할 지침"));
  const g3 = el("div", "form-group");
  const pr = textarea(a.prompt, "tall"); pr.oninput = () => { a.prompt = pr.value; };
  g3.appendChild(formRow("지침", pr, "{name} {workspace} {user} {checklist} {not_my_job} {dump_tool} 은 자동으로 바뀝니다.", true));
  if (a.checklist?.length || tab !== S.config.worker) {
    const cl = textarea((a.checklist || []).join("\n")); cl.oninput = () => { a.checklist = cl.value.split("\n"); };
    g3.appendChild(formRow("체크리스트", cl, "한 줄에 한 항목. 감독끼리 겹치지 않게 나눠 주세요.", true));
    const nj = input(a.not_my_job || ""); nj.oninput = () => { a.not_my_job = nj.value; };
    g3.appendChild(formRow("담당 외", nj, "이 감독이 지적하지 않을 영역", true));
  }
  pane.appendChild(g3);
}


// ────────────────────────────────────────────────────────── 음성 입력
// local: 녹음 → 서버(/api/stt)의 PC 안 Whisper 로 인식(음성이 PC 밖으로 안 나감)
// browser: Chrome 음성 인식(말하는 대로 바로 글자, 음성이 Google 서버로 전송됨)
const V = { mode: null, rec: null, stream: null, chunks: [], recog: null, started: 0, timer: null, busy: false };

function sttEngine() { return S.config?.stt?.engine === "browser" ? "browser" : "local"; }

function voiceChip(html) {
  let c = $("voiceChip");
  if (!html) { c?.remove(); return; }
  if (!c) { c = el("span", "chip voice-chip"); c.id = "voiceChip"; $("chips").prepend(c); }
  c.innerHTML = html;
}

function setMicUI(on) {
  $("btnMic").classList.toggle("rec", on);
  $("btnMic").title = on ? "다시 누르면 녹음을 끝내고 글자로 바꿉니다" :
    (sttEngine() === "browser" ? "음성 입력 (Chrome 음성 인식 — 음성이 Google 서버로 전송됨)" : "음성 입력 (이 PC 안에서 인식 — 음성이 밖으로 나가지 않음)");
}

function insertText(text) {
  const t = $("input");
  const sep = t.value && !/\s$/.test(t.value) ? " " : "";
  t.value = t.value + sep + text;
  autosize();
  t.focus();
  t.setSelectionRange(t.value.length, t.value.length);
}

async function toggleVoice() {
  if (V.busy) return;
  if (V.mode) return stopVoice();
  if (sttEngine() === "browser") return startBrowserVoice();
  return startLocalVoice();
}

async function startLocalVoice() {
  if (!navigator.mediaDevices?.getUserMedia) { toast("이 브라우저는 마이크 녹음을 지원하지 않습니다"); return; }
  try { V.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { toast("마이크를 쓸 수 없습니다. 브라우저 주소창의 마이크 권한을 허용해 주세요."); return; }
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"].find((m) => window.MediaRecorder?.isTypeSupported?.(m)) || "";
  V.rec = new MediaRecorder(V.stream, mime ? { mimeType: mime } : undefined);
  V.chunks = [];
  V.rec.ondataavailable = (e) => { if (e.data.size) V.chunks.push(e.data); };
  V.rec.onstop = finishLocalVoice;
  V.rec.start();
  V.mode = "local";
  V.started = Date.now();
  setMicUI(true);
  api("POST", "/api/stt/warmup").then((d) => { V.firstDownload = !d.downloaded; }).catch(() => {});  // 말하는 동안 모델 준비
  const tick = () => { const s = Math.floor((Date.now() - V.started) / 1000); voiceChip(`<span class="rec-dot"></span>녹음 중 ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} · 다시 누르면 글자로 바꿈`); };
  tick();
  V.timer = setInterval(tick, 500);
}

async function finishLocalVoice() {
  clearInterval(V.timer);
  V.stream?.getTracks().forEach((tr) => tr.stop());
  const blob = new Blob(V.chunks, { type: V.rec?.mimeType || "audio/webm" });
  V.mode = null; V.rec = null; V.stream = null;
  setMicUI(false);
  if (blob.size < 1000) { voiceChip(null); toast("녹음이 너무 짧습니다"); return; }
  V.busy = true;
  voiceChip(`<span class="spinner"></span>${V.firstDownload ? "처음 한 번 음성 모델(약 480MB)을 받는 중… 몇 분 걸릴 수 있습니다" : "이 PC 안에서 글자로 바꾸는 중…"}`);
  try {
    const d = await api("POST", "/api/stt", { data: await readAsDataURL(blob), mime: blob.type });
    if (d.text) insertText(d.text); else toast("알아들은 말이 없습니다. 조금 더 크게 말해 주세요.");
  } catch (e) { toast(e.message, 5000); }
  V.busy = false;
  voiceChip(null);
}

function startBrowserVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast("이 브라우저는 음성 인식을 지원하지 않습니다(Chrome 사용). 설정에서 'PC 안에서 인식'으로 바꿔 주세요."); return; }
  const t = $("input");
  const base = t.value && !/\s$/.test(t.value) ? t.value + " " : t.value;
  let finalText = "";
  V.recog = new SR();
  V.recog.lang = "ko-KR";
  V.recog.interimResults = true;
  V.recog.continuous = true;
  V.recog.onresult = (ev) => {
    let interim = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) finalText += r[0].transcript; else interim += r[0].transcript;
    }
    t.value = base + finalText + interim;
    autosize();
  };
  V.recog.onerror = (ev) => { if (ev.error !== "no-speech" && ev.error !== "aborted") toast(`음성 인식 오류: ${ev.error}`); };
  V.recog.onend = () => { V.mode = null; V.recog = null; setMicUI(false); voiceChip(null); t.focus(); };
  V.recog.start();
  V.mode = "browser";
  setMicUI(true);
  voiceChip('<span class="rec-dot"></span>듣는 중 (Chrome · Google 전송) · 다시 누르면 끝');
}

function stopVoice() {
  if (V.mode === "local" && V.rec && V.rec.state !== "inactive") V.rec.stop();
  else if (V.mode === "browser" && V.recog) V.recog.stop();
}

// ────────────────────────────────────────────────────────── 이벤트
function bind() {
  const inp = $("input");
  inp.addEventListener("input", () => { autosize(); mentionSel = 0; updateMention(); });
  inp.addEventListener("keydown", (e) => {
    const pop = $("mentionPop");
    if (!pop.classList.contains("hidden")) {
      const items = [...pop.querySelectorAll(".menu-item")];
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); mentionSel = (mentionSel + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length; updateMention(); return; }
      if ((e.key === "Enter" || e.key === "Tab") && items[mentionSel]) {
        e.preventDefault();
        if (items[mentionSel].dataset.cmd) pickSlash(items[mentionSel].dataset.cmd); else pickMention(items[mentionSel].dataset.name);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  $("btnSend").onclick = sendMessage;
  $("modeBtn").onclick = (e) => { e.stopPropagation(); toggleModeMenu(); };
  $("btnAttach").onclick = () => $("fileInput").click();
  $("btnMic").onclick = toggleVoice;
  $("fileInput").onchange = () => { attachFiles([...$("fileInput").files]); $("fileInput").value = ""; };
  $("btnStop").onclick = () => send({ type: "stop", conv: S.conv });
  $("btnFiles").onclick = () => toggleFiles();
  $("fpClose").onclick = () => toggleFiles(false);
  $("fpRefresh").onclick = loadFiles;
  $("fpOpen").onclick = () => S.conv && api("POST", `/api/conv/${S.conv}/open`, {}).catch((e) => toast(e.message));
  $("btnSettings").onclick = () => openSettings();
  $("btnLeave").onclick = () => S.conv && archiveConv(S.conv, !S.summary?.archived);
  $("btnTheme").onclick = cycleTheme;
  $("usageBox").onclick = openUsageModal;
  $("members").onclick = (e) => { e.stopPropagation(); toggleModelPop(); };
  $("btnSettingsSide").onclick = () => openSettings();
  $("btnCompose").onclick = openNewConvModal;
  $("btnBack").onclick = () => setLeftDrawer(true);
  $("drawerBack").onclick = closeDrawers;
  document.addEventListener("visibilitychange", sendPresence);
  window.matchMedia("(max-width: 860px)").addEventListener("change", () => { if (!narrow()) $("app").classList.remove("drawer-left"); syncDrawerBack(); syncPlaceholder(); });
  $("search").addEventListener("input", renderSidebar);
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#modeMenu") && e.target !== $("modeBtn")) $("modeMenu").classList.add("hidden");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("modalRoot").innerHTML && cancelHold()) { e.preventDefault(); return; }
    if ($("modalRoot").innerHTML) closeModal();
    closeDrawers();
    $("modeMenu").classList.add("hidden");
    $("mentionPop").classList.add("hidden");
  });
  // 끌어다 놓기로 첨부
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { dragDepth++; $("dropOverlay").classList.remove("hidden"); } });
  window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("dropOverlay").classList.add("hidden"); } });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; $("dropOverlay").classList.add("hidden");
    if (e.dataTransfer.files.length) attachFiles([...e.dataTransfer.files]);
  });
  // 파일 패널의 파일을 대화 쪽으로 끌어다 놓기
  const col = document.querySelector(".thread-col");
  const fromPanel = (e) => [...e.dataTransfer.types].includes(DRAG_PATH);
  col.addEventListener("dragover", (e) => { if (fromPanel(e)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; col.classList.add("drop-target"); } });
  col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("drop-target"); });
  col.addEventListener("drop", (e) => {
    col.classList.remove("drop-target");
    const path = e.dataTransfer.getData(DRAG_PATH);
    if (!path) return;
    e.preventDefault(); e.stopPropagation();
    attachWorkspaceFile(path);
  });
  window.addEventListener("beforeunload", flushHold);
  if (narrow()) S.filesOpen = false;  // 폰: 처음엔 대화부터 보이게
  syncPlaceholder();
  $("filesPanel").classList.toggle("hidden", !S.filesOpen);
}

bind();
watchViewport();
initServiceWorker();
applyTheme();
setMicUI(false);
renderUsage();
connect();
loadModels().then(() => { if (S.config) renderHeader(); });
