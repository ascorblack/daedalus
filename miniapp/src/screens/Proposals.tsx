import { useCallback, useEffect, useState } from "react";
import { api, Proposal } from "../api";
import { Pill, timeAgo } from "../components";
import { PageHeader } from "../shell";

export function ProposalsScreen({ toast }: { toast: (t: string) => void }) {
  const [items, setItems] = useState<Proposal[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [diff, setDiff] = useState<string>("");
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      setItems(await api.get<Proposal[]>("/api/proposals"));
    } catch (e) {
      toast((e as Error).message);
    }
  }, [toast]);
  useEffect(() => {
    load();
  }, [load]);

  async function show(id: string) {
    setOpen(id);
    setDiff("loading…");
    try {
      setDiff((await api.get<{ diff: string }>(`/api/proposals/${id}/diff`)).diff);
    } catch (e) {
      setDiff(`could not load the diff: ${(e as Error).message}`);
    }
  }

  async function decide(id: string, decision: "approve" | "reject") {
    try {
      const r = await api.post<{ result: string }>(`/api/proposals/${id}/decide`, { decision, reason: reasons[id] ?? "" });
      toast(r.result);
      setReasons((m) => ({ ...m, [id]: "" }));
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const pending = (items ?? []).filter((p) => p.status === "pending").length;
  return (
    <>
      <PageHeader title="Changes" subtitle={items ? (pending ? `${pending} waiting for you` : `${items.length} proposal${items.length === 1 ? "" : "s"}`) : undefined} />
      <div className="screen narrow">
      {items === null && <div className="empty">Loading…</div>}
      {items?.length === 0 && <div className="empty"><b>No change proposals yet</b><div>The agent opens one when it changes its own code.</div></div>}
      {(items ?? []).map((p) => (
        <div key={p.id} className="card">
          <div className="row">
            <div className="grow">
              <div className="title">{p.title}</div>
              <div className="sub">
                {p.repo} · {p.branch} · {timeAgo(p.created_at)}
                {p.pr_url && (
                  <>
                    {" · "}
                    <a href={p.pr_url} target="_blank" rel="noreferrer">
                      PR #{p.pr_number}
                    </a>
                  </>
                )}
              </div>
            </div>
            <Pill status={p.status === "merged" ? "done" : p.status === "pending" ? "waiting" : p.status === "rejected" || p.status === "closed" ? p.status : "failed"} />
          </div>
          <div style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{p.summary}</div>
          {p.reason && <div className="sub">reason: {p.reason}</div>}
          <div className="btnrow">
            <button className="btn small" onClick={() => (open === p.id ? setOpen(null) : show(p.id))}>
              {open === p.id ? "hide diff" : "diff"}
            </button>
            {p.status === "pending" && (
              <>
                <button className="btn small primary" onClick={() => decide(p.id, "approve")}>
                  Approve
                </button>
                <button className="btn small danger" onClick={() => decide(p.id, "reject")}>
                  Reject
                </button>
              </>
            )}
          </div>
          {p.status === "pending" && <input className="field" style={{ marginTop: 8 }} placeholder="reason (optional, sent to the agent on rejection)" value={reasons[p.id] ?? ""} onChange={(e) => setReasons((m) => ({ ...m, [p.id]: e.target.value }))} />}
          {open === p.id && <Diff text={diff} />}
        </div>
      ))}
      </div>
    </>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => {
        const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
