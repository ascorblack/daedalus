// The page's terminals: one registry for every view (dock, full screen, the Terminals screen), and the
// few things all of them share — the environments links are rewritten for, the font size, the
// colour scheme.

import type { TerminalEnv } from "../api";
import { InstanceShared, onFontSize, storedFontSize, TerminalInstance } from "./instance";
import { TerminalRegistry } from "./registry";

const envs = new Map<string, TerminalEnv>();
const live = new Set<TerminalInstance>();
let webgl2: boolean | null = null;

function domOnly(): boolean {
  try {
    return localStorage.getItem("daedalus.term.renderer") === "dom";
  } catch {
    return false;
  }
}

const shared: InstanceShared = {
  env: (name) => envs.get(name),
  fontSize: storedFontSize,
  webgl2: () => {
    if (webgl2 === null) {
      try {
        // `daedalus.term.renderer = dom` turns WebGL off on this device: the way out for a GPU that
        // draws a terminal wrongly, and for the screenshot run, whose emulated pixel ratio Chromium
        // does not report in the device-pixel box the WebGL canvas is sized from.
        webgl2 = !domOnly() && !!document.createElement("canvas").getContext("webgl2");
      } catch {
        webgl2 = false;
      }
    }
    return webgl2;
  },
};

/** Options a view asks for when it is the first to open a terminal. */
const pending = new Map<string, { readOnly: boolean }>();

export const terminals = new TerminalRegistry<TerminalInstance>({
  create: (id) => {
    const instance = new TerminalInstance(id, shared, pending.get(id)?.readOnly ?? false);
    live.add(instance);
    return instance;
  },
  dispose: (instance) => {
    live.delete(instance);
    instance.dispose();
  },
  sleep: (instance) => instance.sleep(),
  wake: (instance) => instance.wake(),
  webgl: (instance, on) => instance.setWebgl(on),
});

/** `terminals.acquire`, with the options the first view wants. */
export function acquireTerminal(id: string, options: { readOnly?: boolean; env?: string } = {}): TerminalInstance {
  pending.set(id, { readOnly: !!options.readOnly });
  const instance = terminals.acquire(id);
  pending.delete(id);
  if (options.env) instance.envName = options.env;
  return instance;
}

/** The open instance for a terminal, if any view has it. */
export function instanceFor(id: string): TerminalInstance | undefined {
  for (const instance of live) if (instance.id === id) return instance;
  return undefined;
}

/** The environments as the host last listed them. */
export function setTerminalEnvs(list: TerminalEnv[]): void {
  for (const env of list) envs.set(env.env, env);
}

onFontSize(() => live.forEach((instance) => instance.applyFontSize()));

// The scheme can change under an open terminal (the system's, Telegram's, the app's own switch); the
// terminal's colours are read from the same tokens, so they are read again.
if (typeof window !== "undefined" && typeof MutationObserver !== "undefined") {
  const again = () => live.forEach((instance) => instance.applyTheme());
  new MutationObserver(again).observe(document.documentElement, { attributes: true, attributeFilter: ["data-scheme", "data-tg", "style", "class"] });
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", again);
}

// The browser checks read what a terminal holds — its text, its size, its renderer — without
// depending on how xterm.js paints (WebGL leaves no text in the document). Only a page that asked
// for it gets the hook, through a flag nobody sets by accident.
type DebugHook = {
  ids(): string[];
  text(id: string): string[];
  size(id: string): { cols: number; rows: number } | null;
  renderer(id: string): string | null;
  baseY(id: string): number;
  webglContexts(): number;
};

try {
  if (typeof window !== "undefined" && localStorage.getItem("daedalus.debug.terminals") === "1") {
    const hook: DebugHook = {
      ids: () => [...live].map((i) => i.id),
      text: (id) => {
        const buffer = instanceFor(id)?.terminal?.buffer.active;
        if (!buffer) return [];
        const out: string[] = [];
        for (let y = 0; y < buffer.length; y++) out.push(buffer.getLine(y)?.translateToString(true) ?? "");
        return out;
      },
      size: (id) => {
        const term = instanceFor(id)?.terminal;
        return term ? { cols: term.cols, rows: term.rows } : null;
      },
      renderer: (id) => instanceFor(id)?.rendererKind ?? null,
      baseY: (id) => instanceFor(id)?.terminal?.buffer.active.baseY ?? 0,
      webglContexts: () => [...live].filter((i) => i.rendererKind === "webgl").length,
    };
    (window as unknown as { __terminals: DebugHook }).__terminals = hook;
  }
} catch {
  /* storage refused: no hook */
}
