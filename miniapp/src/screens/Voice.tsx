// Voice: the operator talks, a small fast model answers out loud, and the work goes to agents.
//
// The page is a conversation, not a transcript reader: one big control to take the mic, the words
// being said under it, the answer as it is spoken, and beside it the agents the concierge started —
// each one a tap away from its own session, where the actual work is visible.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Dot, StatusLabel, timeAgo } from "../components";
import { Icon } from "../icons";
import { pathFor, sessionPath } from "../router";
import { PageHeader, go, screenTitle } from "../shell";
import { useQuery } from "../store";
import { errorText, haptic } from "../ui";
import { createLocalListener, localListenSupported } from "../stt";
import { AgentNews, Listener, Speaker, agentNote, createRecognition, createRecorder, createSpeaker, recognitionSupported, recorderSupported, sendUtterance, voiceLang } from "../voice";

type Agent = AgentNews;
type VoiceState = {
  enabled: boolean;
  session_id: string;
  model: string;
  tts: { configured: boolean; reason?: string; voice?: string; model?: string };
  stt: {
    configured: boolean;
    reason?: string;
    /** The model that runs on this machine, where one is installed and selected. */
    local?: { model: string; label: string; installed: boolean; active: boolean; streaming: boolean; loaded: string };
  };
  agents: Agent[];
  listening: boolean;
};

type Phase = "idle" | "listening" | "thinking" | "speaking" | "delegating";

const PHASE_WORD: Record<Phase, string> = { idle: "Ready", listening: "Listening", thinking: "Thinking", speaking: "Speaking", delegating: "Setting that up" };

/** How long after the last spoken word the microphone stays deaf: a speaker's tail reaches it late. */
const ECHO_TAIL_MS = 400;

export function VoiceScreen({ onOpen }: { onOpen: (id: string) => void }) {
  const { data: state, refresh } = useQuery<VoiceState>("/api/voice", { staleMs: 10000, pollMs: 60000 });
  const [phase, setPhase] = useState<Phase>("idle");
  const [delegating, setDelegating] = useState("");
  const [heard, setHeard] = useState("");
  const [said, setSaid] = useState("");
  const [lastAsked, setLastAsked] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [problem, setProblem] = useState("");
  const [typed, setTyped] = useState("");
  const [micOn, setMicOn] = useState(false);
  const speaker = useRef<Speaker | null>(null);
  const listener = useRef<Listener | null>(null);
  const lang = useMemo(() => voiceLang(), []);
  const serverTts = !!state?.tts.configured;
  // Which recogniser listens, in the order the server decides and the page merely follows: a model
  // that runs on the server's processor first, then this browser's own recognition, then a recorder
  // whose cut utterances the server transcribes. The local model wins over the browser because it is
  // a deliberate choice the operator made and paid disk for; the browser's is whatever it shipped.
  const localStt = !!state?.stt.local?.active && localListenSupported();
  const canRecognise = recognitionSupported();
  const canRecord = recorderSupported() && !!state?.stt.configured;
  const canTalk = localStt || canRecognise || canRecord;
  const recogniser = localStt ? `${state?.stt.local?.label} on this machine` : canRecognise ? "this browser" : canRecord ? "recorded here, transcribed on the server" : "";

  useEffect(() => {
    setAgents(state?.agents ?? []);
  }, [state?.agents]);

  // The stream handler is built once, on mount; what it needs of the live state it reads through a ref.
  const micOnRef = useRef(false);
  useEffect(() => {
    micOnRef.current = micOn;
  }, [micOn]);

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
    setPhase((f) => (on ? "speaking" : f === "speaking" ? (micOnRef.current ? "listening" : "idle") : f));
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
      if (event === "partial") setSaid(String(p.text ?? ""));
      else if (event === "say") speaker.current?.say(String(p.text ?? ""));
      else if (event === "agents") setAgents((p.agents ?? []) as Agent[]);
      else if (event === "error") setProblem(String(p.message ?? "the concierge stopped"));
      else if (event === "done") setPhase((f) => (f === "thinking" || f === "delegating" ? "idle" : f));
      else if (event === "status") {
        const next = String(p.state ?? "idle");
        setDelegating(next === "delegating" ? String(p.title ?? "") : "");
        setPhase((f) => (next === "idle" ? (f === "speaking" ? f : micOnRef.current ? "listening" : "idle") : (next as Phase)));
      }
    }
    return () => {
      stop = true;
      controller.abort();
    };
  }, []);

  // ── speaking ─────────────────────────────────────────────────────────────────────────────
  useEffect(() => {
    speaker.current = createSpeaker({ server: serverTts, lang, onSpeaking });
    return () => {
      speaker.current?.stop();
      speaker.current = null;
    };
  }, [serverTts, lang, onSpeaking]);

  // ── what the operator says ───────────────────────────────────────────────────────────────
  const send = useCallback(async (text: string) => {
    const body = text.trim();
    if (!body) return;
    setLastAsked(body);
    setHeard("");
    setSaid("");
    setProblem("");
    setPhase("thinking");
    try {
      await api.post("/api/voice/say", { text: body });
    } catch (e) {
      setProblem(errorText(e));
      setPhase("idle");
    }
  }, []);

  const bargeIn = useCallback(() => {
    speaker.current?.cancel();
    void api.post("/api/voice/interrupt", {}).catch(() => undefined);
  }, []);

  const startMic = useCallback(async () => {
    speaker.current?.unlock();
    const handlers = {
      onInterim: (t: string) => earsOpen() && setHeard(t),
      onFinal: (t: string) => earsOpen() && void send(t),
      onSpeechStart: () => earsOpen() && bargeIn(),
      onError: (m: string) => setProblem(m),
    };
    const l = localStt
      ? createLocalListener(handlers)
      : canRecognise
      ? createRecognition(lang, handlers)
      : createRecorder({
          ...handlers,
          onUtterance: async (blob) => {
            if (!earsOpen()) return;
            setPhase("thinking");
            try {
              const text = await sendUtterance(blob);
              if (text.trim()) {
                setLastAsked(text.trim());
                setHeard("");
                setSaid("");
              } else setPhase("idle");
            } catch (e) {
              setProblem(errorText(e));
              setPhase("idle");
            }
          },
        });
    listener.current = l;
    await l.start();
    setMicOn(true);
    setPhase("listening");
    haptic("medium");
  }, [bargeIn, canRecognise, earsOpen, lang, localStt, send]);

  const stopMic = useCallback(() => {
    listener.current?.stop();
    listener.current = null;
    setMicOn(false);
    setHeard("");
    setPhase((f) => (f === "listening" ? "idle" : f));
  }, []);

  useEffect(() => () => listener.current?.stop(), []);

  async function newConversation() {
    stopMic();
    speaker.current?.cancel();
    setSaid("");
    setLastAsked("");
    setAgents([]);
    try {
      await api.post("/api/voice/new", {});
      await refresh();
    } catch (e) {
      setProblem(errorText(e));
    }
  }

  if (state && !state.enabled) {
    return (
      <>
        <PageHeader title={<VoiceTitle />} />
        <div className="screen wide">
          <div className="empty">
            <b>The voice page is switched off</b>
            <div>Turn it on in the configuration under [voice], then reload.</div>
          </div>
        </div>
      </>
    );
  }

  const phaseNow: Phase = delegating ? "delegating" : phase;
  return (
    <>
      <PageHeader
        title={<VoiceTitle />}
        subtitle={state ? `${state.model} · ${serverTts ? "server voice" : "browser voice"}` : "…"}
        actions={
          <>
            {state?.session_id && (
              <a className="btn ghost small" href={sessionPath(state.session_id)} onClick={(e) => go(e, sessionPath(state.session_id))}>
                Transcript
              </a>
            )}
            <button className="btn small" onClick={() => void newConversation()}>
              New conversation
            </button>
          </>
        }
      />
      <div className="screen wide voice">
        <div className="voice-grid">
          <section className="voice-stage card">
            <div className="voice-status">
              <span className={`chip ${phaseNow === "idle" ? "" : "accent"}`}>
                <Dot status={phaseNow === "idle" ? "idle" : phaseNow === "listening" ? "waiting" : "running"} />
                {PHASE_WORD[phaseNow]}
                {delegating && `: ${delegating}`}
              </span>
              {!serverTts && <span className="chip">browser voice</span>}
            </div>

            <button
              className={`mic-button ${micOn ? "on" : ""}`}
              onClick={() => (micOn ? stopMic() : void startMic())}
              disabled={!canTalk}
              aria-pressed={micOn}
              aria-label={micOn ? "Stop listening" : "Start listening"}
            >
              <Icon name="mic" size={40} />
              <span className="mic-ring" aria-hidden />
            </button>
            <div className="voice-hint sub">{!canTalk ? "This browser cannot listen; type below instead." : micOn ? "Talk. Tap again to stop." : "Tap to talk."}</div>

            <div className="voice-captions">
              {lastAsked && <p className="voice-asked">{lastAsked}</p>}
              {heard && <p className="voice-heard">{heard}</p>}
              {said ? <p className="voice-said">{said}</p> : <p className="voice-said empty-line sub">{micOn ? "…" : ""}</p>}
              {problem && <p className="voice-problem">{problem}</p>}
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
              <input className="field" placeholder="…or type an utterance" value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Type an utterance" />
              <button className="btn primary" type="submit" disabled={!typed.trim()}>
                <Icon name="send" size={16} />
              </button>
            </form>
            <div className="sub voice-why">
              {canTalk
                ? `Listening: ${recogniser}.`
                : "This browser has no speech recognition, and nothing here can turn a recording into words — download a speech model in Settings → Voice, or configure a transcription endpoint."}
            </div>
          </section>

          <aside className="voice-agents">
            <h2 className="voice-agents-head">
              Agents<span className="sub">{agents.length ? ` · ${agents.length}` : ""}</span>
            </h2>
            {agents.length === 0 && <div className="sub voice-agents-empty">Nothing delegated yet. Ask for something that takes real work and it appears here.</div>}
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

function VoiceTitle() {
  return (
    <>
      {screenTitle("voice")} <span className="chip accent voice-beta">beta</span>
    </>
  );
}

/** The Settings card: what the page runs on and what it can and cannot do here. */
export function VoiceSettings() {
  const { data } = useQuery<VoiceState>("/api/voice", { staleMs: 10000 });
  if (!data) return <div className="sub">Loading…</div>;
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
        <b>{data.tts.configured ? `server · ${data.tts.model} · ${data.tts.voice}` : data.tts.reason || "the browser's own synthesiser ([voice.tts] is empty)"}</b>
      </div>
      <div className="kv">
        <span>Speech in</span>
        <b>
          {data.stt.local?.active
            ? `${data.stt.local.label}, running on this machine${data.stt.local.streaming ? " — words appear as they are said" : ""}`
            : recognitionSupported()
              ? "this browser recognises speech itself"
              : data.stt.configured
                ? "recorded here, transcribed on the server"
                : "not available in this browser, and nothing is configured to transcribe a recording"}
        </b>
      </div>
      <div className="btnrow">
        <a className="btn small" href={pathFor("voice")} onClick={(e) => go(e, pathFor("voice"))}>
          Open the voice page
        </a>
      </div>
    </div>
  );
}
