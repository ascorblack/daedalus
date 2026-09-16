// Adding a model: the one thing a fresh installation has to do before anything else works, and the
// same three steps when a second model is added later from Settings. A provider endpoint is an
// address, not a choice of model, so nothing is picked on the operator's behalf — but everything the
// endpoint is willing to say about a model (its window, whether it sees pictures, whether it
// reasons, what it costs) fills the form in, so the choice is one click and a glance, not research.

import { useEffect, useMemo, useState } from "react";
import { api, Preset, Settings } from "../api";
import { Icon } from "../icons";
import { PageHeader } from "../shell";
import { errorText, numInput } from "../ui";

export type OnboardingState = {
  has_model: boolean;
  presets: number;
  default_preset: string;
  providers: { id: string; kind: string; base_url: string; via_proxy: boolean; key_held: boolean | null; ready: boolean }[];
  needs: string[];
  message: string;
};

/** One model as an endpoint described it. Only the id is ever certain. */
export type ModelEntry = {
  id: string;
  name?: string;
  context_length?: number;
  max_output_tokens?: number;
  input_modalities?: string[];
  images?: boolean;
  reasoning?: boolean;
  pricing?: { input?: number; output?: number; cache_hit?: number };
};

/** Not a provider id: the provider ids the server accepts cannot contain a space. */
const CUSTOM = "a new endpoint";
const KIND_NAMES: Record<string, string> = {
  deepseek: "DeepSeek",
  openrouter: "OpenRouter",
  opencode: "OpenCode Go",
  vllm: "vLLM (self-hosted)",
};

function providerName(id: string, kind: string): string {
  return KIND_NAMES[kind] ?? id;
}

function money(usd: number): string {
  if (usd === 0) return "free";
  return usd < 1 ? `$${usd.toFixed(usd < 0.1 ? 3 : 2)}` : `$${usd.toFixed(2)}`;
}

function tokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

/** A model id turned into a preset id: the same rule the server accepts (letters, digits, . _ -). */
export function presetIdFor(provider: string, model: string): string {
  const slug = model.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "");
  return `${provider}.${slug || "model"}`;
}

/** Step 1: which endpoint the model runs on. */
function ProviderStep({ state, chosen, onPick }: { state: OnboardingState | null; chosen: string; onPick: (id: string) => void }) {
  const providers = state?.providers ?? [];
  const ordered = [...providers.filter((p) => p.ready), ...providers.filter((p) => !p.ready)];
  return (
    <div className="pickgrid">
      {ordered.map((p) => (
        <button key={p.id} className={`pick ${chosen === p.id ? "on" : ""}`} onClick={() => onPick(p.id)} aria-pressed={chosen === p.id}>
          <span className="pick-top">
            <b className="truncate">{providerName(p.id, p.kind)}</b>
            {p.ready ? <span className="pill done">key ready</span> : <span className="pill">no key</span>}
          </span>
          <span className="sub mono truncate">{p.base_url || "no address configured"}</span>
          <span className="sub faint">{p.via_proxy ? "its key is held by the key proxy" : "keyed from this installation's configuration"}</span>
        </button>
      ))}
      <button className={`pick dashed ${chosen === CUSTOM ? "on" : ""}`} onClick={() => onPick(CUSTOM)} aria-pressed={chosen === CUSTOM}>
        <span className="pick-top">
          <b>OpenAI-compatible endpoint</b>
          <span className="pill">new</span>
        </span>
        <span className="sub">Anything serving /v1/chat/completions: a local vLLM, a machine on the network, another vendor.</span>
      </button>
    </div>
  );
}

/** Step 1b: the address and key of an endpoint this installation does not know yet. */
function CustomProvider({ busy, onCreate }: { busy: boolean; onCreate: (id: string, baseUrl: string, apiKey: string) => void }) {
  const [id, setId] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const clean = id.trim().replace(/[^A-Za-z0-9._-]+/g, "-");
  return (
    <div className="mfields" style={{ marginTop: 12 }}>
      <label className="mfield">
        <span>Name it</span>
        <input className="field" value={id} placeholder="e.g. workshop" onChange={(e) => setId(e.target.value)} />
      </label>
      <label className="mfield wide">
        <span>Address (the OpenAI API root, usually ending in /v1)</span>
        <input className="field mono" value={baseUrl} placeholder="http://10.0.0.5:9000/v1" onChange={(e) => setBaseUrl(e.target.value)} spellCheck={false} />
      </label>
      <label className="mfield">
        <span>Key (blank for an endpoint that needs none)</span>
        <input className="field" type="password" value={apiKey} placeholder="the endpoint's API key" onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
      </label>
      <div className="mfield">
        <span>&nbsp;</span>
        <button className="btn primary" disabled={busy || !clean || !baseUrl.trim()} onClick={() => onCreate(clean, baseUrl.trim(), apiKey)}>
          {busy ? "Adding…" : "Add this endpoint"}
        </button>
      </div>
    </div>
  );
}

/** Step 2: the models the endpoint serves, or a model id typed by hand. */
function ModelStep({ entries, loading, error, chosen, typed, onPick, onType, onRetry }: {
  entries: ModelEntry[] | null;
  loading: boolean;
  error: string;
  chosen: string;
  typed: string;
  onPick: (entry: ModelEntry) => void;
  onType: (id: string) => void;
  onRetry: () => void;
}) {
  const [filter, setFilter] = useState("");
  const q = filter.trim().toLowerCase();
  const shown = useMemo(
    () => (entries ?? []).filter((e) => !q || e.id.toLowerCase().includes(q) || (e.name ?? "").toLowerCase().includes(q)),
    [entries, q],
  );
  return (
    <>
      <div className="modelbar">
        <input className="field" placeholder={entries ? `Filter ${entries.length} models` : "Filter"} value={filter} onChange={(e) => setFilter(e.target.value)} disabled={!entries} />
        <input className="field mono" placeholder="…or type the model id" value={typed} onChange={(e) => onType(e.target.value)} spellCheck={false} />
      </div>
      {loading && <div className="empty">Asking the endpoint what it serves…</div>}
      {!loading && error && (
        <div className="empty">
          <b>The endpoint did not list its models</b>
          <div className="sub">{error}</div>
          <div className="sub faint">Type the model id above instead — the list is a convenience, not a requirement.</div>
          <button className="btn small" onClick={onRetry}>Try again</button>
        </div>
      )}
      {!loading && !error && entries && shown.length === 0 && <div className="empty">Nothing matches that filter.</div>}
      {!loading && shown.length > 0 && (
        <div className="modelgrid">
          {shown.map((e) => (
            <button key={e.id} className={`pick model ${chosen === e.id ? "on" : ""}`} onClick={() => onPick(e)} aria-pressed={chosen === e.id}>
              <span className="pick-top">
                <b className="truncate">{e.name ?? e.id}</b>
                {chosen === e.id && <Icon name="check" size={16} />}
              </span>
              <span className="sub mono truncate">{e.id}</span>
              <span className="mtags">
                {e.context_length ? <span className="pill">{tokens(e.context_length)} context</span> : null}
                {e.images ? <span className="pill">images</span> : null}
                {e.reasoning ? <span className="pill">reasoning</span> : null}
                {e.pricing?.input !== undefined && e.pricing?.output !== undefined ? (
                  <span className="pill">{money(e.pricing.input)} in · {money(e.pricing.output)} out / 1M</span>
                ) : null}
              </span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

const BLANK: Preset = { provider: "", model: "", label: "", thinking: true, reasoning_effort: "medium", images: false, context_window: 128000, max_output_tokens: 32000 };

/**
 * The flow itself. `onSaved` receives the preset id and the settings that came back; the caller
 * decides what happens next — the first model ends onboarding, a later one closes the sheet.
 */
export function AddModel({ onSaved, onCancel, toast }: { onSaved: (presetId: string, settings: Settings) => void; onCancel?: () => void; toast: (t: string) => void }) {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [provider, setProvider] = useState("");
  const [entries, setEntries] = useState<ModelEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [lookupError, setLookupError] = useState("");
  const [typed, setTyped] = useState("");
  const [preset, setPreset] = useState<Preset>(BLANK);
  const [pricing, setPricing] = useState<ModelEntry["pricing"] | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get<OnboardingState>("/api/onboarding").then(setState).catch((e) => toast(errorText(e)));
  }, [toast]);

  async function lookup(id: string) {
    setLoading(true);
    setLookupError("");
    setEntries(null);
    try {
      const r = await api.post<{ models: string[]; entries?: ModelEntry[] }>("/api/providers/lookup-models", { provider: id });
      setEntries(r.entries ?? r.models.map((m) => ({ id: m })));
    } catch (e) {
      setLookupError(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  function pickProvider(id: string) {
    setProvider(id);
    setEntries(null);
    setLookupError("");
    setTyped("");
    setPricing(null);
    setPreset({ ...BLANK, provider: id === CUSTOM ? "" : id });
    if (id !== CUSTOM) void lookup(id);
  }

  async function createProvider(id: string, baseUrl: string, apiKey: string) {
    setBusy(true);
    try {
      await api.put<Settings>(`/api/providers/${encodeURIComponent(id)}`, { kind: "openai_compat", base_url: baseUrl, api_key: apiKey });
      setState(await api.get<OnboardingState>("/api/onboarding"));
      pickProvider(id);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function pickModel(entry: ModelEntry) {
    setTyped(entry.id);
    setPricing(entry.pricing ?? null);
    setPreset((p) => ({
      ...p,
      model: entry.id,
      label: entry.name && entry.name !== entry.id ? entry.name : p.label,
      images: entry.images ?? p.images,
      thinking: entry.reasoning ?? p.thinking,
      // The history a run may hold is capped below a very large window on purpose: every turn pays
      // for the context it carries, and 400k of it rarely makes the answer better.
      context_window: entry.context_length ? Math.min(entry.context_length, 400000) : p.context_window,
      max_output_tokens: entry.max_output_tokens ? Math.min(entry.max_output_tokens, 64000) : p.max_output_tokens,
    }));
  }

  const model = (typed.trim() || preset.model).trim();
  const ready = !!provider && provider !== CUSTOM && !!model;

  async function save() {
    if (!ready) return;
    setBusy(true);
    const id = presetIdFor(provider, model);
    try {
      // The price the endpoint published is stored on the provider, where costs are read from: a
      // model with no price anywhere is recorded as unmetered and counts against no cap.
      if (pricing?.input !== undefined && pricing?.output !== undefined) {
        const current = (await api.get<Settings>("/api/settings")).providers[provider]?.pricing ?? {};
        await api.put<Settings>(`/api/providers/${encodeURIComponent(provider)}`, { pricing: { ...current, [model]: pricing } });
      }
      const next = await api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, { ...preset, provider, model });
      onSaved(id, next);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="addmodel">
      <section className="card step">
        <div className="step-head">
          <span className="step-n">1</span>
          <div className="grow">
            <b>Where it runs</b>
            <div className="sub">An endpoint this installation can reach. The ones with a key are ready to use; the others need one in Settings → Models first.</div>
          </div>
        </div>
        <ProviderStep state={state} chosen={provider} onPick={pickProvider} />
        {provider === CUSTOM && <CustomProvider busy={busy} onCreate={createProvider} />}
      </section>

      <section className={`card step ${provider && provider !== CUSTOM ? "" : "muted"}`}>
        <div className="step-head">
          <span className="step-n">2</span>
          <div className="grow">
            <b>Which model</b>
            <div className="sub">Listed straight from the endpoint. Whatever it says about a model fills the next step in.</div>
          </div>
        </div>
        {provider && provider !== CUSTOM ? (
          <ModelStep entries={entries} loading={loading} error={lookupError} chosen={model} typed={typed} onPick={pickModel} onType={setTyped} onRetry={() => void lookup(provider)} />
        ) : (
          <div className="sub faint">Pick an endpoint above.</div>
        )}
      </section>

      <section className={`card step ${ready ? "" : "muted"}`}>
        <div className="step-head">
          <span className="step-n">3</span>
          <div className="grow">
            <b>How it runs</b>
            <div className="sub">All of it changeable later in Settings → Models; nothing here is permanent.</div>
          </div>
        </div>
        <div className="mfields">
          <label className="mfield">
            <span>Label</span>
            <input className="field" value={preset.label} placeholder={model ? `${provider}/${model}` : "what the app calls it"} onChange={(e) => setPreset({ ...preset, label: e.target.value })} />
          </label>
          <label className="mfield">
            <span>Context window (tokens)</span>
            <input className="field" type="number" min={8000} step={1000} value={preset.context_window} onChange={(e) => { const v = numInput(e.target.value); if (v !== null) setPreset({ ...preset, context_window: v }); }} />
          </label>
          <label className="mfield">
            <span>Max output per reply</span>
            <input className="field" type="number" min={1024} step={1000} value={preset.max_output_tokens} onChange={(e) => { const v = numInput(e.target.value); if (v !== null) setPreset({ ...preset, max_output_tokens: v }); }} />
          </label>
        </div>
        <div className="btnrow">
          <button className={`btn small ${preset.thinking ? "primary" : ""}`} aria-pressed={preset.thinking} onClick={() => setPreset({ ...preset, thinking: !preset.thinking })}>
            thinking {preset.thinking ? "on" : "off"}
          </button>
          <div className="segmented inline" role="group" aria-label="reasoning effort">
            {["low", "medium", "high"].map((e) => (
              <button key={e} className={preset.reasoning_effort === e ? "on" : ""} disabled={!preset.thinking} onClick={() => setPreset({ ...preset, reasoning_effort: e })}>
                {e}
              </button>
            ))}
          </div>
          <button className={`btn small ${preset.images ? "primary" : ""}`} aria-pressed={preset.images} title="the model accepts pictures" onClick={() => setPreset({ ...preset, images: !preset.images })}>
            images {preset.images ? "on" : "off"}
          </button>
        </div>
      </section>

      <div className="addmodel-foot">
        <div className="sub mono truncate">{ready ? presetIdFor(provider, model) : "pick an endpoint and a model"}</div>
        <span className="grow" />
        {onCancel && (
          <button className="btn" onClick={onCancel}>
            Cancel
          </button>
        )}
        <button className="btn primary" disabled={!ready || busy} onClick={() => void save()}>
          {busy ? "Saving…" : state?.has_model ? "Add this model" : "Add this model and start"}
        </button>
      </div>
    </div>
  );
}

/** The first-run page: the app opens here until a model exists, because nothing else can work yet. */
export function OnboardingScreen({ onDone, toast }: { onDone: () => void; toast: (t: string) => void }) {
  return (
    <>
      <PageHeader title="Add a model" subtitle="Your agent has no model yet — one is all it takes to start." />
      <div className="screen wide">
        <AddModel toast={toast} onSaved={onDone} />
      </div>
    </>
  );
}
