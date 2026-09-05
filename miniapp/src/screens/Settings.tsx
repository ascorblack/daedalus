import { useEffect, useState } from "react";
import { api, Settings } from "../api";

export function SettingsScreen({ toast }: { toast: (t: string) => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [status, setStatus] = useState<any>(null);
  useEffect(() => {
    api.get<Settings>("/api/settings").then(setS).catch((e) => toast((e as Error).message));
    api.get("/api/status").then(setStatus).catch(() => setStatus(null));
  }, [toast]);
  if (!s) return <div className="empty">Loading…</div>;

  async function save(patch: Partial<Settings>) {
    try {
      const next = await api.put<Settings>("/api/settings", patch);
      setS({ ...next, providers_available: next.providers_available ?? s?.providers_available ?? [] });
      toast("saved");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Model
        </div>
        <label className="field">Provider</label>
        <select className="field" value={s.model.provider} onChange={(e) => save({ model: { ...s.model, provider: e.target.value } })}>
          {(s.providers_available ?? []).map((p) => (
            <option key={p}>{p}</option>
          ))}
          {!(s.providers_available ?? []).includes(s.model.provider) && <option>{s.model.provider}</option>}
        </select>
        <label className="field">Model name</label>
        <input className="field" defaultValue={s.model.name} onBlur={(e) => e.target.value !== s.model.name && save({ model: { ...s.model, name: e.target.value } })} />
        <label className="field">Thinking</label>
        <div className="btnrow" style={{ marginTop: 0 }}>
          <button className={`btn small ${s.model.thinking ? "primary" : ""}`} onClick={() => save({ model: { ...s.model, thinking: !s.model.thinking } })}>
            {s.model.thinking ? "on" : "off"}
          </button>
          {["low", "medium", "high"].map((e) => (
            <button key={e} className={`btn small ${s.model.reasoning_effort === e ? "primary" : ""}`} onClick={() => save({ model: { ...s.model, reasoning_effort: e } })}>
              {e}
            </button>
          ))}
        </div>
        <label className="field">Fallback chain (comma-separated provider ids)</label>
        <input className="field" defaultValue={s.model.chain.join(", ")} onBlur={(e) => save({ model: { ...s.model, chain: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) } })} />
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Self-change
        </div>
        <div className="btnrow" style={{ marginTop: 0 }}>
          {["manual", "auto"].map((m) => (
            <button key={m} className={`btn small ${s.self_change.approval === m ? "primary" : ""}`} onClick={() => save({ self_change: { ...s.self_change, approval: m } })}>
              {m} approval
            </button>
          ))}
          <button className={`btn small ${s.self_change.auto_rebuild ? "primary" : ""}`} onClick={() => save({ self_change: { ...s.self_change, auto_rebuild: !s.self_change.auto_rebuild } })}>
            auto rebuild {s.self_change.auto_rebuild ? "on" : "off"}
          </button>
        </div>
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Limits & alerts
        </div>
        <div className="sub">daily cap: ${(s as any).usd_per_day} — set in the environment, enforced by the supervisor</div>
        <label className="field">Max iterations per run</label>
        <input className="field" type="number" defaultValue={s.limits.max_iterations} onBlur={(e) => save({ limits: { ...s.limits, max_iterations: Number(e.target.value) } })} />
        <label className="field">Balance alert thresholds (USD, comma-separated)</label>
        <input className="field" defaultValue={s.balance.thresholds_usd.join(", ")} onBlur={(e) => save({ balance: { ...s.balance, thresholds_usd: e.target.value.split(",").map(Number).filter((n) => !Number.isNaN(n)) } })} />
        <label className="field">Balance poll interval (seconds)</label>
        <input className="field" type="number" defaultValue={s.balance.poll_seconds} onBlur={(e) => save({ balance: { ...s.balance, poll_seconds: Number(e.target.value) } })} />
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Chat & scheduler
        </div>
        <label className="field">Telegram verbosity</label>
        <div className="btnrow" style={{ marginTop: 0 }}>
          {[0, 1, 2].map((v) => (
            <button key={v} className={`btn small ${s.telegram.verbosity === v ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, verbosity: v } })}>
              {v}
            </button>
          ))}
        </div>
        <label className="field">Scheduled runs</label>
        <div className="btnrow" style={{ marginTop: 0 }}>
          {["per_task", "per_run"].map((m) => (
            <button key={m} className={`btn small ${s.scheduler.topic_mode === m ? "primary" : ""}`} onClick={() => save({ scheduler: { ...s.scheduler, topic_mode: m } })}>
              one topic {m === "per_task" ? "per task" : "per run"}
            </button>
          ))}
        </div>
      </div>

      {status && (
        <div className="card">
          <div className="section-title" style={{ marginTop: 0 }}>
            Runtime
          </div>
          <div className="sub">providers: {status.providers?.join(", ")}</div>
          {status.supervisor ? (
            <div className="sub">
              bot {String(status.supervisor.bot).slice(0, 10)} · core {String(status.supervisor.core).slice(0, 10)} · {status.supervisor.child_running ? "running" : "stopped"}
            </div>
          ) : (
            <div className="sub">supervisor: not connected (development mode)</div>
          )}
          {status.budget_exceeded && <div className="sub" style={{ color: "var(--bad)" }}>budget exceeded: {status.budget_exceeded}</div>}
        </div>
      )}
    </>
  );
}
