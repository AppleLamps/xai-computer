export type Startup = {
  model: string;
  desktop: string;
  allowed_roots: string[];
  dry_run: boolean;
  verbose: boolean;
  max_tool_loops: number;
};

export type SessionInfo = {
  id: string;
  created: string;
  busy: boolean;
  event_count: number;
  token_totals: Record<string, number>;
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
    | "tool_start"
    | "tool_end"
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
  saved_sessions: Array<Record<string, unknown>>;
}> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return request(`/api/startup${query}`);
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

export async function answerApproval(sessionId: string, generation: number, answer: "yes" | "cancel"): Promise<{ ok: true }> {
  return request("/api/approval", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ session_id: sessionId, generation, answer })
  });
}

export async function updateSettings(input: { dry_run?: boolean; verbose?: boolean; model?: string }): Promise<{ ok: true; startup: Startup }> {
  return request("/api/settings", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(input)
  });
}

export async function undoLast(): Promise<Record<string, unknown>> {
  return request("/api/undo", { method: "POST", headers: jsonHeaders, body: "{}" });
}
