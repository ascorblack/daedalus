import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactElement } from "react";
import { api, MessageView, Question, SessionDetail } from "../api";
import { Avatar, Pill, Status, fmtInt, fmtUsd } from "../components";
import { renderMarkdown } from "../md";

type LiveTool = { id: string; name: string; args: string; result?: string; error?: boolean };
type LiveState = { text: string; thinking: string; tools: LiveTool[] };
const EMPTY_LIVE: LiveState = { text: "", thinking: "", tools: [] };

export function SessionScreen({ id, onBack, toast }: { id: string; onBack: () => void; toast: (t: string) => void }) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [view, setView] = useState<"chat" | "files" | "mcp">("chat");
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const stickToBottom = useRef(true);

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

  // While a run is active, re-read the transcript even if the event stream stalls.
  useEffect(() => {
    if (!detail || (detail.status !== "running" && detail.status !== "waiting")) {
      setLive(EMPTY_LIVE);
      return;
    }
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [detail, load]);

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
      if (event === "message_start") setLive((s) => ({ ...s, text: "", thinking: "" }));
      else if (event === "content_block_delta") {
        const d = p.delta ?? {};
        if (d.type === "text_delta") setLive((s) => ({ ...s, text: s.text + (d.text ?? "") }));
        if (d.type === "thinking_delta") setLive((s) => ({ ...s, thinking: s.thinking + (d.text ?? "") }));
      } else if (event === "tool_use_start") {
        setLive((s) => ({ ...s, tools: [...s.tools, { id: p.tool_call_id, name: p.tool_name, args: "" }] }));
      } else if (event === "tool_use_stop") {
        setLive((s) => ({ ...s, tools: s.tools.map((t) => (t.id === p.tool_call_id ? { ...t, args: JSON.stringify(p.final_input ?? {}, null, 1) } : t)) }));
      } else if (event === "tool_result") {
        setLive((s) => ({ ...s, tools: s.tools.map((t) => (t.id === p.tool_call_id ? { ...t, result: String(p.content ?? p.output ?? ""), error: !!p.is_error } : t)) }));
      } else if (event === "message_stop" || event === "state_changed" || event === "tool_call_pending" || event === "run_settled") load();
    }
    return () => {
      stop = true;
    };
  }, [id, load]);

  // Keep the newest content visible unless the operator scrolled up on purpose.
  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [detail, live]);

  function onScroll() {
    const el = scroller.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  async function send() {
    const text = draft.trim();
    if (!text && pending.length === 0) return;
    setSending(true);
    try {
      if (pending.length > 0) {
        const form = new FormData();
        form.append("text", text);
        for (const f of pending) form.append("files", f, f.name);
        const res = await fetch(`/api/sessions/${id}/upload`, { method: "POST", headers: api.authHeaders(), body: form });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      } else {
        await api.post(`/api/sessions/${id}/messages`, { text });
      }
      setDraft("");
      setPending([]);
      setLive(EMPTY_LIVE);
      stickToBottom.current = true;
      load();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  async function stop() {
    await api.post(`/api/sessions/${id}/stop`);
    toast("stopping");
  }

  const status = (detail?.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";
  return (
    <div className="chat">
      <div className="topbar">
        <button className="btn small" onClick={onBack}>
          ‹
        </button>
        {detail && <Avatar status={status} seed={detail.id} />}
        <div className="grow">
          <div className="title">{detail?.title ?? "…"}</div>
          <div className="sub">
            {detail?.model} · {fmtInt(detail?.usage.i)}↑ {fmtInt(detail?.usage.o)}↓ · {fmtUsd(detail?.usage.usd)}
          </div>
        </div>
        <Pill status={status} />
        {status === "running" && (
          <button className="btn small danger" onClick={stop}>
            stop
          </button>
        )}
        <button className={`btn small ${view === "files" ? "primary" : ""}`} onClick={() => setView(view === "files" ? "chat" : "files")}>
          files
        </button>
        <button className={`btn small ${view === "mcp" ? "primary" : ""}`} onClick={() => setView(view === "mcp" ? "chat" : "mcp")}>
          mcp
        </button>
        <button
          className="btn small danger"
          title="delete session"
          onClick={async () => {
            if (!window.confirm("Delete this session, its topic and its workspace?")) return;
            try {
              await api.delete(`/api/sessions/${id}`);
              onBack();
            } catch (e) {
              toast((e as Error).message);
            }
          }}
        >
          🗑
        </button>
      </div>

      <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
        {view === "mcp" && detail && <McpPanel sessionId={id} toast={toast} />}
        {view === "files" && detail && <Files sessionId={id} />}
        {view === "chat" && (
          <div className="timeline">
            {detail && <Transcript messages={detail.messages} />}
            {busy && live.thinking && <Thinking text={live.thinking} open />}
            {live.tools.map((t) => (
              <ToolBlock key={t.id} name={t.name} args={t.args} result={t.result} error={t.error} running={t.result === undefined} />
            ))}
            {busy && live.text && <div className="msg assistant" dangerouslySetInnerHTML={{ __html: renderMarkdown(live.text) }} />}
            {busy && !live.text && live.tools.every((t) => t.result !== undefined) && <div className="streaming">thinking…</div>}
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
          <div className="composer-row">
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => setPending((p) => [...p, ...Array.from(e.target.files ?? [])])} />
            <button className="btn" title="attach files" onClick={() => fileInput.current?.click()}>
              📎
            </button>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={status === "running" ? "follow-up (queued for the next step)" : "message"}
              rows={1}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <button className="btn primary" onClick={send} disabled={sending || (!draft.trim() && pending.length === 0)}>
              ↑
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// -- transcript ---------------------------------------------------------------------------

function Transcript({ messages }: { messages: MessageView[] }) {
  // Pair every tool call with its result so each tool renders as one block.
  const results = new Map<string, { content: string; is_error: boolean }>();
  for (const m of messages) for (const r of m.tool_results) results.set(r.id, r);
  const out: ReactElement[] = [];
  messages.forEach((m, i) => {
    if (m.role === "tool") return;
    if (m.role === "system") {
      out.push(
        <div key={i} className="msg system">
          {m.text.slice(0, 200)}
        </div>,
      );
      return;
    }
    if (m.role === "user") {
      out.push(<div key={i} className="msg user" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.text) }} />);
      return;
    }
    if (m.thinking) out.push(<Thinking key={`${i}-th`} text={m.thinking} />);
    if (m.text) out.push(<div key={`${i}-tx`} className="msg assistant" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.text) }} />);
    for (const c of m.tool_calls) {
      const r = results.get(c.id);
      out.push(<ToolBlock key={c.id} name={c.name} args={JSON.stringify(c.arguments, null, 1)} result={r?.content} error={r?.is_error} running={false} />);
    }
  });
  return <>{out}</>;
}

function Thinking({ text, open = false }: { text: string; open?: boolean }) {
  return (
    <details className="thinking" open={open}>
      <summary>💭 thinking · {text.length.toLocaleString()} chars</summary>
      <div className="thinking-body">{text}</div>
    </details>
  );
}

function argsPreview(args: string): string {
  try {
    const a = JSON.parse(args || "{}");
    const key = ["command", "path", "pattern", "url", "query", "task", "server", "skill", "title", "name"].find((k) => k in a);
    if (key) return String(a[key]).slice(0, 90);
    return Object.keys(a).length ? JSON.stringify(a).slice(0, 90) : "";
  } catch {
    return args.slice(0, 90);
  }
}

function ToolBlock({ name, args, result, error, running }: { name: string; args: string; result?: string; error?: boolean; running: boolean }) {
  const mark = running ? "▶" : error ? "✗" : "✓";
  return (
    <details className={`tool ${running ? "running" : error ? "error" : ""}`}>
      <summary>
        <span className="mark">{mark}</span> <b>{name}</b> <span className="preview">{argsPreview(args)}</span>
      </summary>
      <div className="tool-body">
        <div className="label">arguments</div>
        <pre>{args || "{}"}</pre>
        {result !== undefined && (
          <>
            <div className="label">result</div>
            <pre>{result}</pre>
          </>
        )}
      </div>
    </details>
  );
}

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
