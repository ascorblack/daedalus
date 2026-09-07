import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { confirmAsync } from "../ui";
import { timeAgo } from "../components";

type Entry = {
  id: number;
  at: string;
  kind: string;
  severity: "info" | "notice" | "warning" | "error";
  title: string;
  body: string;
  session_id: string | null;
  run_id: string | null;
  read: number;
};

const DOT: Record<Entry["severity"], string> = { info: "var(--muted)", notice: "var(--info)", warning: "var(--warn)", error: "var(--bad)" };

export function InboxScreen({ toast, onOpen, onUnread }: { toast: (t: string) => void; onOpen: (id: string) => void; onUnread?: (n: number) => void }) {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [unread, setUnread] = useState(0);
  const [onlyUnread, setOnlyUnread] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ entries: Entry[]; unread: number }>(`/api/inbox?unread=${onlyUnread ? 1 : 0}&limit=200`);
      setEntries(r.entries);
      setUnread(r.unread);
      onUnread?.(r.unread);
    } catch (e) {
      toast((e as Error).message);
    }
  }, [toast, onlyUnread, onUnread]);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  async function markAll() {
    await api.post("/api/inbox/read", {});
    load();
  }

  async function toggle(e: Entry) {
    setOpen(open === e.id ? null : e.id);
    if (!e.read) {
      await api.post("/api/inbox/read", { ids: [e.id] });
      setEntries((list) => (list ?? []).map((x) => (x.id === e.id ? { ...x, read: 1 } : x)));
      setUnread((n) => Math.max(0, n - 1));
      onUnread?.(Math.max(0, unread - 1));
    }
  }

  async function remove(id: number) {
    if (!(await confirmAsync("Remove this inbox entry?"))) return;
    try {
      await api.delete(`/api/inbox/${id}`);
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <>
      <div className="btnrow" style={{ marginTop: 0, marginBottom: 12 }}>
        <button className={`btn small ${onlyUnread ? "primary" : ""}`} onClick={() => setOnlyUnread((v) => !v)}>
          unread only
        </button>
        <button className="btn small" onClick={markAll} disabled={unread === 0}>
          mark all read {unread > 0 ? `(${unread})` : ""}
        </button>
      </div>
      {entries === null && <div className="empty">Loading…</div>}
      {entries?.length === 0 && <div className="empty">{onlyUnread ? "Nothing unread." : "Nothing happened while you were away."}</div>}
      {entries?.map((e) => (
        <div key={e.id} className={`card pressable ${e.read ? "" : "unread"}`} onClick={() => toggle(e)}>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <span style={{ width: 9, height: 9, borderRadius: 999, background: DOT[e.severity], marginTop: 6, flex: "none", opacity: e.read ? 0.4 : 1 }} />
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="title" style={{ fontWeight: e.read ? 500 : 700 }}>{e.title}</div>
              <div className="sub">
                {e.kind.replace(/_/g, " ")} · {timeAgo(e.at)}
                {e.session_id && (
                  <>
                    {" · "}
                    <a
                      onClick={(ev) => {
                        ev.stopPropagation();
                        onOpen(e.session_id!);
                      }}
                    >
                      open session
                    </a>
                  </>
                )}
              </div>
              {open === e.id && e.body && <pre className="diff" style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{e.body}</pre>}
            </div>
            <button
              className="btn small"
              onClick={(ev) => {
                ev.stopPropagation();
                remove(e.id);
              }}
            >
              ✕
            </button>
          </div>
        </div>
      ))}
    </>
  );
}
