// The one script the three launcher pages share. What it does depends on which page it is on —
// the body says — and everything it writes comes out of the same message table the page was
// rendered from, so a line the browser writes is in the language of the lines around it.
//
// The pages poll rather than hold a socket open: the launcher is a small process that may be in
// the middle of a build, and a poll that misses one answer costs nothing.

const el = (id) => document.getElementById(id);
const page = document.body.dataset.page;

// The launcher answers an action only when this token comes back with it. It is minted per process
// and rendered into the page, so a request from any other page in the browser — which can reach the
// same loopback port — carries nothing and is refused.
const token = document.querySelector('meta[name="csrf"]')?.content || "";

let table = readMessages(document);
const T = (key) => table[key] ?? key;

function readMessages(doc) {
  try {
    return JSON.parse(doc.getElementById("messages")?.textContent || "{}");
  } catch (err) {
    return {};
  }
}

async function act(action) {
  await fetch("/api/action/" + action, { method: "POST", headers: { "X-Daedalus-Desktop": token } });
}

// ---- the language, in the corner of every page -------------------------------------------------

// Switching languages fetches this same page in the other one and swaps the part of it that holds
// words. A reload would do the same thing and lose whatever has been typed into the setup form, so
// the typed values are carried across instead — they never leave the page to do it.
async function switchLang(lang) {
  if (lang === document.body.dataset.lang) return;
  await fetch("/api/lang", {
    method: "POST",
    headers: { "X-Daedalus-Desktop": token, "Content-Type": "application/json" },
    body: JSON.stringify({ lang }),
  });
  const name = (field) => field.name + ":" + (field.type === "checkbox" ? field.value : "");
  const typed = new Map();
  for (const field of document.querySelectorAll("input[name]")) {
    typed.set(name(field), field.type === "checkbox" ? field.checked : field.value);
  }
  const answer = await fetch(location.pathname + location.search, { headers: { "Accept-Language": lang } });
  const fresh = new DOMParser().parseFromString(await answer.text(), "text/html");
  document.querySelector("main").replaceWith(fresh.querySelector("main"));
  document.getElementById("messages").textContent = fresh.getElementById("messages").textContent;
  table = readMessages(document);
  document.body.dataset.lang = lang;
  document.documentElement.lang = lang;
  for (const button of document.querySelectorAll(".langs button")) {
    button.setAttribute("aria-pressed", String(button.dataset.lang === lang));
  }
  for (const field of document.querySelectorAll("input[name]")) {
    const key = name(field);
    if (!typed.has(key)) continue;
    if (field.type === "checkbox") field.checked = typed.get(key);
    else if (field.type !== "radio") field.value = typed.get(key);
  }
  if (page === "setup") setupPanels();
}

document.addEventListener("click", (event) => {
  const lang = event.target.closest(".langs button");
  if (lang) switchLang(lang.dataset.lang);
});

// ---- the first page ----------------------------------------------------------------------------

// One provider key is asked for at a time. All three fields are in the form and all three are
// posted: an untouched field carries what is already on file, and the launcher reads an empty one
// as "leave it alone", so showing one panel changes nothing about what is written.
function setupPanels() {
  document.querySelectorAll(".seg button[data-provider]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".seg button[data-provider]").forEach((other) => {
        const chosen = other === button;
        other.setAttribute("aria-pressed", String(chosen));
        document.querySelector(`[data-panel="${other.dataset.provider}"]`).hidden = !chosen;
      });
    });
  });
  // The language the form was filled in is the language the installation keeps.
  const field = el("lang-field");
  if (field) field.value = document.body.dataset.lang;
}

// ---- the waiting page --------------------------------------------------------------------------

function drawProgress(status) {
  const steps = [...document.querySelectorAll("#steps .row")];
  const at = status.steps ? status.steps.indexOf(status.stage) : -1;
  const ready = status.running > 0 && !status.busy;
  steps.forEach((row, i) => {
    row.classList.toggle("done", ready || (at >= 0 && i < at));
    row.classList.toggle("now", !ready && i === at);
  });

  const track = el("track");
  track.hidden = !status.busy && !ready;
  track.className = "track";
  if (ready) {
    track.classList.add("done");
  } else if (status.size > 0) {
    track.classList.add("determinate");
    track.firstElementChild.style.width = Math.min(100, Math.round((status.done / status.size) * 100)) + "%";
  } else {
    track.classList.add("indeterminate");
  }

  const failed = Boolean(status.failure) && !status.busy;
  el("working").hidden = failed;
  el("trouble").hidden = !failed;
  if (failed) {
    el("what").textContent = status.docker_missing ? T("docker.missing") : status.failure;
    el("trouble-log").textContent = (status.log || []).join("\n");
    return;
  }
  el("idle").hidden = Boolean(status.busy) || ready;
  el("live").textContent = ready ? T("progress.done") : (status.log || []).slice(-1)[0] || T("progress.working");
  if (ready) setTimeout(() => (location.href = "/status"), 900);
}

// ---- the status page ---------------------------------------------------------------------------

function drawStatus(status) {
  // In native mode there is no Docker to report and the same tile says what the agent runs on.
  el("docker").textContent = status.mode === "native" ? T("status.tile.machine") : status.docker || T("status.unavailable");
  el("containers").textContent = status.running + " " + T("status.running");
  if (el("ports")) el("ports").textContent = status.ports || T("status.none");
  el("telegram").textContent = status.telegram ? T("status.on") : T("status.off");
  el("appurl").textContent = status.app_url;
  el("appurl").href = status.app_url;
  el("busy").textContent = status.busy ? status.busy + "…" : "";
  for (const button of document.querySelectorAll("button[data-action]")) {
    button.disabled = Boolean(status.busy);
  }
  // The agent's own code. A change waiting for a restart is the only thing on this page the
  // operator has to act on, so it gets a card of its own and the button that applies it.
  const change = status.change || {};
  el("change").hidden = !change.commit;
  if (change.commit) {
    el("change-title").textContent = change.pending ? T("change.pending") : changeOutcome(change.status);
    el("change-body").textContent = change.pending ? change.summary : change.detail || change.summary;
    el("change-apply").hidden = !change.pending;
  }
  const message = status.docker_missing ? T("docker.missing") : status.failure;
  el("alert").textContent = message || "";
  el("alert").hidden = !message;
  el("log").textContent = status.log.length ? status.log.join("\n") : T("status.log.empty");
}

// The supervisor's word for what happened, in the operator's.
function changeOutcome(status) {
  if (status === "applied") return T("change.applied");
  if (status === "rolled_back") return T("change.reverted");
  return T("change.failed");
}

// ---- the poll ----------------------------------------------------------------------------------

async function refresh() {
  let status;
  try {
    status = await (await fetch("/api/status")).json();
  } catch (err) {
    const alert = el("alert") || el("live");
    if (alert) alert.textContent = T("status.silent");
    if (alert && alert.id === "alert") alert.hidden = false;
    return;
  }
  if (page === "status") drawStatus(status);
  if (page === "progress") drawProgress(status);
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  await act(button.dataset.action);
  refresh();
});

if (page === "setup") setupPanels();
if (page === "status" || page === "progress") {
  refresh();
  setInterval(refresh, page === "progress" ? 1000 : 2000);
}
