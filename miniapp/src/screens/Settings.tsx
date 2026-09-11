import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, IconName } from "../icons";
import { pathFor } from "../router";
import { PageHeader, go, useMedia } from "../shell";
import { api, telegram, HeartbeatStatus, Preset, ProviderConf, SearchBackendInfo, SearchCheck, Settings } from "../api";
import { numInput } from "../ui";
import { timeAgo } from "../components";

const DEFAULT_KINDS = ["deepseek", "openrouter", "opencode", "vllm", "openai_compat"];

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

function Toggle({ on, onClick, children, title, disabled }: { on: boolean; onClick: () => void; children: React.ReactNode; title?: string; disabled?: boolean }) {
  return (
    <button className={`btn small ${on ? "primary" : ""}`} onClick={onClick} title={title} disabled={disabled} aria-pressed={on}>
      {children}
    </button>
  );
}

/** One model: a compact line to scan, an expanded panel to edit. */
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
  const [open, setOpen] = useState(false);
  useEffect(() => setLabel(p.label), [p.label]);
  useEffect(() => setModel(p.model), [p.model]);
  const title = p.label || `${p.provider}/${p.model}`;
  return (
    <div className={`mrow ${isDefault ? "default" : ""} ${open ? "open" : ""}`}>
      <div className="mline">
        <button className={`radio ${isDefault ? "on" : ""}`} onClick={onDefault} aria-label="make default" title={isDefault ? "default for new sessions" : "make this the default for new sessions"} />
        <button className="mmain" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="mtitle">{title}</span>
          <span className="mmeta">{p.provider} · {p.model}</span>
        </button>
        <div className="mtags">
          {isDefault && <span className="pill idle">default</span>}
          {!isDefault && inChain && <span className="pill">fallback</span>}
          <span className="pill">{p.thinking ? `think ${p.reasoning_effort}` : "no thinking"}</span>
          {p.images && <span className="pill">images</span>}
          <span className="pill">{Math.round(p.context_window / 1000)}k</span>
        </div>
        <div className="mactions">
          <button className={`iconbtn small ${open ? "on" : ""}`} onClick={() => setOpen((o) => !o)} title={open ? "close" : "edit"} aria-label="edit"><span className={`chev ${open ? "down" : ""}`}>›</span></button>
          <DeleteButton label="✕" onDelete={onDelete} />
        </div>
      </div>
      {open && (
        <div className="mpanel">
          <div className="mfields">
            <label className="mfield">
              <span>Label</span>
              <input className="field" value={label} placeholder={`${p.provider}/${p.model}`} onChange={(e) => setLabel(e.target.value)} onBlur={() => label.trim() !== p.label && onPatch({ label: label.trim() })} />
            </label>
            <label className="mfield">
              <span>Client</span>
              <select className="field" value={p.provider} onChange={(e) => onPatch({ provider: e.target.value })}>
                {providers.map((x) => (
                  <option key={x}>{x}</option>
                ))}
                {!providers.includes(p.provider) && <option>{p.provider}</option>}
              </select>
            </label>
            <label className="mfield wide">
              <span>Model id</span>
              <div className="row" style={{ gap: 8 }}>
                <input className="field" style={{ flex: 1, minWidth: 0 }} value={model} placeholder="model id" onChange={(e) => setModel(e.target.value)} onBlur={() => model.trim() && model.trim() !== p.model && onPatch({ model: model.trim() })} />
                <ModelsMenu load={() => onLookup(p.provider)} current={p.model} onPick={(m) => onPatch({ model: m })} />
              </div>
            </label>
            <label className="mfield">
              <span>Context window (tokens)</span>
              <input className="field" type="number" min={8000} step={1000} defaultValue={p.context_window} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null && v !== p.context_window) onPatch({ context_window: v }); }} />
            </label>
            <label className="mfield">
              <span>Max output per reply (thinking included)</span>
              <input className="field" type="number" min={1024} step={1000} defaultValue={p.max_output_tokens} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null && v !== p.max_output_tokens) onPatch({ max_output_tokens: v }); }} />
            </label>
          </div>
          <div className="btnrow">
            <Toggle on={p.thinking} onClick={() => onPatch({ thinking: !p.thinking })}>thinking {p.thinking ? "on" : "off"}</Toggle>
            <div className="segmented inline" role="group" aria-label="reasoning effort">
              {["low", "medium", "high"].map((e) => (
                <button key={e} className={p.reasoning_effort === e ? "on" : ""} disabled={!p.thinking} onClick={() => onPatch({ reasoning_effort: e })}>{e}</button>
              ))}
            </div>
            <Toggle on={p.images} onClick={() => onPatch({ images: !p.images })} title="the model accepts pictures">images {p.images ? "on" : "off"}</Toggle>
            {!isDefault && <Toggle on={inChain} onClick={onChain} title="tried after the default when it fails">{inChain ? "fallback ✓" : "use as fallback"}</Toggle>}
            <span className="sub mono mid">{id}</span>
          </div>
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
    <div className="mpanel add">
      <div className="mfields">
        <label className="mfield">
          <span>Client</span>
          <select className="field" value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.map((x) => (
              <option key={x}>{x}</option>
            ))}
          </select>
        </label>
        <label className="mfield wide">
          <span>Model id</span>
          <div className="row" style={{ gap: 8 }}>
            <input className="field" style={{ flex: 1, minWidth: 0 }} value={model} placeholder="model id" onChange={(e) => setModel(e.target.value)} />
            <ModelsMenu load={lookup} current={model} onPick={setModel} />
          </div>
        </label>
        <label className="mfield">
          <span>Label (optional)</span>
          <input className="field" value={label} placeholder="e.g. Qwen fast" onChange={(e) => setLabel(e.target.value)} />
        </label>
      </div>
      <div className="btnrow">
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

/** One client: id, kind and readiness on a line; the address and the key behind it. */
function ProviderBlock({ id, p, kinds, available, onPatch, onRemove }: {
  id: string;
  p: ProviderConf;
  kinds: string[];
  available: boolean;
  onPatch: Patch;
  onRemove: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [baseUrl, setBaseUrl] = useState(p.base_url);
  const [keyDraft, setKeyDraft] = useState("");
  useEffect(() => setBaseUrl(p.base_url), [p.base_url]);
  useEffect(() => setKeyDraft(""), [p.api_key_set]);
  return (
    <div className={`mrow ${open ? "open" : ""}`}>
      <div className="mline noradio">
        <button className="mmain" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="mtitle mono">{id}</span>
          <span className="mmeta">{p.kind} · {p.base_url || "no address"}</span>
        </button>
        <div className="mtags">
          <span className={`pill ${available ? "idle" : "waiting"}`}>{available ? "ready" : "needs URL or key"}</span>
          {p.api_key_set && <span className="pill">key stored</span>}
        </div>
        <div className="mactions">
          <button className={`iconbtn small ${open ? "on" : ""}`} onClick={() => setOpen((o) => !o)} title={open ? "close" : "edit"} aria-label="edit"><span className={`chev ${open ? "down" : ""}`}>›</span></button>
          <DeleteButton label="✕" onDelete={() => onRemove(id)} />
        </div>
      </div>
      {open && (
        <div className="mpanel">
          <div className="mfields">
            <label className="mfield">
              <span>Kind</span>
              <select className="field" value={p.kind} onChange={(e) => onPatch(id, { kind: e.target.value })}>
                {kinds.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
            </label>
            <label className="mfield wide">
              <span>base_url (the API root, usually …/v1)</span>
              <input
                className="field"
                value={baseUrl}
                placeholder="http://host:9000/v1"
                onChange={(e) => setBaseUrl(e.target.value)}
                onBlur={() => baseUrl.trim() !== p.base_url && baseUrl.trim() && onPatch(id, { base_url: baseUrl.trim() })}
              />
            </label>
            <label className="mfield wide">
              <span>api_key {p.api_key_set ? "(stored)" : "(optional; a key proxy or a self-hosted endpoint needs none)"}</span>
              <div className="row" style={{ gap: 8 }}>
                <input
                  className="field"
                  style={{ flex: 1, minWidth: 0 }}
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
                    forget key
                  </button>
                )}
              </div>
            </label>
          </div>
        </div>
      )}
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
    <div className="mpanel add">
      <div className="mfields">
        <label className="mfield">
          <span>Client id</span>
          <input className="field" value={id} placeholder="local-vllm" onChange={(e) => setId(e.target.value)} />
        </label>
        <label className="mfield">
          <span>Kind</span>
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
            {kinds.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>
        <label className="mfield wide">
          <span>base_url</span>
          <input className="field" value={baseUrl} placeholder="http://host:9000/v1" onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
      </div>
      <div className="btnrow">
        <button className="btn small primary" onClick={add}>add</button>
        <button className="btn small" onClick={() => setOpen(false)}>cancel</button>
      </div>
    </div>
  );
}

type SpendView = { since: string; total: { spent_usd: number; unmetered: number; cap_usd: number }; per_provider: Record<string, { spent_usd: number; unmetered: number; cap_usd: number }> };

function TotalCaps({ s, save }: { s: Settings; save: (patch: any) => Promise<void> }) {
  const [spend, setSpend] = useState<SpendView | null>(null);
  const load = useCallback(() => {
    api.get<SpendView>("/api/limits/spend").then(setSpend).catch(() => setSpend(null));
  }, []);
  useEffect(load, [load, s.limits.total_since, s.limits.usd_total, s.limits.usd_total_per_provider]);
  const providers = Object.keys(s.providers ?? {});
  const caps = s.limits.usd_total_per_provider ?? {};
  return (
    <>
      <label className="field">Total spend cap, all sessions and providers (USD, 0 = none)</label>
      <div className="composer-row">
        <input className="field" type="number" step="1" min={0} defaultValue={s.limits.usd_total} onBlur={(e) => { const v = numInput(e.target.value, 0); if (v !== null && v !== s.limits.usd_total) save({ limits: { usd_total: v } }); }} />
        <span className="sub" style={{ whiteSpace: "nowrap" }}>spent {spend ? `$${spend.total.spent_usd.toFixed(2)}` : "…"}</span>
      </div>
      <label className="field">Per-provider total caps (USD, 0 = none)</label>
      {providers.map((pid) => (
        <div key={pid} className="composer-row" style={{ marginBottom: 6 }}>
          <span style={{ minWidth: 90 }}>{pid}</span>
          <input className="field" type="number" step="1" min={0} defaultValue={caps[pid] ?? 0} onBlur={(e) => { const v = numInput(e.target.value, 0); if (v !== null && v !== (caps[pid] ?? 0)) save({ limits: { usd_total_per_provider: { [pid]: v } } }); }} />
          <span className="sub" style={{ whiteSpace: "nowrap" }}>spent {spend?.per_provider[pid] ? `$${spend.per_provider[pid].spent_usd.toFixed(2)}` : "$0.00"}</span>
        </div>
      ))}
      <div className="row" style={{ alignItems: "center", gap: 10 }}>
        <span className="sub">counting since {s.limits.total_since ? new Date(s.limits.total_since).toLocaleString() : "the beginning"}</span>
        <button className="btn small" onClick={async () => { await api.post("/api/limits/reset-total"); load(); }}>reset counters</button>
      </div>
    </>
  );
}

function NumField({ label, value, min, step, onSave, hint }: { label: string; value: number; min?: number; step?: number; onSave: (v: number) => void; hint?: string }) {
  return (
    <div>
      <label className="field">{label}</label>
      <input className="field" type="number" min={min} step={step} defaultValue={value} onBlur={(e) => { const v = numInput(e.target.value, min); if (v !== null && v !== value) onSave(v); }} />
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

function SearchBlock({ s, save }: { s: Settings; save: (patch: any) => Promise<void> }) {
  const search = s.tools.web.search;
  const backends = s.search_backends ?? [];
  const [check, setCheck] = useState<SearchCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const saveSearch = (patch: any) => save({ tools: { web: { search: patch } } });
  const info = (id: string) => backends.find((b) => b.id === id);
  const usable = (b: SearchBackendInfo) => !b.needs_key || b.available !== false;
  const optionLabel = (b: SearchBackendInfo) => `${b.label}${b.needs_key ? (b.available === false ? " — no key in the key proxy" : b.available == null ? " — key proxy not reachable" : "") : ""}`;
  const runCheck = async (backend: string) => {
    setChecking(true);
    setCheckError("");
    try {
      setCheck(await api.post<SearchCheck>("/api/settings/search-check", { backend }));
    } catch (e) {
      setCheck(null);
      setCheckError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };
  const fallbackIds = search.fallback ?? [];
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>WebSearch</div>
      <div className="sub">One tool, switchable backends: the free self-hosted SearXNG by default, DuckDuckGo as the no-install fallback, paid APIs through the key proxy once their key is in keyproxy.env. The tool's contract does not change with the backend.</div>
      <label className="field">Backend</label>
      <select className="field" value={search.backend} onChange={(e) => saveSearch({ backend: e.target.value })}>
        {backends.map((b) => (
          <option key={b.id} value={b.id} disabled={!usable(b)}>{optionLabel(b)}</option>
        ))}
        {!backends.some((b) => b.id === search.backend) && <option value={search.backend}>{search.backend}</option>}
      </select>
      <label className="field">Fallbacks (tried in order when the backend fails or returns nothing)</label>
      <div className="btnrow">
        {backends.filter((b) => b.id !== search.backend).map((b) => {
          const on = fallbackIds.includes(b.id);
          return (
            <button
              key={b.id}
              className={`btn small ${on ? "primary" : ""}`}
              disabled={!on && !usable(b)}
              title={optionLabel(b)}
              onClick={() => saveSearch({ fallback: on ? fallbackIds.filter((x) => x !== b.id) : [...fallbackIds, b.id] })}
            >
              {on ? `${fallbackIds.indexOf(b.id) + 1}. ` : ""}{b.id}
            </button>
          );
        })}
      </div>
      <div className="grid2">
        <NumField label="Results per search" value={search.results} min={1} onSave={(v) => saveSearch({ results: v })} />
        <NumField label="Search timeout (s)" value={search.timeout_seconds} min={1} onSave={(v) => saveSearch({ timeout_seconds: v })} />
      </div>
      {search.backend === "searxng" || fallbackIds.includes("searxng") ? (
        <>
          <div className="section-title">SearXNG</div>
          <TextField label="URL" value={search.searxng.url} onSave={(v) => saveSearch({ searxng: { url: v } })} />
          <TextField label="Engines (comma-separated; empty = the instance's defaults)" value={search.searxng.engines} placeholder="google,duckduckgo,bing" onSave={(v) => saveSearch({ searxng: { engines: v } })} />
          <div className="grid2">
            <TextField label="Categories" value={search.searxng.categories} placeholder="general" onSave={(v) => saveSearch({ searxng: { categories: v } })} />
            <NumField label="Safe search (0–2)" value={search.searxng.safesearch} min={0} onSave={(v) => saveSearch({ searxng: { safesearch: Math.min(2, v) } })} />
          </div>
        </>
      ) : null}
      {search.backend === "duckduckgo" || fallbackIds.includes("duckduckgo") ? (
        <>
          <div className="section-title">DuckDuckGo</div>
          <div className="grid2">
            <TextField label="HTML endpoint" value={search.duckduckgo.url} onSave={(v) => saveSearch({ duckduckgo: { url: v } })} />
            <TextField label="Region" value={search.duckduckgo.region} placeholder="wt-wt, ru-ru, us-en" onSave={(v) => saveSearch({ duckduckgo: { region: v } })} />
          </div>
        </>
      ) : null}
      {search.backend === "serper" || fallbackIds.includes("serper") ? (
        <>
          <div className="section-title">Serper (Google)</div>
          <div className="grid2">
            <TextField label="Country (gl, empty = Google's default)" value={search.serper.gl} placeholder="ru, us" onSave={(v) => saveSearch({ serper: { gl: v } })} />
            <TextField label="Language (hl, empty = per query)" value={search.serper.hl} placeholder="ru, en" onSave={(v) => saveSearch({ serper: { hl: v } })} />
          </div>
        </>
      ) : null}
      {search.backend === "keenable" || fallbackIds.includes("keenable") ? (
        <>
          <div className="section-title">Keenable</div>
          <NumField label="Snippet length (chars)" value={search.keenable.snippet_max_length} min={180} step={60} onSave={(v) => saveSearch({ keenable: { snippet_max_length: v } })} />
        </>
      ) : null}
      {search.backend === "tavily" || fallbackIds.includes("tavily") ? (
        <>
          <div className="section-title">Tavily</div>
          <label className="field">Search depth</label>
          <select className="field" value={search.tavily.depth} onChange={(e) => saveSearch({ tavily: { depth: e.target.value } })}>
            {["basic", "advanced", "fast", "ultra-fast"].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </>
      ) : null}
      {search.backend === "exa" || fallbackIds.includes("exa") ? (
        <>
          <div className="section-title">Exa</div>
          <label className="field">Search type</label>
          <select className="field" value={search.exa.type} onChange={(e) => saveSearch({ exa: { type: e.target.value } })}>
            {["auto", "instant", "fast", "deep"].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </>
      ) : null}
      <div className="btnrow" style={{ marginTop: 12 }}>
        <button className="btn small primary" disabled={checking} onClick={() => runCheck("")}>{checking ? "checking…" : "Check the configured chain"}</button>
        <button className="btn small" disabled={checking || !info(search.backend)} onClick={() => runCheck(search.backend)}>Check {search.backend} alone</button>
      </div>
      {checkError && <div className="sub" style={{ color: "var(--bad)" }}>{checkError}</div>}
      {check && (
        <div className="sub" style={{ marginTop: 8 }}>
          {check.attempts.map((a) => (
            <div key={a.backend}>
              {a.backend}: {a.error ? `error — ${a.error}` : `${a.hits} results`} ({a.ms} ms)
            </div>
          ))}
          {check.hits.slice(0, 3).map((h) => (
            <div key={h.url} style={{ marginTop: 4 }}>
              <div>{h.title}{h.source ? ` · ${h.source}` : ""}</div>
              <div style={{ wordBreak: "break-all" }}>{h.url}</div>
            </div>
          ))}
        </div>
      )}
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
        <div className="sub">Any OpenAI-compatible /audio/transcriptions endpoint: one of the configured providers (OpenRouter, a vLLM with a Whisper model — its key stays in the key proxy) or a URL of its own. Nothing configured = voice notes are attached as files only and the site has no microphone. The agent receives the words marked as a transcript, never the audio; in Telegram the transcript is shown with ✓ Send / ✗ Discard first.</div>
        <label className="field">Provider</label>
        <select className="field" value={asr.provider || ""} onChange={(e) => save({ asr: { ...asr, api_key: "", provider: e.target.value } })}>
          <option value="">custom endpoint (URL + key below)</option>
          {Object.keys(s.providers).map((pid) => (
            <option key={pid} value={pid}>{pid}{s.providers[pid].base_url ? ` · ${s.providers[pid].base_url.replace(/^https?:\/\//, "")}` : ""}</option>
          ))}
        </select>
        {!asr.provider && (
          <>
            <label className="field">Endpoint base URL</label>
            <input className="field" defaultValue={asr.url} placeholder="https://api.openai.com/v1" onBlur={(e) => save({ asr: { ...asr, api_key: "", url: e.target.value.trim() } })} />
            <label className="field">API key {asr.api_key_set ? "(set — leave empty to keep)" : ""}</label>
            <input className="field" type="password" defaultValue="" placeholder={asr.api_key_set ? "••••••" : ""} onBlur={(e) => e.target.value && save({ asr: { ...asr, api_key: e.target.value } })} />
          </>
        )}
        <div className="grid2">
          <div>
            <label className="field">Model</label>
            <input className="field" defaultValue={asr.model} placeholder={asr.provider === "openrouter" ? "openai/whisper-1" : "whisper-1"} onBlur={(e) => save({ asr: { ...asr, api_key: "", model: e.target.value.trim() } })} />
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
        <div className="section-title" style={{ marginTop: 0 }}>WebFetch</div>
        <div className="grid2">
          <NumField label="Fetch timeout (s)" value={web.fetch_timeout_seconds} min={1} onSave={(v) => save({ tools: { web: { fetch_timeout_seconds: v } } })} />
          <NumField label="Fetch max chars" value={web.fetch_max_chars} min={1000} step={1000} onSave={(v) => save({ tools: { web: { fetch_max_chars: v } } })} />
        </div>
        <TextField label="Proxy (http/https/socks5 URL, empty = direct)" value={web.proxy} placeholder="socks5://127.0.0.1:1080" hint="Used by WebFetch and by the directly scraped search backend (DuckDuckGo); SearXNG and the key proxy are reached directly." onSave={(v) => save({ tools: { web: { proxy: v } } })} />
        <TextField label="User agent" value={web.user_agent} onSave={(v) => save({ tools: { web: { user_agent: v } } })} />
      </div>

      <SearchBlock s={s} save={save} />

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
        <input className="field" type="number" defaultValue={hb.interval_minutes} onBlur={(e) => { const v = numInput(e.target.value, 1); if (v !== null) put({ interval_minutes: v }); }} />
        <label className="field">Active hours (UTC, HH:MM-HH:MM)</label>
        <input className="field" defaultValue={hb.active_hours} onBlur={(e) => put({ active_hours: e.target.value })} />
        <label className="field">Max runs per day</label>
        <input className="field" type="number" defaultValue={hb.max_runs_per_day} onBlur={(e) => { const v = numInput(e.target.value, 0); if (v !== null) put({ max_runs_per_day: v }); }} />
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

/** The doctor's checks as a screen of their own. */
export function HealthScreen({ toast }: { toast: (t: string) => void }) {
  return (
    <>
      <PageHeader title="Health" />
      <div className="screen narrow">
        <HealthTab toast={toast} />
        {!telegram()?.initData && (
          <div className="btnrow">
            <button
              className="btn small"
              onClick={async () => {
                try {
                  await api.post("/api/auth/logout");
                  sessionStorage.removeItem("daedalus_token");
                } finally {
                  window.location.reload();
                }
              }}
            >
              Log out of this browser
            </button>
          </div>
        )}
      </div>
    </>
  );
}

type Section = "models" | "rules" | "limits" | "tools" | "chat" | "heartbeat" | "about";
const SECTIONS: { id: Section; label: string; hint: string; icon: IconName }[] = [
  { id: "models", label: "Models & providers", hint: "which model opens a session, the fallbacks, the clients", icon: "model" },
  { id: "rules", label: "Working rules", hint: "the standing instructions and how self-changes are approved", icon: "pen" },
  { id: "limits", label: "Limits & budget", hint: "spend caps, iterations, context compaction, balance alerts", icon: "chart" },
  { id: "tools", label: "Tools & search", hint: "web search backends, fetch, exec, speech, vision", icon: "wrench" },
  { id: "chat", label: "Chat & scheduler", hint: "Telegram behaviour and scheduled runs", icon: "inbox" },
  { id: "heartbeat", label: "Heartbeat", hint: "the periodic check-in run", icon: "loop" },
  { id: "about", label: "About", hint: "versions, providers, this browser", icon: "settings" },
];

export function SettingsScreen({ toast, section }: { toast: (t: string) => void; section?: string | null }) {
  const [s, setS] = useState<Settings | null>(null);
  const [status, setStatus] = useState<any>(null);
  const wide = useMedia("(min-width: 1024px)");
  const current: Section | null = SECTIONS.some((x) => x.id === section) ? (section as Section) : null;
  const shown: Section | null = current ?? (wide ? "models" : null);
  useEffect(() => {
    api.get<Settings>("/api/settings").then(setS).catch((e) => toast((e as Error).message));
    api.get("/api/status").then(setStatus).catch(() => setStatus(null));
  }, [toast]);

  async function save(patch: Partial<Settings>) {
    try {
      const next = await api.put<Settings>("/api/settings", patch);
      setS({ ...next, providers_available: next.providers_available ?? s?.providers_available ?? [] });
      toast("saved");
    } catch (e) {
      toast((e as Error).message);
    }
  }
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

  const index = (
    <div className="settings-index">
      {SECTIONS.map((sec) => (
        <a key={sec.id} href={pathFor("settings", sec.id)} className={`settings-link ${shown === sec.id ? "active" : ""}`} aria-current={shown === sec.id ? "page" : undefined} onClick={(e) => go(e, pathFor("settings", sec.id))}>
          <Icon name={sec.icon} size={18} />
          <span className="settings-link-text">
            <b>{sec.label}</b>
            <span className="sub">{sec.hint}</span>
          </span>
          <span className="chev">›</span>
        </a>
      ))}
    </div>
  );

  const body = (sec: Section) => {
    if (!s) return <div className="empty">Loading…</div>;
    const kinds = s.provider_kinds ?? DEFAULT_KINDS;
    const providerIds = Object.keys(s.providers ?? {});
    switch (sec) {
      case "models":
        return (
          <>
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>Models</div>
              <div className="sub">A model is a client plus a model id. The default one opens new sessions; any session can switch from the chip in its header. Open a row to edit it.</div>
              <div className="mlist">
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
              </div>
              {Object.keys(s.presets ?? {}).length === 0 && <div className="sub" style={{ marginTop: 6 }}>No models yet: add one below.</div>}
              <AddPresetRow providers={providerIds} toast={toast} onAdd={(id, p) => void patchPreset(id, p)} />
              <div className="sub" style={{ marginTop: 10 }}>Fallback order: {(s.model.chain ?? []).length ? s.model.chain.join(" → ") : "none"} (tried after the default when it fails).</div>
            </div>
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>Providers (clients)</div>
              <div className="sub">OpenAI-compatible endpoints the models run on. Keys stay in the key proxy where one is configured; a self-hosted vLLM needs none.</div>
              <div className="mlist">
                {providerIds.map((id) => (
                  <ProviderBlock key={id} id={id} p={s.providers[id]} kinds={kinds} available={(s.providers_available ?? []).includes(id)} onPatch={patchProvider} onRemove={(pid) => void removeProvider(pid)} />
                ))}
              </div>
              <AddProviderRow kinds={kinds} toast={toast} onAdd={(pid, base, kind) => void patchProvider(pid, { kind, base_url: base })} />
            </div>
          </>
        );
      case "rules":
        return (
          <>
            <RulesEditor rules={s.prompt.rules} fallback={s.prompt.default_rules ?? ""} onSave={(rules) => save({ prompt: { rules } })} />
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>Self-change</div>
              <div className="sub">How a pull request the agent opens on its own code is handled.</div>
              <div className="btnrow" style={{ marginTop: 8 }}>
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
          </>
        );
      case "limits":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>Limits & alerts</div>
            <div className="sub">daily cap: ${(s as any).usd_per_day} — set in the environment, enforced by the supervisor</div>
            <label className="field">Max iterations per run</label>
            <input className="field" type="number" defaultValue={s.limits.max_iterations} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ limits: { ...s.limits, max_iterations: v } }); }} />
            <label className="field">Spend cap per run (USD, 0 = none; calls without a known price do not count)</label>
            <input className="field" type="number" step="0.5" defaultValue={s.limits.usd_per_run} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ limits: { ...s.limits, usd_per_run: v } }); }} />
            <TotalCaps s={s} save={save} />
            <div className="section-title">Context compaction</div>
            <div className="sub">When a finished run's prompt filled this share of the model window, the history is replaced by one structured summary (fixed headings, your messages quoted verbatim) before the next run. 0 = manual /compact only.</div>
            <div className="grid2">
              <NumField label="Compact above (share of window)" value={s.compaction?.auto_ratio ?? 0.5} min={0} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, auto_ratio: v } })} />
              <NumField label="Keep recent messages" value={s.compaction?.keep_recent_messages ?? 6} min={0} onSave={(v) => save({ compaction: { ...s.compaction, keep_recent_messages: v } })} />
              <NumField label="Summary budget (words)" value={s.compaction?.max_words ?? 1200} min={200} step={100} onSave={(v) => save({ compaction: { ...s.compaction, max_words: v } })} />
              <NumField label="Core mid-run trigger (share)" value={s.compaction?.core_trigger_ratio ?? 0.85} min={0.1} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, core_trigger_ratio: v } })} hint="the core's own incremental compaction inside a long run" />
            </div>
            <label className="field">Balance alert thresholds (USD, comma-separated)</label>
            <input className="field" defaultValue={s.balance.thresholds_usd.join(", ")} onBlur={(e) => save({ balance: { ...s.balance, thresholds_usd: e.target.value.split(",").map(Number).filter((n) => !Number.isNaN(n)) } })} />
            <label className="field">Balance poll interval (seconds)</label>
            <input className="field" type="number" defaultValue={s.balance.poll_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ balance: { ...s.balance, poll_seconds: v } }); }} />
          </div>
        );
      case "tools":
        return <ToolsTab s={s} save={save} />;
      case "chat":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>Chat & scheduler</div>
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
            <input className="field" type="number" defaultValue={s.telegram.stale_after_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, stale_after_seconds: v } }); }} />
            <label className="field">Largest accepted file (MB)</label>
            <input className="field" type="number" defaultValue={s.telegram.max_inbound_file_mb} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, max_inbound_file_mb: v } }); }} />
            <label className="field">Wait for a caption after a bare photo (seconds)</label>
            <input className="field" type="number" defaultValue={s.telegram.photo_caption_wait_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, photo_caption_wait_seconds: v } }); }} />
            <label className="field">Mark a tool call as slow after (seconds)</label>
            <input className="field" type="number" defaultValue={s.telegram.slow_tool_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, slow_tool_seconds: v } }); }} />
            <label className="field">Scheduled runs</label>
            <div className="btnrow" style={{ marginTop: 0 }}>
              {["per_task", "per_run"].map((m) => (
                <button key={m} className={`btn small ${s.scheduler.topic_mode === m ? "primary" : ""}`} onClick={() => save({ scheduler: { ...s.scheduler, topic_mode: m } })}>
                  one topic {m === "per_task" ? "per task" : "per run"}
                </button>
              ))}
            </div>
          </div>
        );
      case "heartbeat":
        return <HeartbeatTab s={s} toast={toast} />;
      case "about":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>Runtime</div>
            {status ? (
              <>
                <div className="kv"><span>Providers</span><b>{status.providers?.join(", ")}</b></div>
                {status.supervisor ? (
                  <>
                    <div className="kv"><span>Bot</span><b className="mono">{String(status.supervisor.bot).slice(0, 10)}</b></div>
                    <div className="kv"><span>Core</span><b className="mono">{String(status.supervisor.core).slice(0, 10)}</b></div>
                    <div className="kv"><span>Process</span><b>{status.supervisor.child_running ? "running" : "stopped"}</b></div>
                  </>
                ) : (
                  <div className="sub">supervisor: not connected (development mode)</div>
                )}
                {status.budget_exceeded && <div className="sub" style={{ color: "var(--bad)" }}>budget exceeded: {status.budget_exceeded}</div>}
              </>
            ) : (
              <div className="sub">status unavailable</div>
            )}
            {!telegram()?.initData && (
              <>
                <div className="section-title">This browser</div>
                <div className="btnrow" style={{ marginTop: 0 }}>
                  <button className="btn small" onClick={() => { try { localStorage.setItem("daedalus.scheme", "dark"); } catch { /* private */ } window.location.reload(); }}>Dark</button>
                  <button className="btn small" onClick={() => { try { localStorage.setItem("daedalus.scheme", "light"); } catch { /* private */ } window.location.reload(); }}>Light</button>
                  <button className="btn small" onClick={() => { try { localStorage.removeItem("daedalus.scheme"); } catch { /* private */ } window.location.reload(); }}>Follow the system</button>
                </div>
                <div className="btnrow">
                  <button
                    className="btn small"
                    onClick={async () => {
                      try {
                        await api.post("/api/auth/logout");
                        sessionStorage.removeItem("daedalus_token");
                      } finally {
                        window.location.reload();
                      }
                    }}
                  >
                    Log out of this browser
                  </button>
                </div>
              </>
            )}
          </div>
        );
    }
  };

  if (wide) {
    return (
      <>
        <PageHeader title="Settings" />
        <div className="screen wide settings-split">
          <aside className="settings-nav">{index}</aside>
          <div className="settings-body">{shown && body(shown)}</div>
        </div>
      </>
    );
  }
  if (!current) {
    return (
      <>
        <PageHeader title="Settings" />
        <div className="screen narrow">{index}</div>
      </>
    );
  }
  return (
    <>
      <PageHeader title={SECTIONS.find((x) => x.id === current)!.label} back={pathFor("settings")} />
      <div className="screen narrow">{body(current)}</div>
    </>
  );
}
