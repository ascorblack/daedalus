import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmtInt, fmtUsd } from "../components";

type Daily = { day: string; provider_id: string; model: string; calls: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; reasoning_tokens: number; cost_usd: number | null; unmetered: number };
type Recent = {
  at: string;
  provider_id: string;
  model: string;
  purpose: string;
  session_id: string | null;
  session_title: string | null;
  run_id: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  cost_usd: number | null;
  duration_ms: number;
  raw: Record<string, unknown>;
};
type BySession = { session_id: string | null; title: string | null; calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null; unmetered: number };
type UsageData = { daily: Daily[]; recent: Recent[]; sessions: BySession[] };

const PURPOSE_LABEL: Record<string, string> = { stream: "turn", structured: "compaction", text: "text" };

function fmtMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString();
}

function dayLabel(day: string): string {
  const today = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  return day === today ? "today" : day === yesterday ? "yesterday" : day.slice(5);
}

export function UsageScreen() {
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [balance, setBalance] = useState<{ balances: Record<string, number | null>; thresholds: number[] } | null>(null);
  const [provider, setProvider] = useState<string>("all");
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => {
    api.get<UsageData>("/api/usage?days=14").then(setUsage).catch(() => setUsage({ daily: [], recent: [], sessions: [] }));
    api.get<{ balances: Record<string, number | null>; thresholds: number[] }>("/api/balance").then(setBalance).catch(() => setBalance(null));
  }, []);

  const today = new Date().toISOString().slice(0, 10);
  const todayRows = useMemo(() => (usage?.daily ?? []).filter((d) => d.day === today), [usage, today]);
  const providers = useMemo(() => Array.from(new Set((usage?.recent ?? []).map((r) => r.provider_id))), [usage]);
  if (!usage) return <div className="empty">Loading…</div>;
  const sum = (rows: Daily[], key: keyof Daily) => rows.reduce((a, r) => a + ((r[key] as number) ?? 0), 0);
  const todayCost = todayRows.some((r) => r.cost_usd !== null) ? sum(todayRows, "cost_usd") : null;
  const recent = usage.recent.filter((r) => provider === "all" || r.provider_id === provider);
  const avgMs = recent.length ? Math.round(recent.reduce((a, r) => a + r.duration_ms, 0) / recent.length) : 0;

  // group daily rows by day for a readable table
  const days = new Map<string, Daily[]>();
  for (const d of usage.daily) days.set(d.day, [...(days.get(d.day) ?? []), d]);

  return (
    <>
      <div className="grid4">
        <div className="card stat">
          <span className="sub">spent today</span>
          <b>{fmtUsd(todayCost)}</b>
          <span className="sub">
            {fmtInt(sum(todayRows, "calls"))} calls{sum(todayRows, "unmetered") > 0 ? ` · ${fmtInt(sum(todayRows, "unmetered"))} unmetered` : ""}
          </span>
        </div>
        <div className="card stat">
          <span className="sub">tokens today</span>
          <b>{fmtTok(sum(todayRows, "input_tokens") + sum(todayRows, "output_tokens"))}</b>
          <span className="sub">
            {fmtTok(sum(todayRows, "cache_read_tokens"))} cached · {fmtTok(sum(todayRows, "reasoning_tokens"))} reasoning
          </span>
        </div>
      </div>

      {balance && Object.keys(balance.balances).length > 0 && (
        <div className="card">
          <div className="section-title" style={{ marginTop: 0 }}>
            Balances
          </div>
          {Object.entries(balance.balances).map(([p, b]) => (
            <div key={p} className="row">
              <div className="grow">{p}</div>
              <b>{b === null ? "unavailable" : fmtUsd(b)}</b>
            </div>
          ))}
          <div className="sub">alerts below: {balance.thresholds.map((t) => fmtUsd(t)).join(", ")}</div>
        </div>
      )}

      {usage.sessions.length > 0 && (
        <>
          <div className="section-title">By session, last 14 days</div>
          <div className="card">
            {usage.sessions.map((s, i) => (
              <div key={i} className="usage-row" style={{ gridTemplateColumns: "1fr auto", cursor: "default" }}>
                <div className="what">
                  <div className="l1">
                    <span className="name">{s.title ?? (s.session_id ? s.session_id : "outside sessions")}</span>
                  </div>
                  <div className="l2">
                    <span>{fmtInt(s.calls)} calls</span>
                    <span>{fmtTok(s.input_tokens)}↑ {fmtTok(s.output_tokens)}↓</span>
                  </div>
                </div>
                <div className="cost">
                  {fmtUsd(s.cost_usd)}
                  {s.unmetered > 0 && <div className="sub">{fmtInt(s.unmetered)} unmetered</div>}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="section-title">By day and model</div>
      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>day</th>
              <th>model</th>
              <th className="num">calls</th>
              <th className="num">in</th>
              <th className="num">out</th>
              <th className="num">cached</th>
              <th className="num">usd</th>
            </tr>
          </thead>
          <tbody>
            {Array.from(days.entries()).map(([day, rows]) =>
              rows.map((d, i) => (
                <tr key={`${day}-${i}`}>
                  <td style={{ color: i === 0 ? "inherit" : "transparent" }}>{dayLabel(day)}</td>
                  <td>
                    <span className="sub">{d.provider_id}/</span>
                    {d.model}
                  </td>
                  <td className="num">{fmtInt(d.calls)}</td>
                  <td className="num">{fmtTok(d.input_tokens)}</td>
                  <td className="num">{fmtTok(d.output_tokens)}</td>
                  <td className="num">{fmtTok(d.cache_read_tokens)}</td>
                  <td className="num">
                    {fmtUsd(d.cost_usd)}
                    {d.unmetered > 0 && <span className="sub"> +{d.unmetered} unmetered</span>}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>

      <div className="section-title">Recent calls</div>
      <div className="filters">
        {["all", ...providers].map((p) => (
          <button key={p} className={`btn small ${provider === p ? "primary" : ""}`} onClick={() => setProvider(p)}>
            {p}
          </button>
        ))}
        <span className="sub" style={{ marginLeft: "auto", alignSelf: "center" }}>avg {fmtMs(avgMs)}</span>
      </div>
      <div className="card">
        {recent.slice(0, 80).map((r, i) => (
          <div key={i}>
            <div className="usage-row" onClick={() => setOpen(open === i ? null : i)}>
              <div className="when">
                {r.at.slice(11, 16)}
                <br />
                <span style={{ fontSize: 11 }}>{dayLabel(r.at.slice(0, 10))}</span>
              </div>
              <div className="what">
                <div className="l1">
                  <span className={`badge ${r.purpose}`}>{PURPOSE_LABEL[r.purpose] ?? r.purpose}</span>
                  <span className="name">{r.session_title ?? (r.session_id ? r.session_id : "—")}</span>
                </div>
                <div className="l2">
                  <span>
                    {r.provider_id}/{r.model}
                  </span>
                  <span>
                    {fmtTok(r.input_tokens)}↑ {fmtTok(r.output_tokens)}↓{r.cache_read_tokens ? ` · ${fmtTok(r.cache_read_tokens)} cached` : ""}
                    {r.reasoning_tokens ? ` · ${fmtTok(r.reasoning_tokens)} reasoning` : ""}
                  </span>
                </div>
              </div>
              <div className="cost">
                {fmtUsd(r.cost_usd)}
                <br />
                <span className="ms">{fmtMs(r.duration_ms)}</span>
              </div>
            </div>
            {open === i && <pre className="usage-raw">{JSON.stringify(r.raw, null, 2)}</pre>}
          </div>
        ))}
        {recent.length === 0 && <div className="empty">No calls yet.</div>}
      </div>
    </>
  );
}
