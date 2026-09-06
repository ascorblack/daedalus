import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { api, MessageView, Question, SessionDetail } from "../api";
import { Status, fmtInt, fmtUsd } from "../components";
import { codeBlock, renderMarkdown } from "../md";

// ── data shapes ───────────────────────────────────────────────────────────────────────────

type LiveTool = { id: string; name: string; args: string; result?: string; error?: boolean };
type LiveState = { text: string; thinking: string; tools: LiveTool[]; startedAt: number | null };
const EMPTY_LIVE: LiveState = { text: "", thinking: "", tools: [], startedAt: null };

type ToolItem = { kind: "tool"; id: string; name: string; args: Record<string, unknown>; result?: string; error?: boolean; running: boolean };
type NoteItem = { kind: "note"; text: string };
type ThinkItem = { kind: "thinking"; text: string };
type Activity = ToolItem | NoteItem | ThinkItem;

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
  const results = new Map<string, { content: string; is_error: boolean }>();
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
    if (m.role === "tool") return;
    if (m.summary) {
      turns.push({ key: `s${i}`, summary: m, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0 });
      current = null;
      return;
    }
    if (m.role === "user") {
      current = open(`u${i}`, at);
      current.user = m;
      return;
    }
    if (m.role === "system") return;
    if (!current) current = open(`a${i}`, at);
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
      current.activity.push({ kind: "tool", id: c.id, name: c.name, args: c.arguments, result: content, error: r?.is_error ?? liveResult?.error, running });
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
    if (live.thinking) t.activity.push({ kind: "thinking", text: live.thinking });
    for (const lt of fresh) {
      const running = lt.result === undefined;
      if (running) t.pendingTools++;
      t.activity.push({ kind: "tool", id: lt.id, name: lt.name, args: parseArgs(lt.args), result: lt.result, error: lt.error, running });
    }
    if (live.text) t.answer = live.text;
    t.endedAt = Date.now();
  }
  return turns;
}

// ── screen ────────────────────────────────────────────────────────────────────────────────

export function SessionScreen({ id, onBack, toast }: { id: string; onBack: () => void; toast: (t: string) => void }) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [view, setView] = useState<"chat" | "files" | "mcp">("chat");
  const [menu, setMenu] = useState(false);
  const [editingTitle, setEditingTitle] = useState<string | null>(null);
  const [sheet, setSheet] = useState<Turn | null>(null);
  const [tick, setTick] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const stick = useRef(true);
  const userScrolling = useRef(false);

  const load = useCallback(async () => {
    try {
      setDetail(await api.get<SessionDetail>(`/api/sessions/${id}`));
    } catch (e) {
      toast((e as Error).message);
    }
  }, [id, toast]);

  useEffect(() => {
    load();
  }, [load]);

  const status = (detail?.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";

  // While a run is active, re-read the transcript even if the event stream stalls; tick the timer.
  useEffect(() => {
    if (!busy) {
      setLive(EMPTY_LIVE);
      return;
    }
    const t = setInterval(load, 3000);
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
    (async () => {
      const res = await fetch(url, { headers });
      if (!res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
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
          handle(event, JSON.parse(data));
        }
      }
    })().catch(() => undefined);
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
        load().then(() => setLive((s) => ({ ...s, text: "", thinking: "" })));
      } else if (event === "state_changed" || event === "tool_call_pending" || event === "run_settled" || event === "compaction_completed") load();
    }
    return () => {
      stop = true;
    };
  }, [id, load]);

  const turns = useMemo(() => buildTurns(detail?.messages ?? [], live, busy), [detail, live, busy]);

  // Follow the newest content only while the reader is at the bottom and not scrolling by hand.
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current && !userScrolling.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

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
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  }

  async function send() {
    const text = draft.trim();
    const files = pending;
    if (sending || (!text && files.length === 0)) return;
    setSending(true);
    setDraft("");
    setPending([]);
    if (fileInput.current) fileInput.current.value = "";
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
      load();
    } catch (e) {
      setDraft(text);
      setPending(files);
      toast((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  async function stop() {
    await api.post(`/api/sessions/${id}/stop`);
    toast("stopping");
  }

  async function rename(title: string) {
    setEditingTitle(null);
    if (!title.trim() || title.trim() === detail?.title) return;
    try {
      await api.patch(`/api/sessions/${id}`, { title: title.trim() });
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function compact() {
    setMenu(false);
    if (busy) {
      toast("stop the run first");
      return;
    }
    if (!window.confirm("Replace the whole history with a summary? The agent keeps only the summary.")) return;
    toast("compacting…");
    try {
      await api.post(`/api/sessions/${id}/compact`, { instructions: "" });
      await load();
      toast("compacted");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function remove() {
    setMenu(false);
    if (!window.confirm("Delete this session, its topic and its workspace?")) return;
    try {
      await api.delete(`/api/sessions/${id}`);
      onBack();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  void tick;
  return (
    <div className="chat">
      <div className="chat-head">
        <button className="iconbtn" onClick={onBack} aria-label="back">
          <Icon name="back" />
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
          <div className="sub">
            {busy ? <span className="live-dot" /> : null}
            {detail?.model} · {fmtInt(detail?.usage.i)}↑ {fmtInt(detail?.usage.o)}↓ · {fmtUsd(detail?.usage.usd)}
          </div>
        </div>
        <button className="iconbtn" onClick={() => setMenu((m) => !m)} aria-label="menu">
          <Icon name="more" />
        </button>
        {menu && (
          <div className="menu" onClick={() => setMenu(false)}>
            <button onClick={() => setView(view === "files" ? "chat" : "files")}>{view === "files" ? "Back to chat" : "Files"}</button>
            <button onClick={() => setView(view === "mcp" ? "chat" : "mcp")}>{view === "mcp" ? "Back to chat" : "MCP servers"}</button>
            <button onClick={() => setEditingTitle(detail?.title ?? "")}>Rename</button>
            <button onClick={compact}>Compact history</button>
            <button className="danger" onClick={remove}>
              Delete session
            </button>
          </div>
        )}
      </div>

      <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
        {view === "mcp" && detail && <McpPanel sessionId={id} toast={toast} />}
        {view === "files" && detail && <Files sessionId={id} />}
        {view === "chat" && (
          <div className="timeline">
            {turns.map((t, i) => (
              <TurnView key={t.key} turn={t} live={busy && i === turns.length - 1} onThoughts={() => setSheet(t)} />
            ))}
            {detail?.pending && <QuestionCard sessionId={id} questions={detail.pending.questions} onDone={load} toast={toast} />}
          </div>
        )}
      </div>

      {view === "chat" && (
        <div className="composer">
          {pending.length > 0 && (
            <div className="attachments">
              {pending.map((f, i) => (
                <span key={i} className="pill">
                  {f.name} ({Math.ceil(f.size / 1024)} KB)
                  <button className="x" onClick={() => setPending((p) => p.filter((_, j) => j !== i))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="composer-box">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={status === "running" ? "Steer the agent (applies before its next step)" : "Ask anything"}
              rows={1}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) {
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
              <span className="chip">
                <Icon name="model" /> {shortModel(detail?.model)}
              </span>
              <span className="grow" />
              {status === "running" ? (
                <button className="roundbtn stop" onClick={stop} aria-label="stop">
                  <Icon name="stop" />
                </button>
              ) : (
                <button className="roundbtn send" onClick={send} disabled={sending || (!draft.trim() && pending.length === 0)} aria-label="send">
                  <Icon name="up" />
                </button>
              )}
            </div>
          </div>
        </div>
      )}
      {sheet && <ThoughtsSheet turn={sheet} onClose={() => setSheet(null)} />}
    </div>
  );
}

function shortModel(name?: string): string {
  if (!name) return "model";
  return name.split("/").pop()!.replace(/^deepseek-/, "").slice(0, 18);
}

// ── turns ─────────────────────────────────────────────────────────────────────────────────

function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 ? ` ${s % 60}s` : ""}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function TurnView({ turn, live, onThoughts }: { turn: Turn; live: boolean; onThoughts: () => void }) {
  if (turn.summary) return <SummaryBlock message={turn.summary} />;
  const hasWork = turn.activity.length > 0 || live;
  const elapsed = (live ? Date.now() : turn.endedAt) - turn.startedAt;
  return (
    <div className="turn">
      {turn.user && <div className="msg user" dangerouslySetInnerHTML={{ __html: renderMarkdown(turn.user.text) }} />}
      {hasWork && (
        <button className="thinking-head" onClick={onThoughts}>
          <span className={`dots ${live ? "on" : ""}`}>
            <i />
            <i />
            <i />
          </span>
          {live ? "Thinking for" : "Thought for"} {fmtDuration(elapsed)}
          <span className="chev">›</span>
        </button>
      )}
      <ActivityList items={turn.activity} compact />
      {turn.answer && <div className={`answer ${live ? "streaming" : ""}`} dangerouslySetInnerHTML={{ __html: renderMarkdown(turn.answer) }} />}
      {live && !turn.answer && turn.pendingTools === 0 && turn.activity.length > 0 && <div className="working">working…</div>}
    </div>
  );
}

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
      {open && <div className="summary-body" dangerouslySetInnerHTML={{ __html: renderMarkdown(message.text) }} />}
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
      return { verb: r ? "Viewing image" : "Viewed image", noun: "image", detail: base(str("path")), icon: "image" };
    case "AskUser":
      return { verb: "Asked you", noun: "question", detail: "", icon: "question" };
    case "Skill":
      return { verb: r ? "Loading skill" : "Loaded skill", noun: "skill", detail: str("skill") || str("name"), icon: "skill" };
    case "SpawnTask":
      return { verb: "Started task", noun: "task", detail: str("title"), icon: "spawn" };
    case "Remember":
    case "Recall":
    case "Forget":
      return { verb: t.name === "Recall" ? "Recalled" : t.name === "Forget" ? "Forgot" : "Remembered", noun: "memory", detail: str("query") || str("text").slice(0, 60), icon: "bulb" };
    default:
      if (t.name.startsWith("Self")) return { verb: t.name.replace(/^Self/, "Self: "), noun: "step", detail: str("branch") || str("title") || str("repo"), icon: "wrench" };
      if (t.name.startsWith("Schedule")) return { verb: t.name.replace(/^Schedule/, "Schedule: "), noun: "task", detail: str("name") || str("schedule_id"), icon: "clock" };
      if (t.name.startsWith("Mcp")) return { verb: t.name.replace(/^Mcp_?/, "MCP "), noun: "call", detail: str("server"), icon: "plug" };
      return { verb: t.name, noun: "call", detail: Object.keys(a).length ? JSON.stringify(a).slice(0, 60) : "", icon: "dot" };
  }
}

function ActivityList({ items, compact }: { items: Activity[]; compact: boolean }) {
  const out: ReactElement[] = [];
  let i = 0;
  while (i < items.length) {
    const it = items[i];
    if (it.kind === "note") {
      out.push(<div key={i} className="note" dangerouslySetInnerHTML={{ __html: renderMarkdown(it.text) }} />);
      i++;
      continue;
    }
    if (it.kind === "thinking") {
      if (!compact) out.push(<div key={i} className="thought">{it.text}</div>);
      i++;
      continue;
    }
    // Group consecutive tools of one family (Read/Read/Read → "Read 3 files").
    const family = it.name;
    let j = i;
    while (j < items.length && items[j].kind === "tool" && (items[j] as ToolItem).name === family) j++;
    const group = items.slice(i, j) as ToolItem[];
    if (group.length > 1) {
      const d = describe(group[group.length - 1]);
      const any = group.some((g) => g.running);
      out.push(
        <div key={i} className="group">
          <div className="row head">
            <Icon name={d.icon} /> {groupVerb(family, any)} {group.length} {d.noun}s
          </div>
          {group.map((g) => (
            <ToolRow key={g.id} item={g} nested />
          ))}
        </div>,
      );
    } else out.push(<ToolRow key={it.id} item={it} />);
    i = j;
  }
  return <>{out}</>;
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

function ToolRow({ item, nested }: { item: ToolItem; nested?: boolean }) {
  const [open, setOpen] = useState(false);
  const d = describe(item);
  const expanded = open || (item.running && item.name === "Exec");
  return (
    <div className={`row-wrap ${nested ? "nested" : ""}`}>
      <div className={`row ${item.error ? "error" : ""} ${item.running ? "running" : ""}`} onClick={() => setOpen((o) => !o)}>
        <Icon name={d.icon} />
        <span className="verb">{d.verb}</span>
        {d.detail && <span className="detail">{d.detail}</span>}
      </div>
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
  else parts.push(<div key="c" dangerouslySetInnerHTML={{ __html: codeBlock(JSON.stringify(a, null, 1), "args") }} />);
  if (item.result !== undefined) parts.push(<pre key="r" className={`result ${item.error ? "error" : ""}`}>{item.result.slice(0, 6000)}</pre>);
  return <div className="toolcard">{parts}</div>;
}

function langOf(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = { py: "python", ts: "typescript", tsx: "tsx", js: "javascript", md: "markdown", sh: "bash", json: "json", toml: "toml", yaml: "yaml", yml: "yaml", html: "html", css: "css" };
  return map[ext] ?? ext;
}

function ThoughtsSheet({ turn, onClose }: { turn: Turn; onClose: () => void }) {
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="grip" />
        <h3>Thoughts</h3>
        <div className="sheet-body">
          <ActivityList items={turn.activity} compact={false} />
          {turn.activity.length === 0 && <div className="empty">Nothing yet.</div>}
        </div>
      </div>
    </div>
  );
}

// ── icons ─────────────────────────────────────────────────────────────────────────────────

type IconName = "back" | "more" | "plus" | "up" | "stop" | "model" | "terminal" | "file" | "pen" | "search" | "globe" | "attach" | "image" | "question" | "skill" | "spawn" | "bulb" | "wrench" | "clock" | "plug" | "dot" | "compact";

const PATHS: Record<IconName, string> = {
  back: "M15 18l-6-6 6-6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  plus: "M12 5v14M5 12h14",
  up: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  model: "M4 12l8-8 8 8-8 8-8-8z",
  terminal: "M4 5h16v14H4zM7 9l3 3-3 3M12 15h5",
  file: "M6 3h8l4 4v14H6zM14 3v4h4",
  pen: "M4 20l4-1 11-11-3-3L5 16zM13 6l3 3",
  search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
  attach: "M21 12l-8 8a5 5 0 0 1-7-7l9-9a3 3 0 0 1 4 4l-9 9a1 1 0 0 1-2-2l8-8",
  image: "M4 5h16v14H4zM8 13l3-3 4 4 2-2 3 3",
  question: "M9 9a3 3 0 1 1 4 3c-1 .5-1 1-1 2M12 17h.01",
  skill: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  spawn: "M12 3v6M12 15v6M3 12h6M15 12h6",
  bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3 11v1h6v-1a6 6 0 0 0-3-11z",
  wrench: "M14 4a5 5 0 0 0 6 6l-9 9-3-3 9-9a5 5 0 0 0-3-3z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
  plug: "M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3",
  dot: "M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  compact: "M4 7h16M4 12h10M4 17h6",
};

function Icon({ name }: { name: IconName }) {
  return (
    <svg className={`ic ic-${name}`} viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
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
      toast((e as Error).message);
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

function Files({ sessionId }: { sessionId: string }) {
  const [path, setPath] = useState("");
  const [data, setData] = useState<any>(null);
  useEffect(() => {
    api.get(`/api/sessions/${sessionId}/files?path=${encodeURIComponent(path)}`).then(setData).catch(() => setData(null));
  }, [sessionId, path]);
  if (!data) return <div className="empty">…</div>;
  const up = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  return (
    <div>
      <div className="sub" style={{ marginBottom: 8 }}>
        /{path}{" "}
        {path && (
          <button className="btn small" onClick={() => setPath(up)}>
            up
          </button>
        )}
      </div>
      {data.kind === "dir" &&
        data.entries.map((e: any) => (
          <div key={e.name} className="card pressable row" onClick={() => setPath(path ? `${path}/${e.name}` : e.name)}>
            <span>{e.dir ? "📁" : "📄"}</span>
            <div className="grow title">{e.name}</div>
            {!e.dir && <span className="sub">{fmtInt(e.size)} B</span>}
          </div>
        ))}
      {data.kind === "file" && <pre className="diff">{data.content}</pre>}
      {data.kind === "binary" && <div className="empty">binary file, {fmtInt(data.size)} bytes</div>}
    </div>
  );
}

type McpServer = { name: string; description: string; connected: boolean; error: string | null; tools: string[] };

function McpPanel({ sessionId, toast }: { sessionId: string; toast: (t: string) => void }) {
  const [data, setData] = useState<{ enabled: string[]; servers: McpServer[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(() => {
    api.get<{ enabled: string[]; servers: McpServer[] }>(`/api/sessions/${sessionId}/mcp`).then(setData).catch((e) => toast((e as Error).message));
  }, [sessionId, toast]);
  useEffect(load, [load]);
  async function toggle(server: string, enabled: boolean) {
    setBusy(server);
    try {
      setData(await api.put(`/api/sessions/${sessionId}/mcp`, { server, enabled }));
      toast(`${server}: ${enabled ? "enabled" : "disabled"}`);
    } catch (e) {
      toast((e as Error).message);
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
