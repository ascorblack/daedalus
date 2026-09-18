import { Component, createContext, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { ReactElement, ReactNode } from "react";
import { api, AsrStatus, ModelFallback, ProviderUsage, Schedule, SessionCheckpoints, SlashCommand, MessageView, Question, SessionDetail, Compacting } from "../api";
import { Dot, Status, copyText, fmtInt, statusWord, timeAgo } from "../components";
import { OverflowMenu, confirmDialog, Overlay } from "../dialogs";
import { absDate, clock, commandPreview, duration, plainPreview, shortDateTime } from "../format";
import { EVIDENCE_EVENT, EvidenceRequest, codeBlock, renderCached, renderMarkdown } from "../md";
import { confirmAsync, enterSends, errorText, fmtBytes, haptic } from "../ui";
import { Icon, IconName } from "../icons";
import { AuthImg, FilePreview, PreviewSource, canPreview, downloadHref, fileGlyph, previewKind, sessionBase } from "../preview";
import { Activity, LiveStore, SummaryItem, ToolItem, Turn, applyLive, buildTurns, createLiveStore, isOlderPage, liveAfter, liveBase, prepend, reconcile } from "../turns";
import { MoveSessionSheet } from "../projects";
import { panelShortcut } from "../panel";
import { Panel, usePanel, usePanelWidth } from "../panelhost";
import { SessionDetails } from "../details";
import { JobsTab } from "../jobs";
import { navigate, pathFor, useRoute } from "../router";
import { useMedia } from "../shell";
import { Windowed } from "../virtual";
import { DICT, plural, t } from "../i18n";

/**
 * Markdown parsed once per text. `cacheKey` names a message that will never change again, so its
 * rendering survives the component: a turn scrolled out of the window and back in is not re-parsed.
 */
const Md = memo(function Md({ text, className, cacheKey }: { text: string; className?: string; cacheKey?: string }) {
  const html = useMemo(() => (cacheKey ? renderCached(cacheKey, text) : renderMarkdown(text)), [text, cacheKey]);
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
});

/** How much of the end of the conversation a run's event is answered with. */
const TAIL_AFTER_EVENT = 24;
/** Events come in bursts; one read answers the burst. */
const EVENT_COALESCE_MS = 150;
/** How many older messages a page holds when the reader scrolls past the top. */
const OLDER_PAGE = 200;

// ── screen ────────────────────────────────────────────────────────────────────────────────

export type SessionScreenProps = {
  id: string;
  onBack: () => void;
  onOpen?: (id: string) => void;
  toast: (t: string) => void;
  /** Two sessions side by side (wide screens): which half this one is. */
  pane?: "left" | "right";
  /** Open another session beside this one; absent when the screen cannot split. */
  onSplit?: () => void;
};

export function SessionScreen({ id, onBack, onOpen, toast, pane, onSplit }: SessionScreenProps) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  // The streaming turn's state is not React state: a token must repaint the turn it belongs to,
  // not the screen. The components that show it subscribe; everything else never hears about it.
  const liveRef = useRef<LiveStore | null>(null);
  if (!liveRef.current) liveRef.current = createLiveStore();
  const live = liveRef.current;
  const [draft, setDraft] = useState("");
  const [moving, setMoving] = useState(false);
  const [pending, setPending] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  // The right panel: the route carries the open tab and the previewed file for the pane the URL
  // names; the second pane of a dual view keeps its panel to itself. Phones host it in a sheet.
  const route = useRoute();
  const phone = !useMedia("(min-width: 1024px)");
  const panel = usePanel(id, sessionBase(id), { route: pane === "right" ? null : route.query, beside: pane === "left" ? route.with : null, pane });
  const [panelPct, dragPanel] = usePanelWidth();
  const body = useRef<HTMLDivElement>(null);
  const [detailsFocus, setDetailsFocus] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState<string | null>(null);
  const [modes, setModes] = useState<string[]>([]);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [commandResult, setCommandResult] = useState<{ line: string; text: string } | null>(null);
  const [picker, setPicker] = useState<null | { presets: Record<string, { provider: string; model: string; label: string }>; global: string }>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const [custom, setCustom] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const stick = useRef(true);
  const userScrolling = useRef(false);
  const [atBottom, setAtBottom] = useState(true);
  const [preview, setPreview] = useState<PreviewSource | null>(null);
  const [receipt, setReceipt] = useState<string | null>(null);
  const [dragging, setDragging] = useState(0);
  const [providerUsage, setProviderUsage] = useState<ProviderUsage | null>(null);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [asr, setAsr] = useState<AsrStatus | null>(null);
  const [snapshots, setSnapshots] = useState<SessionCheckpoints | null>(null);

  async function loopAction(a: string) {
    try {
      await api.post(`/api/sessions/${id}/loop/action`, { action: a });
      toast(t("session.loop.action", { action: a }));
      load(true);
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function setMode(mode: string) {
    try {
      await api.post(`/api/sessions/${id}/mode`, { mode: mode === "default" ? null : mode });
      toast(t("session.mode.toast", { mode }));
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  // Which turns can still put the files back. Read once with the session and again after a revert
  // (which takes a snapshot of its own); a session that never snapshots answers with an empty list,
  // and then nothing about the undo changes.
  const readSnapshots = useCallback(async () => {
    try {
      setSnapshots(await api.get<SessionCheckpoints>(`/api/sessions/${id}/checkpoints`));
    } catch {
      setSnapshots(null);
    }
  }, [id]);

  const turnAction = useCallback(
    async (kind: "revert" | "fork", seq: number) => {
      try {
        if (kind === "revert") {
          // With snapshots off — which is a project's default — there is nothing to put the files
          // back from, and the operator should read that before clicking rather than in the toast after.
          const noSnapshots = detail?.project?.settings.snapshots === false;
          const body = t(noSnapshots ? "session.revert.body.nosnapshots" : "session.revert.body");
          if (!(await confirmAsync(t("session.revert.title"), { body, action: t("session.revert.action") }))) return;
          const r = await api.post<{ dropped: number; workspace_restored: boolean; untouched: string[] }>(`/api/sessions/${id}/revert`, { seq });
          const ws = r.workspace_restored
            ? r.untouched.length
              ? t("session.reverted.ws.nested", { n: r.untouched.length })
              : t("session.reverted.ws")
            : t("session.reverted.ws.none");
          toast(plural("session.reverted", r.dropped, { ws }));
          void readSnapshots();
        } else {
          const r = await api.post<{ id: string; title: string; messages: number }>(`/api/sessions/${id}/fork`, { seq });
          toast(t("session.fork.done", { title: r.title, n: r.messages }));
        }
        load();
      } catch (e) {
        toast(errorText(e));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [id, toast, readSnapshots, detail?.project?.settings.snapshots],
  );

  const [offline, setOffline] = useState(false);
  // What the screen holds, kept where code can read it without waiting for a render: a merge has to
  // decide whether the history has a hole in it before React gets to the next commit.
  const msgs = useRef<MessageView[]>([]);
  // Reads overlap — an event read and the safety-net poll — and they come back in whatever order the
  // network gives them. Only the newest one is allowed to land, so a slow older answer cannot put an
  // older status back on the screen.
  const readSeq = useRef(0);
  const load = useCallback(
    async (quiet = false) => {
      const mine = ++readSeq.current;
      try {
        const next = await api.get<SessionDetail>(`/api/sessions/${id}`);
        if (mine !== readSeq.current) return;
        msgs.current = next.messages;
        setDetail(next);
        setOffline(false);
      } catch (e) {
        // Timers and the event stream retry by themselves: one banner in the header, not a toast every few seconds.
        setOffline(true);
        if (!quiet) toast(errorText(e));
      }
    },
    [id, toast],
  );

  // A different session is a different conversation: nothing the last one held may be folded into it,
  // and an answer still on its way belongs to the one that was left.
  useEffect(() => {
    msgs.current = [];
    readSeq.current++;
    return () => forgetDisclosed(id);
  }, [id]);

  // A run changes the end of the conversation and nothing else, so a run's events are answered by
  // reading the end of it. `TAIL_AFTER_EVENT` messages is more than a round adds; when it turns out
  // not to reach what the screen already holds, `reconcile` says so and the whole thing is re-read.
  const refresh = useCallback(
    async (kind: "tail" | "state") => {
      const mine = ++readSeq.current;
      try {
        const next = await api.get<SessionDetail>(`/api/sessions/${id}?tail=${kind === "tail" ? TAIL_AFTER_EVENT : 1}`);
        if (mine !== readSeq.current) return;
        setOffline(false);
        if (kind === "state") {
          setDetail((prev) => (prev ? { ...next, messages: prev.messages } : next));
          return;
        }
        const merged = reconcile(msgs.current, next.messages);
        if (merged.gap) {
          await load(true);
          return;
        }
        msgs.current = merged.messages;
        setDetail((prev) => (prev ? { ...next, messages: merged.messages } : next));
      } catch (e) {
        setOffline(true);
        void e;
      }
    },
    [id, load],
  );

  // Events arrive in bursts — a message ends, the state changes, the run settles — and one read
  // answers all of them.
  const queued = useRef<{ timer: number; kind: "tail" | "state" } | null>(null);
  const refreshSoon = useCallback(
    (kind: "tail" | "state") => {
      const pendingRead = queued.current;
      if (pendingRead) {
        if (kind === "tail") pendingRead.kind = "tail";
        return;
      }
      const timer = window.setTimeout(() => {
        const wanted = queued.current?.kind ?? kind;
        queued.current = null;
        void refresh(wanted);
      }, EVENT_COALESCE_MS);
      queued.current = { timer, kind };
    },
    [refresh],
  );
  useEffect(() => () => {
    if (queued.current) window.clearTimeout(queued.current.timer);
    queued.current = null;
  }, [id]);

  // Older messages, when the reader scrolls past the top of what was loaded. The API may not take
  // the cursor yet, in which case it answers with the tail it always answers with — that is not an
  // older page, and asking again would only repeat it, so the screen stops asking.
  const [older, setOlder] = useState<"more" | "loading" | "done">("more");
  /** How far the reader was from the end when an older page went in, so they stay where they were. */
  const keepFromEnd = useRef<number | null>(null);
  /** One page at a time: the rendered range asks as often as the reader scrolls. */
  const olderBusy = useRef(false);
  const loadOlder = useCallback(async () => {
    const oldest = detail?.messages[0]?.seq;
    if (oldest == null || oldest <= 0) {
      setOlder("done");
      return;
    }
    if (olderBusy.current) return;
    olderBusy.current = true;
    setOlder("loading");
    try {
      const page = await api.get<SessionDetail>(`/api/sessions/${id}?before=${oldest}&tail=${OLDER_PAGE}`);
      if (!isOlderPage(page.messages, oldest)) {
        setOlder("done");
        return;
      }
      const el = scroller.current;
      keepFromEnd.current = el ? el.scrollHeight - el.scrollTop : null;
      msgs.current = prepend(msgs.current, page.messages);
      setDetail((prev) => (prev ? { ...prev, messages: msgs.current } : prev));
      setOlder(page.messages.length < OLDER_PAGE ? "done" : "more");
    } catch {
      setOlder("more");
    } finally {
      olderBusy.current = false;
    }
  }, [id, detail?.messages]);

  useEffect(() => {
    load();
    api.get<Record<string, unknown>>("/api/modes").then((m) => setModes(Object.keys(m))).catch(() => setModes([]));
    api.get<SlashCommand[]>("/api/commands").then(setCommands).catch(() => setCommands([]));
    api.get<AsrStatus>("/api/asr").then(setAsr).catch(() => setAsr(null));
    readSnapshots();
  }, [load, readSnapshots]);

  // A recording from the microphone becomes text in the composer (or goes straight out with autosend).
  const [transcribing, setTranscribing] = useState(false);
  async function onRecording(blob: Blob, seconds: number) {
    if (asr && seconds > asr.max_seconds) {
      toast(t("session.transcribe.long", { n: seconds, max: asr.max_seconds }));
      return;
    }
    setTranscribing(true);
    try {
      const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
      const form = new FormData();
      form.append("audio", blob, `recording.${ext}`);
      const res = await fetch(`/api/sessions/${id}/transcribe`, { method: "POST", headers: api.authHeaders(), body: form });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `transcription failed (${res.status})`);
      const r = (await res.json()) as { transcript: string; text: string; autosend: boolean };
      if (r.autosend && !draft.trim()) {
        await api.post(`/api/sessions/${id}/messages`, { text: r.text });
        toast(t("session.transcribe.sent", { text: r.transcript.slice(0, 80) }));
        stick.current = true;
        load();
      } else {
        setDraft((d) => (d.trim() ? `${d.trimEnd()}\n\n${r.text}` : r.text));
        textarea.current?.focus();
        haptic("success");
      }
    } catch (e) {
      toast(errorText(e));
    } finally {
      setTranscribing(false);
    }
  }

  // The scheduled tasks that belong to this session: created from it, aimed at it, or running in it now.
  const loadSchedules = useCallback(() => {
    api
      .get<Schedule[]>("/api/schedules")
      .then((all) => setSchedules(all.filter((x) => x.target_session === id || x.created_by_session === id || x.active_session_id === id)))
      .catch(() => undefined);
  }, [id]);
  useEffect(() => {
    loadSchedules();
    const t = setInterval(loadSchedules, 60000);
    return () => clearInterval(t);
  }, [loadSchedules]);

  // Usage of the provider the session talks to: a subscription's windows, or the day's metered spend.
  const provider = detail?.provider ?? "";
  useEffect(() => {
    if (!provider) return;
    let gone = false;
    const pull = () =>
      api
        .get<ProviderUsage>(`/api/usage/provider/${encodeURIComponent(provider)}`)
        .then((u) => !gone && setProviderUsage(u))
        .catch(() => undefined);
    pull();
    const t = setInterval(pull, 60000);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [provider, detail?.usage.c]);

  async function scheduleAction(sc: Schedule, action: "run" | "delete") {
    try {
      if (action === "delete") {
        if (!(await confirmAsync(t("session.sched.delete.title", { name: sc.name })))) return;
        await api.delete(`/api/schedules/${sc.id}`);
        toast(t("session.sched.deleted"));
      } else {
        const r = await api.post<{ session_id: string }>(`/api/schedules/${sc.id}/run`);
        toast(t(r.session_id === id ? "session.sched.runninghere" : "session.sched.runningown"));
        if (r.session_id !== id) onOpen?.(r.session_id);
      }
      loadSchedules();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const status = (detail?.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";
  // The run is over, its answer is on the screen, and behind it the snapshot of the turn and the
  // handover to the other fronts are still being written. Not a run — nothing is being generated,
  // and the composer is open — but not nothing either: it is exactly the window in which an undo
  // waits for the files and a restart is refused, and drawing it as fully idle is what made both
  // of those read as the app misbehaving. The host says so twice, once to raise it and once to
  // take it away; between the two this is what there is to show.
  const saving = !busy && !!detail?.housekeeping;
  const compacting = detail?.compacting ?? null;

  // The event stream carries every change while a run is active; this is the safety net, not the
  // feed, and it asks what the session is doing — not for the conversation over again.
  useEffect(() => {
    if (!busy) {
      live.reset();
      return;
    }
    const t = setInterval(() => refresh("state"), 20000);
    return () => clearInterval(t);
  }, [busy, live, refresh]);

  // Live events while the screen is open.
  useEffect(() => {
    const url = api.streamUrl(id);
    const headers = api.authHeaders();
    let stop = false;
    const controller = new AbortController();
    (async () => {
      let backoff = 1000;
      while (!stop) {
        try {
          const res = await fetch(url, { headers, signal: controller.signal });
          if (!res.body) return;
          const reader = res.body.getReader();
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
        // The stream ended (server restart, proxy timeout): re-read the transcript and reconnect.
        // Whatever the last stream was in the middle of is over as far as this screen knows, and a
        // half-written turn kept across the gap would put the cursor back under an answer that
        // finished while the connection was down. The re-read brings back everything that is real.
        live.reset();
        load(true);
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, 15000);
      }
    })();
    function handle(event: string, p: Record<string, any>) {
      live.update((s) => liveAfter(s, event, p));
      if (event === "message_start") {
        // The queue the last run left behind starts the next one without anybody pressing send:
        // the chip says so as the first token arrives, not at the next read.
        setDetail((prev) => (prev && prev.status === "idle" ? { ...prev, status: "running" } : prev));
      } else if (event === "message_stop") {
        // The streamed copy is dropped only once the written one is on the screen, so the answer
        // never blinks out and back in. The cursor does not wait for that read — `liveAfter` has
        // already ended the turn — and so the read being slow costs nothing anybody can see.
        void refresh("tail").then(() => live.update((s) => ({ ...s, text: "", thinking: "" })));
      } else if (event === "model_changed") {
        // The header must not wait for the next read to stop naming a model that is not answering:
        // a fallback is at its most confusing in the seconds right after it happens.
        setDetail((prev) =>
          prev
            ? {
                ...prev,
                effective_model: String(p.to ?? ""),
                fallback: p.fallback ? { from: String(p.configured ?? p.from ?? ""), to: String(p.to ?? ""), reason: String(p.reason ?? "") } : null,
              }
            : prev,
        );
      } else if (event === "run_settled") {
        // The run is over as the host knows it. The chip flips on this event, not on the read it
        // triggers: the read says the same thing a round trip later.
        setDetail((prev) => (prev ? { ...prev, status: p.status === "awaiting" ? "waiting" : "idle", housekeeping: !!p.housekeeping } : prev));
        refreshSoon("tail");
      } else if (event === "compaction_completed") refreshSoon("tail");
      else if (event === "state_changed" || event === "tool_call_pending") refreshSoon("state");
    }
    return () => {
      stop = true;
      controller.abort();
    };
  }, [id, live, load, refresh, refreshSoon]);

  // The settled conversation, built once per message that lands. A turn whose messages did not
  // change comes back as the same object, so `memo` on the view holds and a streamed token
  // re-renders the turn it belongs to instead of all of them.
  const built = useRef<Turn[]>([]);
  const turns = useMemo(() => {
    built.current = buildTurns(detail?.messages ?? [], built.current);
    return built.current;
  }, [detail?.messages]);
  const tail = busy ? liveBase(turns) : null;
  const settled = useMemo(() => (tail ? turns.slice(0, -1) : turns), [turns, tail]);
  const turnKeys = useMemo(() => settled.map((t) => t.key), [settled]);

  // A page of older messages is put in front of everything the reader is looking at, so the screen
  // moves down by its height unless the distance to the end is restored.
  useLayoutEffect(() => {
    const el = scroller.current;
    const keep = keepFromEnd.current;
    if (!el || keep == null) return;
    keepFromEnd.current = null;
    el.scrollTop = el.scrollHeight - keep;
  }, [detail?.messages]);

  // A history short enough to have arrived whole has no page before it: it is never asked for one,
  // and the line that says a page is on its way is not in its way.
  const pageable = older !== "done" && (detail?.messages.length ?? 0) >= OLDER_PAGE;

  // The windowed list says when the oldest turn it holds is on screen. Held in a ref so the list
  // does not re-register its scroll listener every time a message lands.
  const wantOlder = useRef<() => void>(() => undefined);
  useEffect(() => {
    wantOlder.current = () => {
      if (older === "more" && pageable) void loadOlder();
    };
  }, [older, pageable, loadOlder]);
  const onTopOfList = useCallback(() => wantOlder.current(), []);

  // Follow the newest content only while the reader is at the bottom and not scrolling by hand.
  const pinBottom = useCallback(() => {
    const el = scroller.current;
    if (el && stick.current && !userScrolling.current) el.scrollTop = el.scrollHeight;
  }, []);
  useEffect(pinBottom, [turns, pinBottom]);

  // Images, code blocks and the streaming turn take their height after the list has been laid out:
  // while the reader is at the end, the end is where they stay.
  useEffect(() => {
    const el = scroller.current;
    const list = el?.querySelector(".timeline");
    if (!list || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => pinBottom());
    ro.observe(list);
    return () => ro.disconnect();
  }, [pinBottom]);

  // The first paint of a long history lands mid-way once images and code blocks take their height:
  // pin the bottom again after layout settles.
  const firstLoad = useRef(true);
  useEffect(() => {
    if (!detail || !firstLoad.current) return;
    firstLoad.current = false;
    const el = scroller.current;
    if (!el) return;
    const pin = () => {
      if (stick.current) el.scrollTop = el.scrollHeight;
    };
    const raf = requestAnimationFrame(pin);
    const timers = [120, 400, 1000].map((ms) => window.setTimeout(pin, ms));
    return () => {
      cancelAnimationFrame(raf);
      timers.forEach(clearTimeout);
    };
  }, [detail]);

  // A rotation, a split pane opening, a desktop window resized: the list is a different width and
  // every turn a different height. While the reader was at the end, the end is where they stay —
  // the heights are re-measured over the next few frames, so the pin is repeated over them.
  useEffect(() => {
    const vv = window.visualViewport;
    const on = () => {
      const el = scroller.current;
      if (!el || !stick.current) return;
      const pin = () => {
        if (stick.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
      };
      const timers = [0, 120, 400].map((ms) => window.setTimeout(pin, ms));
      window.setTimeout(() => timers.forEach(clearTimeout), 600);
    };
    window.addEventListener("orientationchange", on);
    window.addEventListener("resize", on);
    vv?.addEventListener("resize", on);
    return () => {
      window.removeEventListener("orientationchange", on);
      window.removeEventListener("resize", on);
      vv?.removeEventListener("resize", on);
    };
  }, []);

  function jumpToBottom() {
    const el = scroller.current;
    if (!el) return;
    stick.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setAtBottom(true);
  }

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let timer: number | undefined;
    const startHand = () => {
      userScrolling.current = true;
      window.clearTimeout(timer);
    };
    const endHand = () => {
      timer = window.setTimeout(() => {
        userScrolling.current = false;
      }, 400);
    };
    el.addEventListener("touchstart", startHand, { passive: true });
    el.addEventListener("touchend", endHand, { passive: true });
    el.addEventListener("wheel", startHand, { passive: true });
    el.addEventListener("wheel", endHand, { passive: true });
    return () => {
      el.removeEventListener("touchstart", startHand);
      el.removeEventListener("touchend", endHand);
      el.removeEventListener("wheel", startHand);
      el.removeEventListener("wheel", endHand);
    };
  }, []);

  function onScroll() {
    const el = scroller.current;
    if (!el) return;
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    stick.current = gap < 48;
    setAtBottom(gap < 160);
    // Near the top of what was loaded: ask for the page before it, if the API has one. A history
    // short enough to have arrived whole has no page before it and is never asked for one.
    if (el.scrollTop < 400 && older === "more" && pageable) void loadOlder();
  }

  // Files from the clipboard (a screenshot, a copied file) and files dropped on the chat join the draft.
  function addFiles(files: Iterable<File>) {
    const named = Array.from(files).map((f) => {
      // A pasted screenshot arrives as "image.png" every time: give each one a name of its own.
      if (!/^(image|blob|file)(\.[a-z0-9]+)?$/i.test(f.name)) return f;
      const ext = f.name.includes(".") ? f.name.slice(f.name.lastIndexOf(".")) : f.type.startsWith("image/") ? `.${f.type.slice(6).replace("jpeg", "jpg")}` : "";
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
      return new File([f], `${f.type.startsWith("image/") ? "screenshot" : "pasted"}-${stamp}${ext}`, { type: f.type, lastModified: f.lastModified });
    });
    if (named.length) setPending((p) => [...p, ...named]);
  }
  function onPaste(e: React.ClipboardEvent) {
    const items = Array.from(e.clipboardData?.items ?? []);
    const files = items.filter((it) => it.kind === "file").map((it) => it.getAsFile()).filter((f): f is File => !!f);
    if (!files.length) return;
    // Text pasted alongside (rich-text editors add an HTML rendering of the image) is not wanted.
    e.preventDefault();
    addFiles(files);
    haptic("light");
  }
  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(0);
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  }

  const paletteQuery = draft.startsWith("/") && !draft.includes("\n") ? draft.slice(1).split(" ")[0].toLowerCase() : null;
  const paletteItems = paletteQuery === null || draft.includes(" ") ? [] : commands.filter((c) => c.name.startsWith(paletteQuery));

  async function runCommand(line: string) {
    const name = line.slice(1).split(" ")[0].toLowerCase();
    const spec = commands.find((c) => c.name === name);
    if (spec?.confirm && !(await confirmAsync(t("session.command.confirm", { name })))) return;
    setDraft("");
    if (textarea.current) textarea.current.style.height = "auto";
    try {
      const r = await api.post<{ text: string }>(`/api/sessions/${id}/command`, { line });
      const short = r.text.length < 140 && !r.text.includes("\n");
      if (short) toast(r.text);
      else setCommandResult({ line, text: r.text });
      load();
    } catch (e) {
      setDraft(line);
      toast(errorText(e));
    }
  }

  function pickCommand(c: SlashCommand) {
    if (c.args) {
      setDraft(`/${c.name} `);
      textarea.current?.focus();
    } else {
      runCommand(`/${c.name}`);
    }
  }

  async function send() {
    const text = draft.trim();
    const files = pending;
    if (sending || (!text && files.length === 0)) return;
    if (text.startsWith("/") && files.length === 0 && commands.some((c) => c.name === text.slice(1).split(" ")[0].toLowerCase())) {
      await runCommand(text);
      return;
    }
    setSending(true);
    setDraft("");
    setPending([]);
    if (fileInput.current) fileInput.current.value = "";
    if (textarea.current) textarea.current.style.height = "auto";
    try {
      if (files.length > 0) {
        const form = new FormData();
        form.append("text", text);
        for (const f of files) form.append("files", f, f.name);
        const res = await fetch(`/api/sessions/${id}/upload`, { method: "POST", headers: api.authHeaders(), body: form });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      } else {
        await api.post(`/api/sessions/${id}/messages`, { text });
      }
      stick.current = true;
      haptic("light");
      load();
    } catch (e) {
      setDraft(text);
      setPending(files);
      toast(errorText(e));
    } finally {
      setSending(false);
    }
  }

  async function stop() {
    if (!(await confirmDialog({ title: t("session.stop.title"), body: t("session.stop.body"), action: t("session.stop.action"), danger: true }))) return;
    try {
      await api.post(`/api/sessions/${id}/stop`);
      haptic("medium");
      toast(t("session.stopping"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function rename(title: string) {
    setEditingTitle(null);
    if (!title.trim() || title.trim() === detail?.title) return;
    try {
      await api.patch(`/api/sessions/${id}`, { title: title.trim() });
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function compact() {
    if (busy) {
      toast(t("session.stopfirst"));
      return;
    }
    if (!(await confirmAsync(t("session.compact.title"), { body: t("session.compact.body"), action: t("session.compact.action"), danger: false }))) return;
    toast(t("session.compacting"));
    try {
      await api.post(`/api/sessions/${id}/compact`, { instructions: "" });
      await load();
      toast(t("session.compacted"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function clearHistory() {
    if (busy) {
      toast(t("session.stopfirst"));
      return;
    }
    if (!(await confirmAsync(t("session.clear.title"), { body: t("session.clear.body"), action: t("session.clear.action") }))) return;
    try {
      const r = await api.post<{ dropped: number }>(`/api/sessions/${id}/clear`);
      toast(plural("session.cleared", r.dropped));
      await load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function openPicker() {
    try {
      const st = await api.get<any>("/api/settings");
      const def = st.presets?.[st.model?.preset];
      setPicker({ presets: st.presets ?? {}, global: def ? def.label || `${def.provider}/${def.model}` : String(st.model?.preset ?? t("settings.heartbeat.default")) });
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function chooseModel(body: Record<string, unknown>) {
    setPicker(null);
    try {
      const r = await api.post<{ model: string }>(`/api/sessions/${id}/model`, body);
      toast(t("session.model.picked", { model: r.model }));
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function remove() {
    if (!(await confirmAsync(t("session.delete.title"), { body: t("session.delete.body"), action: t("session.delete.action") }))) return;
    try {
      await api.delete(`/api/sessions/${id}`);
      onBack();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function exportMarkdown() {
    try {
      const res = await fetch(`/api/sessions/${id}/export/download`, { headers: api.authHeaders() });
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `session-${id}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      toast(t("session.export.failed", { error: String(e) }));
    }
  }

  const subRunning = (detail?.subagents ?? []).filter((x) => x.running).length;

  // A workspace file opens in the panel's Preview tab; a file waiting in the composer, which is
  // nowhere the panel can fetch it from, still opens in the dialog.
  const openPreview = useCallback(
    (src: PreviewSource) => {
      if ("file" in src) setPreview(src);
      else panel.openFile(src);
    },
    [panel.openFile],
  );

  // Ctrl/⌘ . toggles the panel, with Shift it covers the chat. The pane the URL names owns the keys.
  useEffect(() => {
    if (phone || pane === "right") return;
    const onKey = (e: KeyboardEvent) => {
      const which = panelShortcut(e);
      if (!which) return;
      e.preventDefault();
      if (which === "toggle") panel.toggle();
      else panel.expand();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [phone, pane, panel.toggle, panel.expand]);

  const sessionCtx = useMemo(
    () => ({
      id,
      workspace: detail?.workspace ?? "",
      preview: openPreview,
      // An empty list is a session that never snapshots (a project with them off, a workspace over
      // the size cap): there is nothing retention took away and the undo behaves as it always did.
      revertable: snapshots && snapshots.total > 0 ? new Set(snapshots.checkpoints.filter((c) => c.kind === "before" && c.seq != null).map((c) => c.seq as number)) : null,
    }),
    [id, detail?.workspace, snapshots, openPreview],
  );

  // What the answer cited, clicked: a file opens at the lines it named, a Verify receipt opens as a
  // receipt, and any other run id is a background job — its log is a file in the workspace.
  useEffect(() => {
    const workspace = detail?.workspace ?? "";
    const on = (e: Event) => {
      const cited = (e as CustomEvent<EvidenceRequest>).detail;
      if (!cited) return;
      if (cited.kind === "run") {
        if (/^v\d+$/.test(cited.id)) setReceipt(cited.id);
        else openPreview({ base: sessionBase(id), path: `.jobs/${cited.id}.log` });
        return;
      }
      const rel = workspaceRelative(cited.path, workspace);
      if (rel) openPreview({ base: sessionBase(id), path: rel, lines: cited.lines });
      else toast(t("session.outside", { path: cited.path }));
    };
    document.addEventListener(EVIDENCE_EVENT, on);
    return () => document.removeEventListener(EVIDENCE_EVENT, on);
  }, [id, detail?.workspace, toast, openPreview]);
  return (
    <div className={`chat ${pane ? `pane pane-${pane}` : ""}`} onDragEnter={(e) => { if (e.dataTransfer?.types.includes("Files")) setDragging((d) => d + 1); }} onDragLeave={() => setDragging((d) => Math.max(0, d - 1))} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      {dragging > 0 && <div className="dropzone"><Icon name="attach" size={28} /> {t("session.drop")}</div>}
      <div className={`chat-head ${busy || saving || compacting ? "live" : ""}`}>
        {(pane || phone) && (
          <button className="iconbtn" onClick={onBack} aria-label={t(pane === "right" ? "session.closepane" : "shell.back")} title={t(pane === "right" ? "session.closepane" : "shell.back")}>
            <Icon name={pane === "right" ? "close" : "back"} />
          </button>
        )}
        <div className="grow chat-identity" style={{ minWidth: 0 }}>
          {editingTitle !== null ? (
            <input
              className="title-edit"
              autoFocus
              value={editingTitle}
              onChange={(e) => setEditingTitle(e.target.value)}
              onBlur={() => rename(editingTitle)}
              onKeyDown={(e) => {
                if (e.key === "Enter") rename(editingTitle);
                if (e.key === "Escape") setEditingTitle(null);
              }}
            />
          ) : (
            <OverflowMenu
              label={t("session.title.menu")}
              className="chat-title"
              trigger={<><span className="truncate">{detail?.title ?? "…"}</span><Icon name="chevron" size={14} /></>}
              items={[
                { label: t("session.rename"), icon: "pen", onSelect: () => setEditingTitle(detail?.title ?? "") },
                { label: t("session.project.move"), icon: "folder", onSelect: () => setMoving(true) },
                ...(onSplit ? [{ label: t("session.split"), icon: "split" as IconName, onSelect: onSplit }] : []),
                { label: t("session.export"), icon: "download", onSelect: exportMarkdown },
                "-",
                { label: t("session.compact"), icon: "compact", onSelect: compact, disabled: busy },
                { label: t("session.clear"), icon: "trash", onSelect: clearHistory, disabled: busy, danger: true },
                { label: t("session.delete"), icon: "trash", onSelect: remove, danger: true },
              ]}
            />
          )}
          {detail?.subagent_of && (
            <button className="leader-link" onClick={() => onOpen?.(detail.subagent_of!)} title={t("session.leader")}>
              ↳ {detail.leader_title ?? t("session.leader.word")}
            </button>
          )}
        </div>
        {compacting ? (
          <CompactionBar c={compacting} />
        ) : busy || saving ? (
          <LiveBar status={status} saving={saving} base={tail} live={live} workspace={detail?.workspace} onJump={jumpToBottom} />
        ) : status === "failed" ? (
          <span className="head-status failed" role="status"><Dot status={status} /><b>{statusWord(status)}</b></span>
        ) : null}
        {offline && <span className="head-status offline">{t("session.reconnecting")}</span>}
        {/* The model, until the composer holds it: a muted label, amber while another model stands in
            for the configured one. It goes back to the plain name by itself. */}
        <button className={`head-model ${detail?.fallback ? "attn" : ""}`} onClick={openPicker} title={detail?.fallback ? t("session.model.fallback.turn", { to: detail.fallback.to, from: detail.fallback.from }) : t("session.model.for")}>
          {detail?.fallback ? t("session.model.fallback", { to: shortModel(detail.fallback.to, 14), from: shortModel(detail.fallback.from, 14) }) : shortModel(detail?.model, 22)}
        </button>
        <div className="head-actions">
          <button className={`iconbtn ${panel.state.tab ? "on" : ""}`} onClick={panel.toggle} aria-label={t("panel.toggle")} title={t("panel.toggle.title")} aria-pressed={!!panel.state.tab}>
            <Icon name="panel" />
          </button>
          <OverflowMenu
            label={t("session.actions")}
            items={[
              { label: t("panel.tab.details"), icon: "settings", onSelect: () => { setDetailsFocus("session"); panel.open("details"); } },
              { label: t("session.files"), icon: "folder", onSelect: () => panel.open("files") },
              { label: t("panel.tab.jobs"), icon: "terminal", onSelect: () => panel.open("jobs") },
              { label: t("session.mcp"), icon: "plug", onSelect: () => { setDetailsFocus("mcp"); panel.open("details"); } },
              ...(onSplit ? [{ label: t("session.split"), icon: "split" as IconName, onSelect: onSplit }] : []),
              "-",
              { label: t("session.export"), icon: "download", onSelect: exportMarkdown },
            ]}
          />
        </div>
        {(busy || saving || compacting) && <HeadProgress status={status} compacting={compacting} />}
      </div>

      <div ref={body} className={`chat-body ${panel.state.tab && !phone ? "with-panel" : ""} ${panel.state.expanded && !phone ? "panel-full" : ""}`} style={{ ["--panel-w" as string]: `${panelPct}%` }}>
        <div className="chat-main">
          <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
            <div className="timeline">
              {pageable && <div className="sub older-note">{older === "loading" ? t("session.older") : ""}</div>}
              {snapshots?.pruned && (
                <div className="sub older-note">
                  {snapshots.pruned_before ? t("session.pruned.before", { date: absDate(snapshots.pruned_before) }) : t("session.pruned")}
                </div>
              )}
              <SessionContext.Provider value={sessionCtx}>
                <Windowed
                  keys={turnKeys}
                  scroller={scroller}
                  pinned={() => stick.current}
                  dragging={() => userScrolling.current}
                  onTop={onTopOfList}
                  render={(i) => (
                    <Safe>
                      <TurnView turn={settled[i]} live={false} onTurnAction={turnAction} />
                    </Safe>
                  )}
                />
                {busy && <LiveTurn base={tail} live={live} onTurnAction={turnAction} onRender={pinBottom} />}
              </SessionContext.Provider>
              {detail?.pending && <QuestionCard key={detail.pending.questions.map((q) => q.question).join("|")} sessionId={id} questions={detail.pending.questions} onDone={() => load()} toast={toast} />}
            </div>
          </div>
          {!atBottom && (
            <button className="jump-down" onClick={jumpToBottom} aria-label={t("session.jump.label")} title={t("session.jump")}>
              <Icon name="down" size={18} />
            </button>
          )}
          <div className="composer">
            {pending.length > 0 && (
              <div className="attachments" aria-label={t("session.attachments")}>
                {pending.map((f, i) => (
                  <AttachmentCard key={`${f.name}-${f.size}-${f.lastModified}-${i}`} file={f} onOpen={() => setPreview({ file: f })} onRemove={() => setPending((p) => p.filter((_, j) => j !== i))} />
                ))}
              </div>
            )}
            {paletteItems.length > 0 && (
              <div className="palette">
                {paletteItems.slice(0, 8).map((c) => (
                  <button key={c.name} className="palette-item" onClick={() => pickCommand(c)}>
                    <span className="mono">/{c.name} <span className="sub">{c.args}</span></span>
                    <span className="sub">{c.description}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="composer-box line">
              <input ref={fileInput} type="file" multiple hidden onChange={(e) => setPending((p) => [...p, ...Array.from(e.target.files ?? [])])} />
              <button className="roundbtn" title={t("session.attach")} onClick={() => fileInput.current?.click()} aria-label={t("session.attach")}>
                <Icon name="plus" />
              </button>
              <textarea
                ref={textarea}
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  const el = e.target;
                  el.style.height = "auto";
                  el.style.height = `${Math.min(el.scrollHeight, Math.max(120, window.innerHeight * 0.4))}px`;
                }}
                placeholder={t(status === "running" ? "session.composer.running" : status === "waiting" ? "session.composer.waiting" : "session.composer.idle")}
                rows={1}
                onPaste={onPaste}
                onKeyDown={(e) => {
                  if (e.key === "Tab" && paletteItems.length > 0) {
                    e.preventDefault();
                    pickCommand(paletteItems[0]);
                    return;
                  }
                  if (e.key === "Escape" && paletteQuery !== null) {
                    setDraft("");
                    return;
                  }
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    send();
                    return;
                  }
                  if (e.key === "Enter" && !e.shiftKey && enterSends()) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              {asr?.configured && !draft.trim() && <MicButton onRecording={onRecording} busy={transcribing} />}
              {status === "running" && (
                <button className="roundbtn stop" onClick={stop} aria-label={t("session.stop")} title={t("session.stop")}>
                  <Icon name="stop" />
                </button>
              )}
              {(status !== "running" || draft.trim() || pending.length > 0) && (
                <button className="roundbtn send" onClick={send} disabled={sending || (!draft.trim() && pending.length === 0)} aria-label={t(status === "running" ? "session.send.steer" : "session.send")} title={t(status === "running" ? "session.send.steer.title" : "session.send")}>
                  <Icon name="up" />
                </button>
              )}
            </div>
          </div>
        </div>
        {detail && (
          <Panel
            state={panel.state}
            onTab={panel.open}
            onClose={panel.close}
            onExpand={panel.expand}
            onBack={panel.back}
            onForward={panel.forward}
            root={detail.project?.name ?? t("session.files.crumb")}
            downloadUrl={(e) => downloadHref(e.base, e.path)}
            sheet={phone}
            onDrag={(dx) => dragPanel(dx, body.current?.clientWidth ?? window.innerWidth)}
            badges={{ details: subRunning }}
            details={
              <SessionDetails
                id={id}
                detail={detail}
                busy={busy}
                modes={modes}
                schedules={schedules}
                provider={provider}
                providerUsage={providerUsage}
                onOpen={onOpen}
                toast={toast}
                reload={load}
                focus={detailsFocus}
                on={{
                  rename,
                  setMode,
                  openPicker,
                  compact,
                  clearHistory,
                  remove,
                  exportMarkdown,
                  loopAction,
                  scheduleAction,
                  move: () => setMoving(true),
                  showLog: (line, text) => setCommandResult({ line, text }),
                  openFiles: () => panel.open("files"),
                }}
              />
            }
            files={<Files base={sessionBase(id)} uploadUrl={`${sessionBase(id)}/files/upload`} onPreview={openPreview} toast={toast} />}
            jobs={<JobsTab sessionId={id} messages={detail.messages} onOpen={panel.openFile} onPreview={openPreview} />}
          />
        )}
      </div>

      {commandResult && (
        <Overlay><div className="sheet-backdrop" onClick={() => setCommandResult(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <h3 className="mono">{commandResult.line}</h3>
            <div className="sheet-body">
              <pre className="diff" style={{ whiteSpace: "pre-wrap" }}>{commandResult.text}</pre>
            </div>
          </div>
        </div></Overlay>
      )}

      {moving && <MoveSessionSheet sessionId={id} current={detail?.project?.id ?? ""} onClose={() => setMoving(false)} onMoved={() => load(true)} toast={toast} />}

      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}

      {receipt && <ReceiptDialog sessionId={id} receipt={receipt} onClose={() => setReceipt(null)} />}

      {picker && (
        <Overlay><div className="sheet-backdrop" onClick={() => setPicker(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <h3>{t("session.model.for")}</h3>
            <div className="sheet-body">
              <button className="menu-item" onClick={() => chooseModel({ clear: true })}>
                {t("session.model.global")} <span className="sub">{picker.global}</span>
              </button>
              {Object.entries(picker.presets).map(([pid, p]) => (
                <button key={pid} className="menu-item" onClick={() => chooseModel({ preset: pid })}>
                  {p.label || p.model} <span className="sub">{p.provider}/{p.model}</span>
                </button>
              ))}
              <div className="sub" style={{ margin: "10px 0 4px" }}>{t("session.model.custom")}</div>
              <div className="composer-row">
                <input className="field" placeholder="vllm/Qwen3.6" value={custom} onChange={(e) => setCustom(e.target.value)} />
                <button
                  className="btn primary"
                  disabled={!custom.trim()}
                  onClick={() => {
                    const [prov, ...rest] = custom.trim().split("/");
                    const model = rest.join("/");
                    chooseModel(model ? { provider: prov, model } : { model: prov });
                  }}
                >
                  {t("session.model.use")}
                </button>
              </div>
            </div>
          </div>
        </div></Overlay>
      )}
    </div>
  );
}

function AttachmentCard({ file, onOpen, onRemove }: { file: File; onOpen: () => void; onRemove: () => void }) {
  const isImage = file.type.startsWith("image/") || previewKind(file.name) === "image";
  const url = useMemo(() => (isImage ? URL.createObjectURL(file) : null), [file, isImage]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return (
    <div className={`attachment ${isImage ? "image" : ""}`}>
      <button type="button" className="attachment-open" onClick={onOpen} title={canPreview(file.name) ? t("preview.open") : file.name}>
        {url ? <img src={url} alt={file.name} /> : <span className="attachment-glyph" aria-hidden>{fileGlyph(file.name)}</span>}
        <span className="attachment-meta">
          <span className="attachment-name">{file.name}</span>
          <span className="sub">{fmtBytes(file.size)}</span>
        </span>
      </button>
      <button type="button" className="attachment-x" onClick={onRemove} aria-label={t("common.remove")} title={t("common.remove")}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}

/** Hold-free recording: one tap starts, the next stops; the seconds tick while it runs. Disabled where the browser has no microphone API. */
function MicButton({ onRecording, busy }: { onRecording: (blob: Blob, seconds: number) => void; busy: boolean }) {
  const [rec, setRec] = useState<MediaRecorder | null>(null);
  const [seconds, setSeconds] = useState(0);
  const chunks = useRef<Blob[]>([]);
  const startedAt = useRef(0);
  const supported = typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
  useEffect(() => {
    if (!rec) return;
    const t = setInterval(() => setSeconds(Math.round((Date.now() - startedAt.current) / 1000)), 500);
    return () => clearInterval(t);
  }, [rec]);
  useEffect(() => () => rec?.stream.getTracks().forEach((tr) => tr.stop()), [rec]);
  async function start() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find((t) => MediaRecorder.isTypeSupported(t));
      const r = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      chunks.current = [];
      r.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      r.onstop = () => {
        stream.getTracks().forEach((tr) => tr.stop());
        const blob = new Blob(chunks.current, { type: r.mimeType || "audio/webm" });
        const took = Math.round((Date.now() - startedAt.current) / 1000);
        setRec(null);
        setSeconds(0);
        if (blob.size > 0 && took >= 1) onRecording(blob, took);
      };
      startedAt.current = Date.now();
      r.start(250);
      setRec(r);
      haptic("light");
    } catch {
      setRec(null);
    }
  }
  function stop() {
    rec?.stop();
  }
  if (rec) {
    return (
      <button className="chip recording" onClick={stop} title={t("session.mic.stop")} aria-label={t("session.mic.stop.label")}>
        <span className="rec-dot" /> {t("session.mic.seconds", { n: seconds })}
      </button>
    );
  }
  return (
    <button className="roundbtn" onClick={start} disabled={!supported || busy} title={t(!supported ? "session.mic.none" : busy ? "session.mic.busy" : "session.mic.title")} aria-label={t("session.mic")}>
      <Icon name={busy ? "dot" : "mic"} />
    </button>
  );
}

class Safe extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <div className="note">{t("session.norender")}</div> : this.props.children;
  }
}

function shortModel(name?: string, max = 18): string {
  if (!name) return t("session.model.none");
  const short = name.split("/").pop()!.replace(/^deepseek-/, "").replace(/\s*\(.*\)$/, "");
  return short.length > max ? `${short.slice(0, max - 1)}…` : short;
}

// ── turns ─────────────────────────────────────────────────────────────────────────────────

// The windowed list unmounts a turn as soon as it leaves the overscan band, so what the reader
// opened cannot live in the component: a step expanded and a long result fetched would both be gone
// two screens later, and the result fetched again on the way back. It is held here instead, keyed by
// session and by the row, and read back when the row mounts again — the same reason the markdown
// render cache is module-level. What a session held is dropped when the screen leaves it.
const disclosed = new Map<string, { open?: boolean; full?: string | null }>();

function remember(key: string, patch: { open?: boolean; full?: string | null }): void {
  disclosed.set(key, { ...disclosed.get(key), ...patch });
}

function forgetDisclosed(sessionId: string): void {
  const prefix = `${sessionId}:`;
  for (const key of [...disclosed.keys()]) if (key.startsWith(prefix)) disclosed.delete(key);
}

/** `useState` for a disclosure that has to survive its row leaving the window. */
function useDisclosed(key: string, initial: boolean): [boolean, (next: boolean | ((o: boolean) => boolean)) => void] {
  const [open, set] = useState(() => disclosed.get(key)?.open ?? initial);
  const write = useCallback(
    (next: boolean | ((o: boolean) => boolean)) => {
      set((o) => {
        const now = typeof next === "function" ? next(o) : next;
        remember(key, { open: now });
        return now;
      });
    },
    [key],
  );
  return [open, write];
}

function stepCount(items: Activity[]): number {
  return items.filter((a) => a.kind === "tool").length;
}

/**
 * The one line that says this answer is not the configured model's.
 *
 * It sits above the answer rather than under it, because it changes how the answer is read and the
 * reader has to have it before the text. Folded, it is the two names; opened, the reason the run
 * moved and the way to the calls themselves, which the Usage screen already lists per model.
 */
function FallbackChip({ fallback }: { fallback: ModelFallback }) {
  const [open, setOpen] = useState(false);
  const reason = DICT[`session.model.reason.${fallback.reason}`] ? t(`session.model.reason.${fallback.reason}`) : fallback.reason;
  return (
    <div className="fallback-note">
      <button className="chip attn" onClick={() => setOpen((o) => !o)}>
        <Icon name="model" size={14} /> {t("session.model.fallback.turn", { to: fallback.to, from: fallback.from })}
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </button>
      {open && (
        <div className="sub">
          {t("session.model.fallback.why", { from: fallback.from, to: fallback.to, reason })}{" "}
          <a href={pathFor("usage")} onClick={(e) => { e.preventDefault(); navigate(pathFor("usage")); }}>
            {t("session.model.fallback.calls")}
          </a>
        </div>
      )}
    </div>
  );
}

const TurnView = memo(function TurnView({ turn, live, onTurnAction }: { turn: Turn; live: boolean; onTurnAction?: (kind: "revert" | "fork", seq: number) => void }) {
  const { id: sessionId, revertable } = useContext(SessionContext);
  const [open, setOpen] = useDisclosed(`${sessionId}:turn:${turn.key}`, live);
  const wasLive = useRef(live);
  useEffect(() => {
    // Expanded while the agent works; folds away once the turn is over.
    if (wasLive.current && !live) setOpen(false);
    if (live) setOpen(true);
    wasLive.current = live;
  }, [live]);
  if (turn.summary) return <SummaryBlock message={turn.summary} />;
  const hasWork = turn.activity.length > 0 || live;
  const elapsed = (live ? Date.now() : turn.endedAt) - turn.startedAt;
  const steps = stepCount(turn.activity);
  // Retention drops the oldest snapshots once a store passes its bounds. Where this turn's snapshot
  // has gone, the undo is not offered: it would cut the history and leave the files as they are,
  // which is not what "revert to here" reads as. A session that never snapshots keeps the offer.
  const canRevert = revertable === null || (turn.user?.seq != null && revertable.has(turn.user.seq));
  return (
    <div className="turn">
      {turn.user && turn.user.origin && turn.user.origin !== "operator" && (
        <div className="sub" style={{ textAlign: "right", marginBottom: 2 }}>
          <span className="badge">{turn.user.origin}</span>
        </div>
      )}
      {turn.user && (
        <div className="msg-wrap">
          <Md className="msg user" text={turn.user.text} cacheKey={live ? undefined : `u${turn.user.seq ?? turn.key}`} />
          {/* Under the message, not beside it: a row beside the bubble is off-screen on a phone. */}
          <MessageActions
            text={turn.user.text}
            actions={
              turn.user.seq && onTurnAction && !live
                ? ([
                    { icon: "fork", label: t("session.fork.action"), onSelect: () => onTurnAction("fork", turn.user!.seq!) },
                    ...(canRevert ? [{ icon: "undo", label: t("session.revert.action.menu"), danger: true, onSelect: () => onTurnAction("revert", turn.user!.seq!) }] : []),
                  ] as MessageAction[])
                : []
            }
          />
        </div>
      )}
      {hasWork && (
        <button className="thinking-head" onClick={() => setOpen((o) => !o)}>
          <span className={`dots ${live ? "on" : ""}`}>
            <i />
            <i />
            <i />
          </span>
          {t(live ? (turn.pendingTools > 0 ? "session.working.for" : "session.thinking.for") : "session.worked", { t: duration(elapsed) })}
          {steps > 0 && <span className="steps">{plural("session.steps", steps)}</span>}
          <span className={`chev ${open ? "down" : ""}`}>›</span>
        </button>
      )}
      {open && (
        <div className="activity">
          <ActivityList items={turn.activity} compact={false} />
          {live && !turn.answer && turn.pendingTools === 0 && turn.activity.length > 0 && <div className="working">{t("session.working")}</div>}
        </div>
      )}
      {turn.fallback && (turn.answer || live) && <FallbackChip fallback={turn.fallback} />}
      {turn.answer && <Md className={`answer ${live ? "streaming" : ""}`} text={turn.answer} cacheKey={live ? undefined : `a${turn.key}`} />}
      {turn.answer && !live && <MessageActions text={turn.answer} />}
      <SentFiles items={turn.activity} />
    </div>
  );
});

/**
 * The turn the run is writing right now. It reads the stream directly, so a token repaints this
 * component and nothing else: the settled turns above it never hear about it.
 */
function LiveTurn({ base, live, onTurnAction, onRender }: { base: Turn | null; live: LiveStore; onTurnAction?: (kind: "revert" | "fork", seq: number) => void; onRender?: () => void }) {
  const state = useSyncExternalStore(live.subscribe, live.get);
  useClock(1000);
  useLayoutEffect(() => onRender?.());
  const turn = applyLive(base, state, Date.now());
  // `ended` is the model's full stop. From it the turn reads as written — no cursor under it, no
  // dots over it — whatever the session is still doing behind the answer.
  return (
    <Safe>
      <TurnView turn={turn} live={!state.ended} onTurnAction={onTurnAction} />
    </Safe>
  );
}

/** The bar over the composer while a run is going: what the agent is on, and for how long.
 *
 *  It stays for the moment after the run, quieter, while the turn is being written down — see
 *  `saving` above the composer. Same bar rather than a second one: the thing it reports on is the
 *  same turn, and a line that appears somewhere else would read as a new event.
 */
/** The run, as one line in the header: dot, word, elapsed, step, what the agent is doing. Clicking it
 *  brings the newest content back into view. The `livebar` classes are what the settle harness watches. */
function LiveBar({ status, saving, base, live, workspace, onJump }: { status: Status; saving: boolean; base: Turn | null; live: LiveStore; workspace?: string; onJump: () => void }) {
  const state = useSyncExternalStore(live.subscribe, live.get);
  useClock(1000);
  if (saving) {
    return (
      <button className="livebar head-status saving" onClick={onJump} role="status" aria-live="polite" title={t("session.jump.step")}>
        <Dot status="idle" />
        <b>{t("session.livebar.saving")}</b>
      </button>
    );
  }
  const turn = applyLive(base, state, Date.now());
  const steps = stepCount(turn.activity);
  const running = [...turn.activity].reverse().find((a) => a.kind === "tool" && a.running) as ToolItem | undefined;
  // A session waiting on the operator has nothing in progress, whatever its last turn holds.
  const step =
    status === "waiting"
      ? t("session.livebar.waiting")
      : running
        ? (() => {
            const d = describe(running, workspace);
            return `${d.verb}${d.detail ? ` ${d.detail}` : ""}`;
          })()
        : state.ended
          ? t("session.livebar.saving")
          : turn.answer
            ? t("session.livebar.writing")
            : state.thinking
              ? t("session.reasoning")
              : t("session.livebar.thinking");
  return (
    <button className={`livebar head-status ${status}`} onClick={onJump} role="status" aria-live="polite" title={t("session.jump.step")}>
      <Dot status={status} />
      <b>{statusWord(status === "waiting" ? "waiting" : "running")}</b>
      <span className="num">{duration(Date.now() - turn.startedAt)}</span>
      {steps > 0 && <span>{t("session.livebar.step", { n: steps })}</span>}
      {step && <span className="truncate">· {step}</span>}
    </button>
  );
}

/** The 2 px line along the header's bottom edge: a shimmer while a run is on, a measured width while
 *  the history is being compacted. */
function HeadProgress({ status, compacting }: { status: Status; compacting: Compacting | null }) {
  const pct = compacting ? compactionPct(compacting) : null;
  // A session waiting on the operator is not progressing: the line holds still, in the waiting colour.
  const mode = pct !== null ? "measured" : status === "waiting" ? "waiting" : "busy";
  return (
    <div className={`head-progress ${mode}`} aria-hidden>
      <i style={pct === null ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

function compactionPct(c: Compacting): number {
  const parts = c.parts_total > 1 ? Math.round((100 * c.parts_done) / c.parts_total) : 0;
  return c.stage === "writing" ? 97 : c.stage === "merging" ? 88 : c.parts_total > 1 ? Math.round(parts * 0.8) : 35;
}

/** A repaint on a timer, for the elapsed times — and only in the components that show one. */
function useClock(ms: number): void {
  const [, beat] = useState(0);
  useEffect(() => {
    const t = setInterval(() => beat((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [ms]);
}

type MessageAction = { icon: IconName; label: string; danger?: boolean; onSelect: () => void };

/** The row of small buttons under a message: copy it, and whatever else the turn allows. */
function MessageActions({ text, actions = [] }: { text: string; actions?: MessageAction[] }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    setCopied(await copyText(text));
    window.setTimeout(() => setCopied(false), 1600);
  };
  return (
    <div className="msg-actions">
      <button className="iconbtn small" onClick={copy} aria-label={t(copied ? "common.copied" : "common.copy")} title={t(copied ? "common.copied" : "common.copy")}>
        <Icon name={copied ? "check" : "copy"} size={15} />
      </button>
      {actions.map((a) => (
        <button key={a.label} className={`iconbtn small ${a.danger ? "danger" : ""}`} onClick={a.onSelect} aria-label={a.label} title={a.label}>
          <Icon name={a.icon} size={15} />
        </button>
      ))}
    </div>
  );
}

function SummaryBlock({ message }: { message: MessageView }) {
  const [open, setOpen] = useState(false);
  const meta = message.compaction;
  return (
    <div className="summary">
      <button className="summary-head" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" /> {t("session.summary")}
        {meta ? t("session.summary.meta", { n: meta.messages ?? 0, reason: meta.reason }) : ""}
        <span className="chev">{open ? "⌄" : "›"}</span>
      </button>
      {open && <Md className="summary-body" text={message.text} />}
    </div>
  );
}

/** The verb of a step, in the reader's language: "Ran command", "Выполнил команду". */
function verb(name: string, running: boolean): string {
  return t(`tool.${name}.${running ? "on" : "off"}`);
}

/** Verb + detail for a tool ("Ran command", "Read file"); the icon and the family it groups under. */
function describe(item: ToolItem, workspace?: string): { verb: string; family: string; detail: string; icon: IconName } {
  const a = item.args;
  const str = (k: string) => (typeof a[k] === "string" ? (a[k] as string) : a[k] === undefined ? "" : JSON.stringify(a[k]));
  const base = (p: string) => p.split("/").filter(Boolean).pop() ?? p;
  const r = item.running;
  switch (item.name) {
    case "Exec":
      return { verb: verb("Exec", r), family: "Exec", detail: commandPreview(str("command"), workspace), icon: "terminal" };
    case "Read":
      return { verb: verb("Read", r), family: "Read", detail: base(str("path")), icon: "file" };
    case "Write":
      return { verb: verb("Write", r), family: "Write", detail: base(str("path")), icon: "pen" };
    case "Edit":
      return { verb: verb("Edit", r), family: "Edit", detail: base(str("path")), icon: "pen" };
    case "Find":
    case "Search":
      return { verb: verb("Find", r), family: "search", detail: str("pattern") || str("query"), icon: "search" };
    case "WebSearch":
      return { verb: verb("WebSearch", r), family: "search", detail: str("query"), icon: "search" };
    case "WebFetch":
      return { verb: verb("WebFetch", r), family: "WebFetch", detail: str("url").replace(/^https?:\/\//, "").slice(0, 60), icon: "globe" };
    case "SendFile":
      return { verb: verb("SendFile", r), family: "SendFile", detail: base(str("path")), icon: "attach" };
    case "ImageView":
      return { verb: verb("ImageView", r), family: "ImageView", detail: `${base(str("path"))}${str("task") ? " · " + str("task").slice(0, 60) : ""}`, icon: "image" };
    case "AskUser":
      return { verb: t("tool.AskUser"), family: "AskUser", detail: "", icon: "question" };
    case "Skill":
      return { verb: verb("Skill", r), family: "Skill", detail: str("skill") || str("name"), icon: "skill" };
    case "Remember":
    case "Recall":
    case "Forget":
      return { verb: t(`tool.${item.name}`), family: item.name, detail: str("query") || str("text").slice(0, 60), icon: "bulb" };
    case "Verify":
      return { verb: r ? t("tool.Verify.on") : item.error ? t("tool.Verify.failed") : t("tool.Verify.off"), family: "Verify", detail: str("criterion"), icon: "wrench" };
    case "SubAgent":
      return { verb: verb("SubAgent", r), family: "SubAgent", detail: str("name") || str("task").split("\n")[0].slice(0, 60), icon: "spawn" };
    case "SubAgentList":
      return { verb: t("tool.SubAgentList"), family: "SubAgentList", detail: "", icon: "spawn" };
    case "SpawnAgent":
      return { verb: verb("SpawnAgent", r), family: "SpawnAgent", detail: str("title"), icon: "spawn" };
    case "AskPeer":
      return { verb: verb("AskPeer", r), family: "AskPeer", detail: str("name"), icon: "question" };
    case "StaySilent":
      return { verb: t("tool.StaySilent"), family: "StaySilent", detail: str("note").slice(0, 60), icon: "dot" };
    case "HistorySearch":
      return { verb: verb("HistorySearch", r), family: "search", detail: str("query"), icon: "search" };
    case "HistoryExpand":
      return { verb: t("tool.HistoryExpand"), family: "HistoryExpand", detail: t("tool.HistoryExpand.range", { from: str("from_seq"), to: str("to_seq") }), icon: "file" };
    default: {
      if (item.name.startsWith("Board")) {
        const what = item.name.replace(/^Board/, "").toLowerCase();
        const word = what === "add" || what === "update" ? t(`tool.board.${what}.${r ? "on" : "off"}`) : what === "get" ? t("tool.board.get") : t("tool.board.list");
        return { verb: t("tool.board", { what: word }), family: item.name, detail: str("title") || str("task_id") || str("id"), icon: "skill" };
      }
      // The names of the agent's own machinery are the tool ids themselves: they are what the agent
      // writes in its own reasoning and what the documentation calls them, so they stay as they are.
      if (item.name.startsWith("Self")) return { verb: item.name.replace(/^Self/, "Self: "), family: item.name, detail: str("branch") || str("title") || str("repo"), icon: "wrench" };
      if (item.name.startsWith("Schedule")) return { verb: item.name.replace(/^Schedule/, "Schedule: "), family: item.name, detail: str("name") || str("schedule_id"), icon: "clock" };
      if (item.name.startsWith("Service")) {
        const what = item.name.replace(/^Service/, "").toLowerCase();
        const word = what === "start" || what === "stop" ? verb(`Service${what[0].toUpperCase()}${what.slice(1)}`, r) : what === "logs" ? t("tool.ServiceLogs") : t("tool.ServiceList");
        return { verb: word, family: item.name, detail: str("name") ? `${str("name")}${str("command") ? " · " + str("command").slice(0, 50) : ""}` : "", icon: "globe" };
      }
      if (item.name.startsWith("Loop")) return { verb: item.name.replace(/^Loop/, "Loop: "), family: item.name, detail: str("reason") || str("note") || str("instruction").slice(0, 60), icon: "loop" };
      if (item.name.startsWith("Mcp")) {
        // Mcp_Postingboard_read_thread → "postingboard · read thread", with the first string argument as the detail.
        const parts = item.name.replace(/^Mcp_?/, "").split("_");
        const server = (parts.shift() ?? "").toLowerCase();
        const tool = parts.join(" ").replace(/_/g, " ");
        const first = Object.values(a).find((v) => typeof v === "string") as string | undefined;
        return { verb: `${server} · ${tool}`.trim(), family: item.name, detail: (first ?? "").slice(0, 60), icon: "plug" };
      }
      return { verb: item.name, family: item.name, detail: Object.keys(a).length ? JSON.stringify(a).slice(0, 60) : "", icon: "dot" };
    }
  }
}

function ActivityList({ items, compact }: { items: Activity[]; compact: boolean }) {
  const out: ReactElement[] = [];
  let i = 0;
  while (i < items.length) {
    const it = items[i];
    if (it.kind === "note") {
      out.push(
        <div key={i} className="note">
          <Md className="md" text={it.text} />
        </div>,
      );
      i++;
      continue;
    }
    if (it.kind === "thinking") {
      if (!compact) out.push(<ThoughtBlock key={i} text={it.text} />);
      i++;
      continue;
    }
    if (it.kind === "summary") {
      let j = i;
      while (j < items.length && items[j].kind === "summary") j++;
      const group = items.slice(i, j) as SummaryItem[];
      if (group.length > 1) out.push(<SummaryGroup key={i} group={group} />);
      else out.push(<SummaryRow key={i} text={it.text} reason={it.reason} />);
      i = j;
      continue;
    }
    // Group consecutive tools of one family (Read/Read/Read → "Read 3 files").
    const family = it.name;
    let j = i;
    while (j < items.length && items[j].kind === "tool" && (items[j] as ToolItem).name === family) j++;
    const group = items.slice(i, j) as ToolItem[];
    if (group.length > 1) out.push(<ToolGroup key={i} family={family} group={group} />);
    else out.push(<ToolRow key={it.id} item={it} />);
    i = j;
  }
  return <>{out}</>;
}

function ToolGroup({ family, group }: { family: string; group: ToolItem[] }) {
  const running = group.some((g) => g.running);
  const [open, setOpen] = useState(running);
  useEffect(() => {
    if (running) setOpen(true);
  }, [running]);
  const d = describe(group[group.length - 1]);
  return (
    <div className="group">
      <div className={`act head ${running ? "running" : ""}`} onClick={() => setOpen((o) => !o)}>
        <Icon name={d.icon} />
        <span className="verb">{groupVerb(d.family, running, group.length, family)}</span>
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </div>
      {open && group.map((g) => <ToolRow key={g.id} item={g} nested />)}
    </div>
  );
}

/** "Read 3 files", "Прочитал 3 файла" — the whole line, because the count sits inside it. */
const GROUPED = ["Exec", "Read", "Write", "Edit", "search", "WebFetch", "SendFile"];

function groupVerb(family: string, running: boolean, n: number, name: string): string {
  if (!GROUPED.includes(family)) return plural(`tool.group.other${running ? "" : ".done"}`, n, { name });
  return plural(`tool.group.${family}${running ? "" : ".done"}`, n);
}

function SummaryGroup({ group }: { group: SummaryItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act prose" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" />
        <span className="verb">{t("session.compacted.title")}</span>
        <span className="detail">{t("session.compacted.group", { n: group.length })}</span>
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </div>
      {open && group.map((s, k) => <SummaryRow key={k} text={s.text} reason={s.reason} />)}
    </div>
  );
}

function SummaryRow({ text, reason }: { text: string; reason: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act prose" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" />
        <span className="verb">{t("session.compacted.title")}</span>
        <span className="detail">{reason !== "auto" ? `${reason} · ` : ""}{plainPreview(text, 100)}</span>
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </div>
      {open && <Md className="summary-inline" text={text} />}
    </div>
  );
}

function ThoughtBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act prose" onClick={() => setOpen((o) => !o)}>
        <Icon name="bulb" />
        <span className="verb">{t("session.reasoning")}</span>
        <span className="detail">{plainPreview(text, 100)}</span>
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </div>
      {open && <div className="thought">{text}</div>}
    </div>
  );
}

function ToolRow({ item, nested }: { item: ToolItem; nested?: boolean }) {
  const { id: sessionId, workspace } = useContext(SessionContext);
  const [open, setOpen] = useDisclosed(`${sessionId}:tool:${item.id}`, false);
  const d = describe(item, workspace);
  const expanded = open || (item.running && item.name === "Exec");
  return (
    <div className={`act-wrap ${nested ? "nested" : ""}`}>
      <div className={`act ${item.error ? "error" : ""} ${item.running ? "running" : ""}`} onClick={() => setOpen((o) => !o)}>
        <Icon name={d.icon} />
        <span className="verb">{d.verb}</span>
        {d.detail && <span className="detail">{d.detail}</span>}
        <span className={`chev ${expanded ? "down" : ""}`}>›</span>
      </div>
      {(item.name === "ImageView" || item.name === "SendFile") && !item.running && <ToolAttachment item={item} />}
      {expanded && <ToolCard item={item} />}
    </div>
  );
}

function ToolCard({ item }: { item: ToolItem }) {
  const a = item.args;
  const parts: ReactNode[] = [];
  if (item.name === "Exec") parts.push(<div key="c" dangerouslySetInnerHTML={{ __html: codeBlock(String(a.command ?? ""), "bash") }} />);
  else if (item.name === "Write") parts.push(<div key="c" dangerouslySetInnerHTML={{ __html: codeBlock(String(a.content ?? "").slice(0, 4000), langOf(String(a.path ?? ""))) }} />);
  else if (item.name === "Edit")
    parts.push(
      <div key="c" className="diff">
        <pre className="del">{String(a.old_string ?? a.old ?? "")}</pre>
        <pre className="add">{String(a.new_string ?? a.new ?? "")}</pre>
      </div>,
    );
  else parts.push(<div key="c" dangerouslySetInnerHTML={{ __html: codeBlock(JSON.stringify(a, null, 2), "args") }} />);
  if (item.result !== undefined) parts.push(<ToolResultText key="r" item={item} />);
  return <div className="toolcard">{parts}</div>;
}

type Verification = { id: number; criterion: string; command: string; exit_code: number; passed: number; output_head: string; duration_ms: number; at: string; sandboxed: number; dependencies: string; tests_run: number | null };

/** A cited Verify receipt: what was claimed, the command that checked it, and how it ended. */
function ReceiptDialog({ sessionId, receipt, onClose }: { sessionId: string; receipt: string; onClose: () => void }) {
  const [row, setRow] = useState<Verification | null | undefined>(undefined);
  useEffect(() => {
    let gone = false;
    api
      .get<Verification[]>(`/api/sessions/${sessionId}/verifications`)
      .then((rows) => !gone && setRow(rows.find((r) => `v${r.id}` === receipt) ?? null))
      .catch(() => !gone && setRow(null));
    return () => {
      gone = true;
    };
  }, [sessionId, receipt]);
  return (
    <Overlay>
      <div className="sheet-backdrop" onClick={onClose}>
        <div className="sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={t("session.receipt", { id: receipt })}>
          <div className="grip" />
          <h3>{t("session.receipt", { id: receipt })}</h3>
          <div className="sheet-body">
            {row === undefined && <div className="empty">{t("common.loading")}</div>}
            {row === null && <div className="empty">{t("session.receipt.none", { id: receipt })}</div>}
            {row && (
              <>
                <p className="receipt-claim">
                  <b>{row.passed ? `✅ ${t("session.receipt.ok")}` : `❌ ${t("session.receipt.fail")}`}</b> — {row.criterion}
                </p>
                <div className="sub">
                  {t("session.receipt.meta", { code: row.exit_code, secs: (row.duration_ms / 1000).toFixed(1), when: timeAgo(row.at) })}
                  {row.tests_run !== null && t("session.receipt.tests", { n: row.tests_run })}
                  {row.sandboxed ? t("session.receipt.sandboxed") : ""}
                  {row.dependencies && t("session.receipt.depends", { list: row.dependencies })}
                </div>
                <div dangerouslySetInnerHTML={{ __html: codeBlock(row.command, "sh") }} />
                {row.output_head && <pre className="filetext">{row.output_head}</pre>}
              </>
            )}
          </div>
        </div>
      </div>
    </Overlay>
  );
}

const SessionContext = createContext<{ id: string; workspace: string; preview: (src: PreviewSource) => void; revertable: Set<number> | null }>({
  id: "",
  workspace: "",
  preview: () => undefined,
  revertable: null,
});

/** A tool's path as the workspace knows it: absolute paths inside the workspace become relative, others stay unreachable. */
function workspaceRelative(path: string, workspace: string): string | null {
  if (!path) return null;
  if (!path.startsWith("/")) return path.replace(/^\.\//, "");
  const root = workspace.replace(/\/+$/, "");
  if (root && (path === root || path.startsWith(root + "/"))) return path.slice(root.length + 1);
  return null;
}

/** The image an ImageView looked at, or the file a SendFile handed over: shown under the step, opened in the preview. */
function ToolAttachment({ item }: { item: ToolItem }) {
  const { id, workspace, preview } = useContext(SessionContext);
  const path = typeof item.args.path === "string" ? item.args.path : "";
  const rel = workspaceRelative(path, workspace);
  if (!rel || !id) return null;
  const src: PreviewSource = { base: sessionBase(id), path: rel };
  const name = rel.split("/").pop() ?? rel;
  if (previewKind(name) === "image") {
    return (
      <div className="tool-attachment">
        <AuthImg src={src} alt={name} className="tool-image" onClick={() => preview(src)} />
      </div>
    );
  }
  return (
    <div className="tool-attachment">
      <button type="button" className="file-chip" onClick={() => preview(src)} title={t(canPreview(name) ? "preview.open" : "preview.download")}>
        <span aria-hidden>{fileGlyph(name)}</span> {name}
      </button>
    </div>
  );
}

/** The files the agent handed over in this turn, attached under the answer whatever the trace shows. */
/** The compaction in flight: which stage, how many parts are summarised, how long it has run. */
function CompactionBar({ c }: { c: Compacting }) {
  useClock(1000);
  const what =
    c.stage === "writing"
      ? t("session.compacting.writing")
      : c.stage === "merging"
        ? t("session.compacting.merging")
        : c.parts_total > 1
          ? t("session.compacting.part", { n: Math.min(c.parts_done + 1, c.parts_total), total: c.parts_total })
          : t("session.compacting.summarising");
  return (
    <div className="livebar head-status compacting" role="status" aria-live="polite">
      <Dot status="compacting" />
      <b>{t("session.compacting.bar")}</b>
      <span className="num">{duration(Date.now() - new Date(c.started_at).getTime())}</span>
      <span className="truncate">{t("session.compacting.messages", { n: c.messages, what })}</span>
    </div>
  );
}

function SentFiles({ items }: { items: Activity[] }) {
  const { id, preview } = useContext(SessionContext);
  const sent = items.filter((a): a is ToolItem => a.kind === "tool" && a.name === "SendFile" && !a.running && !a.error && typeof a.args.path === "string");
  if (!sent.length || !id) return null;
  return (
    <div className="sent-files" aria-label={t("session.sentfiles")}>
      {sent.map((file) => {
        const path = String(file.args.path);
        const name = path.split("/").filter(Boolean).pop() ?? path;
        const caption = typeof file.args.caption === "string" ? file.args.caption : "";
        // The file is served by the call that sent it, so a path outside the workspace opens too.
        const src: PreviewSource = { base: `${sessionBase(id)}/sent/${encodeURIComponent(file.id)}`, path: name };
        if (previewKind(name) === "image") {
          return (
            <figure key={file.id} className="sent-file image">
              <AuthImg src={src} alt={name} className="tool-image" onClick={() => preview(src)} />
              {caption && <figcaption className="sub">{caption}</figcaption>}
            </figure>
          );
        }
        return (
          <button key={file.id} type="button" className="file-chip sent-file" onClick={() => preview(src)} title={caption || t(canPreview(name) ? "preview.open" : "preview.download")}>
            <span aria-hidden>{fileGlyph(name)}</span>
            <span className="truncate">{name}</span>
          </button>
        );
      })}
    </div>
  );
}

function ToolResultText({ item }: { item: ToolItem }) {
  const { id: sessionId } = useContext(SessionContext);
  const key = `${sessionId}:result:${item.id}`;
  const [full, setFullState] = useState<string | null>(() => disclosed.get(key)?.full ?? null);
  const setFull = (text: string | null) => {
    remember(key, { full: text });
    setFullState(text);
  };
  const [loading, setLoading] = useState(false);
  const text = full ?? item.result ?? "";
  // Whether there is more of it is the server's answer, not a comparison of lengths: what is on
  // screen is a redacted preview and the length is the text's, so a redaction that shortens the
  // preview would otherwise offer to fetch a result that is already whole.
  const clipped = full === null && !!item.clipped && item.length !== undefined;
  async function loadAll() {
    setLoading(true);
    try {
      const r = await api.get<{ content: string }>(`/api/sessions/${sessionId}/tool-results/${encodeURIComponent(item.id)}`);
      setFull(r.content);
    } catch {
      setFull(text);
    } finally {
      setLoading(false);
    }
  }
  const approval = item.error ? /Approval key: ([0-9a-f]{12})/.exec(text) : null;
  const [granted, setGranted] = useState(false);
  async function allowOnce() {
    if (!approval) return;
    try {
      await api.post(`/api/sessions/${sessionId}/policy/grant`, { key: approval[1] });
      setGranted(true);
    } catch {
      setGranted(false);
    }
  }
  return (
    <>
      <pre className={`result ${item.error ? "error" : ""} ${full !== null ? "full" : ""}`}>{text}</pre>
      {approval && (
        <button type="button" className="btn small" onClick={allowOnce} disabled={granted} title={t("session.allow.title")}>
          {t(granted ? "session.allowed.once" : "session.allow.once", { key: approval[1] })}
        </button>
      )}
      {clipped && (
        <button type="button" className="btn small" onClick={loadAll} disabled={loading}>
          {loading ? t("common.loading") : t("session.showall", { n: fmtInt(item.length!) })}
        </button>
      )}
    </>
  );
}

function langOf(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = { py: "python", ts: "typescript", tsx: "tsx", js: "javascript", md: "markdown", sh: "bash", json: "json", toml: "toml", yaml: "yaml", yml: "yaml", html: "html", css: "css" };
  return map[ext] ?? ext;
}

// ── questions, files, mcp ─────────────────────────────────────────────────────────────────

function QuestionCard({ sessionId, questions, onDone, toast }: { sessionId: string; questions: Question[]; onDone: () => void; toast: (t: string) => void }) {
  const [answers, setAnswers] = useState(questions.map(() => ({ selected: [] as string[], custom: "" })));
  function toggle(qi: number, label: string, multi: boolean) {
    setAnswers((prev) =>
      prev.map((a, i) => {
        if (i !== qi) return a;
        if (!multi) return { ...a, selected: [label] };
        return { ...a, selected: a.selected.includes(label) ? a.selected.filter((x) => x !== label) : [...a.selected, label] };
      }),
    );
  }
  async function submit() {
    try {
      await api.post(`/api/sessions/${sessionId}/answer`, {
        answers: questions.map((q, i) => ({ question: q.question, selected: answers[i].selected, custom: answers[i].custom || null })),
      });
      onDone();
    } catch (e) {
      toast(errorText(e));
    }
  }
  const complete = answers.every((a) => a.selected.length > 0 || a.custom.trim());
  return (
    <div className="card question">
      {questions.map((q, qi) => (
        <div key={qi}>
          <div className="title">❓ {q.question}</div>
          {(q.options ?? []).map((o) => (
            <button key={o.label} className={`btn option ${answers[qi].selected.includes(o.label) ? "selected" : ""}`} onClick={() => toggle(qi, o.label, !!q.multiSelect)}>
              {o.label}
              {o.description && <div className="sub">{o.description}</div>}
            </button>
          ))}
          {(q.allow_custom || !(q.options ?? []).length) && (
            <input className="field" style={{ marginTop: 6 }} placeholder={t("session.answer.placeholder")} value={answers[qi].custom} onChange={(e) => setAnswers((p) => p.map((a, i) => (i === qi ? { ...a, custom: e.target.value } : a)))} />
          )}
        </div>
      ))}
      <div className="btnrow">
        <button className="btn primary" disabled={!complete} onClick={submit}>
          {t("session.answer")}
        </button>
      </div>
    </div>
  );
}

/** A file tree under an API root: a session's workspace or a named workspace; uploads go to ``uploadUrl`` when given. */
export function Files({ base, uploadUrl, onPreview, toast }: { base: string; uploadUrl?: string; onPreview: (src: PreviewSource) => void; toast?: (t: string) => void }) {
  const [path, setPath] = useState("");
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [gen, setGen] = useState(0);
  const upload = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setError(null);
    api
      .get(`${base}/files?path=${encodeURIComponent(path)}`)
      .then(setData)
      .catch((e) => {
        setData(null);
        setError(errorText(e));
      });
  }, [base, path, gen]);
  async function sendFiles(files: FileList | null) {
    if (!files?.length || !uploadUrl) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("path", data?.kind === "dir" ? path : path.split("/").slice(0, -1).join("/"));
      for (const f of Array.from(files)) form.append("files", f, f.name);
      const res = await fetch(uploadUrl, { method: "POST", headers: api.authHeaders(), body: form });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      const r = (await res.json()) as { files: string[] };
      toast?.(plural("session.files.added", r.files.length));
      setGen((g) => g + 1);
    } catch (e) {
      toast?.(errorText(e));
    } finally {
      setUploading(false);
      if (upload.current) upload.current.value = "";
    }
  }
  if (error) return <div className="empty">{t("session.files.error", { what: path || t("session.files.theworkspace"), error })}</div>;
  if (!data) return <div className="empty">{t("common.loading")}</div>;
  const crumbs = path ? path.split("/") : [];
  const all: any[] = data.kind === "dir" ? data.entries : [];
  const entries = all.filter((e) => showHidden || !e.name.startsWith("."));
  const hidden = all.length - all.filter((e) => !e.name.startsWith(".")).length;
  const download = downloadHref(base, path);
  return (
    <div className="files">
      <div className="crumbs">
        <button className="crumb" onClick={() => setPath("")}>{t("session.files.crumb")}</button>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="sub"> / </span>
            <button className="crumb" onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>{c}</button>
          </span>
        ))}
        {data.kind !== "dir" && (
          <a className="btn small" style={{ marginLeft: "auto" }} href={download} target="_blank" rel="noreferrer">
            {t("preview.download")}{data.size ? ` · ${fmtBytes(data.size)}` : ""}
          </a>
        )}
        {data.kind === "dir" && uploadUrl && (
          <>
            <input ref={upload} type="file" multiple hidden onChange={(e) => sendFiles(e.target.files)} />
            <button className="btn small" style={{ marginLeft: "auto" }} disabled={uploading} onClick={() => upload.current?.click()} title={t("session.files.upload.title")}>
              <Icon name="up" size={14} /> {t(uploading ? "session.files.uploading" : "session.files.upload")}
            </button>
          </>
        )}
      </div>
      {data.kind === "dir" && entries.length === 0 && <div className="empty">{t("session.files.empty")}</div>}
      {data.kind === "dir" &&
        entries.map((e: any) => (
          <button
            key={e.name}
            className="card pressable row filerow"
            onClick={() => {
              const next = path ? `${path}/${e.name}` : e.name;
              // Files with a preview open in the dialog; folders and the rest are walked into as before.
              if (!e.dir && canPreview(e.name)) onPreview({ base, path: next });
              else setPath(next);
            }}
          >
            <span aria-hidden>{fileGlyph(e.name, e.dir)}</span>
            <div className="grow title">{e.name}</div>
            {!e.dir && <span className="sub">{fmtBytes(e.size)}</span>}
            {e.mtime && <span className="sub">{shortDateTime(e.mtime * 1000)}</span>}
          </button>
        ))}
      {data.kind === "dir" && hidden > 0 && (
        <button className="btn small" onClick={() => setShowHidden((h) => !h)}>
          {plural(showHidden ? "session.files.hidden.hide" : "session.files.hidden", hidden)}
        </button>
      )}
      {data.kind === "file" && data.truncated && <div className="sub" style={{ margin: "6px 0" }}>{t("session.files.truncated")}</div>}
      {data.kind !== "dir" && canPreview(path) && (
        <button className="btn small" style={{ marginBottom: 8 }} onClick={() => onPreview({ base, path })}><Icon name="eye" size={14} /> {t("preview.open")}</button>
      )}
      {data.kind === "file" && <pre className="filetext">{data.content}</pre>}
      {data.kind === "binary" && previewKind(path) === "image" && <AuthImg className="preview" src={{ base, path }} alt={path} onClick={() => onPreview({ base, path })} />}
      {data.kind === "binary" && previewKind(path) !== "image" && <div className="empty">{t("session.files.binary", { size: fmtBytes(data.size) })}</div>}
    </div>
  );
}
