import { useEffect, useRef, useState } from "react";
import { api, HeartbeatStatus, Preset, ProviderConf, Settings } from "../api";
import { timeAgo } from "../components";

const DEFAULT_KINDS = ["deepseek", "openrouter", "vllm", "openai_compat"];

function RulesEditor({ rules, fallback, onSave }: { rules: string; fallback: string; onSave: (rules: string) => void }) {
  const [text, setText] = useState(rules || fallback);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    setText(rules || fallback);
    setDirty(false);
  }, [rules, fallback]);
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>
        Working rules (system prompt)
      </div>
      <div className="sub">Shared by every session, after the persona and before the governance text. {rules ? "Custom text is in use." : "The built-in default is in use."}</div>
      <textarea className="field" rows={14} value={text} onChange={(e) => (setText(e.target.value), setDirty(true))} style={{ fontFamily: "var(--mono)", fontSize: 12.5, marginTop: 8 }} />
      <div className="btnrow">
        <button className="btn primary" disabled={!dirty} onClick={() => (onSave(text.trim() === fallback.trim() ? "" : text), setDirty(false))}>
          Save
        </button>
        <button className="btn" onClick={() => (setText(fallback), setDirty(true))}>
          Reset to default
        </button>
      </div>
    </div>
  );
}

type Patch = (id: string, patch: Record<string, unknown>) => Promise<Settings | undefined>;

/** "⟳ /models" button with a floating list over the card instead of inflating it. */
function ModelsMenu({ load, current, onPick }: { load: () => Promise<string[] | null>; current: string; onPick: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest?.(".models-menu-wrap")) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  async function toggle() {
    if (open) return setOpen(false);
    setOpen(true);
    setBusy(true);
    setModels(await load());
    setBusy(false);
  }
  const shown = (models ?? []).filter((m) => m.toLowerCase().includes(filter.toLowerCase()));
  return (
    <span className="models-menu-wrap">
      <button className="btn small" onClick={toggle} disabled={busy}>
        {busy ? "…" : "⟳ /models"}
      </button>
      {open && (
        <div className="models-menu">
          <input className="field" autoFocus placeholder="filter" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="models-menu-list">
            {busy && <div className="sub" style={{ padding: 8 }}>loading…</div>}
            {!busy && models === null && <div className="sub" style={{ padding: 8 }}>could not reach the endpoint</div>}
            {!busy && models !== null && shown.length === 0 && <div className="sub" style={{ padding: 8 }}>no models</div>}
            {shown.map((m) => (
              <button key={m} className={`models-menu-item ${m === current ? "on" : ""}`} onClick={() => (setOpen(false), onPick(m))}>
                {m}
              </button>
            ))}
          </div>
        </div>
      )}
    </span>
  );
}

function PresetRow({ id, p, isDefault, inChain, providers, onDefault, onChain, onPatch, onDelete, onLookup }: {
  id: string;
  p: Preset;
  isDefault: boolean;
  inChain: boolean;
  providers: string[];
  onDefault: () => void;
  onChain: () => void;
  onPatch: (patch: Partial<Preset>) => void;
  onDelete: () => void;
  onLookup: (provider: string) => Promise<string[] | null>;
}) {
  const [label, setLabel] = useState(p.label);
  const [model, setModel] = useState(p.model);
  const [more, setMore] = useState(false);
  useEffect(() => setLabel(p.label), [p.label]);
  useEffect(() => setModel(p.model), [p.model]);
  return (
    <div className={`preset ${isDefault ? "default" : ""}`}>
      <div className="row" style={{ gap: 8 }}>
        <button className={`radio ${isDefault ? "on" : ""}`} onClick={onDefault} aria-label="make default" title="default for new sessions" />
        <input className="field" style={{ flex: 1 }} value={label} placeholder={`${p.provider}/${p.model}`} onChange={(e) => setLabel(e.target.value)} onBlur={() => label.trim() !== p.label && onPatch({ label: label.trim() })} />
        <button className="btn small" onClick={() => setMore((m) => !m)} title="settings of this model">{more ? "less" : "more"}</button>
        <DeleteButton label="✕" onDelete={onDelete} />
      </div>
      <div className="row" style={{ gap: 8, marginTop: 6 }}>
        <select className="field" style={{ width: 130 }} value={p.provider} onChange={(e) => onPatch({ provider: e.target.value })}>
          {providers.map((x) => (
            <option key={x}>{x}</option>
          ))}
          {!providers.includes(p.provider) && <option>{p.provider}</option>}
        </select>
        <input className="field" style={{ flex: 1 }} value={model} placeholder="model id" onChange={(e) => setModel(e.target.value)} onBlur={() => model.trim() && model.trim() !== p.model && onPatch({ model: model.trim() })} />
        <ModelsMenu load={() => onLookup(p.provider)} current={p.model} onPick={(m) => onPatch({ model: m })} />
      </div>
      <div className="btnrow" style={{ marginTop: 6 }}>
        <button className={`btn small ${p.thinking ? "primary" : ""}`} onClick={() => onPatch({ thinking: !p.thinking })}>thinking {p.thinking ? "on" : "off"}</button>
        {["low", "medium", "high"].map((e) => (
          <button key={e} className={`btn small ${p.reasoning_effort === e ? "primary" : ""}`} disabled={!p.thinking} onClick={() => onPatch({ reasoning_effort: e })}>{e}</button>
        ))}
        <button className={`btn small ${p.images ? "primary" : ""}`} onClick={() => onPatch({ images: !p.images })}>images {p.images ? "on" : "off"}</button>
        {!isDefault && <button className={`btn small ${inChain ? "primary" : ""}`} onClick={onChain} title="use as a fallback when the default fails">{inChain ? "fallback ✓" : "fallback"}</button>}
        <span className="sub" style={{ marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 11 }}>{id}</span>
      </div>
      {more && (
        <div className="grid2">
          <NumField label="Context window (tokens)" value={p.context_window} min={8000} step={1000} onSave={(v) => onPatch({ context_window: v })} hint="history kept before compaction" />
          <NumField label="Max output per reply" value={p.max_output_tokens} min={1024} step={1000} onSave={(v) => onPatch({ max_output_tokens: v })} hint="max_tokens, thinking included" />
        </div>
      )}
    </div>
  );
}

function AddPresetRow({ providers, onAdd, toast }: { providers: string[]; onAdd: (id: string, preset: Preset) => void; toast: (t: string) => void }) {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState(providers[0] ?? "");
  const [model, setModel] = useState("");
  const [label, setLabel] = useState("");
  useEffect(() => { if (!providers.includes(provider)) setProvider(providers[0] ?? ""); }, [providers, provider]);
  if (!open)
    return (
      <button className="btn small" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>
        ＋ add model
      </button>
    );
  async function lookup(): Promise<string[] | null> {
    try {
      return (await api.post<{ models: string[] }>("/api/providers/lookup-models", { provider })).models;
    } catch (e) {
      toast((e as Error).message);
      return null;
    }
  }
  function add() {
    if (!provider || !model.trim()) {
      toast("pick a client and a model id");
      return;
    }
    const id = `${provider}.${model.trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "")}`;
    onAdd(id, { provider, model: model.trim(), label: label.trim(), thinking: true, reasoning_effort: "medium", images: false, context_window: 128000, max_output_tokens: 32000 });
    setOpen(false); setModel(""); setLabel("");
  }
  return (
    <div style={{ marginTop: 10 }}>
      <div className="row" style={{ gap: 8 }}>
        <select className="field" style={{ width: 130 }} value={provider} onChange={(e) => setProvider(e.target.value)}>
          {providers.map((x) => (
            <option key={x}>{x}</option>
          ))}
        </select>
        <input className="field" style={{ flex: 1 }} value={model} placeholder="model id" onChange={(e) => setModel(e.target.value)} />
        <ModelsMenu load={lookup} current={model} onPick={setModel} />
      </div>
      <div className="row" style={{ gap: 8, marginTop: 6 }}>
        <input className="field" style={{ flex: 1 }} value={label} placeholder="label (optional), e.g. Qwen fast" onChange={(e) => setLabel(e.target.value)} />
        <button className="btn small primary" onClick={add}>add</button>
        <button className="btn small" onClick={() => setOpen(false)}>cancel</button>
      </div>
    </div>
  );
}

function DeleteButton({ label, onDelete }: { label: string; onDelete: () => void }) {
  const [confirming, setConfirming] = useState(false);
  return (
    <button
      className={`btn small danger ${confirming ? "primary" : ""}`}
      onClick={() => {
        if (confirming) onDelete();
        else {
          setConfirming(true);
          setTimeout(() => setConfirming(false), 2500);
        }
      }}
    >
      {confirming ? "sure?" : label}
    </button>
  );
}

function ProviderBlock({ id, p, kinds, available, onPatch, onRemove }: {
  id: string;
  p: ProviderConf;
  kinds: string[];
  available: boolean;
  onPatch: Patch;
  onRemove: (id: string) => void;
}) {
  const [baseUrl, setBaseUrl] = useState(p.base_url);
  const [keyDraft, setKeyDraft] = useState("");
  useEffect(() => setBaseUrl(p.base_url), [p.base_url]);
  useEffect(() => setKeyDraft(""), [p.api_key_set]);
  return (
    <div style={{ borderTop: "1px solid var(--line, #333)", paddingTop: 8, marginTop: 8 }}>
      <div className="row" style={{ cursor: "default" }}>
        <b style={{ fontFamily: "var(--mono)", fontSize: 13 }}>{id}</b>
        <select className="field" style={{ flex: 1 }} value={p.kind} onChange={(e) => onPatch(id, { kind: e.target.value })}>
          {kinds.map((k) => (
            <option key={k}>{k}</option>
          ))}
        </select>
        <span className="sub">{available ? "ready" : "needs URL/key"}</span>
        <DeleteButton label="delete" onDelete={() => onRemove(id)} />
      </div>
      <div className="grid2">
        <div>
          <label className="field">base_url</label>
          <input
            className="field"
            value={baseUrl}
            placeholder="http://host:9000/v1"
            onChange={(e) => setBaseUrl(e.target.value)}
            onBlur={() => baseUrl.trim() !== p.base_url && baseUrl.trim() && onPatch(id, { base_url: baseUrl.trim() })}
          />
        </div>
        <div>
          <label className="field">api_key {p.api_key_set ? "(stored)" : "(optional)"}</label>
          <div className="row" style={{ padding: 0 }}>
            <input
              className="field"
              type="password"
              autoComplete="new-password"
              placeholder={p.api_key_set ? "•••• stored — type to replace" : "no key needed"}
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value)}
              onBlur={() => {
                const v = keyDraft.trim();
                if (v) {
                  onPatch(id, { api_key: v });
                  setKeyDraft("");
                }
              }}
            />
            {p.api_key_set && (
              <button className="btn small danger" title="remove stored key" onClick={() => onPatch(id, { api_key: "" })}>
                ✕
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function AddProviderRow({ kinds, onAdd, toast }: { kinds: string[]; onAdd: (id: string, baseUrl: string, kind: string) => void; toast: (t: string) => void }) {
  const [open, setOpen] = useState(false);
  const [id, setId] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [kind, setKind] = useState("vllm");
  function add() {
    const pid = id.trim();
    const base = baseUrl.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(pid)) {
      toast("id: letters, digits, - and _ only");
      return;
    }
    if (!base.startsWith("http")) {
      toast("base_url must start with http:// or https://");
      return;
    }
    onAdd(pid, base, kind);
    setId("");
    setBaseUrl("");
    setOpen(false);
  }
  if (!open)
    return (
      <button className="btn small" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>
        ＋ add client
      </button>
    );
  return (
    <div className="grid2" style={{ marginTop: 10 }}>
      <div>
        <label className="field">new client id</label>
        <input className="field" value={id} placeholder="local-vllm" onChange={(e) => setId(e.target.value)} />
      </div>
      <div>
        <label className="field">kind</label>
        <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
          {kinds.map((k) => (
            <option key={k}>{k}</option>
          ))}
        </select>
      </div>
      <div>
        <label className="field">base_url</label>
        <input className="field" value={baseUrl} placeholder="http://host:9000/v1" onChange={(e) => setBaseUrl(e.target.value)} />
      </div>
      <div className="btnrow" style={{ marginTop: 22 }}>
        <button className="btn small primary" onClick={add}>
          add
        </button>
        <button className="btn small" onClick={() => setOpen(false)}>
          cancel
        </button>
      </div>
    </div>
  );
}

function NumField({ label, value, min, step, onSave, hint }: { label: string; value: number; min?: number; step?: number; onSave: (v: number) => void; hint?: string }) {
  return (
    <div>
      <label className="field">{label}</label>
      <input className="field" type="number" min={min} step={step} defaultValue={value} onBlur={(e) => { const v = Number(e.target.value); if (!Number.isNaN(v) && v !== value && (min === undefined || v >= min)) onSave(v); }} />
      {hint && <div className="sub">{hint}</div>}
    </div>
  );
}

function TextField({ label, value, placeholder, onSave, hint }: { label: string; value: string; placeholder?: string; onSave: (v: string) => void; hint?: string }) {
  return (
    <div>
      <label className="field">{label}</label>
      <input className="field" defaultValue={value} placeholder={placeholder} onBlur={(e) => e.target.value.trim() !== value && onSave(e.target.value.trim())} />
      {hint && <div className="sub">{hint}</div>}
    </div>
  );
}

function ToolsTab({ s, save }: { s: Settings; save: (patch: any) => Promise<void> }) {
  const web = s.tools.web;
  const asr = s.asr;
  return (
    <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>Voice notes (speech-to-text)</div>
        <div className="sub">Any OpenAI-compatible /audio/transcriptions endpoint (OpenAI, a local whisper server). Empty URL = voice notes are attached as files only. The transcript is shown with ✓ Send / ✗ Discard before it reaches the agent.</div>
        <label className="field">Endpoint base URL</label>
        <input className="field" defaultValue={asr.url} placeholder="https://api.openai.com/v1" onBlur={(e) => save({ asr: { ...asr, api_key: "", url: e.target.value.trim() } })} />
        <label className="field">API key {asr.api_key_set ? "(set — leave empty to keep)" : ""}</label>
        <input className="field" type="password" defaultValue="" placeholder={asr.api_key_set ? "••••••" : ""} onBlur={(e) => e.target.value && save({ asr: { ...asr, api_key: e.target.value } })} />
        <div className="grid2">
          <div>
            <label className="field">Model</label>
            <input className="field" defaultValue={asr.model} onBlur={(e) => save({ asr: { ...asr, api_key: "", model: e.target.value.trim() } })} />
          </div>
          <div>
            <label className="field">Language hint (empty = auto)</label>
            <input className="field" defaultValue={asr.language} onBlur={(e) => save({ asr: { ...asr, api_key: "", language: e.target.value.trim() } })} />
          </div>
        </div>
        <div className="btnrow">
          <button className={`btn small ${asr.autosend ? "primary" : ""}`} onClick={() => save({ asr: { ...asr, api_key: "", autosend: !asr.autosend } })}>
            send without confirmation {asr.autosend ? "on" : "off"}
          </button>
        </div>
      </div>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>ImageView</div>
        <div className="sub">The agent's eyes: a separate image-capable model answers questions about pictures so the main context never carries pixels. Pick any model marked “images on” in General → Models.</div>
        <div className="grid2">
          <div>
            <label className="field">Model</label>
            <select className="field" value={s.vision.preset} onChange={(e) => save({ vision: { preset: e.target.value } })}>
              {Object.entries(s.presets ?? {}).filter(([, p]) => p.images).map(([id, p]) => (
                <option key={id} value={id}>{p.label || `${p.provider}/${p.model}`}</option>
              ))}
              {!(s.presets ?? {})[s.vision.preset]?.images && <option value={s.vision.preset}>{s.vision.preset || "(none image-capable)"}</option>}
            </select>
          </div>
          <NumField label="Max output tokens" value={s.vision.max_output_tokens} min={100} step={100} onSave={(v) => save({ vision: { max_output_tokens: v } })} />
        </div>
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>WebFetch & WebSearch</div>
        <div className="grid2">
          <NumField label="Fetch timeout (s)" value={web.fetch_timeout_seconds} min={1} onSave={(v) => save({ tools: { web: { fetch_timeout_seconds: v } } })} />
          <NumField label="Search timeout (s)" value={web.search_timeout_seconds} min={1} onSave={(v) => save({ tools: { web: { search_timeout_seconds: v } } })} />
          <NumField label="Fetch max chars" value={web.fetch_max_chars} min={1000} step={1000} onSave={(v) => save({ tools: { web: { fetch_max_chars: v } } })} />
          <NumField label="Search results" value={web.search_results} min={1} onSave={(v) => save({ tools: { web: { search_results: v } } })} />
        </div>
        <TextField label="Proxy (http/https/socks5 URL, empty = direct)" value={web.proxy} placeholder="socks5://127.0.0.1:1080" onSave={(v) => save({ tools: { web: { proxy: v } } })} />
        <TextField label="User agent" value={web.user_agent} onSave={(v) => save({ tools: { web: { user_agent: v } } })} />
        <div className="grid2">
          <TextField label="Search endpoint (DuckDuckGo HTML)" value={web.search_url} onSave={(v) => save({ tools: { web: { search_url: v } } })} />
          <TextField label="Search region" value={web.search_region} placeholder="wt-wt, ru-ru, us-en" onSave={(v) => save({ tools: { web: { search_region: v } } })} />
        </div>
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>Exec, Read, Find</div>
        <div className="grid2">
          <NumField label="Tool timeout (s)" value={s.limits.tool_timeout_seconds} min={10} step={30} onSave={(v) => save({ limits: { tool_timeout_seconds: v } })} hint="Exec default; the agent can ask for more per call." />
          <NumField label="Max output chars per call" value={s.tools.exec.max_output_chars} min={2000} step={5000} onSave={(v) => save({ tools: { exec: { max_output_chars: v } } })} hint="Longer output is clipped head+tail; the agent is told to use files." />
        </div>
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>MCP servers</div>
        <div className="sub">Configured under [mcp.servers.&lt;name&gt;] in config.toml; every session starts with them off and toggles them from its ⋯ menu.</div>
        <div className="sub" style={{ marginTop: 6 }}>{Object.keys((s as any).mcp?.servers ?? {}).join(", ") || "none configured"}</div>
        <div className="sub" style={{ marginTop: 6 }}>Changes here apply to the next tool call; no restart needed.</div>
      </div>
    </>
  );
}

type Check = { name: string; ok: boolean; message: string; severity: string; fix_hint: string; fixable: boolean; fixed: boolean };

function HealthTab({ toast }: { toast: (t: string) => void }) {
  const [data, setData] = useState<{ checks: Check[]; summary: Record<string, number> } | null>(null);
  const [busy, setBusy] = useState(false);
  const load = async (fix = false) => {
    setBusy(true);
    try {
      setData(fix ? await api.post("/api/doctor/fix") : await api.get("/api/doctor"));
      if (fix) toast("fixes applied");
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!data) return <div className="empty">{busy ? "Checking…" : "Loading…"}</div>;
  const mark = (c: Check) => (c.fixed ? "🔧" : c.ok ? "✅" : c.severity === "fail" ? "❌" : "⚠️");
  const fixable = data.checks.some((c) => !c.ok && c.fixable);
  return (
    <>
      <div className="card">
        <div className="row">
          <div className="grow">
            <b>{data.summary.ok} ok</b> · {data.summary.warn} warnings · {data.summary.fail} failures
          </div>
          <button className="btn small" disabled={busy} onClick={() => load(false)}>
            re-check
          </button>
          {fixable && (
            <button className="btn small primary" disabled={busy} onClick={() => load(true)}>
              apply safe fixes
            </button>
          )}
        </div>
      </div>
      <div className="card">
        {data.checks.map((c, i) => (
          <div key={i} className="row" style={{ alignItems: "flex-start", padding: "6px 0", borderTop: i ? "1px solid var(--line)" : undefined }}>
            <span style={{ flex: "none", width: 22 }}>{mark(c)}</span>
            <div className="grow" style={{ minWidth: 0 }}>
              <div>
                <b>{c.name}</b> <span className="sub">{c.message}</span>
              </div>
              {!c.ok && c.fix_hint && <div className="sub">→ {c.fix_hint}</div>}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function HeartbeatTab({ s, toast }: { s: Settings; toast: (t: string) => void }) {
  const [hb, setHb] = useState<HeartbeatStatus | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const load = () =>
    api
      .get<HeartbeatStatus>("/api/heartbeat")
      .then((r) => {
        setHb(r);
        if (!dirtyRef.current) setText(r.text);
      })
      .catch((e) => toast((e as Error).message));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!hb) return <div className="empty">Loading…</div>;

  async function put(patch: Record<string, unknown>) {
    try {
      const r = await api.put<HeartbeatStatus>("/api/heartbeat", patch);
      setHb(r);
      if ("text" in patch) {
        setDirty(false);
        dirtyRef.current = false;
        setText(r.text);
      }
      toast("saved");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function runNow() {
    try {
      const r = await api.post<{ session_id: string }>("/api/heartbeat/run");
      toast(`heartbeat started in ${r.session_id}`);
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const presets = Object.keys(s.presets ?? {});
  return (
    <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Heartbeat
        </div>
        <div className="sub">
          A periodic unattended check. The file below is its prompt; empty = nothing runs. The agent reports only what needs you and stays silent
          otherwise (the inbox records every check).
        </div>
        <div className="btnrow">
          <button className={`btn small ${hb.enabled ? "primary" : ""}`} onClick={() => put({ enabled: !hb.enabled })}>
            {hb.enabled ? "on" : "off"}
          </button>
          <button className="btn small" onClick={runNow} disabled={!text.trim() || hb.running}>
            run now
          </button>
          <span className="sub" style={{ alignSelf: "center" }}>
            {hb.armed ? "armed" : hb.enabled ? "on, but the file is empty" : "off"} · today {hb.runs_today}/{hb.max_runs_per_day} · last{" "}
            {hb.last_run ? timeAgo(hb.last_run) : "never"}
            {hb.running ? " · running" : ""}
          </span>
        </div>
        <label className="field">Every N minutes</label>
        <input className="field" type="number" defaultValue={hb.interval_minutes} onBlur={(e) => put({ interval_minutes: Number(e.target.value) })} />
        <label className="field">Active hours (UTC, HH:MM-HH:MM)</label>
        <input className="field" defaultValue={hb.active_hours} onBlur={(e) => put({ active_hours: e.target.value })} />
        <label className="field">Max runs per day</label>
        <input className="field" type="number" defaultValue={hb.max_runs_per_day} onBlur={(e) => put({ max_runs_per_day: Number(e.target.value) })} />
        <label className="field">Model preset (empty = default)</label>
        <select className="field" value={hb.preset} onChange={(e) => put({ preset: e.target.value })}>
          <option value="">default</option>
          {presets.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          HEARTBEAT.md
        </div>
        <textarea
          className="field"
          rows={12}
          value={text}
          placeholder={hb.template}
          onChange={(e) => {
            setText(e.target.value);
            setDirty(true);
            dirtyRef.current = true;
          }}
        />
        <div className="btnrow">
          <button className="btn primary" disabled={!dirty} onClick={() => put({ text })}>
            Save
          </button>
          <button className="btn small" onClick={() => { setText(hb.template ?? ""); setDirty(true); }}>
            insert template
          </button>
          <button className="btn small" onClick={() => { setText(""); setDirty(true); }}>
            clear (switches off)
          </button>
        </div>
      </div>
    </>
  );
}

export function SettingsScreen({ toast }: { toast: (t: string) => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [tab, setTab] = useState<"general" | "tools" | "heartbeat" | "health">("general");
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

  const kinds = s.provider_kinds ?? DEFAULT_KINDS;

  async function patchProvider(id: string, patch: Record<string, unknown>): Promise<Settings | undefined> {
    try {
      const next = await api.put<Settings>(`/api/providers/${encodeURIComponent(id)}`, patch);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
      return next;
    } catch (e) {
      toast((e as Error).message);
      return undefined;
    }
  }

  async function patchPreset(id: string, patch: Partial<Preset>) {
    try {
      const next = await api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, patch);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function removePreset(id: string) {
    try {
      const next = await api.delete<Settings>(`/api/presets/${encodeURIComponent(id)}`);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function lookupProviderModels(provider: string): Promise<string[] | null> {
    try {
      return (await api.post<{ models: string[] }>("/api/providers/lookup-models", { provider })).models;
    } catch (e) {
      toast((e as Error).message);
      return null;
    }
  }

  async function removeProvider(id: string) {
    try {
      const next = await api.delete<Settings>(`/api/providers/${encodeURIComponent(id)}`);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
      toast("provider removed");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const providerIds = Object.keys(s.providers ?? {});

  return (
    <>
      <div className="segmented">
        <button className={tab === "general" ? "on" : ""} onClick={() => setTab("general")}>General</button>
        <button className={tab === "tools" ? "on" : ""} onClick={() => setTab("tools")}>Tools</button>
        <button className={tab === "heartbeat" ? "on" : ""} onClick={() => setTab("heartbeat")}>Heartbeat</button>
        <button className={tab === "health" ? "on" : ""} onClick={() => setTab("health")}>Health</button>
      </div>
      {tab === "health" && <HealthTab toast={toast} />}
      {tab === "tools" && <ToolsTab s={s} save={save} />}
      {tab === "heartbeat" && <HeartbeatTab s={s} toast={toast} />}
      {tab === "general" && (
      <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Models
        </div>
        <div className="sub">A model is a client plus a model id. The marked one is the default for new sessions; any session can switch from the chip in its chat. Several models may share one client.</div>
        {Object.entries(s.presets ?? {}).map(([id, p]) => (
          <PresetRow
            key={id}
            id={id}
            p={p}
            isDefault={s.model.preset === id}
            inChain={(s.model.chain ?? []).includes(id)}
            providers={providerIds}
            onDefault={() => save({ model: { preset: id } as any })}
            onChain={() => save({ model: { chain: (s.model.chain ?? []).includes(id) ? s.model.chain.filter((c) => c !== id) : [...(s.model.chain ?? []), id] } as any })}
            onPatch={(patch) => void patchPreset(id, patch)}
            onDelete={() => void removePreset(id)}
            onLookup={lookupProviderModels}
          />
        ))}
        {Object.keys(s.presets ?? {}).length === 0 && <div className="sub" style={{ marginTop: 6 }}>No models yet: add one below.</div>}
        <AddPresetRow providers={providerIds} toast={toast} onAdd={(id, p) => void patchPreset(id, p)} />
        <div className="sub" style={{ marginTop: 10 }}>Fallback order: {(s.model.chain ?? []).length ? s.model.chain.join(" → ") : "none"} (tried after the default when it fails).</div>
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          Providers (clients)
        </div>
        <div className="sub">OpenAI-compatible endpoints. base_url is the API root (…/v1); a self-hosted vLLM needs no key.</div>
        {providerIds.map((id) => (
          <ProviderBlock
            key={id}
            id={id}
            p={s.providers[id]}
            kinds={kinds}
            available={(s.providers_available ?? []).includes(id)}
            onPatch={patchProvider}
            onRemove={(pid) => void removeProvider(pid)}
          />
        ))}
        <AddProviderRow kinds={kinds} toast={toast} onAdd={(pid, base, kind) => void patchProvider(pid, { kind, base_url: base })} />
      </div>

      <RulesEditor rules={s.prompt.rules} fallback={s.prompt.default_rules ?? ""} onSave={(rules) => save({ prompt: { rules } })} />

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
        <label className="field">Spend cap per run (USD, 0 = none; calls without a known price do not count)</label>
        <input className="field" type="number" step="0.5" defaultValue={s.limits.usd_per_run} onBlur={(e) => save({ limits: { ...s.limits, usd_per_run: Number(e.target.value) } })} />
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
        <div className="btnrow">
          <button className={`btn small ${s.telegram.reactions ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, reactions: !s.telegram.reactions } })}>
            reactions {s.telegram.reactions ? "on" : "off"}
          </button>
          <button className={`btn small ${s.telegram.topic_status_emoji ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, topic_status_emoji: !s.telegram.topic_status_emoji } })}>
            topic status emoji {s.telegram.topic_status_emoji ? "on" : "off"}
          </button>
          <button className={`btn small ${s.telegram.forward_unknown_commands ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, forward_unknown_commands: !s.telegram.forward_unknown_commands } })}>
            unknown /commands → agent {s.telegram.forward_unknown_commands ? "on" : "off"}
          </button>
        </div>
        <label className="field">Ignore messages older than (seconds, 0 = never)</label>
        <input className="field" type="number" defaultValue={s.telegram.stale_after_seconds} onBlur={(e) => save({ telegram: { ...s.telegram, stale_after_seconds: Number(e.target.value) } })} />
        <label className="field">Largest accepted file (MB)</label>
        <input className="field" type="number" defaultValue={s.telegram.max_inbound_file_mb} onBlur={(e) => save({ telegram: { ...s.telegram, max_inbound_file_mb: Number(e.target.value) } })} />
        <label className="field">Wait for a caption after a bare photo (seconds)</label>
        <input className="field" type="number" defaultValue={s.telegram.photo_caption_wait_seconds} onBlur={(e) => save({ telegram: { ...s.telegram, photo_caption_wait_seconds: Number(e.target.value) } })} />
        <label className="field">Mark a tool call as slow after (seconds)</label>
        <input className="field" type="number" defaultValue={s.telegram.slow_tool_seconds} onBlur={(e) => save({ telegram: { ...s.telegram, slow_tool_seconds: Number(e.target.value) } })} />
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
      )}
    </>
  );
}
