// The status page polls the launcher rather than holding a socket open: the launcher is a small
// process that may be doing a build, and a poll that misses one answer costs nothing.
const el = (id) => document.getElementById(id);

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
  const message = status.docker_missing || status.failure;
  el("alert").textContent = message || "";
  el("alert").hidden = !message;
  el("log").textContent = status.log.length ? status.log.join("\n") : "nothing yet";
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  await fetch("/api/action/" + button.dataset.action, { method: "POST" });
  refresh();
});

refresh();
setInterval(refresh, 2000);
