// The terminal wire format between the app and the host (and, byte for byte, the host and the
// terminal daemon, which only relays): binary WebSocket frames whose first byte is the type,
// big-endian integers after it. `docs/architecture/terminals.md` is the specification; the golden
// frames in `testdata/frames.json` are what both ends are tested against.
//
// Sequence numbers are absolute byte offsets into the terminal's output stream. They travel as u64
// and live here as plain numbers: a terminal would have to print eight petabytes before one stopped
// being exact, and a number that is not exact is refused rather than rounded.

export const FRAME = {
  OUTPUT: 0x01,
  SNAPSHOT: 0x02,
  EVENT: 0x03,
  INPUT: 0x10,
  RESIZE: 0x11,
  ACK: 0x12,
  ATTACH: 0x13,
} as const;

/** The largest INPUT payload the daemon accepts in one frame; longer input is split. */
export const MAX_INPUT_BYTES = 32 * 1024;

/** Who holds the PTY's size, as a client sees it. */
export type SizeOwner = "you" | "other" | "host";

/** Who may type: `auto` lets agent writes through after a quiet spell, `human` and `agent` hold it. */
export type KeyboardOwner = "auto" | "human" | "agent";

export type TerminalStatus = "running" | "exited" | "lost";

export type Modes = { alt_screen: boolean; mouse: boolean; bracketed_paste: boolean; app_cursor: boolean };

export type HelloEvent = {
  type: "hello";
  client_id: string;
  read_only: boolean;
  /** ACK at least every this many parsed bytes. */
  ack_bytes: number;
  /** The daemon stops sending once this many bytes are unacknowledged. */
  window_bytes: number;
  terminal: { id: string; title: string; cwd: string; status: TerminalStatus; cols: number; rows: number };
  size: { cols: number; rows: number; owner: SizeOwner };
  keyboard: { owner: KeyboardOwner; until: number | null };
  modes: Modes;
};

/**
 * A shell's mark, as the daemon reports it. Rows are absolute: counted from the terminal's start, so a
 * row keeps its number while older ones leave the history. `seq` is the output offset just after the
 * mark; the event can arrive ahead of those bytes, and the connection holds it until they are drawn.
 */
export type CommandEvent = {
  type: "command";
  /** `prompt`: a prompt starts at `abs_row`. `start`: command `n` runs, its output from `abs_row`. `end`: it ended. */
  phase: "prompt" | "start" | "end";
  n?: number;
  command?: string;
  exit_code?: number | null;
  abs_row: number;
  prompt_row?: number | null;
  /** One past the last row of the output, once the command has ended. */
  end_row?: number | null;
  duration_ms?: number | null;
  seq?: number;
  at?: string;
};

/** One command as the daemon lists it after a snapshot, so a client can place its marks again. */
export type MarkItem = {
  n: number;
  command: string;
  exit_code: number | null;
  prompt_row: number | null;
  output_row: number;
  end_row: number | null;
  running: boolean;
};

/** Right after a SNAPSHOT: every command whose rows reach the snapshot's first row, and the current prompt. */
export type MarksEvent = { type: "marks"; list: MarkItem[]; first_abs_row: number; prompt_row: number | null };

/** Every EVENT the daemon sends, by its `type`. Unknown types are passed through, not dropped. */
export type EventMessage =
  | HelloEvent
  | { type: "size"; cols: number; rows: number; owner: SizeOwner }
  | { type: "title"; title: string }
  | { type: "cwd"; cwd: string }
  | { type: "exit"; code: number | null; signal: string | null }
  | { type: "bell" }
  | { type: "notify"; title: string; body: string }
  | { type: "progress"; state: string; value: number }
  | { type: "keyboard"; owner: KeyboardOwner; until: number | null }
  | { type: "agent_typing"; actor: string; active: boolean }
  | { type: "clients"; count: number; others: unknown[] }
  | ({ type: "mode" } & Modes)
  | { type: "resync"; reason: string; first_abs_row: number }
  | MarksEvent
  | CommandEvent
  | { type: "ping"; at: number }
  | { type: "error"; code: string; message: string }
  | { type: "unknown"; raw: Record<string, unknown> };

export type ServerFrame =
  | { kind: "output"; seq: number; data: Uint8Array }
  | { kind: "snapshot"; cols: number; rows: number; seq: number; data: Uint8Array }
  | { kind: "event"; event: EventMessage };

export type AttachRequest = {
  /** The stream offset the client has parsed up to (0 when it has nothing). */
  lastSeq: number;
  /** Whether the client's terminal still holds the screen up to `lastSeq`, so a tail can follow. */
  haveState: boolean;
  readOnly: boolean;
  /** Lines of scrollback wanted in a snapshot. */
  scrollback?: number;
  /** The client's colours, which the daemon answers OSC 10/11/12 queries with. */
  theme?: { fg: string; bg: string; cursor: string };
};

export class ProtocolError extends Error {}

const KNOWN_EVENTS = new Set([
  "hello", "size", "title", "cwd", "exit", "bell", "notify", "progress", "keyboard", "agent_typing",
  "clients", "mode", "resync", "marks", "command", "ping", "error",
]);

const decoder = new TextDecoder();
const encoder = new TextEncoder();

function readSeq(view: DataView, offset: number): number {
  const value = view.getBigUint64(offset);
  if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw new ProtocolError("sequence number out of range");
  return Number(value);
}

function writeSeq(view: DataView, offset: number, seq: number): void {
  if (!Number.isSafeInteger(seq) || seq < 0) throw new ProtocolError("sequence number out of range");
  view.setBigUint64(offset, BigInt(seq));
}

/** Reads one frame from the server. Throws `ProtocolError` on a frame that cannot be what it says. */
export function decodeServerFrame(buffer: ArrayBuffer | Uint8Array): ServerFrame {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  if (bytes.length < 1) throw new ProtocolError("empty frame");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  switch (bytes[0]) {
    case FRAME.OUTPUT:
      if (bytes.length < 9) throw new ProtocolError("short OUTPUT frame");
      return { kind: "output", seq: readSeq(view, 1), data: bytes.subarray(9) };
    case FRAME.SNAPSHOT:
      if (bytes.length < 13) throw new ProtocolError("short SNAPSHOT frame");
      return { kind: "snapshot", cols: view.getUint16(1), rows: view.getUint16(3), seq: readSeq(view, 5), data: bytes.subarray(13) };
    case FRAME.EVENT: {
      let parsed: unknown;
      try {
        parsed = JSON.parse(decoder.decode(bytes.subarray(1)));
      } catch {
        throw new ProtocolError("EVENT frame is not JSON");
      }
      if (!parsed || typeof parsed !== "object" || typeof (parsed as { type?: unknown }).type !== "string") {
        throw new ProtocolError("EVENT frame has no type");
      }
      const raw = parsed as Record<string, unknown>;
      // A newer daemon may send an event this app does not know; it is surfaced, never an error.
      const event = (KNOWN_EVENTS.has(raw.type as string) ? raw : { type: "unknown", raw }) as EventMessage;
      return { kind: "event", event };
    }
    default:
      throw new ProtocolError(`unknown frame type 0x${bytes[0].toString(16).padStart(2, "0")}`);
  }
}

function frame(type: number, payload: number): { bytes: Uint8Array; view: DataView } {
  const bytes = new Uint8Array(1 + payload);
  bytes[0] = type;
  return { bytes, view: new DataView(bytes.buffer) };
}

/** INPUT frames for typed or pasted text, split so none exceeds `MAX_INPUT_BYTES`. */
export function encodeInput(data: string | Uint8Array): Uint8Array[] {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  const frames: Uint8Array[] = [];
  for (let start = 0; start < bytes.length; start += MAX_INPUT_BYTES) {
    const chunk = bytes.subarray(start, Math.min(bytes.length, start + MAX_INPUT_BYTES));
    const { bytes: out } = frame(FRAME.INPUT, chunk.length);
    out.set(chunk, 1);
    frames.push(out);
  }
  return frames;
}

/** A RESIZE: the grid, and the pixel size the daemon reports to applications that ask. */
export function encodeResize(cols: number, rows: number, pixelWidth = 0, pixelHeight = 0): Uint8Array {
  const { bytes, view } = frame(FRAME.RESIZE, 8);
  const u16 = (n: number) => Math.max(0, Math.min(0xffff, Math.round(n)));
  view.setUint16(1, u16(cols));
  view.setUint16(3, u16(rows));
  view.setUint16(5, u16(pixelWidth));
  view.setUint16(7, u16(pixelHeight));
  return bytes;
}

/** An ACK of everything parsed up to `seq` (the offset after the last byte xterm.js has written). */
export function encodeAck(seq: number): Uint8Array {
  const { bytes, view } = frame(FRAME.ACK, 8);
  writeSeq(view, 1, seq);
  return bytes;
}

export function encodeAttach(request: AttachRequest): Uint8Array {
  const json = encoder.encode(JSON.stringify(request));
  const { bytes } = frame(FRAME.ATTACH, json.length);
  bytes.set(json, 1);
  return bytes;
}
