// Thin client for the Daedalus API. Authenticates with Telegram initData when running
// inside Telegram, or with ?token=... for a browser session.

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string;
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
        disableVerticalSwipes?: () => void;
        enableVerticalSwipes?: () => void;
        enableClosingConfirmation?: () => void;
        disableClosingConfirmation?: () => void;
        openLink?: (url: string) => void;
        BackButton?: { show: () => void; hide: () => void; onClick: (cb: () => void) => void; offClick: (cb: () => void) => void };
        HapticFeedback?: { impactOccurred: (style: string) => void; notificationOccurred: (t: string) => void };
      };
    };
  }
}

const tg = () => window.Telegram?.WebApp;
const tokenFromQuery = new URLSearchParams(window.location.search).get("token");
if (tokenFromQuery) {
  sessionStorage.setItem("daedalus_token", tokenFromQuery);
  // The token is the credential: it must not stay in the address bar, the history or a Referer.
  try {
    window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  } catch {
    /* ignore */
  }
}

function authHeaders(): Record<string, string> {
  const initData = tg()?.initData;
  if (initData) return { Authorization: `tma ${initData}` };
  const token = sessionStorage.getItem("daedalus_token");
  return token ? { "X-Daedalus-Token": token } : {};
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
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
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    if (Array.isArray(detail)) detail = detail.map((d: any) => (d && d.msg ? `${(d.loc ?? []).slice(-1)[0] ?? ""}: ${d.msg}` : JSON.stringify(d))).join("; ");
    const text = typeof detail === "string" && detail ? detail : response.status >= 500 ? `Server unavailable (${response.status})` : `Request failed (${response.status})`;
    throw new ApiError(response.status, text);
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
    const token = sessionStorage.getItem("daedalus_token");
    return `/api/sessions/${sessionId}/download?path=${encodeURIComponent(path)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  },
  authHeaders,
};

export type SessionSummary = {
  id: string;
  title: string;
  status: "idle" | "running" | "waiting" | "failed";
  created_at: string;
  last_message_at: string;
  run_id: string | null;
  metadata?: { subagent_of?: string; subagent_name?: string; loop?: LoopView; [k: string]: unknown };
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

export type ToolInfo = { name: string; description: string; group: string };

export type SubagentView = { session_id: string; name: string | null; running: boolean; status: string; model: string };

export type MessageView = {
  role: "system" | "user" | "assistant" | "tool";
  summary?: boolean;
  internal?: boolean;
  origin?: string;
  seq?: number | null;
  compaction?: { reason: string; messages?: number; at?: string } | null;
  text: string;
  thinking: string;
  tool_calls: { id: string; name: string; arguments: Record<string, unknown> }[];
  tool_results: { id: string; content: string; is_error: boolean }[];
  created_at: string;
};

export type SessionDetail = {
  id: string;
  title: string;
  status: string;
  run_id: string | null;
  workspace: string;
  pending: { questions: Question[] } | null;
  model: string;
  messages: MessageView[];
  mode?: string;
  usd_cap?: number | null;
  brief?: string;
  spawned_by?: string | null;
  tools_off?: string[];
  loop?: LoopView | null;
  subagent_of?: string | null;
  subagent_name?: string | null;
  leader_title?: string | null;
  subagents?: SubagentView[];
  context?: { tokens: number; window: number; messages: number; summaries: number; operator_turns: number };
  usage: { c?: number; i?: number; o?: number; ch?: number; usd?: number | null };
};

export type SlashCommand = { name: string; args: string; description: string; scope: string; confirm: boolean };

export type Question = {
  question: string;
  header?: string;
  options?: { label: string; description?: string | null }[];
  multiSelect?: boolean;
  allow_custom?: boolean;
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
  base_url: string;
  timeout_seconds: number;
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
  model: { preset: string; chain: string[] };
  presets: Record<string, Preset>;
  providers: Record<string, ProviderConf>;
  provider_kinds?: string[];
  prompt: { rules: string; default_rules?: string };
  vision: { preset: string; max_output_tokens: number };
  asr: { url: string; api_key: string; api_key_set?: boolean; model: string; language: string; timeout_seconds: number; max_seconds: number; autosend: boolean };
  tools: {
    web: { fetch_timeout_seconds: number; search_timeout_seconds: number; proxy: string; user_agent: string; fetch_max_chars: number; search_url: string; search_region: string; search_results: number };
    exec: { max_output_chars: number };
  };
  self_change: { approval: string; auto_rebuild: boolean };
  limits: { max_iterations: number; tool_timeout_seconds: number; usd_per_run: number; usd_total: number; usd_total_per_provider: Record<string, number>; total_since: string };
  usd_per_day?: number;
  balance: { enabled: boolean; poll_seconds: number; thresholds_usd: number[] };
  scheduler: { topic_mode: string; catch_up_missed: boolean };
  compaction: { auto_ratio: number; keep_recent_messages: number; max_words: number; chunk_tokens: number; min_messages: number; core_trigger_ratio: number };
  harness: { url: string; timeout_seconds: number; vendors: Record<string, { enabled: boolean; model: string; effort: string; max_turns: number }> };
  telegram: {
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
};

export function telegram() {
  return tg();
}
