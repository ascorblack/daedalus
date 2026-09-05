import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtInt, fmtUsd } from "../components";

type Daily = { day: string; provider_id: string; model: string; calls: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; reasoning_tokens: number; cost_usd: number | null };
type Recent = { at: string; provider_id: string; model: string; purpose: string; session_id: string | null; input_tokens: number; output_tokens: number; cache_read_tokens: number; reasoning_tokens: number; cost_usd: number | null; duration_ms: number; raw: Record<string, unknown> };

export function UsageScreen() {
  const [usage, setUsage] = useState<{ daily: Daily[]; recent: Recent[] } | null>(null);
  const [balance, setBalance] = useState<{ balances: Record<string, number | null>; thresholds: number[] } | null>(null);
  useEffect(() => {
    api.get<{ daily: Daily[]; recent: Recent[] }>("/api/usage?days=14").then(setUsage).catch(() => setUsage({ daily: [], recent: [] }));
    api.get<{ balances: Record<string, number | null>; thresholds: number[] }>("/api/balance").then(setBalance).catch(() => setBalance(null));
  }, []);
  if (!usage) return <div className="empty">Loading…</div>;
  const today = new Date().toISOString().slice(0, 10);
  const todayRows = usage.daily.filter((d) => d.day === today);
  const sum = (rows: Daily[], key: keyof Daily) => rows.reduce((a, r) => a + ((r[key] as number) ?? 0), 0);
  const todayCost = todayRows.some((r) => r.cost_usd !== null) ? sum(todayRows, "cost_usd") : null;
  return (
    <>
      <div className="grid2">
        <div className="card stat">
          <span className="sub">today</span>
          <b>{fmtUsd(todayCost)}</b>
          <span className="sub">{fmtInt(sum(todayRows, "calls"))} calls</span>
        </div>
        <div className="card stat">
          <span className="sub">tokens today</span>
          <b>{fmtInt(sum(todayRows, "input_tokens") + sum(todayRows, "output_tokens"))}</b>
          <span className="sub">{fmtInt(sum(todayRows, "cache_read_tokens"))} cached · {fmtInt(sum(todayRows, "reasoning_tokens"))} reasoning</span>
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
            {usage.daily.map((d, i) => (
              <tr key={i}>
                <td>{d.day.slice(5)}</td>
                <td>{d.model}</td>
                <td className="num">{fmtInt(d.calls)}</td>
                <td className="num">{fmtInt(d.input_tokens)}</td>
                <td className="num">{fmtInt(d.output_tokens)}</td>
                <td className="num">{fmtInt(d.cache_read_tokens)}</td>
                <td className="num">{fmtUsd(d.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="section-title">Recent calls (everything the provider reported)</div>
      {usage.recent.slice(0, 40).map((r, i) => (
        <details key={i} className="tool" style={{ marginBottom: 6 }}>
          <summary>
            {r.at.slice(11, 19)} {r.model} {r.purpose} · {fmtInt(r.input_tokens)}↑ {fmtInt(r.output_tokens)}↓ · {fmtUsd(r.cost_usd)} · {r.duration_ms}ms
          </summary>
          <pre>{JSON.stringify(r.raw, null, 2)}</pre>
        </details>
      ))}
    </>
  );
}
