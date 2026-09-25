// A resize must not parse the same output twice.
//
// xterm.js parses writes in slices: its loop stops after 12 ms to let the page paint, and leaves the
// chunks it already parsed at the front of its queue, cutting them off only once more than 50 have
// piled up. `Terminal.resize` first flushes the queue synchronously, so the new size applies after
// everything written before it — but that flush starts at the front of the queue, not where the loop
// stopped. The chunks already on screen are parsed a second time, and their write callbacks run a
// second time.
//
// Here that is not cosmetic. Every write callback is an acknowledgement to the terminal daemon and a
// decrement of the connection's count of writes in flight: a doubled callback acknowledges bytes the
// terminal never received twice over, sends the count below zero, and a doubled chunk prints its
// lines twice. A resize in that window is ordinary — the window is resized, or the daemon confirms a
// size, while a flood is being parsed — and it showed as lines repeated in the scrollback and a
// connection whose flow control no longer added up.
//
// The fix drops the parsed chunks from the front of the queue before the flush runs, so the flush
// parses only what is still waiting. When the flush comes from inside one of the loop's own write
// callbacks, the chunk whose callback is running has been parsed as well and is dropped with them.
// (The connection already defers a snapshot's reset out of write callbacks for the same reason; this
// covers every other resize.)
//
// None of this is public API. The fix reaches `_core._writeBuffer` and its `_writeBuffer`,
// `_callbacks`, `_bufferOffset`, `_isSyncWriting`, `_innerWrite` and `flushSync`, looked up once;
// when any is missing — a different build — it stays out of the way and says so through `installed`,
// which the tests assert on for the pinned version.

type WriteQueue = {
  _writeBuffer: unknown[];
  _callbacks: unknown[];
  _bufferOffset: number;
  _isSyncWriting: boolean;
  _innerWrite: (...args: unknown[]) => unknown;
  flushSync: () => void;
};

function writeQueue(term: unknown): WriteQueue | null {
  const queue = (term as { _core?: { _writeBuffer?: Partial<WriteQueue> } } | null)?._core?._writeBuffer;
  if (!queue) return null;
  if (!Array.isArray(queue._writeBuffer) || !Array.isArray(queue._callbacks)) return null;
  if (typeof queue._bufferOffset !== "number" || typeof queue._isSyncWriting !== "boolean") return null;
  if (typeof queue._innerWrite !== "function" || typeof queue.flushSync !== "function") return null;
  return queue as WriteQueue;
}

/**
 * Makes a resize flush only the output xterm.js has not parsed yet. `installed` is false when this
 * build lacks the internals, in which case nothing changed.
 */
export function flushOnlyUnparsed(term: unknown): { installed: boolean; dispose(): void } {
  const queue = writeQueue(term);
  if (!queue) return { installed: false, dispose: () => undefined };
  const innerWrite = queue._innerWrite;
  const flushSync = queue.flushSync;
  // How deep in the parse loop the page is: a flush from a write callback runs inside it.
  let looping = 0;
  queue._innerWrite = function (this: WriteQueue, ...args: unknown[]) {
    looping++;
    try {
      return innerWrite.apply(this, args);
    } finally {
      looping--;
    }
  };
  queue.flushSync = function (this: WriteQueue) {
    if (!this._isSyncWriting) {
      // `_bufferOffset` is the next chunk to parse; inside a callback it is the chunk whose callback
      // runs, which is parsed. After a synchronous write it is a sentinel past the end.
      const parsed = Math.min(this._bufferOffset + (looping > 0 ? 1 : 0), this._writeBuffer.length);
      if (parsed > 0) {
        this._writeBuffer.splice(0, parsed);
        this._callbacks.splice(0, parsed);
        this._bufferOffset = 0;
      }
    }
    flushSync.call(this);
  };
  return {
    installed: true,
    dispose: () => {
      queue._innerWrite = innerWrite;
      queue.flushSync = flushSync;
    },
  };
}
