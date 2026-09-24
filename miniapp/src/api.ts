// Thin client for the Daedalus API. Authenticates with Telegram initData when running
// inside Telegram, or with ?token=... for a browser session.

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string;
        initDataUnsafe?: { start_param?: string; user?: { language_code?: string } };
        themeParams: Record<string, string>;
        colorScheme: "light" | "dark";
        ready: () => void;
        expand: () => void;
        onEvent: (event: string, cb: () => void) => void;
        offEvent?: (event: string, cb: () => void) => void;
        setHeaderColor?: (color: string) => void;
        setBackgroundColor?: (color: string) => void;
        platform?: string;
        isExpanded?: boolean;
        showConfirm?: (text: string, cb: (ok: boolean) => void) => void;
        isVersionAtLeast?: (v: string) => boolean;
        disableVerticalSwipes?: () => void;
        enableVerticalSwipes?: () => void;
        enableClosingConfirmation?: () => void;
        disableClosingConfirmation?: () => void;
        openLink?: (url: string) => void;
        BackButton?: { show: () => void; hide: () => void; onClick: (cb: () => void) => void; offClick: (cb: () => void) => void };
        HapticFeedback?: { impactOccurred: (style: string) => void; notificationOccurred: (t: string) => void };
      };
    };
    /** What the desktop window binds into the page. Absent in a browser and inside Telegram. */
    daedalus?: {
      /** Open the platform's folder chooser; resolves to the path, or null when the operator cancelled. */
      pickFolder?: () => Promise<string | null>;
      /** Set by the desktop launcher's own window, so the page can say which kind of window it is in. */
      window?: boolean;
    };
  }
}

const tg = () => window.Telegram?.WebApp;
let memoryToken: string | null = null;
const query = new URLSearchParams(window.location.search);
const tokenFromQuery = query.get("token");
if (tokenFromQuery) {
  try {
    sessionStorage.setItem("daedalus_token", tokenFromQuery);
  } catch {
    /* a webview without site data: the token lives in memory for this load only */
    memoryToken = tokenFromQuery;
  }
  // The token is the credential: it must not stay in the address bar, the history or a Referer;
  // the other parameters stay.
  query.delete("token");
  const rest = query.toString();
  try {
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash);
  } catch {
    /* ignore */
  }
}

export function storedToken(): string | null {
  try {
    return sessionStorage.getItem("daedalus_token") ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

function authHeaders(): Record<string, string> {
  const initData = tg()?.initData;
  if (initData) return { Authorization: `tma ${initData}` };
  const token = storedToken();
  return token ? { "X-Daedalus-Token": token } : {};
}

export class ApiError extends Error {
  /** `data` is the error body as the host sent it: some refusals carry fields beside `detail`, such as
   *  the `code`, `running` and `cap` of a terminal refused at the machine's cap. */
  constructor(public status: number, message: string, public data: Record<string, unknown> = {}) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    let data: Record<string, unknown> = {};
    try {
      const parsed = await response.json();
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed;
      detail = parsed?.detail ?? detail;
    } catch {
      /* ignore */
    }
    if (Array.isArray(detail)) detail = detail.map((d: any) => (d && d.msg ? `${(d.loc ?? []).slice(-1)[0] ?? ""}: ${d.msg}` : JSON.stringify(d))).join("; ");
    const text = typeof detail === "string" && detail ? detail : response.status >= 500 ? `Server unavailable (${response.status})` : `Request failed (${response.status})`;
    throw new ApiError(response.status, text, data);
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => call<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => call<T>("PATCH", path, body),
  delete: <T>(path: string) => call<T>("DELETE", path),
  streamUrl: (sessionId: string) => `/api/sessions/${sessionId}/stream`,
  downloadUrl: (sessionId: string, path: string) => {
    const token = storedToken();
    return `/api/sessions/${sessionId}/download?path=${encodeURIComponent(path)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  },
  authHeaders,
  /** A workspace file as a blob URL: <img>/<iframe> cannot send the auth header, so the bytes are fetched here. */
  fetchBlob: async (sessionId: string, path: string): Promise<{ url: string; type: string; size: number }> => {
    const res = await fetch(`/api/sessions/${sessionId}/download?path=${encodeURIComponent(path)}`, { headers: authHeaders() });
    if (!res.ok) throw new ApiError(res.status, res.status === 404 ? "no such file" : `could not load the file (${res.status})`);
    const blob = await res.blob();
    return { url: URL.createObjectURL(blob), type: blob.type, size: blob.size };
  },
  /**
   * A single-use pass for one terminal's WebSocket. A browser cannot put the auth header on a
   * WebSocket, and inside Telegram there is no cookie, so the socket is opened with this instead.
   */
  terminalTicket: (id: string, readOnly: boolean) =>
    call<{ ticket: string; expires_in: number }>("POST", `/api/terminals/${encodeURIComponent(id)}/ticket`, { read_only: readOnly }),
  /** A new terminal. At the machine's cap the host answers 409 `over_cap`; `confirm` is the operator's "open it anyway". */
  createTerminal: (body: TerminalCreate) => call<TerminalView>("POST", "/api/terminals", body),
  terminal: (id: string) => call<TerminalView>("GET", `/api/terminals/${encodeURIComponent(id)}`),
  /** Ends the process (hang-up, then kill, the whole group). The row stays, as an exited terminal. */
  killTerminal: (id: string) => call<TerminalView>("POST", `/api/terminals/${encodeURIComponent(id)}/kill`, {}),
  /** The same command in the same place under a new id; the old row stays as it ended. */
  /** Starts the same program for the same owner as a new terminal; ``sandbox`` switches the sandbox
   * on or off, and leaving it out keeps what the terminal had. */
  restartTerminal: (id: string, sandbox?: boolean) => call<TerminalView>("POST", `/api/terminals/${encodeURIComponent(id)}/restart`, sandbox === undefined ? {} : { sandbox }),
  /** The commands the terminal's shell reported, oldest first; with `output`, the text each one printed. 501 when its program reports none. */
  terminalCommands: (id: string, last: number, output: boolean) =>
    call<{ commands: TerminalCommand[] }>("GET", `/api/terminals/${encodeURIComponent(id)}/commands?last=${last}&output=${output ? 1 : 0}`),
  /** Forgets an exited or lost terminal; refused (409) while it runs. */
  removeTerminal: (id: string) => call<unknown>("DELETE", `/api/terminals/${encodeURIComponent(id)}`),
};

/** One command a shell reported. Rows are absolute; a running command has no `end_row` yet. */
export type TerminalCommand = {
  n: number;
  command: string;
  cwd: string;
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  prompt_row: number | null;
  output_row: number;
  end_row: number | null;
  output?: string;
  output_truncated?: boolean;
};

export type TerminalCreate = {
  env: TerminalEnvName;
  owner_kind: "session" | "staff" | "project" | "free";
  owner_id?: string;
  project_id?: string;
  cwd?: string;
  title?: string;
  sandbox?: boolean;
  cols?: number;
  rows?: number;
  confirm?: boolean;
};

/** Where a terminal runs: the terminals container, or the machine itself. */
export type TerminalEnvName = "container" | "host";

/** One terminal environment, as the host reports it: whether it can be used, and what it offers. */
export type TerminalEnv = {
  env: TerminalEnvName;
  available: boolean;
  /** Why it is unavailable, as a code (e.g. "not_installed"); empty when available. */
  reason: string;
  version: string;
  /** "ok" when a terminal here can run in the sandbox; otherwise why not, in the daemon's words. */
  sandbox: string;
  shell: string;
  home: string;
  /** The ports a server started in this environment is reachable on, as "lo-hi". */
  port_range: string;
  /** The address those ports are published on; empty when only this machine can reach them. */
  public_host?: string;
  preview_poll_ms?: number;
};

/** One styled run of a preview row: text and its pen. */
export type TerminalRun = { t: string; fg?: number | string; bg?: number | string; b?: boolean; i?: boolean; u?: boolean; d?: boolean; inv?: boolean };

export type TerminalView = {
  id: string;
  env: TerminalEnvName;
  title: string;
  owner: { kind: "session" | "staff" | "project" | "free"; id: string | null; label?: string };
  project_id: string | null;
  profile: string;
  sandbox: boolean;
  /** On the answer to a sandboxed create: the writable folders the sandbox left read-only, and why. */
  sandbox_skipped?: { path: string; reason: string }[];
  cwd: string;
  status: "running" | "exited" | "lost";
  exit_code: number | null;
  exit_signal: string | null;
  created_at: string;
  exited_at: string | null;
  last_output_at: string | null;
  last_input_at: string | null;
  cols: number;
  rows: number;
  live?: {
    clients: number;
    busy: boolean;
    keyboard: { owner: "auto" | "human" | "agent"; until: string | null };
    size_owner: string;
    alt_screen: boolean;
  } | null;
  last_command?: { command: string; exit_code: number | null; at: string } | null;
  preview?: TerminalRun[][];
  /** Who made it: "operator", or "agent:<actor>" for one an agent opened. */
  created_by?: string;
  /** Filled by other parts of the app (a staff member's status, a pending permission); null otherwise. */
  activity?: TerminalActivity | null;
};

/** What another part of the app says a terminal is doing: a line for its card and, when something
 *  waits on the operator, the button that answers it ("Answer" on a permission). Both are optional:
 *  the Terminals screen falls back to its own status line and to Open. */
export type TerminalActivity = { label?: string; level?: "ok" | "warn" | "bad"; action?: { label: string; path: string } };

export type TerminalList = { envs: TerminalEnv[]; terminals: TerminalView[]; capacity?: { running: number; cap: number; queued: number } };

/** One kind of terminal's average cost, as the host has measured it. `cpu_percent` is of one CPU. */
export type TerminalCost = { rss_bytes: number; cpu_percent: number; samples: number };

/** `GET /api/terminals/load`: what the running terminals cost now, and what the machine would carry
 *  with the cap filled. The host writes no sentences here; the page composes them. */
export type TerminalLoad = {
  cap: number;
  running: number;
  queued: unknown[];
  used: {
    /** The terminals' process trees and the terminal daemon itself, which holds their emulators. */
    rss_bytes: number;
    daemon_rss_bytes?: number;
    cpu_percent: number;
    cpus?: number;
    mem_total_bytes: number;
    mem_available_bytes: number;
    machine_cpu_percent: number;
  };
  profiles: Record<string, TerminalCost>;
  /** What the next terminal is expected to cost, and what that guess rests on. */
  likely: TerminalCost & { basis: "running" | "measured" | "default" };
  projection: {
    cap: number;
    sessions: number;
    terminals_rss_bytes: number;
    machine_used_bytes: number;
    mem_total_bytes: number;
    mem_percent: number;
    cpu_percent: number;
    level: "ok" | "warn" | "bad";
    cpu_level: "ok" | "warn" | "bad";
  };
  envs: { env: TerminalEnvName; terminals: number; rss_bytes: number; daemon_rss_bytes?: number; cpus: number }[];
  thresholds: { warn: number; bad: number };
};

/** One cell of the notification matrix: always, only for urgent notifications, or never. */
export type NotifyCell = "on" | "urgent" | "off";
export type NotifyChannel = "in_app" | "push" | "desktop" | "telegram";
export type NotifyCells = Record<NotifyChannel, NotifyCell>;

/** The `[notifications]` section as `GET /api/notifications/preferences` returns it. */
export type NotificationPreferences = {
  matrix: Record<string, NotifyCells>;
  finished_min_seconds: number;
  /** `HH:MM-HH:MM` in the operator's zone, or "" for none. */
  quiet_hours: string;
  /** Project id → the moment the mute ends, or "" for until it is lifted. */
  muted_projects: Record<string, string>;
  quick_actions: boolean;
  telegram_covers_push: boolean;
  [rest: string]: unknown;
};

export type NotificationPreferencesView = { preferences: NotificationPreferences; revision: string; categories: string[]; zone: string };

export type WebSearchConf = {
  backend: string;
  fallback: string[];
  results: number;
  timeout_seconds: number;
  searxng: { url: string; engines: string; categories: string; safesearch: number };
  duckduckgo: { url: string; region: string };
  serper: { base_url: string; gl: string; hl: string };
  keenable: { base_url: string; snippet_max_length: number };
  tavily: { base_url: string; depth: string };
  exa: { base_url: string; type: string };
  perplexity: { base_url: string };
};

/** One WebSearch backend: `available` is null when the key proxy could not be asked. */
export type SearchBackendInfo = { id: string; label: string; needs_key: boolean; available?: boolean | null };

export type SearchCheck = {
  backend: string;
  count: number;
  attempts: { backend: string; hits: number; error: string; ms: number }[];
  hits: { title: string; url: string; source: string; published: string }[];
};

export type SessionSummary = {
  match?: { snippet: string; score: number };
  id: string;
  title: string;
  status: "idle" | "running" | "waiting" | "failed" | "compacting";
  created_at: string;
  last_message_at: string;
  run_id: string | null;
  needs_attention?: boolean;
  background_count?: number;
  unread_result?: boolean;
  archived?: boolean;
  /** The preset label or provider/model the session's next call goes to. */
  model?: string;
  /** The name of the directory the session works in, and whether that directory is its own. */
  workspace?: string;
  /** The whole path of it, for the row's tooltip: the list groups by project now, not by folder. */
  workspace_path?: string;
  workspace_own?: boolean;
  /** The project this agent works in: the id it is grouped by and the name shown. */
  project_id: string;
  project: string;
  metadata?: { subagent_of?: string; subagent_name?: string; loop?: LoopView; forked_from?: { session_id: string; seq: number }; [k: string]: unknown };
  /** How many of the session's terminals are running. */
  terminals?: number;
};

export type TaskView = {
  id: string;
  owner_session_id: string;
  parent_run_id: string | null;
  kind: "job" | "agent";
  state: "running" | "done" | "failed" | "cancelled";
  title: string;
  started_at: string;
  last_activity_at: string;
  progress: number | null;
  child_session_id: string | null;
  result_ref: string | null;
  stop_supported: boolean;
};

export type LoopView = {
  mode: "interval" | "dynamic";
  interval_seconds: number | null;
  status: "active" | "paused" | "stopped" | "done";
  run_count: number;
  max_runs: number | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_reason: string | null;
  stop_reason: string | null;
  pause_note: string | null;
  instruction: string;
};

export type ShareMode = "local" | "public" | "key";
export type ShareView = { mode: ShareMode; slug: string | null; key: string | null; url: string | null; public_base: string };
export type ServiceView = { name: string; command: string; cwd: string; port: number | null; url: string | null; pid: number | null; status: "running" | "stopped" | "dead"; restart: boolean; note: string | null; started_at: string; stopped_at: string | null; share?: ShareView };

/** One provider's usage for the card beside the chat: a subscription's windows, or the day's metered spend. */
export type ProviderUsage = {
  provider: string;
  today: { calls?: number; input_tokens?: number | null; output_tokens?: number | null; cache_read_tokens?: number | null; cost_usd?: number | null; unmetered?: number | null };
  subscription: { logged_in: boolean; plan?: string; limit_reached?: boolean; windows?: { name: string; used_percent: number; resets_at?: number | string | null }[]; error?: string } | null;
  balance: number | null | undefined;
};

export type ToolInfo = { name: string; description: string; group: string };

export type SubagentView = { session_id: string; name: string | null; running: boolean; status: string; model: string; kept?: boolean };

/** A model answering in place of the configured one: what it replaced, what took over, and why. */
export type ModelFallback = { from: string; to: string; reason: string };

export type MediaItem = { id: string; kind: "image" | "animation" | "video" | "audio"; mime_type: string; filename: string; byte_size: number; width?: number | null; height?: number | null; alt: string; caption: string; url?: string };
export type MediaPresentation = { id: string; layout: "single" | "album"; items: MediaItem[] };

export type MessageView = {
  run_id?: string | null;
  role: "system" | "user" | "assistant" | "tool";
  summary?: boolean;
  internal?: boolean;
  origin?: string;
  seq?: number | null;
  /** The engine holds this one and the transcript does not yet: its `seq` is the one it will get. */
  live?: boolean;
  compaction?: { reason: string; messages?: number; at?: string } | null;
  /** The model that produced this answer, as the host recorded it. Empty on turns written before it was recorded. */
  model?: string;
  provider?: string;
  /** Set when that model was not the one the session was set to answer with. */
  fallback?: ModelFallback | null;
  media?: MediaPresentation[];
  text: string;
  thinking: string;
  tool_calls: { id: string; name: string; arguments: Record<string, unknown> }[];
  tool_results: { id: string; content: string; is_error: boolean; length?: number; clipped?: boolean }[];
  created_at: string;
};

export type Compacting = { reason: string; stage: "summarising" | "merging" | "writing"; messages: number; parts_done: number; parts_total: number; started_at: string };

export type SessionDetail = {
  id: string;
  title: string;
  status: string;
  /** The run is over and its answer is written; what the session is still saving behind it is not a run. */
  housekeeping?: boolean;
  /** Why the last run ended in an error, in the words the host was given — a provider's own refusal, typically.
   *  Present until the next run starts: the reply the model managed to write is not the only trace of a failure. */
  error?: string;
  compacting?: Compacting | null;
  run_id: string | null;
  workspace: string;
  workspace_name?: string;
  workspace_own?: boolean;
  workspace_sessions?: { id: string; title: string }[];
  /** The project this session belongs to. */
  project: ProjectRef;
  /** The folder of the project the session works in. */
  folder_id?: string;
  /** The folders of its project the session may read, which its file pane can switch between; empty
   *  for a session with a directory of its own. */
  folders?: SessionFolder[];
  pending: { questions: Question[] } | null;
  model: string;
  provider?: string;
  /** Session override, else the preset: whether the next call thinks, and how hard. */
  thinking?: boolean;
  reasoning_effort?: string;
  /** The model name the session is set to answer with, and the one really answering; they differ during a fallback. */
  configured_model?: string;
  effective_model?: string;
  effective_provider?: string;
  fallback?: ModelFallback | null;
  messages: MessageView[];
  mode?: string;
  usd_cap?: number | null;
  brief?: string;
  spawned_by?: string | null;
  tools_off?: string[];
  loop?: LoopView | null;
  services?: ServiceView[];
  subagent_of?: string | null;
  subagent_name?: string | null;
  /** The project this session is the orchestrator of (current or retired). */
  orchestrator_of?: string | null;
  /** The staff member this session is the work of. */
  staff?: { id: string; session_id: string | null } | null;
  leader_title?: string | null;
  subagents?: SubagentView[];
  /** Whether this session currently sends to and receives from its own Telegram topic. */
  telegram_linked?: boolean;
  context?: {
    tokens: number;
    estimated?: boolean;
    window: number;
    messages: number;
    summaries: number;
    operator_turns: number;
    breakdown?: { instructions: number; tools: number; conversation: number; attachments: number; reserved_response: number; source: string } | null;
    recent_cache?: { read_tokens: number; prompt_tokens: number; hit_percent: number } | null;
    prefix_changed?: string[];
  };
  usage: { c?: number; i?: number; o?: number; ch?: number; usd?: number | null };
};

/**
 * The workspace snapshots a session can still be put back to. Retention drops the oldest once the
 * store passes its bounds, so the undo is offered for the turns that still have one and the screen
 * says, once, that the older ones were removed rather than reverting to nothing.
 */
export type SessionCheckpoints = {
  checkpoints: { seq: number | null; run_id: string | null; kind: string; sha: string; at: string }[];
  total: number;
  pruned: boolean;
  pruned_before: string | null;
  removed: number;
  note: string;
  keep_days: number;
  keep_last: number;
};

export type MemoryRecord = { id: string; scope: string; scope_key: string; kind: string; text: string; salience: number; version: number; created_at: string | null; last_accessed_at: string | null };
export type MemoryBucket = { scope: string; scope_key: string; title: string | null; count: number };
export type MemoryListing = { records: MemoryRecord[]; buckets: MemoryBucket[]; sessions: Record<string, string> };

/** A folder a session's file pane may open, and whether the session may write there. */
export type SessionFolder = {
  id: string;
  path: string;
  label: string;
  env: "container" | "host";
  readonly: boolean;
  writable: boolean;
};

/** One folder of a project, as the host stores it. */
export type ProjectDir = {
  id: string;
  /** The absolute path, as the operator gave it. Shown, never edited in place. */
  path: string;
  label: string;
  /** Where the folder lives: inside the container the bot runs in, or on the machine around it. */
  env: "container" | "host";
  is_git: boolean;
  readonly: boolean;
  position: number;
  /** A scratch folder the installation made for itself rather than one the operator pointed at. */
  managed: boolean;
  /** Whether the bot can reach the folder from where it runs. False in Docker until the folder is mounted. */
  reachable: boolean;
  writable: boolean;
  /** Who could ever work in it: every agent, only what runs in a host terminal, or nothing yet.
   *  Sent by /api/projects; a session's own copy of its project leaves it out. */
  reach?: FolderReach;
};

export type FolderReach = "agents" | "terminals" | "none";

/** Where a folder of this installation may live, from /api/project-environments. */
export type ProjectEnvironments = {
  /** The environment the bot itself runs in: its agents work in folders of this one. */
  local: "container" | "host";
  available: ("container" | "host")[];
  host_bridge: boolean;
  docker: boolean;
};

export type OrchestratorSettings = {
  enabled: boolean;
  session_id: string;
  model: string;
  autonomy: "ask" | "normal" | "full";
  concurrency: number;
  concurrency_cap: number;
  telegram_topic_id: number;
};

export type Project = {
  id: string;
  name: string;
  created_at: string;
  settings: { snapshots: boolean; system?: string; ephemeral?: boolean; default_env?: "container" | "host"; orchestrator?: OrchestratorSettings };
  /** Non-empty on a project the installation made for itself: "voice" is the concierge's. It cannot be moved or removed. */
  system?: string;
  /** In order; the first is the primary folder, where an agent works unless it is given another. */
  folders: ProjectDir[];
  sessions: { id: string; title: string; running?: boolean }[];
};

/** A project as a session names it: everything but the list of agents, which a session view has no use for. */
export type ProjectRef = Omit<Project, "sessions">;

/** A project in the agents listing: the project, and how many agents are in it — counted over the
 *  whole table, not over the page of rows beside it. */
export type ProjectFolder = ProjectRef & { members?: number; total: number; active: number; loops: number; last_message_at: string; orchestrator?: Orchestration | null };

/** What the agents list says about a project whose orchestrator is on: it is drawn as one entry. */
export type Orchestration = { enabled: boolean; session_id: string; staff: number; working: number; needs_you: number };

/** A pending decision of a project: a staff member's question or permission, or one of the orchestrator's own. */
export type Ask = {
  id: string;
  short_id: string;
  project_id: string;
  origin: "staff" | "orchestrator";
  kind: "question" | "permission" | "folder";
  staff_id: string | null;
  task_id: string | null;
  text: string;
  detail: { options?: string[]; urgent?: boolean; [k: string]: unknown };
  routed_to: "orchestrator" | "operator";
  suggestion: string;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution: { allow?: boolean | null; text?: string; selected?: string[]; via?: string };
};

/** One entry of a project's journal: who wrote it, what kind, and what it refers to. */
export type JournalEntry = { id: number; at: string; author: "operator" | "orchestrator" | "staff" | "system"; kind: string; text: string; refs: Record<string, string> };

export type BriefSection = { section: string; body: string; updated_at: string | null; updated_by: string | null };

/** A message sent to a staff member, and how far it got. */
export type StaffMessage = { id: string; staff_id: string; origin: "orchestrator" | "operator"; text: string; mode: string; state: "queued" | "written" | "submitted" | "acknowledged" | "failed"; attempts: number; created_at: string; updated_at: string; error: string };

/** What GET /api/sessions answers: a page of agents and the project folders they are in. */
export type SessionList = {
  sessions: SessionSummary[];
  projects: ProjectFolder[];
  next_cursor?: string | null;
};

export type AsrStatus = { configured: boolean; reason: string; provider: string; model: string; max_seconds: number; autosend: boolean };

export type SlashCommand = { name: string; args: string; description: string; scope: string; confirm: boolean };

export type Question = {
  question: string;
  header?: string;
  options?: { label: string; description?: string | null }[];
  multiSelect?: boolean;
  allow_custom?: boolean;
};

/** How many notifications want the operator: unseen ones worth a badge, and open requests. */
export type NotificationSummary = { unseen: number; needs_you: number };

export type NotificationAction = { id: string; label: string; style: "primary" | "default" | "danger" | "ghost"; quick: boolean };

/** One entry of the notification centre, as `/api/notifications` and the `notify` event carry it. */
export type Notification = {
  id: number;
  at: string;
  updated_at: string;
  category: string;
  kind: string;
  level: "quiet" | "normal" | "urgent";
  tone: "ok" | "info" | "warning" | "error";
  title: string;
  body: string;
  link: string;
  session_id: string | null;
  run_id: string | null;
  project_id: string | null;
  staff_id: string | null;
  terminal_id: string | null;
  source: string;
  dedupe_key: string | null;
  /** What answers it (`ask:…`, `policy:…`); an entry with one is open until it is resolved. */
  request_ref: string | null;
  count: number;
  actions: NotificationAction[];
  seen: boolean;
  resolved: string | null;
  needs_you: boolean;
  delivered: Record<string, unknown>;
};

export type NotificationPage = { entries: Notification[]; next_before: number | null; summary: NotificationSummary };

/** One event of the host's stream (`/api/events`). `seq` is 0 for an event the host does not keep. */
export type AppEvent = {
  seq: number;
  at: string;
  type: string;
  project_id: string | null;
  session_id: string | null;
  staff_id: string | null;
  terminal_id: string | null;
  payload: Record<string, any>;
};

export type Proposal = {
  id: string;
  repo: string;
  branch: string;
  pr_number: number | null;
  pr_url: string | null;
  title: string;
  summary: string;
  status: string;
  reason: string | null;
  created_at: string;
};

export type Schedule = {
  id: string;
  name: string;
  run_in?: string;
  cron: string | null;
  run_at: string | null;
  prompt: string;
  enabled: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_summary: string | null;
  kind: "agent" | "message" | "lazy";
  target_session: string | null;
  created_by_session?: string | null;
  active_session_id?: string | null;
  failure_count: number;
  last_error: string | null;
};

export type HeartbeatStatus = {
  enabled: boolean;
  armed: boolean;
  interval_minutes: number;
  active_hours: string;
  preset: string;
  max_runs_per_day: number;
  last_run: string | null;
  runs_today: number;
  session_id: string | null;
  running: boolean;
  file: string;
  text: string;
  template?: string;
};

export type ProviderConf = {
  kind: string;
  name?: string;
  base_url: string;
  timeout_seconds: number;
  temperature?: number | null;
  api_key?: string; // always "" from the API — stored keys are masked
  api_key_set?: boolean;
  pricing?: Record<string, unknown>;
};

export type Preset = {
  provider: string;
  model: string;
  label: string;
  thinking: boolean;
  reasoning_effort: string;
  images: boolean;
  context_window: number;
  max_output_tokens: number;
};

export type Settings = {
  revision: string;
  model: { preset: string; chain: string[] };
  presets: Record<string, Preset>;
  providers: Record<string, ProviderConf>;
  provider_kinds?: string[];
  prompt: { rules: string; default_rules?: string };
  vision: { preset: string; max_output_tokens: number };
  /** A project orchestrator's defaults. `strongest` is what an empty `preset` means, sent by the host. */
  orchestrator?: { preset: string; strongest?: string };
  asr: { provider: string; url: string; api_key: string; api_key_set?: boolean; model: string; language: string; timeout_seconds: number; max_seconds: number; autosend: boolean };
  tools: {
    web: { fetch_timeout_seconds: number; proxy: string; user_agent: string; fetch_max_chars: number; search: WebSearchConf };
    exec: { max_output_chars: number };
  };
  self_change: { approval: string; auto_rebuild: boolean };
  limits: { max_iterations: number; tool_timeout_seconds: number; usd_per_run: number; usd_total: number; usd_total_per_provider: Record<string, number>; total_since: string };
  usd_per_day?: number;
  balance: { enabled: boolean; poll_seconds: number; thresholds_usd: number[] };
  scheduler: { topic_mode: string; catch_up_missed: boolean };
  compaction: { auto_ratio: number; keep_recent_messages: number; max_words: number; chunk_tokens: number; min_messages: number; core_trigger_ratio: number };
  terminals?: { running_cap: number };
  telegram: {
    mode: "topics" | "private" | null;
    forum_chat_id: number;
    verbosity: number;
    reactions: boolean;
    topic_status_emoji: boolean;
    stale_after_seconds: number;
    max_inbound_file_mb: number;
    forward_unknown_commands: boolean;
    slow_tool_seconds: number;
    photo_caption_wait_seconds: number;
  };
  providers_available: string[];
  search_backends?: SearchBackendInfo[];
};

export function telegram() {
  return tg();
}
