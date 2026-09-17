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
  kind: "recognition" | "recorder";
};

export type ListenerHandlers = {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onSpeechStart: () => void;
  onError: (message: string) => void;
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
 * One queue, one player. Sentences arrive from the server faster than they are read out, so each is
 * fetched (or synthesised) as it arrives and played in the order it came; cancelling drops the lot,
 * including the request in flight, whose result is thrown away rather than played after the barge-in.
 */
export function createSpeaker(opts: { server: boolean; lang: string; onSpeaking: (on: boolean) => void }): Speaker {
  const audio = new Audio();
  audio.preload = "auto";
  let generation = 0;
  let queue: string[] = [];
  let playing = false;

  const done = () => {
    playing = false;
    if (queue.length) void next();
    else opts.onSpeaking(false);
  };

  const next = async () => {
    const mine = generation;
    const text = queue.shift();
    if (text === undefined) return done();
    playing = true;
    opts.onSpeaking(true);
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
