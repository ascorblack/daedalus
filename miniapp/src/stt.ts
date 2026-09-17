// Streaming speech recognition against a model that runs on the server's own CPU.
//
// The browser's `SpeechRecognition` is Chrome, Edge and Safari; Firefox has never had it, and a
// Telegram webview may or may not. Where a local model is installed this replaces it outright, and it
// behaves the same way from the page's point of view: interim words while the sentence is being said,
// a final when it ends. The difference is only where the recognising happens.
//
// Audio goes out as raw mono PCM16, a fifth of a second per request, POSTed rather than socketed —
// the app authenticates with a header, and a browser cannot put one on a WebSocket. Each answer
// carries the words heard so far; the one marked final is an utterance.
//
// The rate is negotiated rather than assumed. `new AudioContext({sampleRate})` is a request, and
// Safari and several Android webviews answer it with the hardware's own 44 100 or 48 000 instead of
// throwing. So the graph is built, `sampleRate` is read back, and that is what the server is told when
// the stream is opened — the model resamples, which it cannot do if it was told the wrong number.

import { api } from "./api";
import type { Listener, ListenerHandlers } from "./voice";

/** What the models want, and what the capture graph is asked for. Not necessarily what it gives. */
const RATE = 16000;

/** How much audio one request carries. Short enough that words appear as they are said. */
const CHUNK_MS = 200;

/** Frames the worklet hands over at a time; 128 is the platform's own block size. */
const WORKLET_SOURCE = `
class Tap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor("daedalus-tap", Tap);
`;

function toPcm16(samples: Float32Array): ArrayBuffer {
  const out = new DataView(new ArrayBuffer(samples.length * 2));
  for (let i = 0; i < samples.length; i++) {
    // Clamp before scaling: a sample above 1 wraps to a loud click instead of clipping quietly.
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    out.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return out.buffer;
}

/** Whether this browser can capture at all. The rest is the server's problem, not the page's. */
export function localListenSupported(): boolean {
  return typeof AudioContext !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

/**
 * A listener that sends samples to the server's model and shows what comes back.
 *
 * The stream id is this page's, for this microphone session: the server keeps one decoder per id and
 * lets go of it when the page closes it or stops feeding it. Errors are counted rather than shouted
 * about — one dropped chunk on a flaky link is not worth interrupting a sentence for, several in a
 * row is.
 */
export function createLocalListener(h: ListenerHandlers): Listener {
  let streamId = "";
  let seq = 0;
  let rate = RATE;
  let media: MediaStream | null = null;
  let context: AudioContext | null = null;
  let node: AudioWorkletNode | ScriptProcessorNode | null = null;
  let pending: Float32Array[] = [];
  let held = 0;
  let inflight = false;
  let inflightDone: Promise<void> = Promise.resolve();
  let failures = 0;
  let spoke = false;
  let wanted = false;

  const url = (final: boolean, n: number) =>
    `/api/voice/listen?stream=${encodeURIComponent(streamId)}&seq=${n}${final ? "&final=true" : ""}`;

  /** Ask the server for a stream, declaring the rate the graph actually settled on. */
  const open = async (hz: number) => {
    const response = await fetch(`/api/voice/listen/open?rate=${Math.round(hz)}`, { method: "POST", headers: api.authHeaders() });
    if (!response.ok) throw new Error(response.status === 409 ? "no local speech model is selected" : `the stream was refused (${response.status})`);
    const body = (await response.json()) as { stream: string; rate: number };
    streamId = body.stream;
    seq = 0;
  };

  const post = async (body: ArrayBuffer, final: boolean) => {
    const response = await fetch(url(final, ++seq), {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", ...api.authHeaders() },
      body,
    });
    if (!response.ok) throw new Error(response.status === 409 ? "no local speech model is selected" : `the stream was refused (${response.status})`);
    return (await response.json()) as { text: string; final: boolean };
  };

  const drain = async () => {
    if (inflight || !pending.length || !wanted) return;
    inflight = true;
    const batch = pending;
    pending = [];
    held = 0;
    const total = batch.reduce((n, part) => n + part.length, 0);
    const joined = new Float32Array(total);
    let at = 0;
    for (const part of batch) {
      joined.set(part, at);
      at += part.length;
    }
    try {
      const result = await post(toPcm16(joined), false);
      failures = 0;
      if (result.final) {
        // An endpoint: the model decided the sentence ended. That is one utterance, and the page
        // treats it exactly as it treats one the browser recognised itself.
        if (result.text.trim()) {
          spoke = false;
          h.onInterim("");
          h.onFinal(result.text.trim());
        }
      } else if (result.text) {
        if (!spoke) {
          spoke = true;
          h.onSpeechStart();
        }
        h.onInterim(result.text);
      }
    } catch (e) {
      // A single failure is a dropped request; three in a row is the feature not working, and the
      // operator should be told rather than left watching a microphone that hears nothing.
      if (++failures >= 3) h.onError(e instanceof Error ? e.message : "the stream stopped");
    } finally {
      inflight = false;
      if (pending.length) inflightDone = drain();
    }
  };

  const take = (samples: Float32Array) => {
    if (!wanted) return;
    // The loudness is taken from the samples already in hand rather than from a second tap on the
    // microphone: this path has the audio, and the orb should react to exactly what is being sent.
    if (h.onLevel) {
      let sum = 0;
      for (const v of samples) sum += v * v;
      h.onLevel(Math.sqrt(sum / samples.length));
    }
    pending.push(samples);
    held += samples.length;
    if (held >= (rate * CHUNK_MS) / 1000) inflightDone = drain();
  };

  const start = async () => {
    if (media) return;
    wanted = true;
    // Built first, and synchronously. iOS Safari starts a context created outside the synchronous span
    // of the user gesture in state "suspended", and there is no gesture left after the await below: the
    // microphone opens, the orange dot appears, and the graph never runs. Asking for the model's rate
    // is what keeps a resampler out of this file; where the browser refuses and gives its own, the
    // server is told which and resamples instead.
    context = new AudioContext({ sampleRate: RATE });
    rate = context.sampleRate || RATE;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
    } catch {
      wanted = false;
      void context.close();
      context = null;
      h.onError("the microphone was refused");
      return;
    }
    if (context.state === "suspended") await context.resume();
    if (context.state !== "running") {
      // A suspended graph is indistinguishable from a model that hears nothing, which is the worst
      // way for this to fail: the page would sit on "Listening" forever.
      h.onError("this browser would not start the microphone — the audio is blocked or suspended");
      stop();
      return;
    }
    try {
      await open(rate);
    } catch (e) {
      h.onError(e instanceof Error ? e.message : "the stream could not be opened");
      stop();
      return;
    }
    const source = context.createMediaStreamSource(media);
    try {
      const moduleUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
      await context.audioWorklet.addModule(moduleUrl);
      URL.revokeObjectURL(moduleUrl);
      const worklet = new AudioWorkletNode(context, "daedalus-tap");
      worklet.port.onmessage = (e) => take(e.data as Float32Array);
      source.connect(worklet);
      // A worklet with nothing downstream is not pulled on in every browser; a silent sink is enough
      // to keep the graph running without anything being heard.
      const sink = context.createGain();
      sink.gain.value = 0;
      worklet.connect(sink).connect(context.destination);
      node = worklet;
    } catch {
      // Older webviews have no AudioWorklet. The deprecated processor still captures, and this page
      // only ever reads from it.
      const processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (e) => take(new Float32Array(e.inputBuffer.getChannelData(0)));
      source.connect(processor);
      processor.connect(context.destination);
      node = processor;
    }
  };

  const stop = () => {
    wanted = false;
    // What is still held is the end of a sentence, and it is the half the operator cares most about:
    // a batch model has it all buffered and a streaming one has an undecoded tail. Sending it with
    // final=true decodes it and pops the stream server-side, so no close call belongs after it.
    const tail = pending;
    const id = streamId;
    pending = [];
    held = 0;
    try {
      node?.disconnect();
    } catch {
      /* already gone */
    }
    node = null;
    media?.getTracks().forEach((t) => t.stop());
    media = null;
    void context?.close();
    context = null;
    streamId = "";
    if (!id) return;
    void (async () => {
      try {
        // The chunks are numbered and the server refuses one out of order, so the flush waits for
        // whatever request was already on its way rather than overtaking it.
        await inflightDone;
        const total = tail.reduce((n, part) => n + part.length, 0);
        const joined = new Float32Array(total);
        let at = 0;
        for (const part of tail) {
          joined.set(part, at);
          at += part.length;
        }
        streamId = id;
        const result = await post(toPcm16(joined), true);
        if (result.text.trim()) h.onFinal(result.text.trim());
      } catch {
        // The stream is going away either way; a failed flush costs the tail of one sentence, and
        // the server drops the decoder on its own after two idle minutes.
        void fetch(`/api/voice/listen/close?stream=${encodeURIComponent(id)}`, { method: "POST", headers: api.authHeaders() }).catch(() => undefined);
      } finally {
        streamId = "";
      }
    })();
  };

  return { kind: "local", start, stop };
}
