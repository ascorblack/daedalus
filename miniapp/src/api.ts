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
        setHeaderColor?: (color: string) => void;
        setBackgroundColor?: (color: string) => void;
        HapticFeedback?: { impactOccurred: (style: string) => void; notificationOccurred: (t: string) => void };
      };
    };
  }
}

const tg = () => window.Telegram?.WebApp;
const tokenFromQuery = new URLSearchParams(window.location.search).get("token");
if (tokenFromQuery) sessionStorage.setItem("daedalus_token", tokenFromQuery);

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
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => call<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => call<T>("PATCH", path, body),
  delete: <T>(path: string) => call<T>("DELETE", path),
  streamUrl: (sessionId: string) => {
    const token = sessionStorage.getItem("daedalus_token");
    return `/api/sessions/${sessionId}/stream${token ? `?token=${encodeURIComponent(token)}` : ""}`;
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
};

export type MessageView = {
  role: "system" | "user" | "assistant" | "tool";
  summary?: boolean;
  internal?: boolean;
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
  usage: { c?: number; i?: number; o?: number; ch?: number; usd?: number | null };
};

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
  cron: string | null;
  run_at: string | null;
  prompt: string;
  enabled: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_summary: string | null;
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
  tools: {
    web: { fetch_timeout_seconds: number; search_timeout_seconds: number; proxy: string; user_agent: string; fetch_max_chars: number; search_url: string; search_region: string; search_results: number };
    exec: { max_output_chars: number };
  };
  self_change: { approval: string; auto_rebuild: boolean };
  limits: { max_iterations: number; tool_timeout_seconds: number; usd_per_run: number };
  usd_per_day?: number;
  balance: { enabled: boolean; poll_seconds: number; thresholds_usd: number[] };
  scheduler: { topic_mode: string; catch_up_missed: boolean };
  telegram: {
    verbosity: number;
    reactions: boolean;
    topic_status_emoji: boolean;
    stale_after_seconds: number;
    max_inbound_file_mb: number;
    forward_unknown_commands: boolean;
    slow_tool_seconds: number;
  };
  providers_available: string[];
};

export function telegram() {
  return tg();
}
