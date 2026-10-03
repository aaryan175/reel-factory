/* Reel Deck — clip grader, live playhead over the segment grid.
   A clip is cut into ~5 s pieces by the Deck (server/grader.py segments(); the kit
   uses the same grid). The clip PLAYS THROUGH (1.5x by default) from its first
   ungraded piece; 1/2/3 grade the SECTION under the playhead and jump straight to
   the next section (holding the key repeats = blasts through sections); Space skips
   a section ungraded; S / → move to the next video. Untouched sections stay ungraded — that is a valid outcome.
   Every grade POSTs to the Deck and is on disk before it says saved.
   Nothing lives in this browser except the last clip and the speed (conveniences, never the grades). */

(function () {
  const $ = (id) => document.getElementById(id);
  const video = $("g-video"), next = $("g-next"), timeline = $("g-timeline"), head = $("g-head");
  const RATES = [1, 1.5, 2, 3, 4];
  let items = [], ci = 0, rate = 1.5;
  let hold = null;          // {grade, done: Set of piece indexes graded during this hold}
  const pending = new Set(); // "ci:si" in flight, so a hold never double-posts a piece

  const FACE = {
    PERSON: ["FACE", "green", ""],
    FAINT: ["FACE faint", "amber", "A small or partial face in some frames"],
    EMPTY: ["NO FACE", "grey", "No face found — may still be the featured person from behind or far"],
    UNSCANNED: ["NOT SCANNED", "grey", "The face scan has not run on this clip"],
    NO_PROXY: ["NO PROXY", "red", "No playable copy of this clip yet"],
  };
  const IDENTITY = {
    settled: ["SETTLED · me", "green"], not_operator: ["RULED · not me", "red"],
    blacklisted: ["BLACKLISTED", "red"], unruled: ["UNRULED", "grey"],
  };
  const GRADE_LABEL = { HERO: ["HERO", "green"], BROLL: ["B-ROLL", "blue"], NEVER: ["NEVER", "red"] };
  const IDENT_LABEL = { ME: ["ME", "amber"], NOTME: ["NOT ME", "grey"] };
  const CELL = { HERO: "hero", BROLL: "broll", NEVER: "never" };

  try { const r = parseFloat(localStorage.getItem("deck.grader.rate")); if (RATES.includes(r)) rate = r; } catch (e) {}

  function stamp(text, color, title) {
    const el = document.createElement("span");
    el.className = "stamp " + color; el.textContent = text;
    if (title) el.title = title;
    return el;
  }
  function secs(t) { return String(Number(t.toFixed(2))); }

  let savedCount = 0, savedLast = "";
  function drawDisk() {
    const el = $("g-disk");
    if (!el) return;
    el.textContent = savedCount
      ? `on disk this session: ${savedCount} grade${savedCount === 1 ? "" : "s"} · last ${savedLast}`
      : "on disk this session: nothing yet — every tap is written to grades.jsonl before it says saved";
  }
  function disk(n, what) {
    savedCount += n;
    savedLast = `${what} ✓ ${new Date().toTimeString().slice(0, 8)}`;
    drawDisk();
  }
  function saved(ok, message) {
    const el = $("g-saved");
    el.className = "g-saved " + (ok ? "ok" : "err");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(el._timer);
    if (ok) el._timer = setTimeout(() => { el.hidden = true; }, 1400);
  }

  // pieces of a clip: [{si, key, t0, t1}]; a clip with no grid is one whole-clip piece (key null)
  function pieces(item) {
    const segs = item.segments || [], keys = item.segment_keys || [];
    if (!segs.length) return [{ si: 0, key: null, t0: 0, t1: item.dur || 0 }];
    return segs.map((s, si) => ({ si, key: keys[si], t0: s[0], t1: s[1] }));
  }
  // the grade that stands for a piece: its own segment grade, else the whole-clip grade
  function pieceGrade(item, p) { return (p.key && item.grades[p.key]) || item.whole || null; }
  function firstUngraded(item) { return pieces(item).findIndex((p) => !pieceGrade(item, p)); }
  function clipOpen(item) { return !item.whole && firstUngraded(item) >= 0; }

  function pieceAt(item, t) {
    const ps = pieces(item);
    const at = ps.findIndex((p) => t >= p.t0 && t < p.t1);
    return at >= 0 ? at : (t >= ps[ps.length - 1].t1 - 0.001 ? ps.length - 1 : 0);
  }
  function current() { const item = items[ci]; return item ? pieces(item)[pieceAt(item, video.currentTime || 0)] : null; }

  function remember() { try { localStorage.setItem("deck.grader.pos", items[ci] ? items[ci].stem : ""); } catch (e) {} }

  // --------------------------------------------------------------- drawing

  function drawCounts() {
    let total = 0, done = 0;
    items.forEach((item) => pieces(item).forEach((p) => { total += 1; if (pieceGrade(item, p)) done += 1; }));
    $("g-count").textContent = items.length
      ? `clip ${ci + 1} / ${items.length} · pieces graded ${done} / ${total}`
      : "the library is empty";
    $("g-bar").style.width = total ? (100 * done / total).toFixed(1) + "%" : "0";
  }

  function drawTimeline() {
    const item = items[ci];
    timeline.querySelectorAll(".g-cell").forEach((c) => c.remove());
    if (!item) return;
    const ps = pieces(item), dur = ps[ps.length - 1].t1 || 1;
    ps.forEach((p) => {
      const cell = document.createElement("button");
      cell.type = "button";
      const grade = pieceGrade(item, p);
      cell.className = "g-cell " + (grade ? CELL[grade] : "none") + (p.key && item.grades[p.key] ? "" : grade ? " via-whole" : "");
      cell.style.flexGrow = String(Math.max(0.01, (p.t1 - p.t0) / dur));
      cell.title = `part ${p.si + 1}/${ps.length} · ${secs(p.t0)}–${secs(p.t1)} s` + (grade ? " · " + GRADE_LABEL[grade][0] : " · ungraded");
      cell.setAttribute("aria-label", cell.title);
      cell.addEventListener("click", () => { try { video.currentTime = p.t0; } catch (e) {} video.play().catch(() => {}); tick(); });
      timeline.insertBefore(cell, head);
    });
  }

  function drawCell(si) {
    const item = items[ci], cell = timeline.querySelectorAll(".g-cell")[si];
    if (!item || !cell) return;
    const p = pieces(item)[si], grade = pieceGrade(item, p);
    cell.className = "g-cell " + (grade ? CELL[grade] : "none") + (p.key && item.grades[p.key] ? "" : grade ? " via-whole" : "");
  }

  let lastPiece = -1;
  function tick() {
    const item = items[ci];
    if (!item) return;
    const ps = pieces(item), dur = ps[ps.length - 1].t1 || 1, t = video.currentTime || 0;
    head.style.left = Math.min(100, 100 * t / dur).toFixed(2) + "%";
    const si = pieceAt(item, t);
    if (si !== lastPiece) {
      lastPiece = si;
      const p = ps[si];
      $("g-stem").textContent = p.key
        ? `${item.stem} · part ${si + 1}/${ps.length} · ${secs(p.t0)}–${secs(p.t1)} s`
        : `${item.stem} · whole clip`;
      const grade = pieceGrade(item, p);
      document.querySelectorAll("[data-grade]").forEach((b) => b.classList.toggle("on", b.dataset.grade === grade));
      timeline.querySelectorAll(".g-cell").forEach((c, i) => c.classList.toggle("here", i === si));
      // (no hold-to-grade: a tap grades and jumps; a held key repeats)
    }
  }
  (function frame() { if (!video.paused) tick(); requestAnimationFrame(frame); })();
  video.addEventListener("timeupdate", tick);
  video.addEventListener("seeked", tick);

  function drawClip() {
    const item = items[ci];
    drawCounts();
    if (!item) return;
    lastPiece = -1;
    $("g-dur").textContent = item.dur ? item.dur.toFixed(1) + "s clip" : "";
    const pills = $("g-pills"); pills.innerHTML = "";
    if (item.new) pills.appendChild(stamp("NEW", "amber"));
    if (item.untagged) pills.appendChild(stamp("UNTAGGED", "amber", "Nobody has tagged this clip yet"));
    const face = FACE[item.verdict] || FACE.UNSCANNED;
    pills.appendChild(stamp(face[0] + (item.face_frames ? " " + item.face_frames : ""), face[1], face[2]));
    const who = IDENTITY[item.identity] || IDENTITY.unruled;
    pills.appendChild(stamp(who[0], who[1]));
    if (item.uses) pills.appendChild(stamp(`cast ${item.uses}×`, "grey"));
    if (item.whole) { const g = GRADE_LABEL[item.whole]; pills.appendChild(stamp("whole clip " + g[0], g[1] + " big")); }
    if (item.ident) { const m = IDENT_LABEL[item.ident]; pills.appendChild(stamp(m[0], m[1] + " big")); }
    $("g-note").textContent = [item.subject ? `subject: ${item.subject}` : "", item.note].filter(Boolean).join(" — ");
    document.querySelectorAll("[data-ident]").forEach((b) => b.classList.toggle("on", b.dataset.ident === item.ident));
    drawTimeline();
    tick();
    remember();
  }

  // ------------------------------------------------------------- playback

  let startAt = 0, loops = 0, lastAdvance = 0;
  function advance(to, why) {
    const now = Date.now();
    if (now - lastAdvance < 500) return;      // a second move inside half a second is the same move
    lastAdvance = now;
    load(to);
  }
  function drawLoop() {
    const item = items[ci], el = $("g-loop");
    if (!el) return;
    if (!item || !clipOpen(item) || !loops) { el.hidden = true; return; }
    const left = pieces(item).filter((p) => !pieceGrade(item, p)).length;
    el.textContent = `looping · ${left} part${left === 1 ? "" : "s"} still ungraded · S skips`;
    el.hidden = false;
  }
  // S / Next: the next clip that still has an ungraded piece (graded clips are not shown again);
  // the arrows step one clip at a time
  function nextOpen(from) {
    for (let i = from; i < items.length; i += 1) if (clipOpen(items[i])) return i;
    return Math.min(items.length - 1, from);
  }
  function load(to) {
    stopHold();
    loops = 0;
    ci = Math.max(0, Math.min(items.length - 1, to));
    const item = items[ci];
    if (!item) return drawClip();
    const first = firstUngraded(item);
    startAt = pieces(item)[first >= 0 ? first : 0].t0;
    if (item.proxy_url && video.getAttribute("src") !== item.proxy_url) {
      video.src = item.proxy_url;          // loadedmetadata seeks to startAt
    } else if (!item.proxy_url) {
      video.removeAttribute("src"); video.load();
    } else {
      try { video.currentTime = startAt; } catch (e) {}
    }
    video.playbackRate = rate;
    if (item.proxy_url) video.play().catch(() => {});
    drawLoop();
    const following = items[ci + 1];
    if (following && following.proxy_url && next.getAttribute("src") !== following.proxy_url) next.src = following.proxy_url;
    drawClip();
  }
  video.addEventListener("loadedmetadata", () => {
    try { video.currentTime = startAt; } catch (e) {}
    video.playbackRate = rate;
    tick();
  });
  video.addEventListener("play", () => { video.playbackRate = rate; });
  // end of clip: an unfinished clip LOOPS from its first ungraded piece (nothing moves on until you grade
  // or skip it); a fully graded clip moves on by itself
  video.addEventListener("ended", () => {
    const item = items[ci];
    if (item && clipOpen(item)) {
      const first = firstUngraded(item);
      try { video.currentTime = pieces(item)[first >= 0 ? first : 0].t0; } catch (e) {}
      loops += 1; drawLoop();
      video.play().catch(() => {});
      return;
    }
    advance(ci + 1, "ended");
  });

  function setRate(r) {
    rate = r; video.playbackRate = r;
    $("g-rate").textContent = `Speed ${r}×`;
    try { localStorage.setItem("deck.grader.rate", String(r)); } catch (e) {}
  }

  // -------------------------------------------------------------- grading

  function body(item, p, fields) {
    return { stem: item.stem, clip_id: item.clip_id, t0: p.key ? p.t0 : null, t1: p.key ? p.t1 : null, ...fields };
  }
  function setLocal(item, p, grade) {
    if (p.key) item.grades[p.key] = grade;
    else { item.whole = grade; item.grade = grade; }
  }
  async function post(url, payload) {
    const result = await postJSON(url, payload);
    if (!result || !result.ok) throw new Error("the Deck did not confirm");
    return result;
  }

  // grade one piece of the CURRENT clip; playback is never paused or moved
  async function gradeIndex(grade, si) {
    const item = items[ci];
    if (!item) return;
    const p = pieces(item)[si], tag = ci + ":" + si, here = ci;
    if (!p || pending.has(tag)) return;
    if (hold) { if (hold.done.has(si)) return; hold.done.add(si); }
    pending.add(tag);
    saved(true, "saving…");
    try {
      await post("/api/grader/grade", body(item, p, { grade }));
      setLocal(item, p, grade);
      saved(true, `saved ✓ part ${si + 1} ${GRADE_LABEL[grade][0]}`);
      disk(1, `${item.stem} part ${si + 1} ${GRADE_LABEL[grade][0]}`);
      if (here === ci) { drawCell(si); lastPiece = -1; tick(); drawCounts(); }
    } catch (error) {
      if (hold) hold.done.delete(si);
      saved(false, `NOT saved — part ${si + 1}: tap again. ` + error.message);
    } finally { pending.delete(tag); }
  }

  // jump to section si of the current video (plays on from there); past the last section = next video
  function goSection(si) {
    const item = items[ci];
    if (!item) return;
    const ps = pieces(item);
    if (si >= ps.length) { advance(nextOpen(ci + 1), "sections-done"); return; }
    if (si < 0) { advance(ci - 1, "back"); return; }
    try { video.currentTime = ps[si].t0; } catch (e) {}
    if (video.paused) video.play().catch(() => {});
    lastPiece = -1; tick();
  }
  // 1/2/3: grade the section under the playhead, then straight on to the next section
  let lastFire = 0;
  function gradeAndNext(grade) {
    const item = items[ci];
    if (!item) return;
    const now = Date.now();
    if (now - lastFire < 160) return;          // key-repeat pace: ~6 sections a second, never a double on one tap
    lastFire = now;
    const si = pieceAt(item, video.currentTime || 0);
    gradeIndex(grade, si);
    goSection(si + 1);
  }

  function startHold(grade) {
    const item = items[ci];
    if (!item) return;
    if (hold && hold.grade === grade) return;
    hold = { grade, done: new Set() };
    document.querySelectorAll("[data-grade]").forEach((b) => b.classList.toggle("holding", b.dataset.grade === grade));
    gradeIndex(grade, pieceAt(item, video.currentTime || 0));
  }
  function stopHold() {
    hold = null;
    document.querySelectorAll("[data-grade]").forEach((b) => b.classList.remove("holding"));
  }

  // 7/8/9: the piece under the playhead and every piece after it, one POST, then the next clip
  let restBusy = false;
  async function gradeRest(grade) {
    const item = items[ci];
    if (!item || restBusy) return;
    stopHold();
    const ps = pieces(item), from = pieceAt(item, video.currentTime || 0), rest = ps.slice(from), here = ci;
    restBusy = true; saved(true, "saving…");
    try {
      const result = await post("/api/grader/grade-many", { items: rest.map((p) => body(item, p, { grade })) });
      rest.forEach((p) => setLocal(item, p, grade));
      saved(true, `saved ✓ ${result.written} part${result.written === 1 ? "" : "s"} ${GRADE_LABEL[grade][0]}`);
      disk(result.written, `${item.stem} ${result.written} parts ${GRADE_LABEL[grade][0]}`);
      if (here === ci) { if (ci < items.length - 1) advance(nextOpen(ci + 1), "rest"); else drawClip(); }
    } catch (error) {
      saved(false, "NOT saved — tap again. " + error.message);
    } finally { restBusy = false; }
  }

  // M/N: identity is a whole-clip ruling; playback carries on
  async function sendIdent(ident) {
    const item = items[ci];
    if (!item) return;
    saved(true, "saving…");
    try {
      await post("/api/grader/grade", { stem: item.stem, clip_id: item.clip_id, ident, t0: null, t1: null });
      item.ident = ident;
      saved(true, "saved ✓");
      disk(1, `${item.stem} ${IDENT_LABEL[ident][0]}`);
      document.querySelectorAll("[data-ident]").forEach((b) => b.classList.toggle("on", b.dataset.ident === item.ident));
      const pills = $("g-pills");
      pills.querySelectorAll(".ident-pill").forEach((x) => x.remove());
      const m = IDENT_LABEL[ident], el = stamp(m[0], m[1] + " big"); el.classList.add("ident-pill"); pills.appendChild(el);
    } catch (error) {
      saved(false, "NOT saved — tap again. " + error.message);
    }
  }

  function jump(kind) {
    const tests = {
      new: (i) => i.new, untagged: (i) => i.untagged, face: (i) => i.verdict === "PERSON",
      noface: (i) => i.verdict === "EMPTY", ungraded: () => true,
    };
    const want = tests[kind];
    let at = items.findIndex((i) => want(i) && clipOpen(i));
    if (at < 0) at = items.findIndex(want);
    if (at < 0) return toast("No clips of that kind in the library.");
    load(at);
  }

  // untouched NO-FACE clips: no whole-clip grade and not one piece graded
  function bulkTargets() {
    return items.filter((i) => i.verdict === "EMPTY" && !i.whole && !Object.keys(i.grades).length);
  }
  function askBulk() {
    const targets = bulkTargets();
    if (!targets.length) return toast("Every NO-FACE clip already has a grade.");
    $("g-confirm-text").textContent = `Mark ${targets.length} ungraded NO-FACE clips as B-ROLL (whole clip)? Some may still be the featured person from behind — clips with any grade already are not touched.`;
    $("g-confirm").hidden = false;
  }
  async function doBulk() {
    const targets = bulkTargets();
    $("g-confirm").hidden = true;
    if (!targets.length) return;
    saved(true, "saving…");
    try {
      const result = await post("/api/grader/grade-many",
        { items: targets.map((i) => ({ stem: i.stem, clip_id: i.clip_id, grade: "BROLL", t0: null, t1: null })) });
      targets.forEach((i) => { i.whole = "BROLL"; i.grade = "BROLL"; });
      saved(true, `saved ✓ ${result.written} marked B-ROLL`);
      disk(result.written, `${result.written} NO-FACE clips B-ROLL`);
      drawClip();
    } catch (error) {
      saved(false, "NOT saved — tap again. " + error.message);
    }
  }

  // -------------------------------------------------------------- wiring

  document.querySelectorAll("[data-grade]").forEach((b) => {
    const grade = b.dataset.grade;
    b.addEventListener("pointerdown", (e) => { e.preventDefault(); gradeAndNext(grade); });
    b.addEventListener("contextmenu", (e) => e.preventDefault());   // long-press on a phone must not open a menu
    b.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); gradeAndNext(grade); } });
  });
  document.querySelectorAll("[data-rest]").forEach((b) => b.addEventListener("click", () => gradeRest(b.dataset.rest)));
  document.querySelectorAll("[data-ident]").forEach((b) => b.addEventListener("click", () => sendIdent(b.dataset.ident)));
  document.querySelectorAll("[data-jump]").forEach((b) => b.addEventListener("click", () => jump(b.dataset.jump)));
  $("g-skip").addEventListener("click", () => advance(nextOpen(ci + 1), "skip"));
  $("g-next-clip").addEventListener("click", () => advance(nextOpen(ci + 1), "next"));
  $("g-back").addEventListener("click", () => advance(ci - 1, "back"));
  $("g-next-section").addEventListener("click", () => goSection(pieceAt(items[ci], video.currentTime || 0) + 1));
  $("g-prev-section").addEventListener("click", () => goSection(pieceAt(items[ci], video.currentTime || 0) - 1));
  $("g-rate").addEventListener("click", () => setRate(RATES[(RATES.indexOf(rate) + 1) % RATES.length]));
  $("g-bulk").addEventListener("click", askBulk);
  $("g-confirm-yes").addEventListener("click", doBulk);
  $("g-confirm-no").addEventListener("click", () => { $("g-confirm").hidden = true; });
  window.addEventListener("blur", stopHold);

  const GRADE_KEYS = { "1": "HERO", "2": "BROLL", "3": "NEVER" };
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    const key = e.key === " " ? "space" : e.key.toLowerCase();
    if (GRADE_KEYS[key]) { e.preventDefault(); gradeAndNext(GRADE_KEYS[key]); return; }   // repeat allowed: hold = blast
    if (key === "space" || key === "d") { e.preventDefault(); if (!e.repeat) goSection(pieceAt(items[ci], video.currentTime || 0) + 1); return; }
    if (key === "a") { e.preventDefault(); if (!e.repeat) goSection(pieceAt(items[ci], video.currentTime || 0) - 1); return; }
    if (e.repeat) return;
    const action = {
      "7": () => gradeRest("HERO"), "8": () => gradeRest("BROLL"), "9": () => gradeRest("NEVER"),
      m: () => sendIdent("ME"), n: () => sendIdent("NOTME"),
      s: () => advance(nextOpen(ci + 1), "skip"), arrowright: () => advance(nextOpen(ci + 1), "next"), arrowleft: () => advance(ci - 1, "back"),
      "0": () => setRate(RATES[(RATES.indexOf(rate) + 1) % RATES.length]),
    }[key];
    if (action) { e.preventDefault(); action(); }
  });

  video.addEventListener("focus", () => video.blur());
  (async function start() {
    setRate(rate);
    drawDisk();
    try {
      items = await api("/api/grader/items");
    } catch (error) {
      $("g-count").textContent = "Could not load the library: " + error.message;
      return;
    }
    items.forEach((i) => { if (!i.grades) i.grades = {}; });
    // resume: the first clip with any ungraded piece and no whole-clip grade, at its first ungraded piece;
    // the remembered clip only matters once everything is graded
    let at = items.findIndex(clipOpen);
    if (at < 0) {
      let last = ""; try { last = localStorage.getItem("deck.grader.pos") || ""; } catch (e) {}
      at = Math.max(0, items.findIndex((i) => i.stem === last));
    }
    load(at);
  })();
})();
