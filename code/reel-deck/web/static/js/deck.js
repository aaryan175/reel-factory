/* Reel Deck — shared helpers: toast + JSON fetch + Continue button + factory-question cards. */

function toast(message, isError) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("err", !!isError);
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 3500);
}

/* In-page notice: stays until dismissed, says what to do. Errors go here (a 3.5s
   toast in the corner is not an error message). */
function notice(message, isError, action) {
  const el = document.getElementById("notice");
  if (!el) return toast(message, isError);
  el.className = "notice " + (isError ? "err" : "info");
  el.innerHTML = "";
  const text = document.createElement("span"); text.textContent = message; el.appendChild(text);
  if (action && action.href) {
    const a = document.createElement("a"); a.href = action.href; a.textContent = action.label || action.href;
    a.className = "btn small"; a.style.marginLeft = "10px"; el.appendChild(a);
  }
  const x = document.createElement("button"); x.type = "button"; x.className = "dismiss"; x.textContent = "✕";
  x.setAttribute("aria-label", "dismiss"); x.addEventListener("click", () => { el.hidden = true; });
  el.appendChild(x);
  el.hidden = false;
  el.scrollIntoView({ block: "nearest" });
}

async function api(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (e) {
    throw new Error("No connection — check your network and try again. Nothing was sent.");
  }
  let body = null;
  try { body = await response.json(); } catch (e) { /* non-JSON */ }
  if (response.status === 401) {
    const login = (body && body.login) || "/login?expired=1";
    notice((body && body.error) || "Your session expired — sign in again.", true, { href: login, label: "Sign in" });
    setTimeout(() => { location.href = login; }, 2500);
    throw new Error((body && body.error) || "Your session expired — sign in again.");
  }
  if (!response.ok) {
    const message = (body && body.error) ? body.error
      : response.status >= 500 ? `The Deck hit an error (${response.status}). Try again in a moment; if it keeps happening, tell whoever runs it.`
      : `Request failed (${response.status}).`;
    throw new Error(message);
  }
  return body;
}

async function postJSON(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("btn-continue");
  if (btn) {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const result = await postJSON("/api/continue", {});
        toast(`Continue sent to the factory (${result.receipt})`);
      } catch (error) {
        toast(error.message, true);
      } finally {
        setTimeout(() => { btn.disabled = false; }, 4000);
      }
    });
  }
});

// Board keeps itself honest: soft-refresh so the LIVE line and ETAs stay
// current, but never while the tab is hidden, a field is focused, or any
// textarea still holds unsent text (never eat what the reviewer is typing).
if (document.querySelector('.lanes')) {
  setTimeout(function refresh() {
    var el = document.activeElement;
    var typing = el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT');
    var dirty = Array.prototype.some.call(
      document.querySelectorAll('textarea, input.q-note'),
      function (t) { return t.value.trim().length > 0; });
    if (document.hidden || typing || dirty) { setTimeout(refresh, 15000); return; }
    location.reload();
  }, 45000);
}

/* Factory questions (Board strip + row page). Tapping an option sends
   `RULING on <receipt>: <n> — <option>` as a NOTES verdict for that row, plus the optional note;
   no navigation. The card says it was sent; the next board refresh drops it (the ruling closes it). */
document.addEventListener("click", async (event) => {
  const btn = event.target.closest(".qcard .q-opt, .qcard .q-send");
  if (!btn) return;
  const card = btn.closest(".qcard");
  const row = parseInt(card.dataset.row, 10);
  const noteEl = card.querySelector(".q-note");
  const note = noteEl ? noteEl.value.trim() : "";
  let text;
  if (btn.classList.contains("q-opt")) {
    text = btn.dataset.ruling + (note ? " — note: " + note : "");
  } else {
    if (!note) { notice("Type your answer in the box first, then press Send answer.", true); if (noteEl) noteEl.focus(); return; }
    text = card.dataset.prefix + note;
  }
  const buttons = card.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await postJSON("/api/verdict", { row, disposition: "NOTES", text, test: window.DECK_TEST === true });
    if (noteEl) noteEl.value = "";
    card.classList.add("sent");
    const done = card.querySelector(".q-sent");
    done.textContent = (window.DECK_TEST === true ? "test sent ✓ — nothing resumes in test mode" : "sent ✓ — row " + row + " resumes");
    done.hidden = false;
    card.querySelectorAll(".q-opts, .q-note-row, .q-more").forEach((el) => { el.hidden = true; });
  } catch (error) {
    buttons.forEach((b) => { b.disabled = false; });
    notice(error.message || "That answer did not send. Try again.", true);
  }
});
