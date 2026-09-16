// The status page polls the launcher rather than holding a socket open: the launcher is a small
// process that may be doing a build, and a poll that misses one answer costs nothing.
const el = (id) => document.getElementById(id);

// The launcher answers an action only when this token comes back with it. It is minted per process
// and rendered into the page, so a request from any other page in the browser — which can reach the
// same loopback port — carries nothing and is refused.
const token = document.querySelector('meta[name="csrf"]')?.content || "";

async function refresh() {
  let status;
  try {
    status = await (await fetch("/api/status")).json();
  } catch (err) {
    el("alert").textContent = "The launcher is not answering. It may have been closed; the stack keeps running.";
    el("alert").hidden = false;
    return;
  }
  el("docker").textContent = status.docker ? status.docker : "not available";
  el("containers").textContent = status.running + " running";
  el("telegram").textContent = status.telegram ? "on" : "off — browser only";
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
    el("change-title").textContent = change.pending ? "Changes are ready — restart to apply" : changeOutcome(change.status);
    el("change-body").textContent = change.pending ? change.summary : change.detail || change.summary;
    el("change-apply").hidden = !change.pending;
  }
  const message = status.docker_missing || status.failure;
  el("alert").textContent = message || "";
  el("alert").hidden = !message;
  el("log").textContent = status.log.length ? status.log.join("\n") : "nothing yet";
}

// The supervisor's word for what happened, in the operator's.
function changeOutcome(status) {
  if (status === "applied") return "The change is running";
  if (status === "rolled_back") return "The change was reversed";
  return "The change was not applied";
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  await fetch("/api/action/" + button.dataset.action, {
    method: "POST",
    headers: { "X-Daedalus-Desktop": token },
  });
  refresh();
});

refresh();
setInterval(refresh, 2000);
