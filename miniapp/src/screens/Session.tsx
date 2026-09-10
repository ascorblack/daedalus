import { Component, createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { api, AsrStatus, LoopView, ProviderUsage, Schedule, SlashCommand, MessageView, Question, SessionDetail } from "../api";
import { ServiceRow, Status, ToolPicker, fmtInt, fmtUsd, loopLabel, timeAgo } from "../components";
import { codeBlock, renderMarkdown } from "../md";
import { confirmAsync, enterSends, errorText, fmtBytes, fmtTok, haptic } from "../ui";
import { Icon, IconName } from "../icons";
import { AuthImg, FilePreview, PreviewSource, canPreview, fileGlyph, previewKind, sessionBase } from "../preview";

/** Markdown parsed once per text: a token streaming into one turn must not re-parse every other. */
const Md = memo(function Md({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
});

/** The trailing retrieval headline ⟦…⟧ is for the transcript index, not for the reader; a half-streamed one is cut too. */
function stripHeadline(text: string): string {
  const m = text.match(/(?:^|\n)\s*⟦[^⟦⟧]{3,2000}⟧\s*$/s);
  if (m && m.index !== undefined) return text.slice(0, m.index).trimEnd();
  const open = text.lastIndexOf("⟦");
  if (open !== -1 && !text.slice(open).includes("⟧")) {
    const lineStart = text.lastIndexOf("\n", open) + 1;
    if (!text.slice(lineStart, open).trim()) return text.slice(0, lineStart).trimEnd();
  }
  return text;
}

// ── data shapes ───────────────────────────────────────────────────────────────────────────

type LiveTool = { id: string; name: string; args: string; result?: string; error?: boolean };
type LiveState = { text: string; thinking: string; tools: LiveTool[]; startedAt: number | null };
const EMPTY_LIVE: LiveState = { text: "", thinking: "", tools: [], startedAt: null };

type ToolItem = { kind: "tool"; id: string; name: string; args: Record<string, unknown>; result?: string; error?: boolean; running: boolean; length?: number };
type NoteItem = { kind: "note"; text: string };
type ThinkItem = { kind: "thinking"; text: string };
type SummaryItem = { kind: "summary"; text: string; reason: string };
type Activity = ToolItem | NoteItem | ThinkItem | SummaryItem;

type Turn = {
  key: string;
  user?: MessageView;
  summary?: MessageView;
  activity: Activity[];
  answer: string;
  startedAt: number;
  endedAt: number;
  pendingTools: number;
};

function parseArgs(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw || "{}");
  } catch {
    return { raw };
  }
}

/** Group the flat message list into turns: a user message plus everything the agent did after it. */
function buildTurns(messages: MessageView[], live: LiveState, busy: boolean): Turn[] {
  const results = new Map<string, { content: string; is_error: boolean; length?: number }>();
  for (const m of messages) for (const r of m.tool_results) results.set(r.id, r);
  const seen = new Set<string>();
  const turns: Turn[] = [];
  let current: Turn | null = null;
  const open = (key: string, at: number): Turn => {
    const t: Turn = { key, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0 };
    turns.push(t);
    return t;
  };
  messages.forEach((m, i) => {
    const at = Date.parse(m.created_at) || Date.now();
    if (m.role === "tool" || m.internal) return;
    if (m.summary) {
      if (m.compaction?.reason !== "core") {
        // The host compacted between runs (auto) or on request (manual): a block of its own after the
        // turn, so the answer that came before it stays the answer.
        turns.push({ key: `s${m.seq ?? i}`, summary: m, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0 });
        current = null;
        return;
      }
      // The core compacted mid-run: a step inside the turn, where the summarised work used to be.
      if (!current) current = open(`a${m.seq ?? i}`, at);
      if (current.answer) {
        current.activity.push({ kind: "note", text: current.answer });
        current.answer = "";
      }
      current.activity.push({ kind: "summary", text: m.text, reason: "core" });
      return;
    }
    if (m.role === "user") {
      current = open(`u${m.seq ?? i}`, at);
      current.user = m;
      return;
    }
    if (m.role === "system") return;
    if (!current) current = open(`a${m.seq ?? i}`, at);
    current.endedAt = at;
    if (current.answer) {
      // Text that turned out not to be final becomes a note.
      current.activity.push({ kind: "note", text: current.answer });
      current.answer = "";
    }
    if (m.thinking) current.activity.push({ kind: "thinking", text: m.thinking });
    if (m.text && m.tool_calls.length) current.activity.push({ kind: "note", text: m.text });
    else if (m.text) current.answer = m.text;
    for (const c of m.tool_calls) {
      seen.add(c.id);
      const r = results.get(c.id);
      const liveResult = live.tools.find((t) => t.id === c.id);
      const content = r?.content ?? liveResult?.result;
      const running = content === undefined;
      if (running) current.pendingTools++;
      current.activity.push({ kind: "tool", id: c.id, name: c.name, args: c.arguments, result: content, error: r?.is_error ?? liveResult?.error, running, length: r?.length });
    }
  });
  if (busy) {
    if (!current) current = open("live", live.startedAt ?? Date.now());
    const t: Turn = current;
    const fresh = live.tools.filter((lt) => !seen.has(lt.id));
    if (t.answer && (live.text || live.thinking || fresh.length)) {
      // Something newer is streaming, so the text before it was not the final answer.
      t.activity.push({ kind: "note", text: t.answer });
      t.answer = "";
    }
    const thinkingKnown = t.activity.some((a) => a.kind === "thinking" && a.text === live.thinking);
    if (live.thinking && !thinkingKnown) t.activity.push({ kind: "thinking", text: live.thinking });
    for (const lt of fresh) {
      const running = lt.result === undefined;
      if (running) t.pendingTools++;
      t.activity.push({ kind: "tool", id: lt.id, name: lt.name, args: parseArgs(lt.args), result: lt.result, error: lt.error, running });
    }
    if (live.text) t.answer = stripHeadline(live.text);
    t.endedAt = Date.now();
  }
  return turns;
}

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
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [view, setView] = useState<"chat" | "files" | "mcp">("chat");
  const [menu, setMenu] = useState(false);
  const [editingTitle, setEditingTitle] = useState<string | null>(null);
  const [modes, setModes] = useState<string[]>([]);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [commandResult, setCommandResult] = useState<{ line: string; text: string } | null>(null);
  const [picker, setPicker] = useState<null | { presets: Record<string, { provider: string; model: string; label: string }>; global: string }>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const [custom, setCustom] = useState("");
  const [tick, setTick] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const stick = useRef(true);
  const userScrolling = useRef(false);
  const [atBottom, setAtBottom] = useState(true);
  const [preview, setPreview] = useState<PreviewSource | null>(null);
  const [dragging, setDragging] = useState(0);
  const [providerUsage, setProviderUsage] = useState<ProviderUsage | null>(null);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [asideOpen, setAsideOpen] = useState(() => pane === undefined);
  const [asr, setAsr] = useState<AsrStatus | null>(null);
  const [asideWidth, setAsideWidth] = usePaneWidth("aside", 272, 200, 520);
  const [paneWidth, setPaneWidth] = usePaneWidth("pane", 420, 280, 900);

  async function loopAction(a: string) {
    try {
      await api.post(`/api/sessions/${id}/loop/action`, { action: a });
      toast(`loop: ${a}`);
      load(true);
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function setMode(mode: string) {
    try {
      await api.post(`/api/sessions/${id}/mode`, { mode: mode === "default" ? null : mode });
      toast(`mode: ${mode} (from the next run)`);
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const turnAction = useCallback(
    async (kind: "revert" | "fork", seq: number) => {
      try {
        if (kind === "revert") {
          if (!(await confirmAsync("Undo this turn and everything after it? The working history is cut and the workspace files are restored where a snapshot exists (nested git repositories stay as they are). The transcript keeps everything."))) return;
          const r = await api.post<{ dropped: number; workspace_restored: boolean; untouched: string[] }>(`/api/sessions/${id}/revert`, { seq });
          const ws = r.workspace_restored ? (r.untouched.length ? `, workspace restored (${r.untouched.length} nested repo(s) untouched)` : ", workspace restored") : ", files not restored (no snapshot)";
          toast(`reverted: ${r.dropped} message(s) removed${ws}`);
        } else {
          const r = await api.post<{ id: string; title: string; messages: number }>(`/api/sessions/${id}/fork`, { seq });
          toast(`forked into "${r.title}" (${r.messages} messages) — open it from the Bots tab`);
        }
        load();
      } catch (e) {
        toast(errorText(e));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [id, toast],
  );

  const [offline, setOffline] = useState(false);
  const load = useCallback(
    async (quiet = false) => {
      try {
        setDetail(await api.get<SessionDetail>(`/api/sessions/${id}`));
        setOffline(false);
      } catch (e) {
        // Timers and the event stream retry by themselves: one banner in the header, not a toast every few seconds.
        setOffline(true);
        if (!quiet) toast(errorText(e));
      }
    },
    [id, toast],
  );

  useEffect(() => {
    load();
    api.get<Record<string, unknown>>("/api/modes").then((m) => setModes(Object.keys(m))).catch(() => setModes([]));
    api.get<SlashCommand[]>("/api/commands").then(setCommands).catch(() => setCommands([]));
    api.get<AsrStatus>("/api/asr").then(setAsr).catch(() => setAsr(null));
  }, [load]);

  // A recording from the microphone becomes text in the composer (or goes straight out with autosend).
  const [transcribing, setTranscribing] = useState(false);
  async function onRecording(blob: Blob, seconds: number) {
    if (asr && seconds > asr.max_seconds) {
      toast(`recording is ${seconds}s, the limit is ${asr.max_seconds}s`);
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
        toast(`sent: ${r.transcript.slice(0, 80)}`);
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
        if (!(await confirmAsync(`Delete the scheduled task "${sc.name}"?`))) return;
        await api.delete(`/api/schedules/${sc.id}`);
        toast("task deleted");
      } else {
        const r = await api.post<{ session_id: string }>(`/api/schedules/${sc.id}/run`);
        toast(r.session_id === id ? "running here" : "started in its own session");
        if (r.session_id !== id) onOpen?.(r.session_id);
      }
      loadSchedules();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const status = (detail?.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";

  // The event stream carries every change while a run is active; this re-read is the safety net, not the feed.
  useEffect(() => {
    if (!busy) {
      setLive(EMPTY_LIVE);
      return;
    }
    const t = setInterval(() => load(true), 20000);
    const clock = setInterval(() => setTick((n) => n + 1), 1000);
    return () => {
      clearInterval(t);
      clearInterval(clock);
    };
  }, [busy, load]);

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
        load(true);
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, 15000);
      }
    })();
    function handle(event: string, p: Record<string, any>) {
      if (event === "message_start") setLive((s) => ({ ...s, text: "", thinking: "", startedAt: s.startedAt ?? Date.now() }));
      else if (event === "content_block_delta") {
        const d = p.delta ?? {};
        if (d.type === "text_delta") setLive((s) => ({ ...s, text: s.text + (d.text ?? "") }));
        if (d.type === "thinking_delta") setLive((s) => ({ ...s, thinking: s.thinking + (d.text ?? "") }));
      } else if (event === "tool_use_start") {
        setLive((s) => ({ ...s, tools: [...s.tools, { id: p.tool_call_id, name: p.tool_name, args: "" }] }));
      } else if (event === "tool_use_stop") {
        setLive((s) => ({ ...s, tools: s.tools.map((t) => (t.id === p.tool_call_id ? { ...t, args: JSON.stringify(p.final_input ?? {}) } : t)) }));
      } else if (event === "tool_result") {
        setLive((s) => ({ ...s, tools: s.tools.map((t) => (t.id === p.tool_call_id ? { ...t, result: String(p.content ?? p.output ?? ""), error: !!p.is_error } : t)) }));
      } else if (event === "message_stop") {
        // The history now carries this message; drop the streamed copy once it is loaded.
        load(true).then(() => setLive((s) => ({ ...s, text: "", thinking: "" })));
      } else if (event === "state_changed" || event === "tool_call_pending" || event === "run_settled" || event === "compaction_completed") load(true);
    }
    return () => {
      stop = true;
      controller.abort();
    };
  }, [id, load]);

  const turns = useMemo(() => buildTurns(detail?.messages ?? [], live, busy), [detail, live, busy]);

  // Follow the newest content only while the reader is at the bottom and not scrolling by hand.
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current && !userScrolling.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

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
    if (spec?.confirm && !(await confirmAsync(`Run /${name}?`))) return;
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
    try {
      await api.post(`/api/sessions/${id}/stop`);
      haptic("medium");
      toast("stopping");
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
    setMenu(false);
    if (busy) {
      toast("stop the run first");
      return;
    }
    if (!(await confirmAsync("Replace the whole history with a summary? The agent keeps only the summary."))) return;
    toast("compacting…");
    try {
      await api.post(`/api/sessions/${id}/compact`, { instructions: "" });
      await load();
      toast("compacted");
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function clearHistory() {
    setMenu(false);
    if (busy) {
      toast("stop the run first");
      return;
    }
    if (!(await confirmAsync("Start over with an empty history? The workspace, the brief, the model and the loop stay; the transcript keeps the old turns."))) return;
    try {
      const r = await api.post<{ dropped: number }>(`/api/sessions/${id}/clear`);
      toast(`history cleared: ${r.dropped} message(s) dropped`);
      await load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function openPicker() {
    try {
      const st = await api.get<any>("/api/settings");
      const def = st.presets?.[st.model?.preset];
      setPicker({ presets: st.presets ?? {}, global: def ? def.label || `${def.provider}/${def.model}` : String(st.model?.preset ?? "default") });
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function chooseModel(body: Record<string, unknown>) {
    setPicker(null);
    try {
      const r = await api.post<{ model: string }>(`/api/sessions/${id}/model`, body);
      toast(`model: ${r.model}`);
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function remove() {
    setMenu(false);
    if (!(await confirmAsync("Delete this session, its topic and its workspace?"))) return;
    try {
      await api.delete(`/api/sessions/${id}`);
      onBack();
    } catch (e) {
      toast(errorText(e));
    }
  }

  void tick;
  const sessionCtx = useMemo(() => ({ id, workspace: detail?.workspace ?? "", preview: setPreview }), [id, detail?.workspace]);
  return (
    <div className={`chat ${pane ? `pane pane-${pane}` : ""}`} onDragEnter={(e) => { if (e.dataTransfer?.types.includes("Files")) setDragging((d) => d + 1); }} onDragLeave={() => setDragging((d) => Math.max(0, d - 1))} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      {dragging > 0 && <div className="dropzone"><Icon name="attach" size={28} /> Drop files to attach</div>}
      <div className="chat-head">
        <button className="iconbtn" onClick={onBack} aria-label={pane === "right" ? "close this pane" : "back"} title={pane === "right" ? "Close this pane" : "Back"}>
          <Icon name={pane === "right" ? "close" : "back"} />
        </button>
        <div className="grow" style={{ minWidth: 0 }}>
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
            <div className="title" onClick={() => setEditingTitle(detail?.title ?? "")} title="tap to rename">
              {detail?.title ?? "…"}
            </div>
          )}
          {detail?.subagent_of && (
            <div className="sub leader-link" onClick={() => onOpen?.(detail.subagent_of!)} title="open the leader session">
              ↳ subagent of <b>{detail.leader_title ?? detail.subagent_of}</b>
            </div>
          )}
          <div className="sub meta">
            {busy ? <span className="live-dot" /> : null}
            <span title={detail?.model}>{shortModel(detail?.model, 28)}</span>
            <span title={`${fmtInt(detail?.usage.i)} tokens in, ${fmtInt(detail?.usage.o)} out`}>
              {fmtTok(detail?.usage.i)}↑ {fmtTok(detail?.usage.o)}↓
            </span>
            <span>{fmtUsd(detail?.usage.usd)}</span>
            {detail?.context && detail.context.tokens > 0 && detail.context.window > 0 && (
              <span className="ctx" title={`context in use: ${detail.context.tokens.toLocaleString()} of ${detail.context.window.toLocaleString()} tokens · ${detail.context.messages} messages (${detail.context.summaries} summaries, ${detail.context.operator_turns} yours)`}>
                ctx {Math.round((100 * detail.context.tokens) / detail.context.window)}%
                <i className="ctxbar" style={{ ["--fill" as string]: `${Math.min(100, Math.round((100 * detail.context.tokens) / detail.context.window))}%` }} />
              </span>
            )}
            {offline && <span className="offline">reconnecting…</span>}
            {detail?.loop && <span className={`badge loop ${detail.loop.status}`} title={detail.loop.instruction}>{loopLabel(detail.loop)}</span>}
          </div>
        </div>
        <div className="head-actions">
          <button className={`iconbtn wide-only ${asideOpen ? "on" : ""}`} onClick={() => setAsideOpen((v) => !v)} aria-label="session panel" title="Session panel (usage, loop, cron, services)">
            <Icon name="columns" />
          </button>
          {onSplit && (
            <button className="iconbtn wide-only" onClick={onSplit} aria-label="open another session beside this one" title="Open another session beside this one">
              <Icon name="split" />
            </button>
          )}
          <button className={`iconbtn ${view === "files" ? "on" : ""}`} onClick={() => setView(view === "files" ? "chat" : "files")} aria-label="workspace files" title="Workspace files">
            <Icon name="folder" />
          </button>
          <button className={`iconbtn ${view === "mcp" ? "on" : ""}`} onClick={() => setView(view === "mcp" ? "chat" : "mcp")} aria-label="MCP servers" title="MCP servers">
            <Icon name="plug" />
          </button>
          <button className="iconbtn" onClick={() => setMenu((m) => !m)} aria-label="session settings" title="Session settings">
            <Icon name="settings" />
          </button>
        </div>
      </div>

      {detail && detail.subagents && detail.subagents.length > 0 && (
        <div className="subagents phone-only" aria-label="subagents">
          <span className="subagents-label">Subagents</span>
          {detail.subagents.map((s) => (
            <button key={s.session_id} className={"chip subchip " + s.status} onClick={() => onOpen?.(s.session_id)} title={`${s.model} · session ${s.session_id}`}>
              {s.running ? <span className="live-dot" /> : <span className={"dot " + s.status} />}
              <span className="name">{s.name || s.session_id}</span>
              <span className="sub">{s.running ? "working" : s.status === "failed" ? "failed" : "done"}</span>
            </button>
          ))}
        </div>
      )}

      {menu && detail && (
        <div className="sheet-backdrop" onClick={() => setMenu(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <div className="sheet-head">
              <h3>Session settings</h3>
              <button className="iconbtn small" onClick={() => setMenu(false)} aria-label="close" title="Close"><Icon name="close" size={16} /></button>
            </div>
            <div className="sheet-body">
              <section className="sheet-section">
              <div className="sheet-section-title">Session</div>
              <label className="field">Title</label>
              <input className="field" defaultValue={detail.title} onBlur={(e) => rename(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
              <label className="field">Model</label>
              <button className="menu-item" onClick={() => { setMenu(false); openPicker(); }}>
                {detail.model || "global default"} <span className="sub">change</span>
              </button>
              <label className="field">Mode</label>
              <select className="field" value={detail.mode || "default"} onChange={(e) => setMode(e.target.value)}>
                {["default", ...modes].map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
              </section>
              <section className="sheet-section">
              <div className="sheet-section-title">Context</div>
              {detail.context && (
                <div className="sub">
                  <b>{detail.context.tokens.toLocaleString()}</b>
                  {detail.context.window > 0 && ` / ${detail.context.window.toLocaleString()} tokens (${Math.round((100 * detail.context.tokens) / detail.context.window)}%)`} · {detail.context.messages} messages in the working history: {detail.context.summaries} summaries, {detail.context.operator_turns} yours. The header's ↑↓ figures are lifetime totals.
                </div>
              )}
              <div className="btnrow">
                <button className="btn small" onClick={compact}><Icon name="compact" size={14} /> Compact history</button>
                <button className="btn small" onClick={clearHistory} title="Start over with an empty history; the workspace, the brief and the settings stay"><Icon name="trash" size={14} /> Clear history</button>
              </div>
              </section>
              <section className="sheet-section">
              <div className="sheet-section-title"><Icon name="loop" size={14} /> Loop{detail.loop ? ` · ${loopLabel(detail.loop).replace(/^loop · /, "")}` : ""}</div>
              <LoopPanel sessionId={id} loop={detail.loop ?? null} onChange={() => load(true)} toast={toast} />
              </section>
              {detail.services && detail.services.length > 0 && (
                <section className="sheet-section">
                  <div className="sheet-section-title"><Icon name="globe" size={14} /> Services</div>
                  {detail.services.map((s) => (
                    <ServiceRow key={s.name} s={s} sessionId={id} onChange={() => load(true)} toast={toast} onLogs={(text) => { setMenu(false); setCommandResult({ line: `service ${s.name} · log`, text }); }} />
                  ))}
                </section>
              )}
              {schedules.length > 0 && (
                <section className="sheet-section">
                  <div className="sheet-section-title"><Icon name="clock" size={14} /> Cron · {schedules.length}</div>
                  {schedules.map((sc) => <ScheduleRow key={sc.id} sc={sc} sessionId={id} onAction={scheduleAction} />)}
                </section>
              )}
              <section className="sheet-section">
              <div className="sheet-section-title"><Icon name="wrench" size={14} /> Tools</div>
              <ToolPicker
                off={detail.tools_off ?? []}
                note="Applies from the agent's next step."
                onChange={async (off) => {
                  try {
                    await api.post(`/api/sessions/${id}/tools`, { tools_off: off });
                    load(true);
                  } catch (e) {
                    toast(errorText(e));
                  }
                }}
              />
              </section>
              <section className="sheet-section">
              <div className="sheet-section-title"><Icon name="pen" size={14} /> Brief</div>
              <label className="field">Standing instructions in this session's system prompt{detail.spawned_by ? `; set by session ${detail.spawned_by}` : ""}</label>
              <textarea
                className="field"
                rows={4}
                defaultValue={detail.brief ?? ""}
                placeholder="What this agent is for, how the work is done, where things are…"
                onBlur={async (e) => {
                  const brief = e.target.value.trim();
                  if (brief === (detail.brief ?? "")) return;
                  try {
                    await api.post(`/api/sessions/${id}/brief`, { brief });
                    toast(brief ? "brief saved (applies from the next run)" : "brief removed");
                    load();
                  } catch (err) {
                    toast(errorText(err));
                  }
                }}
              />
              </section>
              <section className="sheet-section">
              <div className="sheet-section-title"><Icon name="chart" size={14} /> Spend</div>
              <label className="field">Cap for this session (USD, all its runs; empty = global limits only)</label>
              <div className="composer-row">
                <input
                  className="field"
                  type="number"
                  step="0.5"
                  min={0}
                  placeholder="none"
                  defaultValue={detail.usd_cap ?? ""}
                  onBlur={async (e) => {
                    const raw = e.target.value.trim();
                    const cap = raw === "" ? null : Number(raw);
                    if (cap !== null && Number.isNaN(cap)) return;
                    if (cap === (detail.usd_cap ?? null)) return;
                    try {
                      await api.post(`/api/sessions/${id}/cap`, { usd_cap: cap });
                      toast(cap === null ? "session cap removed" : `session cap: $${cap}`);
                      load();
                    } catch (err) {
                      toast(errorText(err));
                    }
                  }}
                />
                <span className="sub" style={{ whiteSpace: "nowrap" }}>spent {fmtUsd(detail.usage.usd)}</span>
              </div>
              </section>
              <section className="sheet-section danger">
              <div className="sheet-section-title">Danger zone</div>
              <div className="btnrow" style={{ marginTop: 0 }}>
                <button className="btn small danger" onClick={remove}><Icon name="trash" size={14} /> Delete session</button>
              </div>
              </section>
            </div>
          </div>
        </div>
      )}

      <div className={`chat-body ${view === "chat" ? "" : "split"} ${asideOpen ? "" : "no-aside"}`} style={{ ["--aside-w" as string]: `${asideWidth}px`, ["--pane-w" as string]: `${paneWidth}px` }}>
        {detail && asideOpen && (
          <aside className="session-aside wide-only">
            <div className="aside-card">
              <div className="aside-title">Session</div>
              <div className="aside-row"><Icon name="model" size={16} /><span className="grow" title={detail.model}>{shortModel(detail.model, 30)}</span></div>
              {detail.context && detail.context.window > 0 && (
                <div className="aside-row" title={`${detail.context.tokens.toLocaleString()} of ${detail.context.window.toLocaleString()} tokens · ${detail.context.messages} messages`}>
                  <Icon name="compact" size={16} />
                  <span className="grow">context {Math.round((100 * detail.context.tokens) / detail.context.window)}%</span>
                  <i className="ctxbar wide" style={{ ["--fill" as string]: `${Math.min(100, Math.round((100 * detail.context.tokens) / detail.context.window))}%` }} />
                </div>
              )}
              <div className="aside-row"><Icon name="chart" size={16} /><span className="grow">{fmtTok(detail.usage.i)}↑ {fmtTok(detail.usage.o)}↓ · {fmtUsd(detail.usage.usd)}</span></div>
              <button className="aside-row link" onClick={() => setView(view === "files" ? "chat" : "files")} title={detail.workspace}>
                <Icon name="folder" size={16} />
                <span className="grow name">{detail.workspace_own === false ? `workspace: ${detail.workspace_name}` : "own workspace"}</span>
                {detail.workspace_sessions && detail.workspace_sessions.length > 0 && <span className="sub" title={detail.workspace_sessions.map((w) => w.title).join(", ")}>+{detail.workspace_sessions.length} session{detail.workspace_sessions.length === 1 ? "" : "s"}</span>}
              </button>
              {detail.workspace_sessions && detail.workspace_sessions.length > 0 && detail.workspace_sessions.slice(0, 4).map((w) => (
                <button key={w.id} className="aside-row link" onClick={() => onOpen?.(w.id)} title="a session working in the same workspace">
                  <span className="dot" style={{ background: "var(--muted)" }} /><span className="grow name sub">{w.title}</span>
                </button>
              ))}
              {detail.subagent_of && (
                <button className="aside-row link" onClick={() => onOpen?.(detail.subagent_of!)}>
                  <Icon name="back" size={16} /><span className="grow">leader: {detail.leader_title ?? detail.subagent_of}</span>
                </button>
              )}
            </div>
            {provider && <ProviderUsageCard provider={provider} usage={providerUsage} />}
            {schedules.length > 0 && (
              <div className="aside-card">
                <div className="aside-title"><Icon name="clock" size={14} /> Cron <span className="sub">{schedules.filter((x) => x.enabled).length} on</span></div>
                {schedules.map((sc) => <ScheduleRow key={sc.id} sc={sc} sessionId={id} onAction={scheduleAction} />)}
              </div>
            )}
            {detail.loop && (
              <div className="aside-card">
                <div className="aside-title"><Icon name="loop" size={14} /> Loop <span className={`badge loop ${detail.loop.status}`}>{detail.loop.status}</span></div>
                <div className="aside-text">{detail.loop.instruction}</div>
                <div className="sub">{loopLabel(detail.loop).replace(/^loop · /, "")}{detail.loop.next_run_at && detail.loop.status === "active" ? ` · next ${new Date(detail.loop.next_run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</div>
                <div className="btnrow" style={{ marginTop: 8 }}>
                  {detail.loop.status === "active" ? (
                    <button className="btn small" onClick={() => loopAction("pause")}><Icon name="pause" size={14} /> pause</button>
                  ) : (
                    <button className="btn small primary" onClick={() => loopAction("resume")}><Icon name="play" size={14} /> resume</button>
                  )}
                  {detail.loop.status === "active" && <button className="btn small" onClick={() => loopAction("run")}><Icon name="up" size={14} /> run now</button>}
                </div>
              </div>
            )}
            {detail.services && detail.services.length > 0 && (
              <div className="aside-card">
                <div className="aside-title"><Icon name="globe" size={14} /> Services <span className="sub">{detail.services.filter((s) => s.status === "running").length} running</span></div>
                {detail.services.map((s) => (
                  <ServiceRow key={s.name} s={s} sessionId={id} onChange={() => load(true)} toast={toast} onLogs={(text) => setCommandResult({ line: `service ${s.name} · log`, text })} />
                ))}
              </div>
            )}
            {detail.subagents && detail.subagents.length > 0 && (
              <div className="aside-card">
                <div className="aside-title"><Icon name="spawn" size={14} /> Subagents <span className="sub">{detail.subagents.filter((s) => s.running).length} working</span></div>
                {detail.subagents.map((s) => (
                  <button key={s.session_id} className={`aside-row link sub-${s.status}`} onClick={() => onOpen?.(s.session_id)} title={`${s.model} · ${s.session_id}`}>
                    {s.running ? <span className="live-dot" /> : <span className={"dot " + s.status} />}
                    <span className="grow name">{s.name || s.session_id}</span>
                    <span className="sub">{s.running ? "working" : s.status === "failed" ? "failed" : s.kept ? "kept" : "done"}</span>
                  </button>
                ))}
              </div>
            )}
          </aside>
        )}
        {detail && asideOpen && <PaneHandle side="left" onDrag={(dx) => setAsideWidth(asideWidth + dx)} />}
        <div className="chat-main">
          <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
            <div className="timeline">
              {turns.map((t, i) => (
                <Safe key={t.key}>
                  <SessionContext.Provider value={sessionCtx}>
                    <TurnView turn={t} live={busy && i === turns.length - 1} onTurnAction={turnAction} />
                  </SessionContext.Provider>
                </Safe>
              ))}
              {detail?.pending && <QuestionCard key={detail.pending.questions.map((q) => q.question).join("|")} sessionId={id} questions={detail.pending.questions} onDone={() => load()} toast={toast} />}
            </div>
          </div>
          {!atBottom && (
            <button className="jump-down" onClick={jumpToBottom} aria-label="scroll to the latest message" title="To the latest message">
              <Icon name="down" size={18} />
            </button>
          )}
          <div className="composer">
            {pending.length > 0 && (
              <div className="attachments" aria-label="attachments">
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
            <div className="composer-box">
              <textarea
                ref={textarea}
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  const el = e.target;
                  el.style.height = "auto";
                  el.style.height = `${Math.min(el.scrollHeight, Math.max(120, window.innerHeight * 0.4))}px`;
                }}
                placeholder={status === "running" ? "Steer the agent (applies before its next step)" : "Ask anything"}
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
              <div className="composer-row">
                <input ref={fileInput} type="file" multiple hidden onChange={(e) => setPending((p) => [...p, ...Array.from(e.target.files ?? [])])} />
                <button className="roundbtn" title="attach files" onClick={() => fileInput.current?.click()} aria-label="attach">
                  <Icon name="plus" />
                </button>
                {asr?.configured && <MicButton onRecording={onRecording} busy={transcribing} />}
                <button className="chip" onClick={openPicker} title="model for this session">
                  <Icon name="model" /> {shortModel(detail?.model)}
                </button>
                <span className="grow" />
                {status === "running" && (
                  <button className="roundbtn stop" onClick={stop} aria-label="stop" title="stop the run">
                    <Icon name="stop" />
                  </button>
                )}
                {(status !== "running" || draft.trim() || pending.length > 0) && (
                  <button className="roundbtn send" onClick={send} disabled={sending || (!draft.trim() && pending.length === 0)} aria-label={status === "running" ? "steer" : "send"} title={status === "running" ? "send as a steer" : "send"}>
                    <Icon name="up" />
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
        {view !== "chat" && detail && <PaneHandle side="right" onDrag={(dx) => setPaneWidth(paneWidth - dx)} />}
        {view !== "chat" && detail && (
          <aside className="side-pane">
            <div className="side-head">
              <span className="side-title"><Icon name={view === "files" ? "folder" : "plug"} size={14} /> {view === "files" ? "Workspace files" : "MCP servers"}</span>
              <button className="iconbtn small" onClick={() => setView("chat")} aria-label="close" title="Close">
                <Icon name="close" size={16} />
              </button>
            </div>
            <div className="side-body">{view === "files" ? <Files base={sessionBase(id)} uploadUrl={`${sessionBase(id)}/files/upload`} onPreview={setPreview} toast={toast} /> : <McpPanel sessionId={id} toast={toast} />}</div>
          </aside>
        )}
      </div>

      {commandResult && (
        <div className="sheet-backdrop" onClick={() => setCommandResult(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <h3 className="mono">{commandResult.line}</h3>
            <div className="sheet-body">
              <pre className="diff" style={{ whiteSpace: "pre-wrap" }}>{commandResult.text}</pre>
            </div>
          </div>
        </div>
      )}

      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}

      {picker && (
        <div className="sheet-backdrop" onClick={() => setPicker(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <h3>Model for this session</h3>
            <div className="sheet-body">
              <button className="menu-item" onClick={() => chooseModel({ clear: true })}>
                Global default <span className="sub">{picker.global}</span>
              </button>
              {Object.entries(picker.presets).map(([pid, p]) => (
                <button key={pid} className="menu-item" onClick={() => chooseModel({ preset: pid })}>
                  {p.label || p.model} <span className="sub">{p.provider}/{p.model}</span>
                </button>
              ))}
              <div className="sub" style={{ margin: "10px 0 4px" }}>Or a specific model: provider/model-id</div>
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
                  Use
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function LoopPanel({ sessionId, loop, onChange, toast }: { sessionId: string; loop: LoopView | null; onChange: () => void; toast: (t: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(loop?.instruction ?? "");
  const [mode, setMode] = useState<"interval" | "dynamic">(loop?.mode ?? "interval");
  const [minutes, setMinutes] = useState(String(loop?.interval_seconds ? Math.round(loop.interval_seconds / 60) : 10));
  const [maxRuns, setMaxRuns] = useState(loop?.max_runs ? String(loop.max_runs) : "");
  async function action(a: string) {
    try {
      await api.post(`/api/sessions/${sessionId}/loop/action`, { action: a });
      toast(`loop: ${a}`);
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function save() {
    if (!text.trim()) return;
    try {
      await api.post(`/api/sessions/${sessionId}/loop`, { instruction: text.trim(), mode, interval_minutes: mode === "interval" ? Math.max(1, Number(minutes) || 10) : null, max_runs: maxRuns.trim() ? Math.max(1, Number(maxRuns) || 1) : null, start_now: true });
      toast(loop ? "loop updated; an iteration starts now" : "loop started");
      setEditing(false);
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  if (!loop && !editing) {
    return (
      <div className="btnrow" style={{ marginTop: 0 }}>
        <span className="sub">No loop.</span>
        <button className="btn small" onClick={() => setEditing(true)}>+ Loop</button>
      </div>
    );
  }
  return (
    <div className="loop-panel">
      {loop && !editing && (
        <>
          <div className="sub loop-instruction">{loop.instruction}</div>
          <div className="sub">
            {loop.status === "active" && loop.next_run_at ? `next wake-up ${new Date(loop.next_run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : loop.status === "active" ? "next wake-up: when the agent asks (LoopNext)" : `${loop.status}${loop.stop_reason || loop.pause_note ? `: ${loop.stop_reason || loop.pause_note}` : ""}`}
            {loop.last_reason ? ` · last reason: ${loop.last_reason}` : ""}
          </div>
          <div className="btnrow" style={{ marginTop: 6 }}>
            {loop.status === "active" ? <button className="btn small" onClick={() => action("pause")}>pause</button> : <button className="btn small primary" onClick={() => action("resume")}>resume</button>}
            {loop.status === "active" && <button className="btn small" onClick={() => action("run")}>run now</button>}
            <button className="btn small" onClick={() => setEditing(true)}>edit</button>
            {loop.status !== "stopped" && loop.status !== "done" && <button className="btn small danger" onClick={() => action("stop")}>stop</button>}
            <button className="btn small danger" onClick={async () => { if (await confirmAsync("Remove the loop from this session?")) action("remove"); }}>remove</button>
          </div>
        </>
      )}
      {editing && (
        <>
          <textarea className="field" rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="what each wake-up is for" />
          <div className="composer-row">
            <select className="field" value={mode} onChange={(e) => setMode(e.target.value as "interval" | "dynamic")}>
              <option value="interval">every N min</option>
              <option value="dynamic">self-paced</option>
            </select>
            {mode === "interval" && <input className="field" type="number" min={1} style={{ maxWidth: 110 }} value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label="minutes" />}
            <input className="field" type="number" min={1} style={{ maxWidth: 130 }} placeholder="max runs" value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} aria-label="max runs" />
          </div>
          <div className="btnrow" style={{ marginTop: 6 }}>
            <button className="btn small primary" onClick={save} disabled={!text.trim()}>{loop ? "save & run" : "start loop"}</button>
            <button className="btn small" onClick={() => setEditing(false)}>cancel</button>
          </div>
        </>
      )}
    </div>
  );
}

// ── side panels: widths, usage, cron, attachments ─────────────────────────────────────────

/** A pane width the operator dragged, remembered per browser. */
function usePaneWidth(key: string, initial: number, min: number, max: number): [number, (w: number) => void] {
  const storageKey = `daedalus.width.${key}`;
  const [width, setWidth] = useState(() => {
    try {
      const v = Number(localStorage.getItem(storageKey));
      return v >= min && v <= max ? v : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (w: number) => {
      const clamped = Math.round(Math.min(max, Math.max(min, w)));
      setWidth(clamped);
      try {
        localStorage.setItem(storageKey, String(clamped));
      } catch {
        /* private mode */
      }
    },
    [storageKey, min, max],
  );
  return [width, set];
}

/** The strip between two panes: drag it to resize (pointer events, so mouse and touch alike). */
function PaneHandle({ side, onDrag }: { side: "left" | "right"; onDrag: (dx: number) => void }) {
  const last = useRef<number | null>(null);
  return (
    <div
      className={`pane-handle wide-only ${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label="resize"
      onPointerDown={(e) => {
        last.current = e.clientX;
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        document.body.classList.add("resizing");
      }}
      onPointerMove={(e) => {
        if (last.current === null) return;
        const dx = e.clientX - last.current;
        last.current = e.clientX;
        if (dx) onDrag(dx);
      }}
      onPointerUp={(e) => {
        last.current = null;
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
        document.body.classList.remove("resizing");
      }}
      onPointerCancel={() => {
        last.current = null;
        document.body.classList.remove("resizing");
      }}
    />
  );
}

const SUBSCRIPTION_LABEL: Record<string, string> = { codex: "Codex · ChatGPT", claude: "Claude · Max", grok: "Grok · SuperGrok" };

function resetIn(at: number | string | null | undefined): string {
  if (!at) return "";
  const d = typeof at === "number" ? new Date(at * 1000) : new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  const mins = Math.max(0, Math.round((d.getTime() - Date.now()) / 60000));
  return mins < 60 ? `${mins}m` : mins < 48 * 60 ? `${Math.round(mins / 60)}h` : `${Math.round(mins / 1440)}d`;
}

/** What the session's provider has left: a subscription's windows, or the day's metered spend and balance. */
function ProviderUsageCard({ provider, usage }: { provider: string; usage: ProviderUsage | null }) {
  const sub = usage?.subscription;
  const today = usage?.today ?? {};
  return (
    <div className="aside-card">
      <div className="aside-title">
        <Icon name="chart" size={14} /> {SUBSCRIPTION_LABEL[provider] ?? provider}
        {sub?.plan && <span className="badge">{sub.plan}</span>}
        {sub?.limit_reached && <span className="badge" style={{ color: "var(--bad)" }}>limit</span>}
      </div>
      {!usage && <div className="sub">…</div>}
      {sub && !sub.logged_in && <div className="sub">not logged in on the host</div>}
      {sub?.error && <div className="sub" style={{ color: "var(--bad)" }}>{sub.error}</div>}
      {(sub?.windows ?? []).map((w) => (
        <div key={w.name} className="quota">
          <div className="sub quota-line">
            <span className="grow">{w.name}</span>
            <span>{Math.round(w.used_percent)}%{w.resets_at ? ` · resets in ${resetIn(w.resets_at)}` : ""}</span>
          </div>
          <div className="quota-bar">
            <i style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%`, background: w.used_percent >= 100 ? "var(--bad)" : w.used_percent >= 80 ? "var(--warn)" : "var(--ok)" }} />
          </div>
        </div>
      ))}
      {usage && (
        <div className="sub" style={{ marginTop: sub ? 6 : 0 }}>
          today: {fmtInt(today.calls)} calls · {fmtTok(today.input_tokens)}↑ {fmtTok(today.output_tokens)}↓
          {!sub && ` · ${fmtUsd(today.cost_usd)}`}
          {usage.balance !== undefined && usage.balance !== null && ` · balance ${fmtUsd(usage.balance)}`}
        </div>
      )}
    </div>
  );
}

/** One scheduled task of the session: cadence, next run, and the two things one does with it. */
function ScheduleRow({ sc, sessionId, onAction }: { sc: Schedule; sessionId: string; onAction: (sc: Schedule, action: "run" | "delete") => void }) {
  const where = sc.kind === "message" ? "reminder" : sc.kind === "lazy" ? "lazy note" : sc.run_in === "self" || sc.target_session === sessionId ? "runs here" : "own session";
  return (
    <div className="sched-row" title={sc.prompt}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="sched-name">
          {!sc.enabled && <span title="switched off">⏸ </span>}
          {sc.name} <span className="badge">{where}</span>
          {sc.active_session_id === sessionId && <span className="live-dot" title="running now" />}
        </div>
        <div className="sub sched-when">
          {sc.cron ? `cron ${sc.cron}` : `once ${sc.run_at ? new Date(sc.run_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : ""}`}
          {sc.next_run_at && sc.enabled ? ` · next ${new Date(sc.next_run_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}` : ""}
          {sc.last_run_at ? ` · last ${timeAgo(sc.last_run_at)}` : ""}
          {sc.failure_count > 0 && <span style={{ color: "var(--bad)" }}> · {sc.failure_count} failed</span>}
        </div>
      </div>
      <button className="iconbtn small" onClick={() => onAction(sc, "run")} title="Run now" aria-label="run now"><Icon name="play" size={14} /></button>
      <button className="iconbtn small" onClick={() => onAction(sc, "delete")} title="Delete" aria-label="delete"><Icon name="trash" size={14} /></button>
    </div>
  );
}

/** A file waiting in the composer: a thumbnail for images, a glyph and the size for the rest. */
function AttachmentCard({ file, onOpen, onRemove }: { file: File; onOpen: () => void; onRemove: () => void }) {
  const isImage = file.type.startsWith("image/") || previewKind(file.name) === "image";
  const url = useMemo(() => (isImage ? URL.createObjectURL(file) : null), [file, isImage]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return (
    <div className={`attachment ${isImage ? "image" : ""}`}>
      <button type="button" className="attachment-open" onClick={onOpen} title={canPreview(file.name) ? "preview" : file.name}>
        {url ? <img src={url} alt={file.name} /> : <span className="attachment-glyph" aria-hidden>{fileGlyph(file.name)}</span>}
        <span className="attachment-meta">
          <span className="attachment-name">{file.name}</span>
          <span className="sub">{fmtBytes(file.size)}</span>
        </span>
      </button>
      <button type="button" className="attachment-x" onClick={onRemove} aria-label={`remove ${file.name}`} title="Remove">
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
      <button className="chip recording" onClick={stop} title="stop and transcribe" aria-label="stop recording">
        <span className="rec-dot" /> {seconds}s · stop
      </button>
    );
  }
  return (
    <button className="roundbtn" onClick={start} disabled={!supported || busy} title={!supported ? "no microphone access in this browser" : busy ? "transcribing…" : "record a voice note (transcribed to text)"} aria-label="record a voice note">
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
    return this.state.failed ? <div className="note">this message could not be rendered</div> : this.props.children;
  }
}

function shortModel(name?: string, max = 18): string {
  if (!name) return "model";
  const short = name.split("/").pop()!.replace(/^deepseek-/, "").replace(/\s*\(.*\)$/, "");
  return short.length > max ? `${short.slice(0, max - 1)}…` : short;
}

// ── turns ─────────────────────────────────────────────────────────────────────────────────

function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 ? ` ${s % 60}s` : ""}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function stepCount(items: Activity[]): number {
  return items.filter((a) => a.kind === "tool").length;
}

const TurnView = memo(function TurnView({ turn, live, onTurnAction }: { turn: Turn; live: boolean; onTurnAction?: (kind: "revert" | "fork", seq: number) => void }) {
  const [open, setOpen] = useState(live);
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
  return (
    <div className="turn">
      {turn.user && turn.user.origin && turn.user.origin !== "operator" && (
        <div className="sub" style={{ textAlign: "right", marginBottom: 2 }}>
          <span className="badge">{turn.user.origin}</span>
        </div>
      )}
      {turn.user && <Md className="msg user" text={turn.user.text} />}
      {turn.user && turn.user.seq && onTurnAction && !live && (
        <div className="turn-actions">
          <button className="btn small" onClick={() => onTurnAction("revert", turn.user!.seq!)} title="undo this turn and everything after it (files too)">
            ↶ revert here
          </button>
          <button className="btn small" onClick={() => onTurnAction("fork", turn.user!.seq!)} title="start a new session from this point">
            ⑂ fork
          </button>
        </div>
      )}
      {hasWork && (
        <button className="thinking-head" onClick={() => setOpen((o) => !o)}>
          <span className={`dots ${live ? "on" : ""}`}>
            <i />
            <i />
            <i />
          </span>
          {live ? (turn.pendingTools > 0 ? "Working for" : "Thinking for") : "Worked for"} {fmtDuration(elapsed)}
          {steps > 0 && <span className="steps">· {steps} step{steps === 1 ? "" : "s"}</span>}
          <span className={`chev ${open ? "down" : ""}`}>›</span>
        </button>
      )}
      {open && (
        <div className="activity">
          <ActivityList items={turn.activity} compact={false} />
          {live && !turn.answer && turn.pendingTools === 0 && turn.activity.length > 0 && <div className="working">working…</div>}
        </div>
      )}
      {turn.answer && <Md className={`answer ${live ? "streaming" : ""}`} text={turn.answer} />}
    </div>
  );
});

function SummaryBlock({ message }: { message: MessageView }) {
  const [open, setOpen] = useState(false);
  const meta = message.compaction;
  return (
    <div className="summary">
      <button className="summary-head" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" /> Context summary
        {meta ? ` · ${meta.messages} messages compacted (${meta.reason})` : ""}
        <span className="chev">{open ? "⌄" : "›"}</span>
      </button>
      {open && <Md className="summary-body" text={message.text} />}
    </div>
  );
}

/** Verb + noun for a tool, Grok-style ("Ran command", "Read file", "Editing 2 files"). */
function describe(t: ToolItem): { verb: string; noun: string; detail: string; icon: IconName } {
  const a = t.args;
  const str = (k: string) => (typeof a[k] === "string" ? (a[k] as string) : a[k] === undefined ? "" : JSON.stringify(a[k]));
  const base = (p: string) => p.split("/").filter(Boolean).pop() ?? p;
  const r = t.running;
  switch (t.name) {
    case "Exec":
      return { verb: r ? "Running command" : "Ran command", noun: "command", detail: str("command").split("\n")[0], icon: "terminal" };
    case "Read":
      return { verb: r ? "Reading file" : "Read file", noun: "file", detail: base(str("path")), icon: "file" };
    case "Write":
      return { verb: r ? "Writing file" : "Wrote file", noun: "file", detail: base(str("path")), icon: "pen" };
    case "Edit":
      return { verb: r ? "Editing file" : "Edited file", noun: "file", detail: base(str("path")), icon: "pen" };
    case "Find":
    case "Search":
      return { verb: r ? "Searching" : "Searched", noun: "search", detail: str("pattern") || str("query"), icon: "search" };
    case "WebSearch":
      return { verb: r ? "Searching the web" : "Searched the web", noun: "search", detail: str("query"), icon: "search" };
    case "WebFetch":
      return { verb: "Browsing", noun: "page", detail: str("url").replace(/^https?:\/\//, "").slice(0, 60), icon: "globe" };
    case "SendFile":
      return { verb: r ? "Sending file" : "Sent file", noun: "file", detail: base(str("path")), icon: "attach" };
    case "ImageView":
      return { verb: r ? "Viewing image" : "Viewed image", noun: "image", detail: `${base(str("path"))}${str("task") ? " · " + str("task").slice(0, 60) : ""}`, icon: "image" };
    case "AskUser":
      return { verb: "Asked you", noun: "question", detail: "", icon: "question" };
    case "Skill":
      return { verb: r ? "Loading skill" : "Loaded skill", noun: "skill", detail: str("skill") || str("name"), icon: "skill" };
    case "Remember":
    case "Recall":
    case "Forget":
      return { verb: t.name === "Recall" ? "Recalled" : t.name === "Forget" ? "Forgot" : "Remembered", noun: "memory", detail: str("query") || str("text").slice(0, 60), icon: "bulb" };
    case "Verify":
      return { verb: r ? "Verifying" : t.error ? "Verification failed" : "Verified", noun: "check", detail: str("criterion"), icon: "wrench" };
    case "SubAgent":
      return { verb: r ? "Starting subagent" : "Started subagent", noun: "subagent", detail: str("name") || str("task").split("\n")[0].slice(0, 60), icon: "spawn" };
    case "SubAgentList":
      return { verb: "Listed subagents", noun: "list", detail: "", icon: "spawn" };
    case "SpawnAgent":
      return { verb: r ? "Creating agent" : "Created agent", noun: "agent", detail: str("title"), icon: "spawn" };
    case "AskPeer":
      return { verb: r ? "Asking peer" : "Asked peer", noun: "peer", detail: str("name"), icon: "question" };
    case "StaySilent":
      return { verb: "Stayed silent", noun: "note", detail: str("note").slice(0, 60), icon: "dot" };
    case "HistorySearch":
      return { verb: r ? "Searching history" : "Searched history", noun: "search", detail: str("query"), icon: "search" };
    case "HistoryExpand":
      return { verb: "Read earlier turns", noun: "range", detail: `seq ${str("from_seq")}–${str("to_seq")}`, icon: "file" };
    default: {
      if (t.name.startsWith("Board")) {
        const what = t.name.replace(/^Board/, "").toLowerCase();
        const verb = what === "add" ? (r ? "adding" : "added") : what === "update" ? (r ? "updating" : "updated") : what === "get" ? "read" : "listed";
        return { verb: `Board: ${verb}`, noun: "task", detail: str("title") || str("task_id") || str("id"), icon: "skill" };
      }
      if (t.name.startsWith("Self")) return { verb: t.name.replace(/^Self/, "Self: "), noun: "step", detail: str("branch") || str("title") || str("repo"), icon: "wrench" };
      if (t.name.startsWith("Schedule")) return { verb: t.name.replace(/^Schedule/, "Schedule: "), noun: "task", detail: str("name") || str("schedule_id"), icon: "clock" };
      if (t.name.startsWith("Mcp")) {
        // Mcp_Postingboard_read_thread → "postingboard · read thread", with the first string argument as the detail.
        const parts = t.name.replace(/^Mcp_?/, "").split("_");
        const server = (parts.shift() ?? "").toLowerCase();
        const tool = parts.join(" ").replace(/_/g, " ");
        const first = Object.values(a).find((v) => typeof v === "string") as string | undefined;
        return { verb: `${server} · ${tool || "call"}`, noun: "call", detail: (first ?? "").slice(0, 60), icon: "plug" };
      }
      return { verb: t.name, noun: "call", detail: Object.keys(a).length ? JSON.stringify(a).slice(0, 60) : "", icon: "dot" };
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
        <span className="verb">
          {groupVerb(family, running)} {group.length} {plural(d.noun)}
        </span>
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </div>
      {open && group.map((g) => <ToolRow key={g.id} item={g} nested />)}
    </div>
  );
}

function plural(noun: string): string {
  const irregular: Record<string, string> = { search: "searches", memory: "memories", query: "queries", check: "checks" };
  return irregular[noun] ?? `${noun}s`;
}

function groupVerb(name: string, running: boolean): string {
  const map: Record<string, [string, string]> = {
    Exec: ["Running", "Ran"],
    Read: ["Reading", "Read"],
    Write: ["Writing", "Wrote"],
    Edit: ["Editing", "Edited"],
    Find: ["Running", "Ran"],
    Search: ["Running", "Ran"],
    WebSearch: ["Running", "Ran"],
    WebFetch: ["Browsing", "Browsed"],
    SendFile: ["Sending", "Sent"],
  };
  const [a, b] = map[name] ?? ["Calling", "Called"];
  return running ? a : b;
}

function SummaryGroup({ group }: { group: SummaryItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" />
        <span className="verb">Context compacted</span>
        <span className="detail">{group.length} older turns folded into summaries</span>
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
      <div className="act" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" />
        <span className="verb">Context compacted</span>
        <span className="detail">{reason !== "auto" ? `${reason} · ` : ""}{text.replace(/\s+/g, " ").slice(0, 80)}</span>
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
      <div className="act" onClick={() => setOpen((o) => !o)}>
        <Icon name="bulb" />
        <span className="verb">Reasoning</span>
        <span className="detail">{text.replace(/\s+/g, " ").slice(0, 80)}</span>
        <span className={`chev ${open ? "down" : ""}`}>›</span>
      </div>
      {open && <div className="thought">{text}</div>}
    </div>
  );
}

function ToolRow({ item, nested }: { item: ToolItem; nested?: boolean }) {
  const [open, setOpen] = useState(false);
  const d = describe(item);
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

const SessionContext = createContext<{ id: string; workspace: string; preview: (src: PreviewSource) => void }>({ id: "", workspace: "", preview: () => undefined });

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
      <button type="button" className="file-chip" onClick={() => preview(src)} title={canPreview(name) ? "preview" : "download"}>
        <span aria-hidden>{fileGlyph(name)}</span> {name}
      </button>
    </div>
  );
}

function ToolResultText({ item }: { item: ToolItem }) {
  const { id: sessionId } = useContext(SessionContext);
  const [full, setFull] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const text = full ?? item.result ?? "";
  const clipped = full === null && item.length !== undefined && item.length > text.length;
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
  return (
    <>
      <pre className={`result ${item.error ? "error" : ""} ${full !== null ? "full" : ""}`}>{text}</pre>
      {clipped && (
        <button type="button" className="btn small" onClick={loadAll} disabled={loading}>
          {loading ? "Loading…" : `Show all (${item.length!.toLocaleString()} characters)`}
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
            <input className="field" style={{ marginTop: 6 }} placeholder="your answer" value={answers[qi].custom} onChange={(e) => setAnswers((p) => p.map((a, i) => (i === qi ? { ...a, custom: e.target.value } : a)))} />
          )}
        </div>
      ))}
      <div className="btnrow">
        <button className="btn primary" disabled={!complete} onClick={submit}>
          Answer
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
      toast?.(`${r.files.length} file${r.files.length === 1 ? "" : "s"} added`);
      setGen((g) => g + 1);
    } catch (e) {
      toast?.(errorText(e));
    } finally {
      setUploading(false);
      if (upload.current) upload.current.value = "";
    }
  }
  if (error) return <div className="empty">could not read {path || "the workspace"}: {error}</div>;
  if (!data) return <div className="empty">Loading…</div>;
  const crumbs = path ? path.split("/") : [];
  const all: any[] = data.kind === "dir" ? data.entries : [];
  const entries = all.filter((e) => showHidden || !e.name.startsWith("."));
  const hidden = all.length - all.filter((e) => !e.name.startsWith(".")).length;
  const token = sessionStorage.getItem("daedalus_token");
  const download = `${base}/download?path=${encodeURIComponent(path)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  return (
    <div className="files">
      <div className="crumbs">
        <button className="crumb" onClick={() => setPath("")}>workspace</button>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="sub"> / </span>
            <button className="crumb" onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>{c}</button>
          </span>
        ))}
        {data.kind !== "dir" && (
          <a className="btn small" style={{ marginLeft: "auto" }} href={download} target="_blank" rel="noreferrer">
            download{data.size ? ` · ${fmtBytes(data.size)}` : ""}
          </a>
        )}
        {data.kind === "dir" && uploadUrl && (
          <>
            <input ref={upload} type="file" multiple hidden onChange={(e) => sendFiles(e.target.files)} />
            <button className="btn small" style={{ marginLeft: "auto" }} disabled={uploading} onClick={() => upload.current?.click()} title="Upload files into this folder">
              <Icon name="up" size={14} /> {uploading ? "uploading…" : "upload"}
            </button>
          </>
        )}
      </div>
      {data.kind === "dir" && entries.length === 0 && <div className="empty">empty</div>}
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
            {e.mtime && <span className="sub">{new Date(e.mtime * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</span>}
          </button>
        ))}
      {data.kind === "dir" && hidden > 0 && (
        <button className="btn small" onClick={() => setShowHidden((h) => !h)}>
          {showHidden ? "hide" : "show"} {hidden} hidden {hidden === 1 ? "entry" : "entries"}
        </button>
      )}
      {data.kind === "file" && data.truncated && <div className="sub" style={{ margin: "6px 0" }}>showing the first 512 KB; download for the whole file</div>}
      {data.kind !== "dir" && canPreview(path) && (
        <button className="btn small" style={{ marginBottom: 8 }} onClick={() => onPreview({ base, path })}><Icon name="eye" size={14} /> preview</button>
      )}
      {data.kind === "file" && <pre className="filetext">{data.content}</pre>}
      {data.kind === "binary" && previewKind(path) === "image" && <AuthImg className="preview" src={{ base, path }} alt={path} onClick={() => onPreview({ base, path })} />}
      {data.kind === "binary" && previewKind(path) !== "image" && <div className="empty">binary file, {fmtBytes(data.size)}</div>}
    </div>
  );
}

type McpServer = { name: string; description: string; connected: boolean; error: string | null; tools: string[] };

function McpPanel({ sessionId, toast }: { sessionId: string; toast: (t: string) => void }) {
  const [data, setData] = useState<{ enabled: string[]; servers: McpServer[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(() => {
    api.get<{ enabled: string[]; servers: McpServer[] }>(`/api/sessions/${sessionId}/mcp`).then(setData).catch((e) => toast(errorText(e)));
  }, [sessionId, toast]);
  useEffect(load, [load]);
  async function toggle(server: string, enabled: boolean) {
    setBusy(server);
    try {
      setData(await api.put(`/api/sessions/${sessionId}/mcp`, { server, enabled }));
      toast(`${server}: ${enabled ? "enabled" : "disabled"}`);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(null);
    }
  }
  if (!data) return <div className="empty">…</div>;
  if (data.servers.length === 0) return <div className="empty">No MCP servers configured. Add them under [mcp.servers.&lt;name&gt;] in config.toml.</div>;
  return (
    <div>
      <div className="sub" style={{ marginBottom: 8 }}>MCP servers for this session (off by default; the agent can toggle them too)</div>
      {data.servers.map((s) => {
        const on = data.enabled.includes(s.name);
        return (
          <div key={s.name} className="card">
            <div className="row">
              <div className="grow">
                <div className="title">{s.name}</div>
                <div className="sub">{s.description || "no description"}{s.error && ` · error: ${s.error}`}</div>
                {s.tools.length > 0 && <div className="sub">{s.tools.join(", ")}</div>}
              </div>
              <button className={`btn small ${on ? "primary" : ""}`} disabled={busy === s.name} onClick={() => toggle(s.name, !on)}>
                {on ? "on" : "off"}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
