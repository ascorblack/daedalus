import { useEffect, useState } from "react";
import { relTime } from "./format";
import { api, LoopView, ServiceView, ShareMode, ToolInfo } from "./api";
import { Icon } from "./icons";
import { OverflowMenu, Sheet } from "./dialogs";
import { confirmAsync, errorText } from "./ui";

export type Status = "idle" | "running" | "waiting" | "failed" | "done";

export function Avatar({ status, seed }: { status: Status; seed: string }) {
  // Deterministic accessory per session so each "bot" stays recognisable.
  const hue = [...seed].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  const accessory = hue % 3;
  return (
    <svg className={`avatar ${status}`} viewBox="0 0 44 44" aria-label={status}>
      <rect className="body" x="4" y="8" width="36" height="30" rx="10" strokeWidth="1.5" />
      {accessory === 0 && <circle cx="22" cy="6" r="2.5" fill={`hsl(${hue} 70% 60%)`} />}
      {accessory === 1 && <rect x="12" y="3" width="20" height="4" rx="2" fill={`hsl(${hue} 70% 60%)`} />}
      {accessory === 2 && <path d="M8 10 L14 3 L20 10" fill="none" stroke={`hsl(${hue} 70% 60%)`} strokeWidth="2" />}
      <circle className="eye" cx="16" cy="22" r="3.2" />
      <circle className="eye" cx="28" cy="22" r="3.2" />
    </svg>
  );
}

/** One word for a state, in the one colour that state has everywhere: idle stays grey. */
export const STATUS_WORD: Record<string, string> = { idle: "Idle", running: "Working", waiting: "Needs you", failed: "Failed", done: "Done", paused: "Paused", stopped: "Stopped", pending: "Pending", merged: "Merged", approved: "Approved", rejected: "Rejected", closed: "Closed", dead: "Died", stopped_: "Stopped" };

export function Dot({ status, className }: { status: string; className?: string }) {
  return <span className={`dot ${status} ${className ?? ""}`} aria-hidden />;
}

/** A dot and the word: "● Working", "● Needs you". */
export function StatusLabel({ status, word }: { status: string; word?: string }) {
  return (
    <span className={`status ${status}`}>
      <span className="dot" aria-hidden />
      {word ?? STATUS_WORD[status] ?? status}
    </span>
  );
}

export function Pill({ status, children }: { status: string; children?: React.ReactNode }) {
  return <span className={`pill ${status}`}>{children ?? STATUS_WORD[status] ?? status}</span>;
}

/** A row of grey lines while the first load is on its way. */
export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="sk-row">
          <div className="skeleton avatar" />
          <div>
            <div className="skeleton line" style={{ width: `${55 + ((i * 17) % 30)}%` }} />
            <div className="skeleton line" style={{ width: `${30 + ((i * 11) % 25)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Checkboxes for the host tools, grouped, all on by default; collapsed until the operator opens it. */
export function ToolPicker({ off, onChange, note }: { off: string[]; onChange: (off: string[]) => void; note?: string }) {
  const [tools, setTools] = useState<ToolInfo[] | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open || tools !== null) return;
    api.get<ToolInfo[]>("/api/tools").then(setTools).catch(() => setTools([]));
  }, [open, tools]);
  const offSet = new Set(off);
  const groups = new Map<string, ToolInfo[]>();
  for (const t of tools ?? []) groups.set(t.group, [...(groups.get(t.group) ?? []), t]);
  const toggle = (name: string) => onChange(offSet.has(name) ? off.filter((n) => n !== name) : [...off, name]);
  const toggleGroup = (items: ToolInfo[]) => {
    const allOn = items.every((t) => !offSet.has(t.name));
    const names = items.map((t) => t.name);
    onChange(allOn ? [...off, ...names.filter((n) => !offSet.has(n))] : off.filter((n) => !names.includes(n)));
  };
  return (
    <div className="toolpicker">
      <button type="button" className="toolpicker-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`chev ${open ? "down" : ""}`}>›</span> Tools{off.length ? ` · ${off.length} off` : " · all on"}
      </button>
      {open && tools === null && <div className="sub">Loading…</div>}
      {open && tools !== null && (
        <div className="toolpicker-body">
          {note && <div className="sub" style={{ marginBottom: 6 }}>{note}</div>}
          {[...groups.entries()].map(([group, items]) => (
            <div key={group} className="toolgroup">
              <label className="toolrow head">
                <input type="checkbox" checked={items.every((t) => !offSet.has(t.name))} ref={(el) => { if (el) el.indeterminate = items.some((t) => offSet.has(t.name)) && !items.every((t) => offSet.has(t.name)); }} onChange={() => toggleGroup(items)} />
                <span>{group}</span>
              </label>
              {items.map((t) => (
                <label key={t.name} className="toolrow" title={t.description}>
                  <input type="checkbox" checked={!offSet.has(t.name)} onChange={() => toggle(t.name)} />
                  <span className="mono">{t.name}</span>
                  <span className="sub">{t.description}</span>
                </label>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function fmtInterval(seconds: number | null | undefined): string {
  if (!seconds) return "dynamic";
  for (const [size, suffix] of [[86400, "d"], [3600, "h"], [60, "m"]] as [number, string][]) if (seconds >= size && seconds % size === 0) return `${seconds / size}${suffix}`;
  return `${seconds}s`;
}

/** "loop · 10m · #12" / "loop · paused" — the short form of a session's loop. */
export function loopLabel(loop: LoopView | null | undefined): string {
  if (!loop) return "";
  const cadence = loop.mode === "interval" ? `every ${fmtInterval(loop.interval_seconds)}` : "self-paced";
  const status = loop.status === "active" ? "" : ` · ${loop.status}`;
  return `loop · ${cadence} · #${loop.run_count}${loop.max_runs ? "/" + loop.max_runs : ""}${status}`;
}

export function timeAgo(iso: string | null | undefined): string {
  return relTime(iso);
}

export function fmtUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "free";
  if (value === 0) return "$0";
  if (value < 0.01) return "< $0.01";
  return `$${value.toFixed(2)}`;
}

export function fmtInt(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString();
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // No clipboard API (http, old webview): a selectable field is the fallback the caller shows anyway.
    return false;
  }
}

const ACCESS_WORD: Record<ShareMode, string> = { local: "Local only", key: "Private link", public: "Public" };

/** One hosted service: what it is, whether it runs, where to open it, and the rest behind a menu. */
export function ServiceRow({ s, sessionId, onChange, toast, onLogs, card }: { s: ServiceView; sessionId: string; onChange: () => void; toast: (t: string) => void; onLogs: (text: string) => void; card?: boolean }) {
  const [share, setShare] = useState(false);
  const [busy, setBusy] = useState(false);
  async function stop() {
    if (!(await confirmAsync(`Stop "${s.name}"?`, { body: "The process is ended; the agent can start it again with ServiceStart.", action: "Stop" }))) return;
    try {
      await api.post(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/stop`);
      toast(`${s.name}: stopped`);
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove() {
    if (!(await confirmAsync(s.status === "running" ? `Stop and remove "${s.name}"?` : `Remove "${s.name}" from the list?`, { body: "Its log stays in the workspace.", action: "Remove" }))) return;
    try {
      await api.delete(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}`);
      toast(`${s.name}: removed`);
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function logs() {
    try {
      const r = await api.get<{ text: string }>(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/logs?lines=200`);
      onLogs(r.text);
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function setMode(mode: ShareMode, rotate = false) {
    if (mode === "public" && s.share?.mode !== "public" && !(await confirmAsync(`Open "${s.name}" to the internet?`, { body: "Anyone with the link reaches it through the site, without a login.", action: "Make public" }))) return;
    setBusy(true);
    try {
      await api.post(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/share`, { mode, rotate_key: rotate });
      toast(mode === "local" ? `${s.name}: local only` : mode === "public" ? `${s.name}: public` : rotate ? `${s.name}: new key` : `${s.name}: private link`);
      onChange();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function copy(text: string, what: string) {
    toast((await copyText(text)) ? `${what} copied` : "select and copy the text below");
  }
  const mode = s.share?.mode ?? "local";
  const shared = mode !== "local" && s.status === "running";
  const openUrl = shared && s.share?.url ? s.share.url : s.url;
  const lan = s.url?.replace(/^https?:\/\//, "");
  const menu = (
    <OverflowMenu
      small
      label={`${s.name} actions`}
      items={[
        ...(s.status === "running" && s.port ? [{ label: "Access…", icon: "share" as const, onSelect: () => setShare(true) }] : []),
        { label: "Log", icon: "file", onSelect: logs },
        ...(openUrl ? [{ label: "Copy address", icon: "copy" as const, onSelect: () => copy(openUrl, "address") }] : []),
        "-" as const,
        ...(s.status === "running" ? [{ label: "Stop", icon: "stop" as const, onSelect: stop }] : []),
        { label: s.status === "running" ? "Stop and remove…" : "Remove from the list", icon: "trash", danger: true, onSelect: remove },
      ]}
    />
  );
  return (
    <div className={`service-row ${s.status} ${card ? "erow service" : ""}`}>
      <div className="service-line">
        <span className={`dot ${s.status}`} />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="service-name">
            {s.name}
            {s.port && <span className="chip mono port">:{s.port}</span>}
            {shared && <span className={`chip ${mode === "public" ? "bad" : "attn"}`}>{ACCESS_WORD[mode]}</span>}
          </div>
          <div className="sub service-meta">
            {s.status === "running" ? `Running · ${relTime(s.started_at)}` : `${s.status === "dead" ? "Died" : "Stopped"}${s.note ? `: ${s.note}` : ""}${s.stopped_at ? ` · ${relTime(s.stopped_at)}` : ""}`}
            {s.status === "running" && lan && !shared ? ` · ${lan}` : ""}
          </div>
          <div className="sub mono service-cmd" title={s.command}>{s.command}</div>
        </div>
        {openUrl && s.status === "running" && (
          <a className="btn small open" href={openUrl} target="_blank" rel="noreferrer" title={shared ? "opens through the site" : "reachable on the local network only"}>
            Open
          </a>
        )}
        {menu}
      </div>
      {share && s.status === "running" && (
        <Sheet title={`Access to ${s.name}`} onClose={() => setShare(false)} size="narrow">
          <div className="access-options" role="radiogroup">
            {(["local", "key", "public"] as ShareMode[]).map((m) => (
              <label key={m} className={`access-option ${mode === m ? "on" : ""}`}>
                <input type="radio" name={`access-${s.name}`} checked={mode === m} disabled={busy} onChange={() => setMode(m)} />
                <span>
                  <b>{ACCESS_WORD[m]}</b>
                  <span className="sub">{m === "local" ? `Reachable only from your network at ${lan ?? "its LAN address"}.` : m === "key" ? "Served through the site; the link carries a key once and sets a cookie." : "Served through the site; anyone with the link, no login."}</span>
                </span>
              </label>
            ))}
          </div>
          {!s.share?.public_base && mode !== "local" && <div className="sub" style={{ color: "var(--warn)", margin: "8px 0" }}>MINIAPP_PUBLIC_URL is not set: the link has no public address yet.</div>}
          {mode !== "local" && s.share?.url && (
            <>
              <label className="field">Link</label>
              <div className="share-field">
                <input className="field mono" readOnly value={s.share.url} onFocus={(e) => e.target.select()} aria-label="share link" />
                <button className="btn small" onClick={() => copy(s.share!.url!, "link")}><Icon name="copy" size={13} /> Copy</button>
              </div>
              {mode === "key" && s.share.key && (
                <>
                  <label className="field">Key</label>
                  <div className="share-field">
                    <input className="field mono" readOnly value={s.share.key} onFocus={(e) => e.target.select()} aria-label="share key" />
                    <button className="btn small" onClick={() => copy(s.share!.key!, "key")}><Icon name="copy" size={13} /> Copy</button>
                    <button className="btn small" disabled={busy} onClick={() => setMode("key", true)} title="Mint a new key; the old link stops working">New key</button>
                  </div>
                </>
              )}
              <div className="sub" style={{ marginTop: 8 }}>The service lives under /s/{s.share.slug}/: pages must use relative links (or honour X-Forwarded-Prefix); plain HTTP only, no WebSocket.</div>
            </>
          )}
        </Sheet>
      )}
    </div>
  );
}
