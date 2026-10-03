/* Review room — ONE surface, the Frame.io model:
   player with comment markers · one comment box tied to the paused frame · one thread
   where the factory's replies appear · a status pill that IS the verdict · version stack.
   Every send still writes the same bus receipts (frame-note / feedback / ruling). */

const D = JSON.parse(document.getElementById("row-data").textContent);
// Reels are cut on the reference clock, 24000/1001 (23.976).
const FPS = 24000 / 1001;

// ?walk=1 = the stranger's walk: every send is a plumbing test (ui-smoketest lane).
window.DECK_TEST = new URLSearchParams(location.search).get("walk") === "1";
if (window.DECK_TEST) { const b = document.getElementById("walk-banner"); if (b) b.hidden = false; }

const $ = (id) => document.getElementById(id);
const player = $("player");
let loaded = null;          // {name, sha256, version_label, path}
let sends = D.sends || [];  // the bus, as the server last read it
let sortMode = "timecode";
let lastSendAt = 0;
const batchState = {};

function mediaURL(path) { return "/media?p=" + encodeURIComponent(path); }
function timecode(t) {
  const m = Math.floor(t / 60);
  return `${m}:${(t % 60).toFixed(3).padStart(6, "0")}`;
}
function frameOf(t) { return Math.round(t * FPS); }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (isNaN(s)) return iso;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/* ---------------------------------------------------------------- player -- */
function loadFile(entry, label) {
  loaded = { name: entry.name, sha256: entry.sha256 || null, version_label: entry.version_label || label || null, path: entry.path };
  player.src = mediaURL(entry.path);
  $("fname").textContent = entry.name;
  document.querySelectorAll("#version-pills .pill, #btn-reference").forEach((p) => p.classList.toggle("on", p.dataset.path === entry.path));
  setPlayIcon();
}
function setPlayIcon() { $("btn-play").textContent = player.paused ? "▶" : "❚❚"; }
function seek(t) { player.pause(); player.currentTime = Math.max(0, Math.min(t, player.duration || t)); }
function togglePlay() { if (player.paused) player.play().catch(() => {}); else player.pause(); }

player.addEventListener("timeupdate", () => {
  const t = player.currentTime || 0;
  $("tc").textContent = `${timecode(t)} · f${frameOf(t)}`;
  $("tc-chip-label").textContent = timecode(t);
  if (player.duration) $("scrub").value = Math.round((t / player.duration) * 1000);
});
player.addEventListener("play", setPlayIcon); player.addEventListener("pause", setPlayIcon);
player.addEventListener("loadedmetadata", renderMarkers);
// no click-to-toggle on the video itself: native controls are on (iOS Safari), and a
// tap there already reaches the native play button — a second toggle would pause it again.
$("btn-play").addEventListener("click", togglePlay);
$("btn-prev").addEventListener("click", () => seek((player.currentTime || 0) - 1 / FPS));
$("btn-next").addEventListener("click", () => seek((player.currentTime || 0) + 1 / FPS));
$("btn-loop").addEventListener("click", (e) => { player.loop = !player.loop; e.currentTarget.setAttribute("aria-pressed", String(player.loop)); e.currentTarget.classList.toggle("on", player.loop); });
$("scrub").addEventListener("input", (e) => { if (player.duration) { player.pause(); player.currentTime = (e.target.value / 1000) * player.duration; } });
document.addEventListener("keydown", (e) => {
  const el = document.activeElement;
  if (e.key === "Escape") { $("status-menu").hidden = true; $("status-pill").setAttribute("aria-expanded", "false"); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;                 // Cmd+C must copy, not jump to the comment box
  if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT")) return;
  if (el && (el.tagName === "BUTTON" || el.tagName === "A" || el.tagName === "SUMMARY" || el.tagName === "SELECT")) return;  // Space/Enter belong to the focused control
  if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); seek((player.currentTime || 0) - 1 / FPS); }
  else if (e.key === "ArrowRight") { e.preventDefault(); seek((player.currentTime || 0) + 1 / FPS); }
  else if (e.key.toLowerCase() === "c") { e.preventDefault(); $("comment-text").focus(); }
});

/* ---------------------------------------------------------- version stack -- */
const pillsBox = $("version-pills");
if (pillsBox) {
  D.versions.forEach((entry) => {
    const pill = document.createElement("button");
    pill.className = "pill"; pill.dataset.path = entry.path;
    const tagged = entry.version_label && entry.version_label !== "untagged";
    const vmatch = /-(v\d{3}[a-z]?)(?:-[A-Z]+)?\.mp4$/i.exec(entry.name);   // v011c-TEST.mp4 → "V011C test"
    pill.textContent = tagged ? entry.version_label : (vmatch ? vmatch[1].toUpperCase() + (/-TEST\.mp4$/i.test(entry.name) ? " test" : "") : entry.name);
    pill.title = entry.name;
    pill.addEventListener("click", () => loadFile(entry));
    pillsBox.appendChild(pill);
  });
}
const referenceBtn = $("btn-reference");
if (referenceBtn) {
  referenceBtn.dataset.path = D.reference.path;
  referenceBtn.addEventListener("click", () => loadFile({ name: "reference-source.mp4", path: D.reference.path, version_label: "REFERENCE" }));
}
if (D.current) loadFile(D.current);
else if (D.batch.variants && D.batch.variants.length) loadFile(D.batch.variants[0]);
window.addEventListener("DOMContentLoaded", () => {
  if (!loaded) {   // nothing to pin a frame on: the chip must not claim one
    const chip = document.getElementById("tc-chip");
    if (chip) { chip.classList.remove("on"); chip.setAttribute("aria-pressed", "false"); chip.disabled = true; chip.title = "no cut loaded — this will be a general comment"; }
    window.__noCut = true;
  }
});

/* ------------------------------------------------------------- composer -- */
const commentBox = $("comment-text");
const tcChip = $("tc-chip");
let tcOn = true;
if (!loaded) tcOn = false;
tcChip.addEventListener("click", () => { tcOn = !tcOn; tcChip.classList.toggle("on", tcOn); tcChip.setAttribute("aria-pressed", String(tcOn)); });
// Typing does NOT pause the video. The note pins the frame showing when you press Send.
commentBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendComment(); } });
$("comment-images").addEventListener("change", (e) => { const n = e.target.files.length; $("attach-count").textContent = n ? String(n) : ""; });
$("btn-send-comment").addEventListener("click", sendComment);

function flashSent(button) {
  // The status pill has children (dot + label): setting button.textContent DELETED them, and the
  // next status change threw on a null label. Flash a label node only.
  const label = button.querySelector("[data-flash-label]") || button;
  if (label._flashTimer) clearTimeout(label._flashTimer);
  if (label._flashRestore === undefined) label._flashRestore = null;
  const restore = label.textContent;
  if (!label._flashing) label._flashRestore = restore;
  label._flashing = true;
  lastSendAt = Date.now();
  button.classList.add("sent"); label.textContent = "SENT ✓";
  label._flashTimer = setTimeout(() => {
    button.classList.remove("sent");
    // the pill's label may have been re-set (setStatus) while flashing: restore what is CURRENT
    label.textContent = label.dataset.current || label._flashRestore;
    label._flashing = false; label._flashTimer = null;
  }, 2200);
}
const repliedCalls = new Set();   // factory questions answered this page-load (thread re-renders keep SENT ✓)
let answerCall = null, answerDismissed = false;
const PLAIN_PLACEHOLDER = commentBox.placeholder;
function setAnswering(call) {
  answerCall = call || null;
  const bar = $("answering"); if (bar) bar.hidden = !answerCall;
  $("composer").classList.toggle("answering", !!answerCall);
  commentBox.placeholder = answerCall
    ? "Answer the factory's question above in plain words (Enter to send). Click ✕ to leave a normal comment instead."
    : PLAIN_PLACEHOLDER;
}
$("answering-off").addEventListener("click", () => { answerDismissed = true; setAnswering(null); commentBox.focus(); });

async function sendComment() {
  const text = commentBox.value.trim();
  const files = $("comment-images").files;
  const btn = $("btn-send-comment");
  if (!text && !files.length) { commentBox.focus(); return notice("Write the comment first.", true); }
  // immediate feedback: under load the POST can take a second or two
  btn.dataset.current = "Send";
  btn.disabled = true; btn.textContent = "sending…";
  try {
    let result;
    if (answerCall) {
      // answering the factory's open question → a ruling on that call (at the paused frame if pinned)
      const at = tcOn && loaded ? ` (at ${timecode(player.currentTime || 0)}, f${frameOf(player.currentTime || 0)})` : "";
      const call = answerCall;
      result = await postJSON("/api/verdict", { row: D.seq, disposition: "NOTES", text: "RULING on " + call + ": " + text + at, watched: loaded, test: window.DECK_TEST === true });
      repliedCalls.add(call);
      optimistic({ kind: "feedback", disposition: "NOTES", text, answers_call: window.DECK_TEST ? null : call });
      setAnswering(null);
    } else if (tcOn && loaded) {
      // a comment on a moment → frame note (with the exact frame)
      const t = player.currentTime || 0;
      const form = new FormData();
      form.append("row", D.seq); form.append("file", loaded.name);
      if (loaded.sha256) form.append("sha256", loaded.sha256);
      form.append("frame", frameOf(t)); form.append("timecode", timecode(t));
      form.append("text", text);
      if (window.DECK_TEST) form.append("test", "1");
      for (const f of files) form.append("images", f);
      result = await api("/api/frame-note", { method: "POST", body: form });
      optimistic({ kind: "frame-note", text, timecode_s: t, frame: `f${frameOf(t)} @ ${timecode(t)}` });
    } else {
      // a general comment → notes on the cut
      result = await postJSON("/api/verdict", { row: D.seq, disposition: "NOTES", text, watched: loaded, test: window.DECK_TEST === true });
      optimistic({ kind: "feedback", disposition: "NOTES", text });
    }
    commentBox.value = ""; $("comment-images").value = ""; $("attach-count").textContent = "";
    btn.disabled = false; flashSent(btn);
    toast(`Sent to the factory (${result.receipt})`);
    setTimeout(refreshSends, 1500);
  } catch (error) { btn.disabled = false; btn.textContent = "Send"; notice(error.message || "The comment did not send.", true); }
}

/* -------------------------------------------------------- status = verdict -- */
const STATUS_LABEL = { needs_review: "Needs review", in_progress: "In progress", needs_changes: "Needs changes", approved: "Approved" };
const statusKey = `deck.status.${D.seq}.${(D.current && D.current.name) || "none"}`;
function setStatus(k, persist = true) {
  const lab = $("status-label");
  lab.dataset.current = STATUS_LABEL[k] || k;
  if (!lab._flashing) lab.textContent = lab.dataset.current;
  const dot = $("status-pill").querySelector(".dot"); dot.className = "dot s-" + k;
  $("status-pill").dataset.status = k;
  if (persist) { try { localStorage.setItem(statusKey, k); } catch (e) {} }
}
function verdictFromBus() {
  // newest real APPROVE / REJECT receipt for this row decides; local choices only fill the gap
  const v = (sends || []).find((s) => !s.test && s.kind === "feedback" && (s.disposition === "APPROVE" || s.disposition === "REJECT"));
  return v ? (v.disposition === "APPROVE" ? "approved" : "needs_changes") : null;
}
function seedStatus() {
  let local = null; try { local = localStorage.getItem(statusKey); } catch (e) {}
  const bus = verdictFromBus();
  // a bus verdict wins over a local "needs review/in progress"; a local note-to-self wins only when there is no verdict
  setStatus(bus || ((local === "in_progress" || local === "needs_review") ? local : "needs_review"), false);
}
seedStatus();
$("status-pill").addEventListener("click", () => {
  const open = $("status-menu").hidden; $("status-menu").hidden = !open; $("status-pill").setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (e) => { if (!e.target.closest("#status")) { $("status-menu").hidden = true; $("status-pill").setAttribute("aria-expanded", "false"); } });
document.querySelectorAll("#status-menu [data-status]").forEach((opt) => {
  opt.addEventListener("click", async () => {
    const k = opt.dataset.status; $("status-menu").hidden = true;
    if (k === "needs_review" || k === "in_progress") { setStatus(k); return; }   // yours to track; nothing to build
    const text = commentBox.value.trim();
    if (k === "needs_changes" && !text && !myCommentsOnThisCut().length) {
      commentBox.focus();
      return notice("Say what needs to change — one comment in the box (or a frame note first). The factory rebuilds from your words.", true);
    }
    const disposition = k === "approved" ? "APPROVE" : "REJECT";
    const pill = $("status-pill"); pill.disabled = true;
    try {
      const result = await postJSON("/api/verdict", { row: D.seq, disposition, text, watched: loaded, test: window.DECK_TEST === true });
      commentBox.value = "";
      setStatus(k); pill.disabled = false; flashSent(pill);
      optimistic({ kind: "feedback", disposition, text });
      toast(`${STATUS_LABEL[k]} sent to the factory (${result.receipt})`);
      setTimeout(refreshSends, 1500);
    } catch (error) { pill.disabled = false; notice(error.message || "The verdict did not send.", true); }
  });
});
function myCommentsOnThisCut() {
  // in test mode the walk's own test comments count, so the flow can be rehearsed end to end
  return sends.filter((s) => (window.DECK_TEST ? true : !s.test) && (s.kind === "frame-note" || (s.kind === "feedback" && s.disposition === "NOTES")));
}

/* --------------------------------------------------------------- thread -- */
// Optimistic: the comment appears the moment you press Send; the poll replaces it.
function optimistic(partial) {
  sends.unshift({ name: "pending-" + Date.now(), state: "waiting", sent_by: window.DECK_USER || "you", sent_at_utc: new Date().toISOString(),
                  test: window.DECK_TEST === true, ack_lines: [], acks: [], ...partial });
  renderThread();
}

const STATE_LABEL = { received: "received by factory", working: "factory is on it", call: "factory needs your call", waiting: "waiting for the factory", no_answer: "no answer" };
const STATE_ICON = { received: "✓", working: "⚙", call: "?", waiting: "○", no_answer: "!" };

function threadItems() {
  const items = [];
  for (const s of sends) {
    const t = typeof s.timecode_s === "number" ? s.timecode_s : null;
    items.push({ id: s.name, who: s.sent_by || "you", role: s.role, at: s.sent_at_utc, t, text: s.text_full || s.text || "",
                 kind: s.kind, disposition: s.disposition, state: s.state, test: s.test, finished: s.finished,
                 answers_call: s.answers_call, replies: (s.ack_lines || []).map(parseAck) });
  }
  // the factory's open questions become comments with a reply box
  const answered = new Set(sends.filter((s) => s.answers_call && !s.test).map((s) => s.answers_call));
  if (D.question && !answered.has(D.question.receipt)) {
    items.push({ id: "q-" + D.question.receipt, who: "factory", factory: true, call: D.question.receipt, at: null, t: null, text: D.question.text });
  }
  for (const c of (D.calls || [])) {
    if (answered.has(c.call_id)) continue;
    items.push({ id: "c-" + c.call_id, who: "factory", factory: true, call: c.call_id, at: null, t: null, text: c.one_liner, severity: c.severity });
  }
  return items;
}
function parseAck(line) {
  const m = /^(ACK|CALL)\s+—\s+(.+?)\s+—\s+(\S+)\s+—\s+(.*)$/.exec(line);
  return m ? { kind: m[1], lane: m[2], at: m[3], text: m[4] } : { kind: "ACK", lane: "factory", at: "", text: line };
}
function sortItems(items) {
  const byTime = (a, b) => Date.parse(b.at || 0) - Date.parse(a.at || 0);
  if (sortMode === "newest") return items.slice().sort((a, b) => (a.factory ? -1 : b.factory ? 1 : byTime(a, b)));
  // by timecode: factory questions first, then timed comments in reel order, then general ones newest-first
  return items.slice().sort((a, b) => {
    if (a.factory !== b.factory) return a.factory ? -1 : 1;
    if ((a.t === null) !== (b.t === null)) return a.t === null ? 1 : -1;
    if (a.t !== null && b.t !== null && a.t !== b.t) return a.t - b.t;
    return byTime(a, b);
  });
}
function renderThread() {
  const box = $("thread"); if (!box) return;
  // a 20-s poll re-render wiped a half-typed reply and dropped focus (UI audit HARD 3): carry them across
  const typed = {}; let focusCall = null, selStart = 0, selEnd = 0;
  box.querySelectorAll(".comment.factory").forEach((el) => {
    const ta = el.querySelector(".reply-text"); if (!ta) return;
    typed[el.dataset.call] = ta.value;
    if (document.activeElement === ta) { focusCall = el.dataset.call; selStart = ta.selectionStart; selEnd = ta.selectionEnd; }
  });
  window.__threadKeep = { typed, focusCall, selStart, selEnd };
  const items = sortItems(threadItems());
  $("thread-count").textContent = items.length ? String(items.length) : "";
  if (!items.length) {
    box.innerHTML = '<div class="empty">No comments yet. Pause on the frame, write it like chat, press Enter. It shows up here with what the factory did with it.</div>';
    return;
  }
  box.innerHTML = items.map((it) => {
    if (it.factory) {
      return `<div class="comment factory" data-call="${esc(it.call)}">
        <div class="c-head"><span class="avatar f">F</span><b>factory</b><span class="c-meta">asks you${it.severity ? " · " + esc(it.severity) : ""}</span></div>
        <div class="c-text"></div>
        <div class="reply-box">${repliedCalls.has(it.call) ? '<span class="stamp green">answered ✓</span>' : `<button class="btn small answer-here" data-call="${esc(it.call)}">Answer in the comment box ↓</button>`}</div>
      </div>`;
    }
    const tcChip = it.t !== null ? `<button class="tcchip" data-t="${it.t}">⏱ ${timecode(it.t)}</button>` : "";
    const disp = it.disposition && it.disposition !== "NOTES" ? `<span class="stamp ${it.disposition === "APPROVE" ? "green" : it.disposition === "REJECT" ? "red" : "grey"}">${it.disposition === "APPROVE" ? "approved" : it.disposition === "REJECT" ? "needs changes" : esc(it.disposition.toLowerCase())}</span>` : "";
    const ruling = it.answers_call ? `<span class="stamp amber">answer</span>` : "";
    const state = `<span class="state ${it.state}" title="${STATE_LABEL[it.state] || it.state}">${it.finished ? "✓✓" : STATE_ICON[it.state] || ""} ${it.test ? "test" : (it.state === "received" ? "received" : it.state === "working" ? "factory is on it" : it.state === "call" ? "needs your call" : it.state === "waiting" ? "waiting" : "no answer")}</span>`;
    const replies = (it.replies || []).map((r) => `<div class="reply ${r.kind === "CALL" ? "call" : ""}"><span class="avatar f">F</span><div><b>factory</b> <span class="c-meta">${esc(r.lane)} · ${esc(r.at)}</span><div class="r-text"></div></div></div>`).join("");
    return `<div class="comment mine ${it.state}" data-id="${esc(it.id)}">
      <div class="c-head"><span class="avatar">${esc((it.who || "?")[0].toUpperCase())}</span><b>${esc(it.who)}</b>${it.role === "EDITOR" ? '<span class="c-meta">editor</span>' : ""}<span class="c-meta">${esc(ago(it.at))}</span>${tcChip}${disp}${ruling}${state}</div>
      <div class="c-text"></div>
      ${replies}
    </div>`;
  }).join("");
  // user + factory text is data: textContent only
  const nodes = Array.from(box.querySelectorAll(".comment"));
  nodes.forEach((el, i) => {
    const it = items[i];
    el.querySelector(".c-text").textContent = it.text || "";
    const rs = el.querySelectorAll(".reply .r-text");
    (it.replies || []).forEach((r, j) => { if (rs[j]) rs[j].textContent = r.text; });
  });
  const keep = window.__threadKeep || { typed: {} };
  box.querySelectorAll(".tcchip").forEach((b) => b.addEventListener("click", () => seek(parseFloat(b.dataset.t))));
  box.querySelectorAll(".answer-here").forEach((btn) => btn.addEventListener("click", () => { answerDismissed = false; setAnswering(btn.dataset.call); commentBox.focus(); }));
  // ONE comment box (two places to add comments confused reviewers): while the
  // factory has an open question, the box under the video answers it; ✕ turns it back into a note.
  const open = items.find((it) => it.factory && !repliedCalls.has(it.call));
  setAnswering(open && !answerDismissed ? open.call : null);
  renderMarkers();
}
async function sendReply(btn) {
  const box = btn.closest(".reply-box").querySelector(".reply-text");
  const text = box.value.trim();
  if (!text) { box.focus(); return notice("Type your reply first.", true); }
  const original = btn.textContent; btn.disabled = true; btn.textContent = "sending…";
  try {
    const result = await postJSON("/api/verdict", { row: D.seq, disposition: "NOTES", text: "RULING on " + btn.dataset.call + ": " + text, test: window.DECK_TEST === true });
    btn.textContent = "SENT ✓"; btn.classList.add("sent"); lastSendAt = Date.now();
    repliedCalls.add(btn.dataset.call);
    optimistic({ kind: "feedback", disposition: "NOTES", text, answers_call: window.DECK_TEST ? null : btn.dataset.call });
    toast(`Reply sent to the factory (${result.receipt})`);
    setTimeout(refreshSends, 1500);
  } catch (e) { btn.textContent = original; btn.disabled = false; notice(e.message || "The reply did not send.", true); }
}
$("sort-thread").addEventListener("click", (e) => {
  sortMode = sortMode === "timecode" ? "newest" : "timecode";
  e.currentTarget.textContent = sortMode === "timecode" ? "by timecode" : "newest first";
  renderThread();
});
function renderMarkers() {
  const m = $("markers"); if (!m || !player.duration) return;
  m.innerHTML = "";
  for (const s of sends) {
    if (typeof s.timecode_s !== "number") continue;
    const d = document.createElement("button");
    d.className = "marker" + (s.state === "received" ? " received" : s.state === "working" ? " working" : "");
    d.style.left = `${Math.min(100, (s.timecode_s / player.duration) * 100)}%`;
    d.title = `${timecode(s.timecode_s)} — ${(s.text || "").slice(0, 80)}`;
    d.addEventListener("click", () => seek(s.timecode_s));
    m.appendChild(d);
  }
}

/* ---- the bus: poll so "waiting" becomes "received" without a reload ---- */
function renderFactoryLine(f) {
  const el = $("sends-factory"); if (!el) return;
  el.textContent = f && f.awake ? `factory ${f.state} · heartbeat ${Math.round(f.age_s || 0)}s ago`
                                : `factory has not checked in${f && f.age_s ? " for " + Math.round(f.age_s / 60) + " min" : ""} — comments will wait`;
  el.classList.toggle("down", !(f && f.awake));
}
async function refreshSends() {
  try {
    const payload = await api(`/api/row/${D.seq}/sends`);
    sends = payload.sends || [];
    renderFactoryLine(payload.factory);
    renderThread();
    if (!$("status-label")._flashing) seedStatus();
  } catch (e) { /* the page copy stays */ }
}
window.refreshSends = refreshSends;
renderFactoryLine(D.factory);
renderThread();
setInterval(refreshSends, 20000);

/* ------------------------------------------------------- trial variants -- */
const grid = $("variant-grid");
if (grid) {
  D.batch.variants.forEach((variant) => {
    const rowEl = document.createElement("div");
    rowEl.className = "variant"; rowEl.id = `variant-${variant.variant}`;
    const shortName = variant.name.replace(/^\d+\s*—\s*TRIAL\s+VARIANTS\s+BATCH\s+\d+\s*—\s*/i, "").replace(/^\d+\s+v\d+\s*—\s*/i, "");
    rowEl.innerHTML = `<div class="vnum">${String(variant.variant).padStart(2, "0")}</div>
      <div class="vname" title="${esc(variant.name)}">${esc(shortName)}</div>
      <div class="vbtns"><button class="btn small" data-watch="1">▶ Watch</button><button class="btn small good" data-v="KEEP">Keep</button><button class="btn small bad" data-v="KILL">Kill</button></div>
      <textarea class="vnote" rows="1" placeholder="what to change on variant ${String(variant.variant).padStart(2, "0")} (optional)"></textarea>`;
    const watch = () => {
      loadFile(variant);
      grid.querySelectorAll(".variant").forEach((el) => el.classList.toggle("playing", el === rowEl));
      const stage = document.querySelector("video"); if (stage) stage.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    rowEl.querySelector(".vname").addEventListener("click", watch);
    rowEl.querySelector("[data-watch]").addEventListener("click", watch);
    rowEl.querySelector(".vnum").addEventListener("click", watch);
    rowEl.querySelectorAll(".vbtns .btn[data-v]").forEach((button) => {
      button.addEventListener("click", () => {
        const verdict = button.dataset.v;
        batchState[variant.variant] = batchState[variant.variant] === verdict ? undefined : verdict;
        rowEl.classList.toggle("keep", batchState[variant.variant] === "KEEP");
        rowEl.classList.toggle("kill", batchState[variant.variant] === "KILL");
        rowEl.querySelectorAll(".vbtns .btn[data-v]").forEach((b) => b.classList.toggle("on", batchState[variant.variant] === b.dataset.v));
      });
    });
    grid.appendChild(rowEl);
  });
  // planned variants that are not rendered yet: show their slots so the batch reads "4 of 10" at a glance
  const have = new Set(D.batch.variants.map((v) => v.variant));
  for (let n = 1; n <= (D.batch.expected || 0); n += 1) {
    if (have.has(n)) continue;
    const ghost = document.createElement("div");
    ghost.className = "variant pending";
    ghost.innerHTML = `<div class="vnum">${String(n).padStart(2, "0")}</div><div class="vname">not rendered yet — it appears here when the lane finishes it</div><div></div>`;
    grid.appendChild(ghost);
  }
  $("btn-send-batch").addEventListener("click", async () => {
    const noteOf = (v) => { const el = document.querySelector(`#variant-${v.variant} .vnote`); return el ? el.value.trim() : ""; };
    const items = D.batch.variants.filter((v) => batchState[v.variant] || noteOf(v))
      .map((v) => ({ variant: v.variant, name: v.name, path: v.path, sha256: v.sha256 || null, verdict: batchState[v.variant] || "NOTES", note: noteOf(v) }));
    const batchNote = ($("batch-note") && $("batch-note").value.trim()) || "";
    if (!items.length && !batchNote) return toast("Mark a variant Keep or Kill, or type a note, first.", true);
    if (!items.length) items.push({ variant: D.batch.variants[0].variant, name: D.batch.variants[0].name, verdict: "NOTES", note: "(see the batch note)" });
    const batchBtn = $("btn-send-batch"); batchBtn.disabled = true;
    try {
      const result = await postJSON("/api/batch-verdict", { row: D.seq, items, note: batchNote });
      toast(`Sent to the factory (${result.receipt})`); batchBtn.disabled = false; flashSent(batchBtn);
      document.querySelectorAll(".vnote").forEach((el) => { el.value = ""; }); if ($("batch-note")) $("batch-note").value = "";
    }
    catch (error) { batchBtn.disabled = false; toast(error.message, true); }
  });
}

/* Keep the page fresh WITHOUT ever wiping what you're typing. */
(function () {
  function busyTyping() {
    const el = document.activeElement;
    if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT")) return true;
    return Array.prototype.some.call(document.querySelectorAll("textarea"), (t) => t.value.trim().length > 0);
  }
  setTimeout(function refresh() {
    if (document.hidden || (player && !player.paused && !player.ended) || busyTyping() ||
        (Date.now() - lastSendAt) < 30000 || document.querySelector(".sent")) { setTimeout(refresh, 15000); return; }
    location.reload();
  }, 60000);
})();
