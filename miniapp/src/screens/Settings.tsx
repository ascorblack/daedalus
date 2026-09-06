import { useEffect, useState } from "react";
import { api, Preset, ProviderConf, Settings } from "../api";

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

function PresetRow({ id, p, isDefault, providers, onDefault, onPatch, onDelete, onLookup }: {
  id: string;
  p: Preset;
  isDefault: boolean;
  providers: string[];
  onDefault: () => void;
  onPatch: (patch: Partial<Preset>) => void;
  onDelete: () => void;
  onLookup: (provider: string) => Promise<string[] | null>;
}) {
  const [label, setLabel] = useState(p.label);
  const [model, setModel] = useState(p.model);
  const [models, setModels] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setLabel(p.label), [p.label]);
  useEffect(() => setModel(p.model), [p.model]);
  return (
    <div className={`preset ${isDefault ? "default" : ""}`}>
      <div className="row" style={{ gap: 8 }}>
        <button className={`radio ${isDefault ? "on" : ""}`} onClick={onDefault} aria-label="make default" title="global default for new sessions" />
        <input className="field" style={{ flex: 1 }} value={label} placeholder={`${p.provider}/${p.model}`} onChange={(e) => setLabel(e.target.value)} onBlur={() => label.trim() !== p.label && onPatch({ label: label.trim() })} />
        <span className="sub" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>{id}</span>
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
        <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); setModels(await onLookup(p.provider)); setBusy(false); }}>
          {busy ? "…" : "⟳ /models"}
        </button>
      </div>
      {models && (
        <div className="btnrow" style={{ marginTop: 6 }}>
          {models.length === 0 && <span className="sub">the server lists no models</span>}
          {models.map((m) => (
            <button key={m} className={`btn small ${m === p.model ? "primary" : ""}`} onClick={() => (setModels(null), onPatch({ model: m }))}>
              {m}
            </button>
          ))}
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
  const [models, setModels] = useState<string[] | null>(null);
  useEffect(() => { if (!providers.includes(provider)) setProvider(providers[0] ?? ""); }, [providers, provider]);
  if (!open)
    return (
      <button className="btn small" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>
        ＋ add model
      </button>
    );
  async function lookup() {
    try {
      const r = await api.post<{ models: string[] }>("/api/providers/lookup-models", { provider });
      setModels(r.models);
    } catch (e) {
      toast((e as Error).message);
    }
  }
  function add() {
    if (!provider || !model.trim()) {
      toast("pick a client and a model id");
      return;
    }
    const id = `${provider}.${model.trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "")}`;
    onAdd(id, { provider, model: model.trim(), label: label.trim() });
    setOpen(false); setModel(""); setLabel(""); setModels(null);
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
        <button className="btn small" onClick={lookup}>⟳ /models</button>
      </div>
      {models && (
        <div className="btnrow" style={{ marginTop: 6 }}>
          {models.map((m) => (
            <button key={m} className={`btn small ${m === model ? "primary" : ""}`} onClick={() => setModel(m)}>{m}</button>
          ))}
        </div>
      )}
      <div className="row" style={{ gap: 8, marginTop: 6 }}>
        <input className="field" style={{ flex: 1 }} value={label} placeholder="label (optional), e.g. Qwen fast" onChange={(e) => setLabel(e.target.value)} />
        <button className="btn small primary" onClick={add}>add</button>
        <button className="btn small" onClick={() => setOpen(false)}>cancel</button>
      </div>
    </div>
  );
}
type Lookup = (baseUrl: string, apiKey: string) => Promise<{ base_url: string; models: string[] } | null>;

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

function ProviderBlock({
  id,
  p,
  kinds,
  available,
  onPatch,
  onLookup,
  onActivate,
  onRemove,
}: {
  id: string;
  p: ProviderConf;
  kinds: string[];
  available: boolean;
  onPatch: Patch;
  onLookup: Lookup;
  onActivate: (id: string, model: string) => void;
  onRemove: (id: string) => void;
}) {
  const [baseUrl, setBaseUrl] = useState(p.base_url);
  const [model, setModel] = useState(p.default_model);
  const [keyDraft, setKeyDraft] = useState("");
  const [models, setModels] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setBaseUrl(p.base_url), [p.base_url]);
  useEffect(() => setModel(p.default_model), [p.default_model]);
  useEffect(() => setKeyDraft(""), [p.api_key_set]);
  useEffect(() => {
    setModels(null);
    setError("");
  }, [p.base_url]);

  async function fetchModels() {
    const base = baseUrl.trim();
    if (!base) {
      setError("enter base_url first");
      return;
    }
    setBusy(true);
    setError("");
    setModels(null);
    const res = await onLookup(base, keyDraft);
    setBusy(false);
    if (!res) return;
    if (res.base_url !== p.base_url) await onPatch(id, { base_url: res.base_url });
    setModels(res.models);
  }

  return (
    <div style={{ borderTop: "1px solid var(--line, #333)", paddingTop: 8, marginTop: 8 }}>
      <div className="row" style={{ cursor: "default" }}>
        <b style={{ fontFamily: "var(--mono)", fontSize: 13 }}>{id}</b>
        <select className="field" style={{ flex: 1 }} value={p.kind} onChange={(e) => onPatch(id, { kind: e.target.value })}>
          {kinds.map((k) => (
            <option key={k}>{k}</option>
          ))}
        </select>
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
      <div className="row" style={{ padding: 0 }}>
        <input
          className="field"
          value={model}
          placeholder="model id, e.g. Qwen3.6"
          onChange={(e) => setModel(e.target.value)}
          onBlur={() => model.trim() !== p.default_model && model.trim() && onPatch(id, { default_model: model.trim() })}
        />
        <button className="btn small" disabled={busy} onClick={() => void fetchModels()}>
          {busy ? "…" : "⟳ from /models"}
        </button>
      </div>
      {error && <div className="sub" style={{ color: "var(--bad)" }}>{error}</div>}
      {models && (
        <div className="sub" style={{ marginTop: 6 }}>
          Models on this endpoint — pick one as its fallback model (the chain uses it), or add it as a model above:
          <div className="btnrow">
            {models.map((m) => (
              <button key={m} className="btn small" onClick={() => (setModels(null), onPatch(id, { default_model: m }))}>
                {m}
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="btnrow" style={{ marginTop: 6 }}>
        <button className={`btn small ${p.supports_thinking ? "primary" : ""}`} onClick={() => onPatch(id, { supports_thinking: !p.supports_thinking })}>
          thinking {p.supports_thinking ? "on" : "off"}
        </button>
        <button className={`btn small ${p.supports_images ? "primary" : ""}`} onClick={() => onPatch(id, { supports_images: !p.supports_images })}>
          images {p.supports_images ? "on" : "off"}
        </button>
        <span className="sub" style={{ marginLeft: "auto" }}>
          {available ? "available" : "not configured yet"}
        </span>
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

function ToolsTab({ s, save, providerIds, lookup }: { s: Settings; save: (patch: any) => Promise<void>; providerIds: string[]; lookup: (provider: string) => Promise<string[] | null> }) {
  const [models, setModels] = useState<string[] | null>(null);
  const web = s.tools.web;
  return (
    <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>ImageView</div>
        <div className="sub">The agent's eyes: a separate image-capable model answers questions about pictures so the main context never carries pixels.</div>
        <div className="grid2">
          <div>
            <label className="field">Client</label>
            <select className="field" value={s.vision.provider} onChange={(e) => save({ vision: { provider: e.target.value } })}>
              {providerIds.map((p) => <option key={p}>{p}</option>)}
              {!providerIds.includes(s.vision.provider) && <option>{s.vision.provider}</option>}
            </select>
          </div>
          <NumField label="Max output tokens" value={s.vision.max_output_tokens} min={100} step={100} onSave={(v) => save({ vision: { max_output_tokens: v } })} />
        </div>
        <label className="field">Model id</label>
        <div className="row" style={{ gap: 8 }}>
          <input className="field" style={{ flex: 1 }} defaultValue={s.vision.model} onBlur={(e) => e.target.value.trim() !== s.vision.model && save({ vision: { model: e.target.value.trim() } })} />
          <button className="btn small" onClick={async () => setModels(await lookup(s.vision.provider))}>⟳ /models</button>
        </div>
        {models && (
          <div className="btnrow">
            {models.length === 0 && <span className="sub">no models listed</span>}
            {models.map((m) => <button key={m} className={`btn small ${m === s.vision.model ? "primary" : ""}`} onClick={() => (setModels(null), save({ vision: { model: m } }))}>{m}</button>)}
          </div>
        )}
        <div className="sub" style={{ marginTop: 6 }}>The client must accept images (toggle “images” on it in General → Providers); a vLLM serving a vision model works too.</div>
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

export function SettingsScreen({ toast }: { toast: (t: string) => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [tab, setTab] = useState<"general" | "tools">("general");
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

  async function lookupModels(baseUrl: string, apiKey: string) {
    try {
      return await api.post<{ base_url: string; models: string[] }>("/api/providers/lookup-models", { base_url: baseUrl, api_key: apiKey || undefined });
    } catch (e) {
      toast((e as Error).message);
      return null;
    }
  }

  async function activateModel(id: string, model: string) {
    const next = await patchProvider(id, { default_model: model });
    if (!next) return;
    try {
      const updated = await api.put<Settings>("/api/settings", { model: { ...next.model, provider: id, name: model } });
      setS({ ...updated, providers_available: updated.providers_available ?? next.providers_available ?? [] });
      toast(`${id}/${model} is now the default model`);
    } catch (e) {
      toast((e as Error).message);
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
      </div>
      {tab === "tools" && <ToolsTab s={s} save={save} providerIds={providerIds} lookup={lookupProviderModels} />}
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
            isDefault={s.model.preset === id || (!s.model.preset && s.model.provider === p.provider && s.model.name === p.model)}
            providers={providerIds}
            onDefault={() => save({ model: { preset: id } as any })}
            onPatch={(patch) => void patchPreset(id, patch)}
            onDelete={() => void removePreset(id)}
            onLookup={lookupProviderModels}
          />
        ))}
        {Object.keys(s.presets ?? {}).length === 0 && <div className="sub" style={{ marginTop: 6 }}>No models yet: add one below.</div>}
        <AddPresetRow providers={providerIds} toast={toast} onAdd={(id, p) => void patchPreset(id, p)} />
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
        <div className="grid2">
          <div>
            <label className="field">Context window (tokens)</label>
            <input className="field" type="number" step={1000} min={8000} defaultValue={s.model.context_window} onBlur={(e) => Number(e.target.value) >= 8000 && Number(e.target.value) !== s.model.context_window && save({ model: { ...s.model, context_window: Number(e.target.value) } })} />
          </div>
          <div>
            <label className="field">Max output per reply</label>
            <input className="field" type="number" step={1000} min={1024} defaultValue={s.model.max_output_tokens} onBlur={(e) => Number(e.target.value) >= 1024 && Number(e.target.value) !== s.model.max_output_tokens && save({ model: { ...s.model, max_output_tokens: Number(e.target.value) } })} />
          </div>
        </div>
        <div className="sub" style={{ marginTop: 6 }}>The window bounds how much history a run keeps before compaction (the model itself may allow more); the output cap is the max_tokens of one reply, thinking included.</div>
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
            onLookup={lookupModels}
            onActivate={(pid, m) => void activateModel(pid, m)}
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
      )}
    </>
  );
}
