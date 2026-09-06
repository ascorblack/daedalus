import { useCallback, useEffect, useRef, useState } from "react";
import { api, MessageView, Question, SessionDetail } from "../api";
import { Avatar, Pill, Status, fmtInt, fmtUsd } from "../components";

type LiveState = { text: string; thinking: string; tool: string | null; tools: string[] };

export function SessionScreen({ id, onBack, toast }: { id: string; onBack: () => void; toast: (t: string) => void }) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [live, setLive] = useState<LiveState>({ text: "", thinking: "", tool: null, tools: [] });
  const [draft, setDraft] = useState("");
  const [showFiles, setShowFiles] = useState(false);
  const [showMcp, setShowMcp] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

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

  // Belt and braces: while a run is active, re-read the transcript even if the event stream stalls.
  useEffect(() => {
    if (!detail || (detail.status !== "running" && detail.status !== "waiting")) {
      setLive({ text: "", thinking: "", tool: null, tools: [] }); // the transcript now holds everything
      return;
    }
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [detail, load]);

  // Live events while the screen is open; the transcript is re-read from the server on each open.
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
    const names: Record<string, string> = {};
    function handle(event: string, p: Record<string, any>) {
      if (event === "message_start") setLive((s) => ({ ...s, text: "", thinking: "" }));
      else if (event === "content_block_delta") {
        const d = p.delta ?? {};
        if (d.type === "text_delta") setLive((s) => ({ ...s, text: s.text + (d.text ?? "") }));
        if (d.type === "thinking_delta") setLive((s) => ({ ...s, thinking: s.thinking + (d.text ?? "") }));
      } else if (event === "tool_use_start") {
        names[p.tool_call_id] = p.tool_name;
        setLive((s) => ({ ...s, tool: p.tool_name }));
      } else if (event === "tool_use_stop") {
        const name = names[p.tool_call_id] ?? "?";
        setLive((s) => ({ ...s, tools: [...s.tools, `${name} ${JSON.stringify(p.final_input ?? {}).slice(0, 120)}`] }));
      } else if (event === "tool_result") setLive((s) => ({ ...s, tool: null }));
      else if (event === "message_stop" || event === "state_changed" || event === "tool_call_pending" || event === "run_settled") load();
    }
    return () => {
      stop = true;
    };
  }, [id, load]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [detail, live.text, live.tools.length]);

  async function send() {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    try {
      await api.post(`/api/sessions/${id}/messages`, { text });
      setLive({ text: "", thinking: "", tool: null, tools: [] });
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function stop() {
    await api.post(`/api/sessions/${id}/stop`);
    toast("stopping");
  }

  const status = (detail?.status ?? "idle") as Status;
  return (
    <>
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
        <button className="btn small" onClick={() => { setShowMcp(false); setShowFiles((v) => !v); }}>
          {showFiles ? "chat" : "files"}
        </button>
        <button className="btn small" onClick={() => { setShowFiles(false); setShowMcp((v) => !v); }}>
          {showMcp ? "chat" : "mcp"}
        </button>
        <button
          className="btn small danger"
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
      <div className="screen">
        {showMcp && detail ? (
          <McpPanel sessionId={id} toast={toast} />
        ) : showFiles && detail ? (
          <Files sessionId={id} />
        ) : (
          <div className="timeline">
            {detail?.messages.map((m, i) => <Bubble key={i} m={m} />)}
            {live.thinking && status === "running" && <div className="thinking">{live.thinking.slice(-600)}</div>}
            {live.tools.map((t, i) => (
              <div key={i} className="tool">
                <span style={{ fontFamily: "var(--mono)", fontSize: 12 }}>› {t}</span>
              </div>
            ))}
            {live.tool && <div className="streaming">▶ {live.tool}…</div>}
            {live.text && status === "running" && <div className="msg assistant">{live.text}</div>}
            {detail?.pending && <QuestionCard sessionId={id} questions={detail.pending.questions} onDone={load} toast={toast} />}
            <div ref={bottom} />
          </div>
        )}
        {!showFiles && !showMcp && (
          <div className="composer">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={status === "running" ? "follow-up (queued for the next step)" : "message"}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <button className="btn primary" onClick={send} disabled={!draft.trim()}>
              ↑
            </button>
          </div>
        )}
      </div>
    </>
  );
}

function Bubble({ m }: { m: MessageView }) {
  if (m.role === "tool") {
    return (
      <>
        {m.tool_results.map((r) => (
          <details key={r.id} className="tool">
            <summary>{r.is_error ? "✗" : "✓"} result</summary>
            <pre>{r.content}</pre>
          </details>
        ))}
      </>
    );
  }
  if (m.role === "system") return <div className="msg system">{m.text.slice(0, 200)}</div>;
  return (
    <>
      {m.thinking && <div className="thinking">{m.thinking.slice(0, 400)}</div>}
      {m.text && <div className={`msg ${m.role}`}>{m.text}</div>}
      {m.tool_calls.map((c) => (
        <details key={c.id} className="tool">
          <summary>
            › {c.name}({JSON.stringify(c.arguments).slice(0, 100)})
          </summary>
          <pre>{JSON.stringify(c.arguments, null, 2)}</pre>
        </details>
      ))}
    </>
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
    <>
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
    </>
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
    <>
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
    </>
  );
}
