// Projects: a folder the operator adds, and the agents that work inside it. The switcher lives in
// the shell (the rail on a desktop, the Agents header on a phone) because a project is a lens over
// every list of agents, not a destination of its own.

import { useCallback, useEffect, useState } from "react";
import { api, Project } from "./api";
import { Sheet } from "./dialogs";
import { Icon } from "./icons";
import { invalidate, useQuery } from "./store";
import { confirmAsync, errorText } from "./ui";

const PICKED = "daedalus.project";

/** The project the operator is looking at, remembered between visits; "" is all of them. */
export function storedProject(): string {
  try {
    return localStorage.getItem(PICKED) ?? "";
  } catch {
    return "";
  }
}

export function rememberProject(id: string): void {
  try {
    if (id) localStorage.setItem(PICKED, id);
    else localStorage.removeItem(PICKED);
  } catch {
    /* private mode */
  }
}

export function useProjects() {
  return useQuery<Project[]>("/api/projects", { staleMs: 15000 });
}

/** Whether this page is running in the desktop window, which can open a real folder chooser. */
export function canPickFolder(): boolean {
  return typeof window.daedalus?.pickFolder === "function";
}

function afterChange(): void {
  invalidate("/api/projects");
  invalidate("/api/sessions");
}

/** The control that says which project is in view and opens the list: rail, header or palette. */
export function ProjectChip({ projects, current, onOpen, collapsed }: { projects: Project[]; current: string; onOpen: () => void; collapsed?: boolean }) {
  const active = projects.find((p) => p.id === current);
  const label = active ? active.name : projects.length ? "All projects" : "Add a project";
  return (
    <button className="project-chip" onClick={onOpen} title={active ? active.root : "Projects"} aria-haspopup="dialog">
      <Icon name="folder" size={16} />
      <span className="rail-text truncate">{collapsed ? "" : label}</span>
      {!collapsed && active && !active.reachable && <span className="badge attn" title="the folder is not mounted here">not mounted</span>}
      {!collapsed && <span className="chev">›</span>}
    </button>
  );
}

/** Pick a project, add one, or open one's settings. */
export function ProjectSwitcher({ projects, current, onPick, onClose, toast }: { projects: Project[]; current: string; onPick: (id: string) => void; onClose: () => void; toast: (t: string) => void }) {
  const [adding, setAdding] = useState(projects.length === 0);
  const [editing, setEditing] = useState<Project | null>(null);
  const pick = (id: string) => {
    onPick(id);
    onClose();
  };
  if (adding) return <AddProjectSheet onClose={() => (projects.length ? setAdding(false) : onClose())} onAdded={(p) => { setAdding(false); pick(p.id); }} toast={toast} />;
  if (editing) return <ProjectSettingsSheet project={editing} onClose={() => setEditing(null)} onRemoved={() => { setEditing(null); if (editing.id === current) onPick(""); onClose(); }} toast={toast} />;
  return (
    <Sheet title="Projects" onClose={onClose} size="narrow">
      <div className="sub" style={{ marginBottom: 8 }}>
        A project is a folder you add. Agents you start in it work in that folder and nowhere else.
      </div>
      <button className={`menu-item ${current ? "" : "on"}`} onClick={() => pick("")}>
        <span className="grow truncate">All projects</span>
        {!current && <Icon name="check" size={16} />}
      </button>
      {projects.map((p) => (
        <div key={p.id} className={`project-row ${p.id === current ? "on" : ""}`}>
          <button className="grow project-pick" onClick={() => pick(p.id)}>
            <span className="project-name truncate">
              {p.name}
              {!p.reachable && <span className="badge attn" title="the folder is not reachable from where the bot runs">not mounted</span>}
              {p.settings.snapshots && <span className="badge" title="every turn is snapshotted, so a change can be undone">snapshots</span>}
            </span>
            <span className="sub mono truncate">{p.root}</span>
            <span className="sub">{p.sessions.length ? `${p.sessions.length} agent${p.sessions.length === 1 ? "" : "s"}` : "no agents yet"}</span>
          </button>
          <button className="iconbtn small" onClick={() => setEditing(p)} title={`Settings for ${p.name}`} aria-label={`Settings for ${p.name}`}>
            <Icon name="settings" size={15} />
          </button>
        </div>
      ))}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>Close</button>
        <button className="btn primary" onClick={() => setAdding(true)}><Icon name="plus" size={15} /> Add a project</button>
      </div>
    </Sheet>
  );
}

/** Name and folder. In the desktop window the folder comes from the platform's chooser; elsewhere it is typed. */
export function AddProjectSheet({ onClose, onAdded, toast }: { onClose: () => void; onAdded: (p: Project) => void; toast: (t: string) => void }) {
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [snapshots, setSnapshots] = useState(false);
  const [busy, setBusy] = useState(false);
  const browse = useCallback(async () => {
    try {
      const picked = await window.daedalus?.pickFolder?.();
      if (!picked) return; // cancelled
      setRoot(picked);
      // The folder's own name is what the operator calls it nine times in ten; they can still type over it.
      if (!name.trim()) setName(picked.replace(/[/\\]+$/, "").split(/[/\\]/).pop() || "");
    } catch (e) {
      toast(errorText(e));
    }
  }, [name, toast]);
  // The server answers 400 on a path that is not absolute, but it answers it into a toast; the
  // mistake belongs beside the field it was made in.
  const typed = root.trim();
  const rootProblem = typed && !(typed.startsWith("/") || /^[A-Za-z]:[\\/]/.test(typed)) ? "a project folder is a full path: it starts with / (or a drive letter on Windows)" : "";
  async function add() {
    if (!name.trim() || !typed || rootProblem || busy) return;
    setBusy(true);
    try {
      const created = await api.post<Project>("/api/projects", { name: name.trim(), root: root.trim(), snapshots });
      afterChange();
      toast(created.reachable ? `${created.name} added` : `${created.name} added — its folder is not mounted here yet`);
      onAdded(created);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title="Add a project" onClose={onClose} size="narrow">
      <label className="field" htmlFor="project-name">Name</label>
      <input id="project-name" className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="What this folder is" />
      <label className="field" htmlFor="project-root">Folder</label>
      <div className="composer-row" style={{ marginTop: 0 }}>
        <input id="project-root" className="field mono" value={root} onChange={(e) => setRoot(e.target.value)} placeholder="/home/you/projects/bakery" onKeyDown={(e) => e.key === "Enter" && add()} />
        {canPickFolder() && <button className="btn" onClick={browse} title="Choose a folder"><Icon name="folder" size={15} /> Browse</button>}
      </div>
      <div className={rootProblem ? "sub attn" : "sub"}>{rootProblem || "The full path of the folder on this machine. Everything an agent of this project reads or writes stays inside it."}</div>
      <label className="toggle-row">
        <input type="checkbox" checked={snapshots} onChange={(e) => setSnapshots(e.target.checked)} />
        <span>Snapshots</span>
        <span className="sub">a hidden commit before every turn, so a change can be undone. Off by default: on a large repository it costs more than the undo is worth.</span>
      </label>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" onClick={add} disabled={busy || !name.trim() || !typed || !!rootProblem}>Add</button>
      </div>
    </Sheet>
  );
}

/** Rename it, turn snapshots on or off, or remove it. The folder is shown and never edited here. */
export function ProjectSettingsSheet({ project, onClose, onRemoved, toast }: { project: Project; onClose: () => void; onRemoved: () => void; toast: (t: string) => void }) {
  const [name, setName] = useState(project.name);
  const [snapshots, setSnapshots] = useState(project.settings.snapshots);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setName(project.name);
    setSnapshots(project.settings.snapshots);
  }, [project]);
  const dirty = name.trim() !== project.name || snapshots !== project.settings.snapshots;
  async function save() {
    if (!dirty || !name.trim() || busy) return;
    setBusy(true);
    try {
      await api.patch(`/api/projects/${encodeURIComponent(project.id)}`, { name: name.trim(), snapshots });
      afterChange();
      toast("saved");
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    const agents = project.sessions.length;
    const running = project.sessions.filter((s) => s.running);
    const body = running.length
      ? `${running.map((s) => s.title).join(", ")} ${running.length === 1 ? "is" : "are"} working in it right now, so this will be refused until ${running.length === 1 ? "it stops" : "they stop"}: removing the project mid-turn would move the folder under ${running.length === 1 ? "it" : "them"}.`
      : agents
        ? `${agents} agent${agents === 1 ? "" : "s"} work${agents === 1 ? "s" : ""} in it. They keep their history and go back to a directory of their own, which is empty. Not one file of the folder is deleted.`
        : "The folder and everything in it stays exactly as it is; only the project is forgotten.";
    if (!(await confirmAsync(`Remove the project "${project.name}"?`, { body, action: "Remove" }))) return;
    try {
      await api.delete(`/api/projects/${encodeURIComponent(project.id)}?detach=1`);
      afterChange();
      toast(`${project.name} removed`);
      onRemoved();
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <Sheet title={project.name} ariaLabel={`project ${project.name}`} onClose={onClose} size="narrow">
      <label className="field" htmlFor="project-rename">Name</label>
      <input id="project-rename" className="field" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
      <label className="field">Folder</label>
      <div className="readonly-path mono">{project.root}</div>
      <div className="sub">
        {project.reachable
          ? project.writable
            ? "Reachable from where the bot runs."
            : "Reachable, but read-only from where the bot runs: agents can read it and not write it."
          : "Not reachable from where the bot runs. In Docker a folder has to be mounted into the container at the same path: the launcher writes that mount, and Stop then Start is what applies it. Where the launcher cannot, the desktop README has the entry to add by hand."}
      </div>
      <label className="toggle-row">
        <input type="checkbox" checked={snapshots} onChange={(e) => setSnapshots(e.target.checked)} />
        <span>Snapshots</span>
        <span className="sub">a hidden commit before every turn and after every run, so a change can be undone. On a large repository this costs a walk of the whole tree twice a turn.</span>
      </label>
      {project.sessions.length > 0 && (
        <>
          <label className="field">Agents in this project</label>
          <div className="sub">{project.sessions.map((s) => s.title).join(" · ")}</div>
        </>
      )}
      <div className="sheet-foot">
        <button className="btn danger" onClick={remove}><Icon name="trash" size={15} /> Remove</button>
        <button className="btn primary" onClick={save} disabled={!dirty || !name.trim() || busy}>Save</button>
      </div>
    </Sheet>
  );
}
