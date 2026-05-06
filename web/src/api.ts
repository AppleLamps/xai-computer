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
    | "done";
  payload: Record<string, unknown>;
};

const jsonHeaders = { "Content-Type": "application/json" };

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
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
  return `/api/local-file?path=${encodeURIComponent(path)}`;
}
