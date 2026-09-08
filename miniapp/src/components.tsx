import { useEffect, useState } from "react";
import { api, LoopView, ServiceView, ToolInfo } from "./api";
import { Icon } from "./icons";
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

export function Pill({ status }: { status: string }) {
  return <span className={`pill ${status}`}>{status}</span>;
}

export function useToast(): [string | null, (t: string) => void] {
  const [toast, setToast] = useState<string | null>(null);
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 2400);
    return () => clearTimeout(id);
  }, [toast]);
  return [toast, setToast];
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
  if (!iso) return "";
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function fmtUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "free";
  if (value === 0) return "$0";
  if (value < 0.01) return "< $0.01";
  return `${value.toFixed(2)}`;
}

export function fmtInt(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString();
}

/** One hosted service: status, address, log and stop. Shared by the session screen and the Services tab. */
export function ServiceRow({ s, sessionId, onChange, toast, onLogs }: { s: ServiceView; sessionId: string; onChange: () => void; toast: (t: string) => void; onLogs: (text: string) => void }) {
  async function stop() {
    if (!(await confirmAsync(`Stop service "${s.name}"?`))) return;
    try {
      await api.post(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/stop`);
      toast(`${s.name}: stopped`);
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
  return (
    <div className={`service-row ${s.status}`}>
      <span className={`dot ${s.status}`} />
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="service-name">
          {s.name}
          {s.url && s.status === "running" ? (
            <a className="service-url" href={s.url} target="_blank" rel="noreferrer">{s.url.replace(/^https?:\/\//, "")}</a>
          ) : (
            <span className="sub"> · {s.status}{s.note ? `: ${s.note}` : ""}</span>
          )}
        </div>
        <div className="sub mono service-cmd" title={s.command}>{s.command}</div>
      </div>
      <button className="iconbtn small" onClick={logs} title="Log" aria-label="log"><Icon name="file" size={15} /></button>
      {s.status === "running" && <button className="iconbtn small" onClick={stop} title="Stop" aria-label="stop"><Icon name="stop" size={15} /></button>}
    </div>
  );
}

