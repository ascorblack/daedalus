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
import { SpeechRuntimeNotice } from "./Components";
import { sttFrame } from "../sttview";
import type { AgentNews, Listener, Speaker, VoicePreset, VoiceUi } from "../voice";
import {
  IDLE_VOICE,
  agentNote,
  createMeter,
  createRecognition,
  createRecorder,
  createSpeaker,
  micReady,
  modelRow,
  orbVisual,
  recognitionSupported,
  recorderSupported,
  sendUtterance,
  shouldBargeIn,
  smoothLevel,
  voiceLang,
  voiceReducer,
} from "../voice";

type Agent = AgentNews;
type VoiceState = {
  enabled: boolean;
  session_id: string;
  /** The model in use, as it is written on a page: a preset's label, or provider/model when it has none. */
  model: string;
  /** The preset the operator chose; "" is "whatever the default is", which `using` resolves. */
  preset?: string;
  using?: string;
  presets?: VoicePreset[];
  tts?: {
    configured: boolean;
    reason?: string;
    voice?: string;
    model?: string;
    /** Which of the three speaks, decided by the server and merely followed here, as with `stt`. */
    kind?: string;
    state?: string;
  };
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


export function VoiceScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const { data: state, refresh } = useQuery<VoiceState>("/api/voice", { staleMs: 10000, pollMs: 60000 });
  const [ui, dispatch] = useReducer(voiceReducer, IDLE_VOICE);
  const [typed, setTyped] = useState("");
  useLang();
  const speaker = useRef<Speaker | null>(null);
  const listener = useRef<Listener | null>(null);
  const meter = useRef<ReturnType<typeof createMeter> | null>(null);
  const lang = useMemo(() => voiceLang(), []);
  // The server produces the audio for both of the first two: a voice on this machine and a speech
  // endpoint both answer /api/voice/tts, and only the browser's own synthesiser does not.
  const serverTts = !!state?.tts?.configured;
  // What the page calls that, which is a third thing: a voice running here is not the server's, and
  // the recognition half of this very page has said so about its own three for a unit already.
  const ttsKind = state?.tts?.kind ?? (serverTts ? "endpoint" : "browser");
  const spokenBy =
    ttsKind === "local"
      ? t("voice.out.local", { voice: state?.tts?.voice || "" })
      : ttsKind === "endpoint"
        ? t("voice.out.server")
        : t("voice.out.browser");
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
  const speakingSince = useRef(0);
  const earsOpen = useCallback(() => !speakingRef.current && Date.now() >= deafUntil.current, []);
  const onSpeaking = useCallback((on: boolean) => {
    speakingRef.current = on;
    if (on) speakingSince.current = Date.now();
    else deafUntil.current = Date.now() + ECHO_TAIL_MS;
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
  //
  // The loop runs only while there is somebody to see it. A reader who asked for less motion gets the
  // properties written once and the loop never started — the stylesheet pins how the orb looks for
  // them, but the sixty writes a second behind that were still happening and cost more than the
  // drawing did. A hidden tab is the same case: the page is still mounted, still connected, still
  // being spoken to, and nothing about that needs an animation frame.
  useEffect(() => {
    const still = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    let shown = 0;
    let last = "";
    const write = () => {
      const visual = orbVisual(uiRef.current.phase, shown);
      const node = orb.current;
      // Three identical writes are three style invalidations for nothing, and an idle page makes the
      // same three sixty times a second.
      const now = `${visual.scale.toFixed(3)} ${visual.glow.toFixed(3)} ${visual.spin.toFixed(2)}`;
      if (!node || now === last) return;
      last = now;
      node.style.setProperty("--orb-scale", visual.scale.toFixed(3));
      node.style.setProperty("--orb-glow", visual.glow.toFixed(3));
      node.style.setProperty("--orb-spin", `${visual.spin.toFixed(2)}s`);
    };
    const paint = () => {
      shown = smoothLevel(shown, rawLevel.current);
      write();
      frame = requestAnimationFrame(paint);
    };
    const settle = () => {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      if (still.matches || document.hidden) {
        // Written once so the custom properties have values at all: the stylesheet reads them even
        // where it overrides what they do.
        shown = 0;
        write();
        return;
      }
      frame = requestAnimationFrame(paint);
    };
    settle();
    document.addEventListener("visibilitychange", settle);
    still.addEventListener("change", settle);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      document.removeEventListener("visibilitychange", settle);
      still.removeEventListener("change", settle);
    };
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
        speak(String(p.text ?? ""));
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
  //
  // The speaker is rebuilt when the page learns which of the three speaks, and that reading arrives
  // after the stream is already open: a sentence that lands in the gap used to be handed to a null
  // and dropped without a trace, which is one of the ways the first answer of a conversation was
  // never heard. It waits here instead, and is spoken by whichever speaker is built next.
  const waiting = useRef<string[]>([]);
  const speak = useCallback((text: string) => {
    if (!text.trim()) return;
    if (speaker.current) speaker.current.say(text);
    else waiting.current.push(text);
  }, []);
  const onUnspoken = useCallback((text: string) => dispatch({ type: "unspoken", text }), []);
  const onBlocked = useCallback((on: boolean) => dispatch({ type: "blocked", on }), []);
  useEffect(() => {
    speaker.current = createSpeaker({ server: serverTts, lang, onSpeaking, onLevel, onUnspoken, onBlocked });
    const held = waiting.current;
    waiting.current = [];
    for (const text of held) speaker.current.say(text);
    return () => {
      speaker.current?.stop();
      speaker.current = null;
    };
  }, [serverTts, lang, onSpeaking, onLevel, onUnspoken, onBlocked]);

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
    // Three things stop, and all three have to: the clip that is playing, the request fetching the
    // rest of the answer (`cancel` aborts it, which is what stops the synthesiser at the far end),
    // and the run that is producing the sentences.
    speaker.current?.cancel();
    dispatch({ type: "barge" });
    // The tail the speaker leaves behind is for its own echo. The operator is talking *now*, and
    // deafening the page for four hundred milliseconds would drop the start of what they said.
    deafUntil.current = 0;
    void api.post("/api/voice/interrupt", {}).catch(() => undefined);
  }, []);

  /** Somebody started talking. `shouldBargeIn` decides whose voice that is; this acts on the answer. */
  const onSpeechStart = useCallback(
    (level?: number) => {
      // The browser's own recognition reports that somebody started talking and never says how
      // loudly, and a listener with no measurement at all is believed — that is its own voice
      // activity detector talking. But on that path the page *does* measure, through the meter it
      // opened for the orb, and not using that reading meant the page's own speaker barging in on
      // itself four hundred milliseconds into every answer.
      const heard = level ?? (meter.current ? rawLevel.current : undefined);
      if (!shouldBargeIn({ speaking: speakingRef.current, playingForMs: Date.now() - speakingSince.current, level: heard })) return;
      haptic("light");
      bargeIn();
    },
    [bargeIn],
  );

  const startMic = useCallback(async () => {
    speaker.current?.unlock();
    const handlers = {
      onInterim: (text: string) => earsOpen() && dispatch({ type: "heard", text }),
      onFinal: (text: string) => earsOpen() && void send(text),
      onSpeechStart,
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
  }, [canRecognise, earsOpen, lang, localStt, onLevel, onSpeechStart, send]);

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
  // Only the one question the page can act on: an installation with no model quick enough to hold a
  // conversation is told where to get one, rather than left to wonder why every answer is late.
  const model = modelRow(state);
  return (
    <>
      <PageHeader
        title={<VoiceTitle />}
        subtitle={state ? `${state.model} · ${spokenBy}` : "…"}
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
              <span className="chip quiet">{spokenBy}</span>
            </div>

            {/* The page where the absence is felt: the chip above says "the browser's own" and this
                says what would change that, with the size on the button. It renders nothing once the
                runtime is installed. */}
            <SpeechRuntimeNotice toast={toast} onInstalled={refresh} />

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
                  <span className={`voice-sentence${ui.unspoken.includes(sentence) ? " unspoken" : ""}`} key={`${i}-${sentence.slice(0, 12)}`}>
                    {sentence}
                    {ui.unspoken.includes(sentence) && <i className="voice-unspoken">{t("voice.unspoken")}</i>}{" "}
                  </span>
                ))}
                {!ui.spoken.length && ui.partial && <span className="voice-sentence writing">{ui.partial}</span>}
                {!ui.spoken.length && !ui.partial && <span className="voice-waiting" aria-hidden />}
              </div>
              {ui.problem && <p className="voice-problem">{ui.problem}</p>}
            </div>

            {/* There is no asking a browser whether it will make a sound; it is found out by handing
                it a sentence and hearing nothing start. When that happens the page says so and offers
                the one thing that fixes it, which is a tap — the same tap the microphone needs. */}
            {ui.blocked && (
              <div className="voice-blocked">
                <span>{t("voice.sound.blocked")}</span>
                <button
                  className="btn small"
                  onClick={() => {
                    speaker.current?.unlock();
                    dispatch({ type: "blocked", on: false });
                    haptic("light");
                  }}
                >
                  {t("voice.sound.enable")}
                </button>
              </div>
            )}

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
            {model.addFast && (
              <div className="sub voice-why">
                {t("voice.card.model.none")}{" "}
                <a href={pathFor("settings", "models")} onClick={(e) => go(e, pathFor("settings", "models"))}>
                  {t("voice.card.model.add")}
                </a>
              </div>
            )}
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
                      {note.waiting ? <span className="chip accent">{t(note.waiting)}</span> : <StatusLabel status={a.status} />}
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

/** Which of the three speaks, in one line: the same order /api/voice decides and the chip follows. */
function spokenLine(data: VoiceState): string {
  const tts = data.tts;
  if (tts?.kind === "local") return t("voice.card.out.local", { voice: tts.voice || "" });
  if (tts?.configured) return t("voice.card.out.server", { model: tts.model || "", voice: tts.voice || "" });
  return tts?.reason || t("voice.card.out.browser");
}

/** The Settings card: what the page runs on and what it can and cannot do here. */
export function VoiceSettings({ toast }: { toast: (t: string) => void }) {
  const { data, refresh } = useQuery<VoiceState>("/api/voice", { staleMs: 10000 });
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState("");
  // The model is chosen here because this is where the operator came looking for it. It is the one
  // thing on this card that is written rather than reported, and the reading that follows the write
  // is the server's, not the app's guess at what it did.
  const pick = async (preset: string) => {
    setSaving(true);
    setProblem("");
    try {
      await api.put("/api/voice/model", { preset });
      await refresh();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setSaving(false);
    }
  };
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  const row = modelRow(data);
  const local = data.stt?.local;
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("voice.card.title")}</div>
      <div className="sub">{t("voice.card.intro")}</div>
      <div className="kv">
        <span>{t("voice.card.enabled")}</span>
        <b>{t(data.enabled ? "voice.card.enabled.yes" : "voice.card.enabled.no")}</b>
      </div>
      <div className="kv">
        <span>{t("voice.card.model")}</span>
        <select className="field" style={{ margin: 0, maxWidth: "60%" }} value={row.value} disabled={saving} aria-label={t("voice.card.model")} onChange={(e) => void pick(e.target.value)}>
          <option value="">{row.fallback ? t("voice.card.model.default.named", { model: row.fallback }) : t("voice.card.model.default")}</option>
          {row.choices.map((c) => (
            <option key={c.id} value={c.id}>
              {c.slow ? t("voice.card.model.option.slow", { label: c.label, detail: c.detail }) : `${c.label} — ${c.detail}`}
            </option>
          ))}
        </select>
      </div>
      <div className="sub">
        {t("voice.card.model.hint")} {row.warn && <span className="chip attn">{t(row.warn)}</span>}
      </div>
      {row.addFast && (
        <div className="sub">
          {t("voice.card.model.none")}{" "}
          <a href={pathFor("settings", "models")} onClick={(e) => go(e, pathFor("settings", "models"))}>
            {t("voice.card.model.add")}
          </a>
        </div>
      )}
      {problem && <div className="sub" style={{ color: "var(--bad)" }}>{problem}</div>}
      <div className="kv">
        <span>{t("voice.card.out")}</span>
        <b>{spokenLine(data)}</b>
      </div>
      <div className="kv">
        <span>{t("voice.card.in")}</span>
        <b>
          {local?.active
            ? t(local.streaming ? "voice.card.in.local.streaming" : "voice.card.in.local", { label: local.label })
            : recognitionSupported()
              ? t("voice.card.in.browser")
              : data.stt?.configured
                ? t("voice.card.in.server")
                : t("voice.card.in.none")}
        </b>
      </div>
      {local?.active && (
        <div className="kv">
          <span>{t("voice.card.memory")}</span>
          <b>
            {data.stt?.state === "loading"
              ? t("voice.card.memory.loading")
              : data.stt?.state === "error"
                ? data.stt.error || t("voice.card.memory.failed")
                : data.stt?.loaded_in_ms
                  ? t("voice.card.memory.ready", { s: (data.stt.loaded_in_ms / 1000).toFixed(1) })
                  : t("voice.card.memory.later")}
          </b>
        </div>
      )}
      {/* Where the two lines above say "the browser's own" because nothing else is installed, the
          offer to install it belongs directly under them rather than on another page. */}
      <SpeechRuntimeNotice toast={toast} onInstalled={refresh} />
      <div className="btnrow">
        <a className="btn small" href={pathFor("voice")} onClick={(e) => go(e, pathFor("voice"))}>
          {t("voice.card.open")}
        </a>
      </div>
    </div>
  );
}
