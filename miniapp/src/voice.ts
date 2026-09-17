// The browser half of the voice page: hearing the operator and speaking the answer.
//
// Both ends have two implementations. Speech in is the browser's own streaming recognition where it
// exists (Chrome, Edge, Safari) and a recorder with a silence detector where it does not (Firefox),
// whose utterances the server transcribes. Speech out is the configured /audio/speech endpoint where
// there is one, and the browser's synthesiser otherwise. The page picks; the screen only sees words.

import { api } from "./api";

type RecognitionEvent = { resultIndex: number; results: { isFinal: boolean; 0: { transcript: string } }[] };
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  onspeechstart: (() => void) | null;
};

declare global {
  interface Window {
    SpeechRecognition?: new () => Recognition;
    webkitSpeechRecognition?: new () => Recognition;
    webkitAudioContext?: typeof AudioContext;
  }
}

/** The language the operator is spoken to in: what the app was opened in, or what the browser is set to. */
export function voiceLang(): string {
  const tg = window.Telegram?.WebApp?.initDataUnsafe?.user?.language_code;
  const raw = tg || navigator.language || "en";
  return raw.includes("-") ? raw : raw === "ru" ? "ru-RU" : raw === "en" ? "en-US" : raw;
}

export function recognitionSupported(): boolean {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

export function recorderSupported(): boolean {
  return typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

export type Listener = {
  start: () => Promise<void>;
  stop: () => void;
  kind: "recognition" | "recorder" | "local";
};

export type ListenerHandlers = {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onSpeechStart: () => void;
  onError: (message: string) => void;
  /** The microphone's loudness, 0 to about 1, as often as the listener has it. Drives the orb. */
  onLevel?: (level: number) => void;
};

/** Streaming recognition: words arrive while they are being said, and a final result is one utterance. */
export function createRecognition(lang: string, h: ListenerHandlers): Listener {
  const Ctor = window.SpeechRecognition ?? window.webkitSpeechRecognition;
  let engine: Recognition | null = null;
  let wanted = false;
  const build = (): Recognition => {
    const r = new Ctor!();
    r.lang = lang;
    r.continuous = true;
    r.interimResults = true;
    r.onspeechstart = () => h.onSpeechStart();
    r.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        const text = result[0]?.transcript ?? "";
        if (result.isFinal) {
          const final = text.trim();
          if (final) h.onFinal(final);
        } else interim += text;
      }
      h.onInterim(interim.trim());
    };
    r.onerror = (e) => {
      // "no-speech" and "aborted" are the engine idling, not a failure worth telling the operator about.
      if (e.error !== "no-speech" && e.error !== "aborted") h.onError(e.error === "not-allowed" ? "the microphone was refused" : e.error);
    };
    // Chrome ends the session on its own after a pause; while the operator wants the mic on, start it again.
    r.onend = () => {
      engine = null;
      if (wanted) window.setTimeout(() => wanted && void start(), 250);
    };
    return r;
  };
  const start = async () => {
    wanted = true;
    if (engine) return;
    engine = build();
    try {
      engine.start();
    } catch {
      /* already started: the onend restart raced with a tap */
    }
  };
  return {
    kind: "recognition",
    start,
    stop: () => {
      wanted = false;
      try {
        engine?.stop();
      } catch {
        /* never started */
      }
      engine = null;
    },
  };
}

const SILENCE_LEVEL = 0.012;
const SILENCE_MS = 800;
const MIN_UTTERANCE_MS = 400;
const MAX_UTTERANCE_MS = 60_000;

/**
 * No streaming recognition here: record, watch the level, and cut an utterance when the operator has
 * been quiet for a moment. Each cut is posted to the server, which transcribes it with the configured
 * endpoint. Coarser than recognition — there are no interim words — but it is the whole of Firefox.
 */
export function createRecorder(h: ListenerHandlers & { onUtterance: (blob: Blob) => void }): Listener {
  let stream: MediaStream | null = null;
  let recorder: MediaRecorder | null = null;
  let context: AudioContext | null = null;
  let timer: number | undefined;
  let chunks: Blob[] = [];
  let speaking = false;
  let quietSince = 0;
  let startedAt = 0;

  const cut = () => {
    if (!recorder || recorder.state !== "recording") return;
    recorder.stop();
  };

  const start = async () => {
    if (stream) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      h.onError("the microphone was refused");
      return;
    }
    const Ctx = window.AudioContext ?? window.webkitAudioContext!;
    context = new Ctx();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const begin = () => {
      if (!stream) return;
      chunks = [];
      speaking = false;
      startedAt = Date.now();
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      recorder.onstop = () => {
        const blob = new Blob(chunks, { type: recorder?.mimeType || "audio/webm" });
        const long = Date.now() - startedAt > MIN_UTTERANCE_MS;
        if (speaking && long && blob.size > 2000) h.onUtterance(blob);
        if (stream) begin();
      };
      recorder.start(250);
    };
    begin();
    timer = window.setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += v * v;
      const level = Math.sqrt(sum / samples.length);
      h.onLevel?.(level);
      const now = Date.now();
      if (level > SILENCE_LEVEL) {
        if (!speaking) h.onSpeechStart();
        speaking = true;
        quietSince = 0;
        h.onInterim("…");
        if (now - startedAt > MAX_UTTERANCE_MS) cut();
        return;
      }
      if (!speaking) return;
      if (!quietSince) quietSince = now;
      else if (now - quietSince > SILENCE_MS) {
        h.onInterim("");
        cut();
      }
    }, 100);
  };

  return {
    kind: "recorder",
    start,
    stop: () => {
      window.clearInterval(timer);
      const live = stream;
      stream = null;
      try {
        recorder?.state === "recording" && recorder.stop();
      } catch {
        /* already stopped */
      }
      recorder = null;
      live?.getTracks().forEach((t) => t.stop());
      void context?.close();
      context = null;
    },
  };
}

export type Speaker = {
  /** One sentence, spoken after everything already queued. */
  say: (text: string) => void;
  /** Barge-in: drop what is queued and stop what is playing. */
  cancel: () => void;
  /** iOS Safari plays nothing that a tap did not start: call this from the first tap. */
  unlock: () => void;
  stop: () => void;
};

/**
 * How loud the answer is while it is being spoken, for the orb to ripple with.
 *
 * Two sources, because there are two speakers. Server audio is a real signal and is measured: the
 * `<audio>` element is routed through an analyser, once — a media element can only be given to one
 * `MediaElementAudioSourceNode` ever, and a second attempt throws and takes the audio with it. The
 * browser's own synthesiser exposes nothing at all: no node, no level, not even a boundary event in
 * every engine. So that half is a cadence rather than a measurement, and it is written here as one
 * honestly: a slow wave with a faster one over it, which reads as speech without claiming to be it.
 */
function createSpeechLevel(audio: HTMLAudioElement, opts: { server: boolean; onLevel?: (level: number) => void }): { start: () => void; stop: () => void } {
  let context: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let samples = new Float32Array(0);
  let frame = 0;
  let began = 0;

  const attach = () => {
    if (analyser || !opts.server) return;
    try {
      const Ctx = window.AudioContext ?? window.webkitAudioContext!;
      context = new Ctx();
      analyser = context.createAnalyser();
      analyser.fftSize = 512;
      samples = new Float32Array(analyser.fftSize);
      const source = context.createMediaElementSource(audio);
      source.connect(analyser);
      // The element is no longer heard directly once it has a source node, so the graph has to carry
      // it to the output itself. Without this line the page goes silent and nothing says why.
      analyser.connect(context.destination);
    } catch {
      analyser = null; // No analyser here: the cadence below stands in, and the audio is untouched.
    }
  };

  const tick = () => {
    if (analyser && samples.length) {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += v * v;
      opts.onLevel?.(Math.sqrt(sum / samples.length));
    } else {
      const t = (performance.now() - began) / 1000;
      opts.onLevel?.(0.12 + 0.06 * Math.sin(t * 7.3) + 0.05 * Math.sin(t * 2.1));
    }
    frame = requestAnimationFrame(tick);
  };

  return {
    start: () => {
      if (frame) return;
      attach();
      void context?.resume().catch(() => undefined);
      began = performance.now();
      frame = requestAnimationFrame(tick);
    },
    stop: () => {
      cancelAnimationFrame(frame);
      frame = 0;
      opts.onLevel?.(0);
    },
  };
}

/**
 * One queue, one player. Sentences arrive from the server faster than they are read out, so each is
 * fetched (or synthesised) as it arrives and played in the order it came; cancelling drops the lot,
 * including the request in flight, whose result is thrown away rather than played after the barge-in.
 */
export function createSpeaker(opts: { server: boolean; lang: string; onSpeaking: (on: boolean) => void; onLevel?: (level: number) => void }): Speaker {
  const audio = new Audio();
  audio.preload = "auto";
  let generation = 0;
  let queue: string[] = [];
  let playing = false;
  const level = createSpeechLevel(audio, opts);

  const done = () => {
    playing = false;
    if (queue.length) void next();
    else {
      level.stop();
      opts.onSpeaking(false);
    }
  };

  const next = async () => {
    const mine = generation;
    const text = queue.shift();
    if (text === undefined) return done();
    playing = true;
    opts.onSpeaking(true);
    level.start();
    if (!opts.server) {
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = opts.lang;
      const match = window.speechSynthesis.getVoices().find((v) => v.lang.toLowerCase().startsWith(opts.lang.slice(0, 2).toLowerCase()));
      if (match) utterance.voice = match;
      utterance.onend = () => mine === generation && done();
      utterance.onerror = () => mine === generation && done();
      window.speechSynthesis.speak(utterance);
      return;
    }
    try {
      const response = await fetch("/api/voice/tts", { method: "POST", headers: { "Content-Type": "application/json", ...api.authHeaders() }, body: JSON.stringify({ text }) });
      if (mine !== generation) return;
      if (!response.ok) throw new Error(String(response.status));
      const url = URL.createObjectURL(await response.blob());
      if (mine !== generation) return URL.revokeObjectURL(url);
      audio.src = url;
      audio.onended = () => {
        URL.revokeObjectURL(url);
        if (mine === generation) done();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        if (mine === generation) done();
      };
      await audio.play();
    } catch {
      if (mine === generation) done();
    }
  };

  return {
    say: (text: string) => {
      const body = text.trim();
      if (!body) return;
      queue.push(body);
      if (!playing) void next();
    },
    cancel: () => {
      generation += 1;
      queue = [];
      playing = false;
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* no synthesiser */
      }
      audio.pause();
      audio.removeAttribute("src");
      level.stop();
      opts.onSpeaking(false);
    },
    unlock: () => {
      // A muted play() inside the tap is what buys the element the right to play later, on iOS.
      audio.muted = true;
      void audio
        .play()
        .then(() => {
          audio.pause();
          audio.muted = false;
        })
        .catch(() => {
          audio.muted = false;
        });
      try {
        window.speechSynthesis.getVoices();
      } catch {
        /* no synthesiser */
      }
    },
    stop: () => {
      generation += 1;
      queue = [];
      playing = false;
      audio.pause();
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* no synthesiser */
      }
      // Unmount goes through here, so the phase must come back down with it: a remount that starts in
      // "speaking" never leaves it, because nothing is playing to end.
      level.stop();
      opts.onSpeaking(false);
    },
  };
}

/** Post one recorded utterance for the server to transcribe; returns what it heard. */
export async function sendUtterance(blob: Blob): Promise<string> {
  const form = new FormData();
  form.append("audio", blob, "utterance.webm");
  const response = await fetch("/api/voice/audio", { method: "POST", headers: api.authHeaders(), body: form });
  if (!response.ok) throw new Error(response.status === 413 ? "that recording is too long" : `the utterance was not accepted (${response.status})`);
  return String(((await response.json()) as { transcript?: string }).transcript ?? "");
}

// ── the agents panel ───────────────────────────────────────────────────────────────────────

/** One delegated agent as the voice endpoint reports it. */
export type AgentNews = {
  session_id: string;
  title: string;
  status: string;
  last_message_at: string;
  answer: string;
  /** The last thing it said mid-run: narration, or what it is waiting for. */
  progress?: string;
  progress_at?: string;
  /** "operator" — it asked a question; "approval" — the policy stopped a call; "" — it is getting on with it. */
  waiting?: string;
};

/** The one line the panel shows for an agent, and when that line is from. */
export type AgentNote = { line: string; when: string; waiting: string; live: boolean };

const WAITING_WORDS: Record<string, string> = { operator: "Waiting for you", approval: "Waiting for approval" };

/**
 * What to show under an agent's title: its latest words and the moment they are from.
 *
 * Mid-run words win over the last answer while they exist, because they are newer and they are what the
 * operator is waiting to hear about; the panel says so with the timestamp of the line itself, not of the
 * session, so "2 min ago" on a running agent means it spoke two minutes ago rather than started then.
 */
export function agentNote(a: AgentNews): AgentNote {
  const progress = (a.progress ?? "").trim();
  const waiting = WAITING_WORDS[a.waiting ?? ""] ?? "";
  if (progress) return { line: progress, when: a.progress_at || a.last_message_at, waiting, live: true };
  return { line: (a.answer ?? "").trim(), when: a.last_message_at, waiting, live: false };
}

// ── what the page is doing, as one value ───────────────────────────────────────────────────
//
// The page has six things it can be doing and eleven things that can change which — the operator's
// microphone, the engine loading, the recogniser's words, the concierge's status, its sentences, the
// speaker starting and stopping. Held as a dozen separate pieces of component state, the rules
// between them (a finished answer goes back to listening only if the microphone is still on; a model
// still loading outranks everything) live in four different callbacks and disagree. So they are one
// value and one function, which is also the only way any of it can be tested without a browser.

/** What the operator sees the page doing. Every one of these is drawn differently. */
export type VoicePhase = "idle" | "loading" | "listening" | "thinking" | "delegating" | "speaking";

/** Where the local recogniser's weights are, as `/api/voice` and the progress stream report them. */
export type EngineState = { state: string; model: string; loadedInMs: number; error: string };

export type VoiceUi = {
  phase: VoicePhase;
  micOn: boolean;
  /** The title of the agent being set up, while one is. */
  delegating: string;
  /** The last thing the operator said, as it was understood. */
  asked: string;
  /** What the recogniser has heard of the sentence being said now. */
  heard: string;
  /** The concierge's answer, a spoken sentence at a time, in the order it was said. */
  spoken: string[];
  /** The answer as it is being written, for the part that has not become a sentence yet. */
  partial: string;
  problem: string;
  agents: AgentNews[];
  engine: EngineState;
};

export type VoiceEvent =
  | { type: "mic"; on: boolean }
  | { type: "heard"; text: string }
  | { type: "asked"; text: string }
  | { type: "partial"; text: string }
  | { type: "say"; text: string }
  | { type: "status"; state: string; title?: string }
  | { type: "done" }
  | { type: "agents"; agents: AgentNews[] }
  | { type: "speaking"; on: boolean }
  | { type: "engine"; engine: Partial<EngineState> }
  | { type: "problem"; message: string }
  | { type: "cleared" };

export const IDLE_VOICE: VoiceUi = {
  phase: "idle",
  micOn: false,
  delegating: "",
  asked: "",
  heard: "",
  spoken: [],
  partial: "",
  problem: "",
  agents: [],
  engine: { state: "ready", model: "", loadedInMs: 0, error: "" },
};

/** Whether the microphone may be opened at all: a model still loading cannot hear anything. */
export function micReady(state: VoiceUi): boolean {
  return state.engine.state !== "loading";
}

/**
 * What the page falls back to when nothing is happening: still loading, still listening, or idle.
 *
 * Every transition that ends — the answer finished, the speaker stopped, the error shown — comes back
 * through here rather than guessing "idle", which is what used to leave the page saying "Ready" while
 * the microphone was still open.
 */
function resting(state: VoiceUi): VoicePhase {
  if (state.engine.state === "loading") return "loading";
  return state.micOn ? "listening" : "idle";
}

export function voiceReducer(state: VoiceUi, event: VoiceEvent): VoiceUi {
  switch (event.type) {
    case "mic": {
      const next = { ...state, micOn: event.on, heard: event.on ? state.heard : "" };
      if (event.on) return { ...next, problem: "", phase: resting(next) };
      // Stopping the microphone does not stop the concierge: an answer being written or spoken keeps
      // its phase, and only a page that was merely listening goes quiet.
      return { ...next, phase: state.phase === "listening" || state.phase === "loading" ? resting(next) : state.phase };
    }
    case "heard":
      return { ...state, heard: event.text };
    case "asked":
      // A new utterance clears the last answer rather than appending to it: the captions are a
      // conversation, and the previous reply is on its way off the screen the moment this one starts.
      return { ...state, asked: event.text, heard: "", spoken: [], partial: "", problem: "", phase: "thinking" };
    case "partial":
      return { ...state, partial: event.text };
    case "say":
      return event.text.trim() ? { ...state, spoken: [...state.spoken, event.text.trim()] } : state;
    case "status": {
      if (event.state === "idle") return { ...state, delegating: "", phase: state.phase === "speaking" ? state.phase : resting(state) };
      const phase = event.state === "delegating" ? "delegating" : event.state === "thinking" ? "thinking" : state.phase;
      return { ...state, delegating: event.state === "delegating" ? event.title ?? "" : "", phase };
    }
    case "done":
      return state.phase === "thinking" || state.phase === "delegating" ? { ...state, delegating: "", phase: resting(state) } : { ...state, delegating: "" };
    case "agents":
      return { ...state, agents: event.agents };
    case "speaking":
      if (event.on) return { ...state, phase: "speaking" };
      return state.phase === "speaking" ? { ...state, phase: resting(state) } : state;
    case "engine": {
      const engine = { ...state.engine, ...event.engine };
      const next = { ...state, engine };
      // A model that has finished loading releases the page: whatever it was waiting to be, it is now.
      if (state.phase === "loading" || engine.state === "loading") return { ...next, phase: resting(next) };
      return next;
    }
    case "problem":
      return { ...state, problem: event.message, phase: resting(state) };
    case "cleared":
      return { ...IDLE_VOICE, micOn: state.micOn, engine: state.engine, phase: resting(state) };
  }
}

// ── the orb ────────────────────────────────────────────────────────────────────────────────

/**
 * One microphone level, smoothed the way an ear is: quick to rise, slow to fall.
 *
 * A raw RMS read sixty times a second makes the orb flicker on every consonant and collapse in every
 * gap between two words, which reads as a fault rather than as speech. Rising fast keeps the reaction
 * immediate; falling slowly keeps a sentence looking like one thing.
 */
export function smoothLevel(previous: number, next: number): number {
  const target = Math.max(0, Math.min(1, next));
  const rate = target > previous ? 0.55 : 0.12;
  return previous + (target - previous) * rate;
}

/** How the orb looks right now: how big, how bright, and how fast its light travels around it. */
export type OrbVisual = { scale: number; glow: number; spin: number };

/**
 * The level-to-look mapping, in one pure function so it can be checked without a browser.
 *
 * The curve is deliberately not linear. Speech at a normal distance from a laptop microphone sits
 * around an RMS of 0.05–0.2, so a linear mapping spends nine tenths of its range on volumes nobody
 * produces and the orb barely moves while someone is talking normally.
 */
export function orbVisual(phase: VoicePhase, level: number): OrbVisual {
  const heard = Math.pow(Math.max(0, Math.min(1, level)) * 3.2, 0.55);
  const loud = Math.max(0, Math.min(1, heard));
  switch (phase) {
    case "listening":
      return { scale: 1 + loud * 0.24, glow: 0.35 + loud * 0.65, spin: 1 };
    case "speaking":
      return { scale: 1 + loud * 0.16, glow: 0.45 + loud * 0.55, spin: 1.6 };
    case "thinking":
    case "delegating":
      return { scale: 1.05, glow: 0.7, spin: 2.4 };
    case "loading":
      return { scale: 1.02, glow: 0.5, spin: 2 };
    default:
      return { scale: 1, glow: 0.3, spin: 0.7 };
  }
}

/**
 * The microphone's loudness, for a listener that has no audio of its own.
 *
 * The local model's listener taps the samples it is already sending, and the recorder already has an
 * analyser; the browser's own `SpeechRecognition` hands over words and nothing else, so on that path
 * the page opens its own read-only tap on the same microphone. The permission has already been
 * granted by then, so nothing is asked of the operator twice.
 */
export function createMeter(onLevel: (level: number) => void): { start: () => Promise<void>; stop: () => void } {
  let stream: MediaStream | null = null;
  let context: AudioContext | null = null;
  let frame = 0;
  return {
    start: async () => {
      if (stream) return;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } catch {
        return; // The listener itself reports a refused microphone; a meter that cannot run is silent.
      }
      const Ctx = window.AudioContext ?? window.webkitAudioContext!;
      context = new Ctx();
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      context.createMediaStreamSource(stream).connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      const tick = () => {
        analyser.getFloatTimeDomainData(samples);
        let sum = 0;
        for (const v of samples) sum += v * v;
        onLevel(Math.sqrt(sum / samples.length));
        frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
    },
    stop: () => {
      cancelAnimationFrame(frame);
      stream?.getTracks().forEach((t) => t.stop());
      stream = null;
      void context?.close();
      context = null;
    },
  };
}
