// Choosing a speech model that runs on this machine.
//
// The list is the whole decision, so it is shown as one: every model with what it speaks, what it
// costs in disk and memory, whether it hears words as they are said or only whole sentences, and two
// bars for how well and how fast. Filters narrow it — a language, and streaming only — because
// twelve entries is more than anyone reads and the operator almost always knows which language they
// intend to speak.
//
// Only one model is in use at a time; several can be installed, and the one in use is a tap away from
// any of them.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon } from "../icons";
import { errorText, haptic } from "../ui";

export type SpeechModel = {
  id: string;
  label: string;
  kind: string;
  streaming: boolean;
  languages: string[];
  language_count: number;
  size_bytes: number;
  disk_bytes: number;
  memory_mb: number;
  licence: string;
  accuracy: number;
  speed: number;
  note: string;
  recommended_for: string[];
  installed: boolean;
  installed_bytes: number;
  selected: boolean;
  verified: boolean;
  detects_language: boolean;
  progress?: { state: string; fraction: number; error: string };
};

export type SpeechView = {
  models: SpeechModel[];
  languages: string[];
  selected: string;
  disk_bytes: number;
  language: string;
  threads: number;
  engine_installed: boolean;
  decoders: { opus: boolean; any: boolean };
  recommended: Record<string, string>;
};

const NAMES: Record<string, string> = {
  en: "English", ru: "Russian", de: "German", fr: "French", es: "Spanish", it: "Italian", pt: "Portuguese",
  nl: "Dutch", pl: "Polish", uk: "Ukrainian", cs: "Czech", sk: "Slovak", sv: "Swedish", da: "Danish",
  fi: "Finnish", nb: "Norwegian", et: "Estonian", lv: "Latvian", lt: "Lithuanian", bg: "Bulgarian",
  hr: "Croatian", sl: "Slovenian", ro: "Romanian", hu: "Hungarian", el: "Greek", mt: "Maltese",
  be: "Belarusian", tr: "Turkish", ar: "Arabic", hi: "Hindi", he: "Hebrew", ja: "Japanese",
  ko: "Korean", vi: "Vietnamese", zh: "Chinese", yue: "Cantonese", th: "Thai", id: "Indonesian",
};

const name = (code: string) => NAMES[code] ?? code.toUpperCase();

function size(bytes: number): string {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`;
  return `${Math.round(bytes / (1 << 20))} MB`;
}

/** One 0-100 score as a bar. Two of these side by side are the whole comparison most people make. */
function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="stt-bar">
      <span className="sub faint">{label}</span>
      <span className="stt-bar-track">
        <span className="stt-bar-fill" style={{ width: `${Math.max(3, Math.min(100, value))}%` }} />
      </span>
    </div>
  );
}

function Languages({ model }: { model: SpeechModel }) {
  const shown = model.languages.slice(0, 4).map(name);
  const rest = model.language_count - shown.length;
  return <>{shown.join(", ")}{rest > 0 ? ` +${rest} more` : ""}</>;
}

export function SpeechModels({ toast }: { toast: (t: string) => void }) {
  const [view, setView] = useState<SpeechView | null>(null);
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [streamingOnly, setStreamingOnly] = useState(false);
  const [busy, setBusy] = useState("");
  const [problem, setProblem] = useState("");
  const live = useRef<Record<string, { state: string; fraction: number; error: string }>>({});

  const load = useCallback(async () => {
    try {
      setView(await api.get<SpeechView>("/api/stt"));
    } catch (e) {
      setProblem(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Download progress comes as a stream rather than a poll, so a bar that is moving looks like it.
  // A finished or failed download reloads the list, which is where "installed" and the disk total
  // come from; everything in between only moves the bar.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/api/stt/progress", { headers: api.authHeaders(), signal: controller.signal });
        if (!response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const data = /^data: (.*)$/m.exec(frame)?.[1];
            if (!data) continue;
            try {
              const update = JSON.parse(data) as { id: string; state: string; fraction: number; error: string };
              live.current = { ...live.current, [update.id]: update };
              setView((v) => (v ? { ...v, models: v.models.map((m) => (m.id === update.id ? { ...m, progress: update } : m)) } : v));
              if (["installed", "failed", "cancelled", "deleted"].includes(update.state)) void load();
              if (update.state === "failed" && update.error) setProblem(update.error);
            } catch {
              /* one malformed frame must not end the stream */
            }
          }
        }
      } catch {
        /* the page navigated away, or the stream dropped; the list still reloads on every action */
      }
    })();
    return () => controller.abort();
  }, [load]);

  async function act(id: string, what: "download" | "use" | "cancel" | "delete") {
    setBusy(id);
    setProblem("");
    try {
      if (what === "download") {
        // The engine is an optional extra natively; the first download is when it becomes worth
        // installing, so it is fetched before the model rather than after, when it would be missed.
        if (view && !view.engine_installed) {
          toast("installing the speech engine…");
          await api.post("/api/stt/engine");
        }
        await api.post(`/api/stt/models/${encodeURIComponent(id)}/download`);
        toast("downloading…");
      } else if (what === "cancel") {
        await api.post(`/api/stt/models/${encodeURIComponent(id)}/cancel`);
      } else if (what === "use") {
        setView(await api.post<SpeechView>("/api/stt/select", { model: id }));
        haptic("medium");
        toast(id ? "this model is now used for speech" : "back to the endpoint and the browser");
        return;
      } else {
        await api.delete(`/api/stt/models/${encodeURIComponent(id)}`);
        toast("removed");
      }
      await load();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy("");
    }
  }

  if (!view) return <div className="card"><div className="sub">{problem || "Loading…"}</div></div>;

  const wanted = query.trim().toLowerCase();
  const shown = view.models.filter((m) => {
    if (streamingOnly && !m.streaming) return false;
    if (language && !m.languages.includes(language)) return false;
    if (wanted && !`${m.label} ${m.note} ${m.languages.map(name).join(" ")}`.toLowerCase().includes(wanted)) return false;
    return true;
  });
  const installedCount = view.models.filter((m) => m.installed).length;
  // One model is fetched at a time — half a gigabyte twice over a link that carries neither faster is
  // not a thing to offer, and twelve Download buttons make it easy to ask for by accident. The other
  // cards say so instead of starting a second and being queued.
  const busyElsewhere = view.models.some((m) => m.progress && ["downloading", "verifying", "unpacking"].includes(m.progress.state));
  // Whisper is told its language at load time, so "auto" is English for it and not a detection.
  const autoDetects = view.models.find((m) => m.id === view.selected)?.detects_language ?? true;

  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>Speech recognition</div>
      <div className="sub">
        A model that runs here, on this machine's processor, with no endpoint and no key. Pick one, download it, and it
        is used for every voice note and everything said on the voice page — ahead of the transcription endpoint and
        ahead of the browser's own recognition. Nothing is downloaded until you ask for it, and deleting one puts the
        disk back.
      </div>

      <div className="kv" style={{ marginTop: 10 }}>
        <span>In use</span>
        <b>{view.selected ? view.models.find((m) => m.id === view.selected)?.label ?? view.selected : "none — the endpoint or the browser listens"}</b>
      </div>
      <div className="kv">
        <span>On disk</span>
        <b>{installedCount ? `${installedCount} model${installedCount > 1 ? "s" : ""} · ${size(view.disk_bytes)}` : "nothing yet"}</b>
      </div>
      {view.selected && !view.decoders.opus && !view.decoders.any && (
        <div className="sub attn" style={{ marginTop: 6 }}>
          No audio decoder is installed here, so only plain WAV can be read — a Telegram voice note cannot. Install
          opus-tools (small) or ffmpeg (large).
        </div>
      )}

      <div className="stt-filters">
        <input className="field" style={{ margin: 0 }} placeholder="Search models" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select className="field" style={{ margin: 0 }} value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="">Any language</option>
          {view.languages.map((code) => (
            <option key={code} value={code}>{name(code)}</option>
          ))}
        </select>
        <button className={`chip select ${streamingOnly ? "on" : ""}`} aria-pressed={streamingOnly} onClick={() => setStreamingOnly((v) => !v)}>
          Streaming only
        </button>
      </div>

      {problem && <div className="sub attn" style={{ marginTop: 8 }}>{problem}</div>}

      <div className="stt-list">
        {shown.map((m) => {
          const progress = m.progress;
          const downloading = progress && ["downloading", "verifying", "unpacking"].includes(progress.state);
          const queued = progress?.state === "queued";
          const recommended = language ? m.recommended_for.includes(language) : m.recommended_for.length > 0;
          return (
            <div key={m.id} className={`stt-card ${m.selected ? "using" : ""}`}>
              <div className="stt-head">
                <b>{m.label}</b>
                {recommended && <span className="chip accent">Recommended{language ? ` for ${name(language)}` : ""}</span>}
                {m.streaming ? <span className="chip ok">Streaming</span> : <span className="chip">Whole sentences</span>}
                {m.selected && <span className="chip accent">In use</span>}
              </div>
              <div className="sub">{m.note}</div>
              <div className="stt-facts sub faint">
                <span><Languages model={m} /></span>
                <span>{size(m.size_bytes)} download · {size(m.disk_bytes)} on disk · ~{m.memory_mb} MB in memory</span>
                <span>
                  {m.licence}
                  {m.verified ? " · checksum published" : " · no published checksum — size and file list only"}
                  {m.detects_language ? "" : " · transcribes as English unless a language is chosen"}
                </span>
              </div>
              <div className="stt-bars">
                <Bar label="Accuracy" value={m.accuracy} />
                <Bar label="Speed" value={m.speed} />
              </div>
              {downloading && (
                <div className="stt-progress">
                  <span className="stt-bar-track">
                    <span className="stt-bar-fill accent" style={{ width: `${Math.round((progress?.fraction ?? 0) * 100)}%` }} />
                  </span>
                  <span className="sub faint">
                    {progress?.state === "downloading" ? `${Math.round((progress?.fraction ?? 0) * 100)}%` : progress?.state}
                  </span>
                </div>
              )}
              {progress?.state === "failed" && <div className="sub attn">{progress.error || "the download failed"}</div>}
              {queued && <div className="sub faint">Waiting — models are fetched one at a time.</div>}
              <div className="btnrow">
                {downloading ? (
                  <button className="btn small" onClick={() => void act(m.id, "cancel")}>Stop</button>
                ) : m.installed ? (
                  <>
                    {m.selected ? (
                      <button className="btn small" onClick={() => void act("", "use")}>Stop using it</button>
                    ) : (
                      <button className="btn small primary" disabled={busy === m.id} onClick={() => void act(m.id, "use")}>Use this one</button>
                    )}
                    <button className="btn small danger" disabled={busy === m.id} onClick={() => void act(m.id, "delete")}>Delete</button>
                  </>
                ) : (
                  <button
                    className="btn small primary"
                    disabled={busy === m.id || (busyElsewhere && !queued)}
                    onClick={() => void act(m.id, "download")}
                  >
                    <Icon name="download" size={14} /> Download {size(m.size_bytes)}
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {!shown.length && <div className="sub faint">No model matches that. Clear the filters to see all {view.models.length}.</div>}
      </div>

      {view.selected && (
        <div className="grid2" style={{ marginTop: 12 }}>
          <div>
            <label className="field">Language</label>
            <select className="field" value={view.language} onChange={(e) => void api.post<SpeechView>("/api/stt/select", { language: e.target.value }).then(setView).catch((x) => setProblem(errorText(x)))}>
              <option value="auto">{autoDetects ? "auto — the model decides" : "auto — English for this model"}</option>
              {view.languages.map((code) => (
                <option key={code} value={code}>{name(code)}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="field">Decoding threads</label>
            <input
              className="field"
              type="number"
              min={1}
              max={16}
              defaultValue={view.threads}
              onBlur={(e) => void api.post<SpeechView>("/api/stt/select", { threads: Number(e.target.value) || 2 }).then(setView).catch((x) => setProblem(errorText(x)))}
            />
          </div>
        </div>
      )}
    </div>
  );
}
