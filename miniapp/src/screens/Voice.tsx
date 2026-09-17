// Voice: the operator talks, a small fast model answers out loud, and the work goes to agents.
//
// The page is a conversation, not a transcript reader. One thing is in the middle of it — an orb that
// breathes when nothing is happening, swells with the operator's own voice while it listens, turns
// and brightens while the concierge thinks, and ripples in time with the answer while it is spoken.
// Under it are the words: what was asked, what is being heard, and the reply a sentence at a time.
// Beside it are the agents the concierge started, each one a tap away from its own session.
//
// The motion is not decoration. There is no other signal on this page: the microphone is open or it
// is not, the model is loaded or it is loading, the answer is being written or being read out, and a
// person who is talking rather than reading has to know which from the corner of their eye. Where the
// reader asked for less of it — `prefers-reduced-motion` — the colours and the words still say all
// four, and the global rule in styles.css takes the animation away.

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { api } from "../api";
import { StatusLabel, timeAgo } from "../components";
import { t, useLang } from "../i18n";
import { Icon } from "../icons";
import { pathFor, sessionPath } from "../router";
import { PageHeader, go, screenTitle } from "../shell";
import { useQuery } from "../store";
import { errorText, haptic } from "../ui";
import { createLocalListener, localListenSupported } from "../stt";
import { sttFrame } from "../sttview";
import type { AgentNews, Listener, Speaker, VoiceUi } from "../voice";
import {
  IDLE_VOICE,
  agentNote,
  createMeter,
  createRecognition,
  createRecorder,
  createSpeaker,
  micReady,
  orbVisual,
  recognitionSupported,
  recorderSupported,
  sendUtterance,
  smoothLevel,
  voiceLang,
  voiceReducer,
} from "../voice";

type Agent = AgentNews;
type VoiceState = {
  enabled: boolean;
  session_id: string;
  model: string;
  tts?: { configured: boolean; reason?: string; voice?: string; model?: string };
  stt?: {
    configured: boolean;
    reason?: string;
    /** Which of the three recognisers listens, decided by the server and merely followed here. */
    kind?: string;
    /** Where the local model's weights are: `loading`, `ready`, `error` — `ready` for everything else. */
    state?: string;
    loaded_in_ms?: number;
    error?: string;
    /** The model that runs on this machine, where one is installed and selected. */
    local?: { model: string; label: string; installed: boolean; active: boolean; streaming: boolean; loaded: string };
  };
  agents?: Agent[];
  listening: boolean;
};

/** How long after the last spoken word the microphone stays deaf: a speaker's tail reaches it late. */
const ECHO_TAIL_MS = 400;

export function VoiceScreen({ onOpen }: { onOpen: (id: string) => void }) {
  const { data: state, refresh } = useQuery<VoiceState>("/api/voice", { staleMs: 10000, pollMs: 60000 });
  const [ui, dispatch] = useReducer(voiceReducer, IDLE_VOICE);
  const [typed, setTyped] = useState("");
  useLang();
  const speaker = useRef<Speaker | null>(null);
  const listener = useRef<Listener | null>(null);
  const meter = useRef<ReturnType<typeof createMeter> | null>(null);
  const lang = useMemo(() => voiceLang(), []);
  const serverTts = !!state?.tts?.configured;
  // Which recogniser listens, in the order the server decides and the page merely follows: a model
  // that runs on the server's processor first, then this browser's own recognition, then a recorder
  // whose cut utterances the server transcribes. The local model wins over the browser because it is
  // a deliberate choice the operator made and paid disk for; the browser's is whatever it shipped.
  const localStt = !!state?.stt?.local?.active && localListenSupported();
  const canRecognise = recognitionSupported();
  const canRecord = recorderSupported() && !!state?.stt?.configured;
  const canTalk = localStt || canRecognise || canRecord;
  const modelName = state?.stt?.local?.label || state?.stt?.local?.model || "";
  const recogniser = localStt
    ? t("voice.recogniser.local", { label: modelName })
    : canRecognise
      ? t("voice.recogniser.browser")
      : canRecord
        ? t("voice.recogniser.server")
        : "";

  useEffect(() => {
    dispatch({ type: "agents", agents: state?.agents ?? [] });
  }, [state?.agents]);

  // The engine's own state comes from the same place the recogniser choice does, so a page opened
  // while the weights are still loading starts in "loading" rather than in "ready" with a microphone
  // that hears nothing.
  useEffect(() => {
    const stt = state?.stt;
    if (!stt) return;
    dispatch({
      type: "engine",
      engine: { state: String(stt.state ?? "ready"), model: stt.local?.model ?? "", loadedInMs: Number(stt.loaded_in_ms ?? 0), error: String(stt.error ?? "") },
    });
  }, [state?.stt]);

  // The stream handler is built once, on mount; what it needs of the live state it reads through a ref.
  const uiRef = useRef(ui);
  uiRef.current = ui;

  // The page is heard by its own microphone: a laptop or phone speaker plays the answer straight back
  // into the recogniser, which would barge in on it and then submit the machine's words as the
  // operator's next utterance. While anything is playing — the server's audio or the browser's own
  // synthesiser — the listener keeps running and everything it hears is dropped.
  const speakingRef = useRef(false);
  const deafUntil = useRef(0);
  const earsOpen = useCallback(() => !speakingRef.current && Date.now() >= deafUntil.current, []);
  const onSpeaking = useCallback((on: boolean) => {
    speakingRef.current = on;
    if (!on) deafUntil.current = Date.now() + ECHO_TAIL_MS;
    dispatch({ type: "speaking", on });
  }, []);

  // ── the orb's level ──────────────────────────────────────────────────────────────────────
  //
  // Sixty values a second is not React's business: each one would be a render of a page that has a
  // list on it. The raw level lands in a ref, one animation frame smooths it and writes three custom
  // properties onto the orb, and the component re-renders only when the phase changes.
  const orb = useRef<HTMLButtonElement | null>(null);
  const rawLevel = useRef(0);
  const onLevel = useCallback((level: number) => {
    rawLevel.current = level;
  }, []);
  useEffect(() => {
    let frame = 0;
    let shown = 0;
    const paint = () => {
      shown = smoothLevel(shown, rawLevel.current);
      const visual = orbVisual(uiRef.current.phase, shown);
      const node = orb.current;
      if (node) {
        node.style.setProperty("--orb-scale", visual.scale.toFixed(3));
        node.style.setProperty("--orb-glow", visual.glow.toFixed(3));
        node.style.setProperty("--orb-spin", `${visual.spin.toFixed(2)}s`);
      }
      frame = requestAnimationFrame(paint);
    };
    frame = requestAnimationFrame(paint);
    return () => cancelAnimationFrame(frame);
  }, []);

  // ── the concierge's half of the conversation ────────────────────────────────────────────
  useEffect(() => {
    let stop = false;
    const controller = new AbortController();
    void (async () => {
      let backoff = 1000;
      while (!stop) {
        try {
          const response = await fetch("/api/voice/stream", { headers: api.authHeaders(), signal: controller.signal });
          // One bodiless answer from a proxy is a dropped connection, not the end of the page: fall
          // through to the backoff below rather than leaving the loop for good.
          if (!response.body) throw new Error("no stream");
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          backoff = 1000;
          while (!stop) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split("\n\n");
            buffer = frames.pop() ?? "";
            for (const frame of frames) {
              const event = /^event: (.*)$/m.exec(frame)?.[1];
              const data = /^data: (.*)$/m.exec(frame)?.[1];
              if (!event || !data) continue;
              try {
                handle(event, JSON.parse(data));
              } catch {
                /* one malformed frame must not end the stream */
              }
            }
          }
        } catch {
          /* aborted or dropped: reconnect below */
        }
        if (stop) return;
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, 15000);
      }
    })();
    function handle(event: string, p: Record<string, any>) {
      if (event === "partial") dispatch({ type: "partial", text: String(p.text ?? "") });
      else if (event === "say") {
        dispatch({ type: "say", text: String(p.text ?? "") });
        speaker.current?.say(String(p.text ?? ""));
      } else if (event === "agents") dispatch({ type: "agents", agents: (p.agents ?? []) as Agent[] });
      else if (event === "error") dispatch({ type: "problem", message: String(p.message ?? "the concierge stopped") });
      else if (event === "done") dispatch({ type: "done" });
      else if (event === "status") dispatch({ type: "status", state: String(p.state ?? "idle"), title: String(p.title ?? "") });
    }
    return () => {
      stop = true;
      controller.abort();
    };
  }, []);

  // ── the model loading into memory ────────────────────────────────────────────────────────
  //
  // Opening this page starts the load (the server does it when it answers /api/voice); this is how
  // the page hears about it finishing without polling for it.
  useEffect(() => {
    if (state?.stt?.kind !== "local") return;
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
          for (const chunk of frames) {
            const data = /^data: (.*)$/m.exec(chunk)?.[1];
            if (!data) continue;
            try {
              const frame = sttFrame(JSON.parse(data));
              if (frame?.kind === "engine") {
                dispatch({ type: "engine", engine: { state: frame.load.state, model: frame.load.model, loadedInMs: frame.load.loaded_in_ms, error: frame.load.error } });
              }
            } catch {
              /* one malformed frame must not end the stream */
            }
          }
        }
      } catch {
        /* the page navigated away, or the stream dropped */
      }
    })();
    return () => controller.abort();
  }, [state?.stt?.kind]);

  // ── speaking ─────────────────────────────────────────────────────────────────────────────
  useEffect(() => {
    speaker.current = createSpeaker({ server: serverTts, lang, onSpeaking, onLevel });
    return () => {
      speaker.current?.stop();
      speaker.current = null;
    };
  }, [serverTts, lang, onSpeaking, onLevel]);

  // ── what the operator says ───────────────────────────────────────────────────────────────
  const send = useCallback(async (text: string) => {
    const body = text.trim();
    if (!body) return;
    dispatch({ type: "asked", text: body });
    try {
      await api.post("/api/voice/say", { text: body });
    } catch (e) {
      dispatch({ type: "problem", message: errorText(e) });
    }
  }, []);

  const bargeIn = useCallback(() => {
    speaker.current?.cancel();
    void api.post("/api/voice/interrupt", {}).catch(() => undefined);
  }, []);

  const startMic = useCallback(async () => {
    speaker.current?.unlock();
    const handlers = {
      onInterim: (text: string) => earsOpen() && dispatch({ type: "heard", text }),
      onFinal: (text: string) => earsOpen() && void send(text),
      onSpeechStart: () => earsOpen() && bargeIn(),
      onError: (message: string) => dispatch({ type: "problem", message }),
      onLevel,
    };
    const l = localStt
      ? createLocalListener(handlers)
      : canRecognise
        ? createRecognition(lang, handlers)
        : createRecorder({
            ...handlers,
            onUtterance: async (blob) => {
              if (!earsOpen()) return;
              try {
                const text = await sendUtterance(blob);
                if (text.trim()) dispatch({ type: "asked", text: text.trim() });
              } catch (e) {
                dispatch({ type: "problem", message: errorText(e) });
              }
            },
          });
    listener.current = l;
    await l.start();
    // The browser's own recognition hands over words and no audio at all, so on that path the level
    // the orb reacts to comes from a second, read-only tap on the microphone.
    if (l.kind === "recognition") {
      meter.current = createMeter(onLevel);
      void meter.current.start();
    }
    dispatch({ type: "mic", on: true });
    haptic("medium");
  }, [bargeIn, canRecognise, earsOpen, lang, localStt, onLevel, send]);

  const stopMic = useCallback(() => {
    listener.current?.stop();
    listener.current = null;
    meter.current?.stop();
    meter.current = null;
    rawLevel.current = 0;
    dispatch({ type: "mic", on: false });
  }, []);

  useEffect(
    () => () => {
      listener.current?.stop();
      meter.current?.stop();
    },
    [],
  );

  async function newConversation() {
    stopMic();
    speaker.current?.cancel();
    dispatch({ type: "cleared" });
    try {
      await api.post("/api/voice/new", {});
      await refresh();
    } catch (e) {
      dispatch({ type: "problem", message: errorText(e) });
    }
  }

  if (state && !state.enabled) {
    return (
      <>
        <PageHeader title={<VoiceTitle />} />
        <div className="screen wide">
          <div className="empty">
            <b>{t("voice.off.title")}</b>
            <div>{t("voice.off.body")}</div>
          </div>
        </div>
      </>
    );
  }

  const ready = micReady(ui);
  const agents = ui.agents;
  return (
    <>
      <PageHeader
        title={<VoiceTitle />}
        subtitle={state ? `${state.model} · ${serverTts ? t("voice.out.server") : t("voice.out.browser")}` : "…"}
        actions={
          <>
            {state?.session_id && (
              <a className="btn ghost small" href={sessionPath(state.session_id)} onClick={(e) => go(e, sessionPath(state.session_id))}>
                {t("voice.transcript")}
              </a>
            )}
            <button className="btn small" onClick={() => void newConversation()}>
              {t("voice.new")}
            </button>
          </>
        }
      />
      <div className="screen wide voice">
        <div className="voice-grid">
          <section className={`voice-stage card phase-${ui.phase}`}>
            <div className="voice-chips">
              <PhaseChip ui={ui} />
              <span className="chip quiet">{serverTts ? t("voice.out.server") : t("voice.out.browser")}</span>
            </div>

            <div className="voice-orb-wrap">
              <button
                ref={orb}
                className={`voice-orb ${ui.micOn ? "on" : ""}`}
                onClick={() => (ui.micOn ? stopMic() : void startMic())}
                disabled={!canTalk || !ready}
                aria-pressed={ui.micOn}
                aria-label={ui.micOn ? t("voice.mic.stop") : t("voice.mic.start")}
              >
                <span className="orb-halo" aria-hidden />
                <span className="orb-shell" aria-hidden />
                <span className="orb-sweep" aria-hidden />
                <span className="orb-ring" aria-hidden />
                <span className="orb-ring two" aria-hidden />
                <span className="orb-glyph" aria-hidden>
                  <Icon name="mic" size={36} />
                </span>
              </button>
            </div>

            <div className="voice-hint sub">
              {!ready
                ? t("voice.loading.model", { name: modelName || t("voice.phase.loading") })
                : ui.engine.state === "error"
                  ? t("voice.loading.failed", { name: modelName, error: ui.engine.error })
                  : !canTalk
                    ? t("voice.tap.none")
                    : ui.micOn
                      ? t("voice.tap.stop")
                      : t("voice.tap")}
            </div>

            <div className="voice-captions" aria-live="polite">
              {ui.asked && <p className="voice-asked">{ui.asked}</p>}
              {ui.heard && <p className="voice-heard">{ui.heard}</p>}
              <div className="voice-said">
                {ui.spoken.map((sentence, i) => (
                  <span className="voice-sentence" key={`${i}-${sentence.slice(0, 12)}`}>
                    {sentence}{" "}
                  </span>
                ))}
                {!ui.spoken.length && ui.partial && <span className="voice-sentence writing">{ui.partial}</span>}
                {!ui.spoken.length && !ui.partial && <span className="voice-waiting" aria-hidden />}
              </div>
              {ui.problem && <p className="voice-problem">{ui.problem}</p>}
            </div>

            <form
              className="voice-compose"
              onSubmit={(e) => {
                e.preventDefault();
                const text = typed;
                setTyped("");
                void send(text);
              }}
            >
              <input className="field" placeholder={t("voice.compose")} value={typed} onChange={(e) => setTyped(e.target.value)} aria-label={t("voice.compose.label")} />
              <button className="btn ghost" type="submit" disabled={!typed.trim()} aria-label={t("voice.compose.label")}>
                <Icon name="send" size={16} />
              </button>
            </form>
            <div className="sub voice-why">{canTalk ? t("voice.listening.with", { what: recogniser }) : t("voice.recogniser.none")}</div>
          </section>

          <aside className="voice-agents">
            <h2 className="voice-agents-head">
              {t("voice.agents")}
              <span className="sub">{agents.length ? ` · ${agents.length}` : ""}</span>
            </h2>
            {agents.length === 0 && <div className="sub voice-agents-empty">{t("voice.agents.empty")}</div>}
            {agents.map((a) => {
              const note = agentNote(a);
              return (
                <button key={a.session_id} className="card row pressable voice-agent" onClick={() => onOpen(a.session_id)}>
                  <div className="grow">
                    <div className="voice-agent-top">
                      <b className="truncate">{a.title}</b>
                      {note.waiting ? <span className="chip accent">{note.waiting}</span> : <StatusLabel status={a.status} />}
                    </div>
                    {note.line && <div className={`sub clamp-2${note.live ? " voice-agent-live" : ""}`}>{note.line}</div>}
                    <div className="sub num">{timeAgo(note.when)}</div>
                  </div>
                </button>
              );
            })}
          </aside>
        </div>
      </div>
    </>
  );
}

/** The one word for what the page is doing, and the animation that says it without being read. */
function PhaseChip({ ui }: { ui: VoiceUi }) {
  const word = t(`voice.phase.${ui.phase}`);
  return (
    <span className={`chip voice-chip ${ui.phase}`}>
      <span className="voice-chip-mark" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      {word}
      {ui.delegating && `: ${ui.delegating}`}
    </span>
  );
}

function VoiceTitle() {
  return (
    <>
      {screenTitle("voice")} <span className="chip accent voice-beta">{t("voice.beta")}</span>
    </>
  );
}

/** The Settings card: what the page runs on and what it can and cannot do here. */
export function VoiceSettings() {
  const { data } = useQuery<VoiceState>("/api/voice", { staleMs: 10000 });
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  const local = data.stt?.local;
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>Voice (beta)</div>
      <div className="sub">A small fast model the operator talks to. It answers what it can itself and hands real work to agent sessions, then reports when they finish.</div>
      <div className="kv">
        <span>Enabled</span>
        <b>{data.enabled ? "yes" : "no — set enabled in [voice]"}</b>
      </div>
      <div className="kv">
        <span>Model</span>
        <b>{data.model || "—"}</b>
      </div>
      <div className="kv">
        <span>Speech out</span>
        <b>{data.tts?.configured ? `server · ${data.tts.model} · ${data.tts.voice}` : data.tts?.reason || "the browser's own synthesiser ([voice.tts] is empty)"}</b>
      </div>
      <div className="kv">
        <span>Speech in</span>
        <b>
          {local?.active
            ? `${local.label}, running on this machine${local.streaming ? " — words appear as they are said" : ""}`
            : recognitionSupported()
              ? "this browser recognises speech itself"
              : data.stt?.configured
                ? "recorded here, transcribed on the server"
                : "not available in this browser, and nothing is configured to transcribe a recording"}
        </b>
      </div>
      {local?.active && (
        <div className="kv">
          <span>In memory</span>
          <b>
            {data.stt?.state === "loading"
              ? "loading now"
              : data.stt?.state === "error"
                ? data.stt.error || "the last load failed"
                : data.stt?.loaded_in_ms
                  ? `ready · loaded in ${(data.stt.loaded_in_ms / 1000).toFixed(1)} s`
                  : "loads when the voice page opens"}
          </b>
        </div>
      )}
      <div className="btnrow">
        <a className="btn small" href={pathFor("voice")} onClick={(e) => go(e, pathFor("voice"))}>
          Open the voice page
        </a>
      </div>
    </div>
  );
}
