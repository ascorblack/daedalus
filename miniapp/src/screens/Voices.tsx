// Choosing the voice the answer is read in.
//
// A synthesiser is not chosen from a table. Nobody can tell two Russian voices apart by their size,
// their licence or a quality bar — they can only be told apart by listening to them — so every card
// here has a Play button that synthesises one sentence in the card's own language, through the very
// engine that will read the answers, before anything is chosen. Everything else on the card is there
// to narrow the field before that: the language, whether the voice is male or female, what it costs
// on disk, and whether it can keep up with a person talking, which two of them cannot.
//
// Only one voice is in use at a time; several can be installed, and the one in use is a tap away.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { errorText, haptic } from "../ui";

export type TtsVoice = {
  id: string;
  label: string;
  kind: string;
  language: string;
  gender: string;
  size_bytes: number;
  disk_bytes: number;
  memory_mb: number;
  sample_rate: number;
  licence: string;
  quality: number;
  speed: number;
  rtf: number;
  keeps_up: boolean;
  note: string;
  speakers: string[];
  recommended_for: string[];
  installed: boolean;
  installed_bytes: number;
  selected: boolean;
  progress?: { state: string; fraction: number; error: string };
};

export type TtsView = {
  models: TtsVoice[];
  languages: string[];
  selected: string;
  disk_bytes: number;
  engine_installed: boolean;
  recommended: Record<string, string>;
  state: {
    voice: string;
    label: string;
    speaker: string;
    speed: number;
    threads: number;
    installed: boolean;
    active: boolean;
    engine_installed: boolean;
    state: string;
    error: string;
    loaded: string;
    encoder: boolean;
  };
};

const name = (code: string) => t(`tts.lang.${code}`);

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

export function TtsVoices({ toast }: { toast: (message: string) => void }) {
  const [view, setView] = useState<TtsView | null>(null);
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [fastOnly, setFastOnly] = useState(false);
  const [busy, setBusy] = useState("");
  const [playing, setPlaying] = useState("");
  const [problem, setProblem] = useState("");
  // One player for the whole list. A second sample starting while the first is still talking would
  // be two voices at once, which is exactly what someone comparing them does not want to hear.
  const player = useRef<HTMLAudioElement | null>(null);

  const load = useCallback(async () => {
    try {
      setView(await api.get<TtsView>("/api/tts"));
    } catch (e) {
      setProblem(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Stop whatever is talking when the screen goes away, so navigating off the page is silence.
  useEffect(() => () => {
    player.current?.pause();
    if (player.current?.src) URL.revokeObjectURL(player.current.src);
  }, []);

  // Download progress comes as a stream rather than a poll, so a bar that is moving looks like it.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/api/tts/progress", { headers: api.authHeaders(), signal: controller.signal });
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

  async function sample(id: string) {
    player.current?.pause();
    setProblem("");
    setPlaying(id);
    try {
      const response = await fetch(`/api/tts/voices/${encodeURIComponent(id)}/sample`, { method: "POST", headers: api.authHeaders() });
      if (!response.ok) throw new Error(await response.text());
      const url = URL.createObjectURL(await response.blob());
      const audio = new Audio(url);
      player.current = audio;
      const finish = () => {
        URL.revokeObjectURL(url);
        setPlaying((current) => (current === id ? "" : current));
      };
      audio.onended = finish;
      audio.onerror = finish;
      await audio.play();
    } catch (e) {
      setProblem(errorText(e));
      setPlaying("");
    }
  }

  async function act(id: string, what: "download" | "use" | "cancel" | "delete") {
    setBusy(id);
    setProblem("");
    try {
      if (what === "download") {
        // The engine is an optional extra natively, and it is the same wheel recognition uses, so
        // this is the recognition install endpoint on purpose rather than a second copy of it.
        if (view && !view.engine_installed) {
          toast(t("tts.engine.installing"));
          await api.post("/api/stt/engine");
        }
        await api.post(`/api/tts/voices/${encodeURIComponent(id)}/download`);
        toast(t("tts.toast.downloading"));
      } else if (what === "cancel") {
        await api.post(`/api/tts/voices/${encodeURIComponent(id)}/cancel`);
      } else if (what === "use") {
        setView(await api.post<TtsView>("/api/tts/select", { voice: id }));
        haptic("medium");
        toast(id ? t("tts.toast.using") : t("tts.toast.stopped"));
        return;
      } else {
        await api.delete(`/api/tts/voices/${encodeURIComponent(id)}`);
        toast(t("tts.toast.removed"));
      }
      await load();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy("");
    }
  }

  async function settings(patch: Record<string, unknown>) {
    try {
      setView(await api.post<TtsView>("/api/tts/select", patch));
    } catch (e) {
      setProblem(errorText(e));
    }
  }

  if (!view) return <div className="card"><div className="sub">{problem || t("common.loading")}</div></div>;

  const wanted = query.trim().toLowerCase();
  const shown = view.models.filter((m) => {
    if (fastOnly && !m.keeps_up) return false;
    if (language && m.language !== language) return false;
    if (wanted && !`${m.label} ${m.note} ${name(m.language)}`.toLowerCase().includes(wanted)) return false;
    return true;
  });
  const installedCount = view.models.filter((m) => m.installed).length;
  const busyElsewhere = view.models.some((m) => m.progress && ["downloading", "verifying", "unpacking"].includes(m.progress.state));
  const chosen = view.models.find((m) => m.id === view.selected);

  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("tts.title")}</div>
      <div className="sub">{t("tts.intro")}</div>

      <div className="kv" style={{ marginTop: 10 }}>
        <span>{t("tts.inuse")}</span>
        <b>
          {chosen ? chosen.label : t("tts.inuse.none")}
          {chosen && view.state.state === "loading" && <span className="sub faint"> · {t("tts.state.loading")}</span>}
          {chosen && view.state.state === "error" && <span className="sub attn"> · {t("tts.state.error")}</span>}
        </b>
      </div>
      <div className="kv">
        <span>{t("tts.ondisk")}</span>
        <b>{installedCount ? t("tts.ondisk.some", { n: installedCount, size: size(view.disk_bytes) }) : t("tts.ondisk.none")}</b>
      </div>
      {view.state.error && <div className="sub attn" style={{ marginTop: 6 }}>{view.state.error}</div>}

      <div className="stt-filters">
        <input className="field" style={{ margin: 0 }} placeholder={t("tts.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <select className="field" style={{ margin: 0 }} value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="">{t("tts.language.any")}</option>
          {view.languages.map((code) => (
            <option key={code} value={code}>{name(code)}</option>
          ))}
        </select>
        <button className={`chip select ${fastOnly ? "on" : ""}`} aria-pressed={fastOnly} onClick={() => setFastOnly((v) => !v)}>
          {t("tts.fast.only")}
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
                {recommended && <span className="chip accent">{language ? t("tts.recommended.for", { lang: name(language) }) : t("tts.recommended")}</span>}
                <span className="chip">{t(`tts.gender.${m.gender}`)}</span>
                <span className="chip">{name(m.language)}</span>
                {/* Only the exception is coloured. Two of these voices render slower than a person
                    talks, which is the one fact on the card that changes how the page feels. */}
                {!m.keeps_up && <span className="chip attn">{t("tts.slow")}</span>}
                {m.selected && <span className="chip accent">{t("tts.card.using")}</span>}
              </div>
              <div className="sub">{m.note}</div>
              <div className="stt-facts sub faint">
                <span>{t("tts.facts.size", { dl: size(m.size_bytes), disk: size(m.disk_bytes), mem: m.memory_mb })}</span>
                <span>{t("tts.facts.rate", { khz: Math.round(m.sample_rate / 1000), licence: m.licence })}</span>
                {m.speakers.length > 1 && <span>{t("tts.facts.speakers", { n: m.speakers.length })}</span>}
              </div>
              <div className="stt-bars">
                <Bar label={t("tts.quality")} value={m.quality} />
                <Bar label={t("tts.speed")} value={m.speed} />
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
              {progress?.state === "failed" && <div className="sub attn">{progress.error || t("tts.failed")}</div>}
              {queued && <div className="sub faint">{t("tts.queued")}</div>}
              <div className="btnrow">
                {downloading ? (
                  <button className="btn small" onClick={() => void act(m.id, "cancel")}>{t("tts.cancel")}</button>
                ) : m.installed ? (
                  <>
                    <button className="btn small" disabled={playing === m.id} onClick={() => void sample(m.id)}>
                      <Icon name="mic" size={14} /> {playing === m.id ? t("tts.playing") : t("tts.play")}
                    </button>
                    {m.selected ? (
                      <button className="btn small" onClick={() => void act("", "use")}>{t("tts.stop")}</button>
                    ) : (
                      <button className="btn small primary" disabled={busy === m.id} onClick={() => void act(m.id, "use")}>{t("tts.use")}</button>
                    )}
                    <button className="btn small danger" disabled={busy === m.id} onClick={() => void act(m.id, "delete")}>{t("tts.delete")}</button>
                  </>
                ) : (
                  <button
                    className="btn small primary"
                    disabled={busy === m.id || (busyElsewhere && !queued)}
                    onClick={() => void act(m.id, "download")}
                  >
                    <Icon name="download" size={14} /> {t("tts.download", { size: size(m.size_bytes) })}
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {!shown.length && <div className="sub faint">{t("tts.nomatch", { n: view.models.length })}</div>}
      </div>

      {chosen && (
        <div className="grid2" style={{ marginTop: 12 }}>
          {chosen.speakers.length > 1 && (
            <div>
              <label className="field">{t("tts.speaker")}</label>
              <select className="field" value={view.state.speaker} onChange={(e) => void settings({ speaker: e.target.value })}>
                <option value="">{t("tts.speaker.first")}</option>
                {chosen.speakers.map((who) => (
                  <option key={who} value={who}>{who}</option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label className="field">{t("tts.speed.label", { speed: view.state.speed.toFixed(2) })}</label>
            <input
              className="field"
              type="range"
              min={0.5}
              max={2}
              step={0.05}
              defaultValue={view.state.speed}
              onChange={(e) => void settings({ speed: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className="field">{t("tts.threads")}</label>
            <input
              className="field"
              type="number"
              min={1}
              max={16}
              defaultValue={view.state.threads}
              onBlur={(e) => void settings({ threads: Number(e.target.value) || 2 })}
            />
          </div>
        </div>
      )}
    </div>
  );
}
