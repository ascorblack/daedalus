// The terminal queries a browser terminal must not answer.
//
// A program asks its terminal questions — where is the cursor, what are you, what colour is the
// background — and waits for the reply on its input. Here the terminal daemon answers every one of
// them, always, with the values xterm.js would give, whether no browser or three are watching. If a
// browser answered too, the program would get two replies (or one from a background tab whose timers
// the browser throttled, seconds late), and a cursor report is byte for byte a Shift+F3 keypress, so
// nobody downstream could tell a stray reply from typing. So every attached xterm.js swallows the
// queries before its own handlers see them.
//
// Swallowed whole: DA1, DA2, XTVERSION, DSR and DEC DSR (xterm.js answers 5, 6, ?6 and ?996 and
// ignores the rest), DECRQM in both forms, the kitty keyboard query, DECRQSS, and the reporting forms
// of XTWINOPS. OSC 4/10/11/12 are swallowed only when every slot is a query: a sequence that also sets
// a colour is left to xterm.js, which applies it (and whose reply the daemon filters out). OSC 133 and
// 633 are swallowed too: command marks arrive from the daemon as events, already parsed.

/** The part of xterm.js's parser API this uses (identical in @xterm/xterm and @xterm/headless). */
export interface ParserLike {
  registerCsiHandler(id: { prefix?: string; intermediates?: string; final: string }, callback: (params: (number | number[])[]) => boolean): { dispose(): void };
  registerDcsHandler(id: { prefix?: string; intermediates?: string; final: string }, callback: (data: string, params: (number | number[])[]) => boolean): { dispose(): void };
  registerOscHandler(ident: number, callback: (data: string) => boolean): { dispose(): void };
}

const swallow = () => true;

/** CSI sequences that are nothing but a request for a reply. */
export const CSI_QUERIES: { prefix?: string; intermediates?: string; final: string }[] = [
  { final: "c" }, // DA1
  { prefix: ">", final: "c" }, // DA2
  { prefix: ">", final: "q" }, // XTVERSION
  { final: "n" }, // DSR 5, CPR 6
  { prefix: "?", final: "n" }, // DECXCPR ?6, colour scheme ?996
  { intermediates: "$", final: "p" }, // DECRQM, ANSI modes
  { prefix: "?", intermediates: "$", final: "p" }, // DECRQM, DEC modes
  { prefix: "?", final: "u" }, // kitty keyboard flags
];

/** XTWINOPS operations that only report (sizes, position, state, title); the rest act and pass through. */
export const WINDOW_REPORTS = new Set([11, 13, 14, 15, 16, 18, 19, 20, 21]);

/** OSC colour commands whose `?` slots are queries. */
export const OSC_COLOURS = [4, 10, 11, 12];

/** OSC commands the daemon has already turned into events. */
export const OSC_MARKS = [133, 633];

/** Whether an OSC 4/10/11/12 payload asks and sets nothing. OSC 4 comes as index;spec pairs. */
export function onlyQueries(ident: number, data: string): boolean {
  const slots = data.split(";");
  const specs = ident === 4 ? slots.filter((_, i) => i % 2 === 1) : slots;
  if (ident === 4 && slots.length % 2 !== 0) return false;
  return specs.length > 0 && specs.every((s) => s === "?");
}

/** Registers every swallowing handler on a terminal's parser; dispose the result to remove them. */
export function swallowQueries(parser: ParserLike): { dispose(): void } {
  const handles = [
    ...CSI_QUERIES.map((id) => parser.registerCsiHandler(id, swallow)),
    parser.registerCsiHandler({ final: "t" }, (params) => {
      const op = params[0];
      return typeof op === "number" && WINDOW_REPORTS.has(op);
    }),
    parser.registerDcsHandler({ intermediates: "$", final: "q" }, swallow), // DECRQSS
    ...OSC_COLOURS.map((ident) => parser.registerOscHandler(ident, (data) => onlyQueries(ident, data))),
    ...OSC_MARKS.map((ident) => parser.registerOscHandler(ident, swallow)),
  ];
  return { dispose: () => handles.forEach((h) => h.dispose()) };
}
