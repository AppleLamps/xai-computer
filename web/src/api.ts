export type Startup = {
  model: string;
  desktop: string;
  allowed_roots: string[];
  dry_run: boolean;
  verbose: boolean;
  bypass_approvals: boolean;
  auto_switch_models: boolean;
  max_tool_loops: number;
};

export type SessionInfo = {
  id: string;
  created: string;
  busy: boolean;
  stopped: boolean;
  bypass_approvals: boolean;
  active_error?: string | null;
  event_count: number;
  token_totals: Record<string, number>;
};

export type SavedSession = {
  id?: string;
  title?: string;
  created?: string;
  updated?: string;
  token_totals?: Record<string, number>;
};

export type ApprovalAction = {
  index: number;
  tool_name: string;
  action_class: string;
  label: string;
  risk: "low" | "medium" | "high";
};

export type ApprovalCard = {
  generation: number;
  action_class: string;
  affected_root: string;
  dry_run: boolean;
  risk_level: "low" | "medium" | "high";
  summary: string;
  actions: ApprovalAction[];
  shell_explanation?: Record<string, string> | null;
};

export type WebEvent = {
  id: number;
  ts: string;
  kind:
    | "session"
    | "user"
    | "assistant"
    | "info"
    | "error"
    | "progress"
    | "approval"
    | "auto_approved"
    | "artifact"
    | "tool_result"
    | "tool_start"
    | "tool_end"
    | "phase"
    | "stopped"
    | "usage"
    | "done"
    | "stream_start"
    | "stream_delta"
    | "stream_cancel";
  payload: Record<string, unknown>;
};

export function streamUrl(sessionId: string, after: number): string {
  const params = new URLSearchParams({
    session_id: sessionId,
    after: String(after),
  });
  if (authToken) {
    params.set("token", authToken);
  }
  return `/api/stream?${params.toString()}`;
}

export const AUTH_REQUIRED_MESSAGE =
  "Authentication required. Re-open the launch URL printed by the server (it includes a one-time token).";

const TOKEN_STORAGE_KEY = "xai_auth_token";

function captureTokenFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("token");
  if (fromUrl) {
    try {
      window.sessionStorage.setItem(TOKEN_STORAGE_KEY, fromUrl);
    } catch {
      // sessionStorage unavailable; token will only live in memory for this load
    }
    params.delete("token");
    const remaining = params.toString();
    const newSearch = remaining ? `?${remaining}` : "";
    const newUrl = window.location.pathname + newSearch + window.location.hash;
    try {
      window.history.replaceState({}, "", newUrl);
    } catch {
      // ignore
    }
    return fromUrl;
  }
  try {
    return window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

let authToken: string | null = captureTokenFromUrl();

export function getAuthToken(): string | null {
  return authToken;
}

export function hasAuthToken(): boolean {
  return Boolean(authToken);
}

const jsonHeaders = { "Content-Type": "application/json" };

function authHeaders(extra?: HeadersInit): HeadersInit {
  const headers = new Headers(extra);
  if (authToken) {
    headers.set("X-Auth-Token", authToken);
  }
  return headers;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const merged: RequestInit = { ...(init || {}) };
  merged.headers = authHeaders(init?.headers);
  const response = await fetch(url, merged);
  if (response.status === 401) {
    authToken = null;
    try {
      window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    } catch {
      // ignore
    }
    throw new Error(AUTH_REQUIRED_MESSAGE);
  }
  const data = (await response.json()) as T & { ok?: boolean; error?: string };
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

export async function getStartup(sessionId?: string): Promise<{
  ok: true;
  startup: Startup;
  models: Record<string, string>;
  session: SessionInfo;
  saved_sessions: SavedSession[];
}> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return request(`/api/startup${query}`);
}

export async function getSession(sessionId: string): Promise<{
  ok: true;
  startup: Startup;
  session: SessionInfo;
  events: WebEvent[];
}> {
  return request(`/api/session?session_id=${encodeURIComponent(sessionId)}`);
}

export async function createSession(): Promise<{ ok: true; session: SessionInfo }> {
  return request("/api/sessions", { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function sendMessage(sessionId: string, text: string): Promise<{ ok: true; turn_id: string; session_id: string }> {
  return request("/api/chat", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ session_id: sessionId, text })
  });
}

export async function getEvents(sessionId: string, after: number): Promise<{
  ok: true;
  session: SessionInfo;
  events: WebEvent[];
}> {
  return request(`/api/events?session_id=${encodeURIComponent(sessionId)}&after=${after}`);
}

export async function stopSession(sessionId: string): Promise<{ ok: true; stopping: boolean; session_id: string }> {
  return request("/api/stop", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ session_id: sessionId })
  });
}

export async function answerApproval(sessionId: string, generation: number, answer: "yes" | "cancel"): Promise<{ ok: true }> {
  return request("/api/approval", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ session_id: sessionId, generation, answer })
  });
}

export async function updateSettings(input: {
  session_id?: string;
  dry_run?: boolean;
  verbose?: boolean;
  bypass_approvals?: boolean;
  auto_switch_models?: boolean;
  model?: string;
}): Promise<{ ok: true; startup: Startup }> {
  return request("/api/settings", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(input)
  });
}

export async function updateAllowedRoots(input: { action: "add" | "remove" | "reset"; path?: string }): Promise<{ ok: true; startup: Startup }> {
  return request("/api/allowed-roots", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(input)
  });
}

export async function undoLast(): Promise<Record<string, unknown>> {
  return request("/api/undo", { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function openLogsFolder(): Promise<Record<string, unknown>> {
  return request("/api/open-logs", { method: "POST", headers: jsonHeaders, body: "{}" });
}

export function localFileUrl(path: string): string {
  const params = new URLSearchParams({ path });
  if (authToken) {
    params.set("token", authToken);
  }
  return `/api/local-file?${params.toString()}`;
}
