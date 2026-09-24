// Projects: a named home for agents and their files. The switcher lives in
// the shell (the sidebar on a desktop, the Agents header on a phone) because a project is a lens
// over every list of agents, not a destination of its own.

import { useCallback, useEffect, useState } from "react";
import { api, Project } from "./api";
import { primaryFolder, projectPath, projectReachable } from "./folders";
import { Sheet } from "./dialogs";
import { Icon } from "./icons";
import { invalidate, useQuery } from "./store";
import { confirmAsync, errorText } from "./ui";
import { plural, t } from "./i18n";

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

function afterChange(): void {
  invalidate("/api/projects");
  invalidate("/api/sessions");
}

/** The control that says which project is in view and opens the list: sidebar, header or palette. */
export function ProjectChip({ projects, current, onOpen, collapsed }: { projects: Project[]; current: string; onOpen: () => void; collapsed?: boolean }) {
  const active = projects.find((p) => p.id === current);
  const label = active ? active.name : projects.length ? t("shell.projects.all") : t("shell.projects.add");
  return (
    <button className="project-chip" onClick={onOpen} title={active ? projectPath(active) : t("shell.projects")} aria-haspopup="dialog">
      <Icon name="folder" size={16} />
      {!collapsed && <span className="sidebar-text truncate">{label}</span>}
      {!collapsed && active && !projectReachable(active) && <span className="badge attn" title={t("project.notmounted.here")}>{t("project.notmounted")}</span>}
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
    <Sheet title={t("shell.projects")} onClose={onClose} size="narrow">
      <div className="sub" style={{ marginBottom: 8 }}>{t("project.intro")}</div>
      <button className={`menu-item ${current ? "" : "on"}`} onClick={() => pick("")}>
        <span className="grow truncate">{t("shell.projects.all")}</span>
        {!current && <Icon name="check" size={16} />}
      </button>
      {projects.map((p) => (
        <div key={p.id} className={`project-row ${p.id === current ? "on" : ""}`}>
          <button className="grow project-pick" onClick={() => pick(p.id)}>
            <span className="project-name truncate">
              {p.name}
              {!projectReachable(p) && <span className="badge attn" title={t("project.notmounted.bot")}>{t("project.notmounted")}</span>}
              {p.settings.snapshots && <span className="badge" title={t("project.snapshots.title")}>{t("project.snapshots.badge")}</span>}
            </span>
            <span className="sub mono truncate">{projectPath(p)}</span>
            <span className="sub">{p.sessions.length ? plural("project.agents", p.sessions.length) : t("project.noagents")}</span>
          </button>
          <button className="iconbtn small" onClick={() => setEditing(p)} title={t("project.settings.for", { name: p.name })} aria-label={t("project.settings.for", { name: p.name })}>
            <Icon name="settings" size={15} />
          </button>
        </div>
      ))}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.close")}</button>
        <button className="btn primary" onClick={() => setAdding(true)}><Icon name="plus" size={15} /> {t("shell.projects.add")}</button>
      </div>
    </Sheet>
  );
}

type DirectoryEntry = { name: string; path: string; readable: boolean; writable: boolean; project_id?: string | null };
type DirectoryListing = { roots?: DirectoryEntry[]; docker?: boolean; root?: string; path?: string; parents?: { name: string; path: string }[]; entries?: DirectoryEntry[]; truncated?: boolean };

/** A name is enough. Choosing an existing folder is the optional second step. */
export function AddProjectSheet({ onClose, onAdded, toast }: { onClose: () => void; onAdded: (p: Project) => void; toast: (t: string) => void }) {
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [busy, setBusy] = useState(false);
  const typed = root.trim();
  const rootProblem = typed && !(typed.startsWith("/") || /^[A-Za-z]:[\\/]/.test(typed)) ? t("project.root.problem") : "";
  async function add() {
    if (!name.trim() || rootProblem || busy) return;
    setBusy(true);
    try {
      const created = await api.post<Project>("/api/projects", { name: name.trim(), root: typed || undefined });
      afterChange();
      toast(t(projectReachable(created) ? "project.added" : "project.added.unmounted", { name: created.name }));
      onAdded(created);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("shell.projects.add")} onClose={onClose} size="narrow">
      <label className="field" htmlFor="project-name">{t("common.name")}</label>
      <input id="project-name" className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={t("project.name.placeholder")} />
      <button className="disclosure" type="button" onClick={() => setChoosing((value) => !value)} aria-expanded={choosing}><span className={`chev ${choosing ? "down" : ""}`}>›</span> {t("project.existing")}</button>
      {choosing ? <DirectoryPicker value={root} onChange={setRoot} toast={toast} /> : <div className="sub">{t("project.automatic.hint")}</div>}
      {rootProblem && <div className="sub attn">{rootProblem}</div>}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" onClick={add} disabled={busy || !name.trim() || !!rootProblem}>{t("common.add")}</button>
      </div>
    </Sheet>
  );
}

function DirectoryPicker({ value, onChange, toast }: { value: string; onChange: (path: string) => void; toast: (text: string) => void }) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [root, setRoot] = useState("");
  const load = useCallback(async (nextRoot: string, path: string) => {
    try {
      const result = await api.get<DirectoryListing>(`/api/project-directories?root=${encodeURIComponent(nextRoot)}&path=${encodeURIComponent(path)}`);
      setListing(result);
      if (result.path) onChange(result.path);
    } catch (e) { toast(errorText(e)); }
  }, [onChange, toast]);
  useEffect(() => { api.get<DirectoryListing>("/api/project-directories").then(setListing).catch((e) => toast(errorText(e))); }, [toast]);
  return (
    <div className="directory-picker">
      <label className="field" htmlFor="project-root">{t("project.folder")}</label>
      <input id="project-root" className="field mono" value={value} onChange={(e) => onChange(e.target.value)} placeholder={t("project.path.placeholder")} />
      {listing?.roots && <div className="directory-roots">{listing.roots.map((entry) => <button key={entry.path} className="btn ghost" onClick={() => { setRoot(entry.path); void load(entry.path, entry.path); }}><Icon name="folder" size={14} /> {entry.name}</button>)}</div>}
      {listing?.parents && <div className="directory-crumbs">{listing.parents.map((entry) => <button key={entry.path} className="linkbtn mono" onClick={() => void load(root, entry.path)}>{entry.name}</button>)}</div>}
      {listing?.entries?.map((entry) => <button key={entry.path} className="menu-item" disabled={!entry.readable} onClick={() => void load(root, entry.path)}><Icon name="folder" size={15} /><span className="grow truncate">{entry.name}</span>{entry.project_id && <span className="badge">{t("project.already")}</span>}{!entry.writable && <span className="badge attn">{t("project.readonly.short")}</span>}</button>)}
      {listing?.truncated && <div className="sub attn">{t("project.browser.truncated")}</div>}
      {listing?.docker && <div className="sub">{t("project.browser.mount")}</div>}
    </div>
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
      toast(t("common.saved"));
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
      ? plural("project.remove.running", running.length, { names: running.map((s) => s.title).join(", ") })
      : agents
        ? plural("project.remove.agents", agents)
        : t("project.remove.empty");
    if (!(await confirmAsync(t("project.remove.title", { name: project.name }), { body, action: t("common.remove") }))) return;
    try {
      await api.delete(`/api/projects/${encodeURIComponent(project.id)}`);
      afterChange();
      toast(t("project.removed", { name: project.name }));
      onRemoved();
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <Sheet title={project.name} ariaLabel={t("project.settings.for", { name: project.name })} onClose={onClose} size="narrow">
      <label className="field" htmlFor="project-rename">{t("common.name")}</label>
      <input id="project-rename" className="field" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
      <label className="field">{t("project.folder")}</label>
      <div className="readonly-path mono">{projectPath(project)}</div>
      <div className="sub">{primaryFolder(project)?.reachable ? (primaryFolder(project)?.writable ? t("project.reachable") : t("project.readonly")) : t("project.unreachable")}</div>
      <label className="toggle-row">
        <input type="checkbox" checked={snapshots} onChange={(e) => setSnapshots(e.target.checked)} />
        <span>{t("project.snapshots")}</span>
        <span className="sub">{t("project.snapshots.hint.edit")}</span>
      </label>
      {project.sessions.length > 0 && (
        <>
          <label className="field">{t("project.agents.in")}</label>
          <div className="sub">{project.sessions.map((s) => s.title).join(" · ")}</div>
        </>
      )}
      <div className="sheet-foot">
        <button className="btn danger" onClick={remove}><Icon name="trash" size={15} /> {t("common.remove")}</button>
        <button className="btn primary" onClick={save} disabled={!dirty || !name.trim() || busy}>{t("common.save")}</button>
      </div>
    </Sheet>
  );
}

/** Move one agent into a project or between two projects.
 *
 * Nothing on disk moves. A private choice creates a child in the destination project; the shared
 * choice uses its root. Files in the old directory stay where they were.
 * The installation's own folders are not offered as a destination — the concierge's Projects tool
 * hides the Voice project for the same reason — unless the session is already in one.
 */
export function MoveSessionSheet({ sessionId, current, currentOwn, onClose, onMoved, toast }: { sessionId: string; current: string; currentOwn: boolean; onClose: () => void; onMoved: () => void; toast: (t: string) => void }) {
  const projects = useProjects();
  const [target, setTarget] = useState(current);
  const [ownDirectory, setOwnDirectory] = useState(currentOwn);
  const [busy, setBusy] = useState(false);
  const chosen = (projects.data ?? []).find((p) => p.id === target);
  async function move() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/api/sessions/${encodeURIComponent(sessionId)}/project`, { project_id: target, own_directory: ownDirectory });
      afterChange();
      invalidate(`/api/sessions/${sessionId}`);
      toast(t("move.done", { name: chosen?.name ?? "" }));
      onMoved();
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("move.title")} onClose={onClose} size="narrow">
      <label className="field" htmlFor="move-project">{t("move.where")}</label>
      <select id="move-project" className="field" value={target} onChange={(e) => { setTarget(e.target.value); setOwnDirectory(false); }}>
        <option value="" disabled>{t("move.choose")}</option>
        {(projects.data ?? []).filter((p) => !p.system || p.id === current).map((p) => (
          <option key={p.id} value={p.id}>{p.name} · {projectPath(p)}</option>
        ))}
      </select>
      {chosen && (
        <>
          <label className="toggle-row">
            <input type="checkbox" checked={ownDirectory} onChange={(e) => setOwnDirectory(e.target.checked)} disabled={!projectReachable(chosen)} />
            <span>{t("move.owndirectory")}</span>
            <span className="sub">{t("move.owndirectory.hint", { root: projectPath(chosen) })}</span>
          </label>
          <div className="sub attn">{ownDirectory ? t("move.warn.separate", { root: projectPath(chosen) }) : t("move.warn.shared", { root: projectPath(chosen) })}</div>
        </>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" onClick={move} disabled={busy || !target || (target === current && ownDirectory === currentOwn)}>{t("move.action")}</button>
      </div>
    </Sheet>
  );
}
