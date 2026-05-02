import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ClipboardCheck,
  FolderOpen,
  History,
  Loader2,
  Monitor,
  Play,
  RotateCcw,
  Send,
  Shield,
  Sparkles,
  X
} from "lucide-react";
import {
  ApprovalCard,
  WebEvent,
  answerApproval,
  createSession,
  getEvents,
  getStartup,
  sendMessage,
  undoLast,
  updateSettings,
  type SessionInfo,
  type Startup
} from "./api";

type TranscriptItem =
  | { id: string; role: "user" | "assistant" | "info" | "error" | "progress"; text: string; ts?: string }
  | { id: string; role: "approval"; card: ApprovalCard; ts?: string };

const quickPrompts = [
  "List what's on my Desktop and tell me the 5 most recently modified files.",
  "Search my Desktop recursively for markdown files that mention API keys, then summarize matches.",
  "Take a screenshot of my current screen and tell me what visible windows are open."
];

function textFromEvent(event: WebEvent): TranscriptItem | null {
  const text = String(event.payload.text ?? "");
  if (event.kind === "user") return { id: String(event.id), role: "user", text, ts: event.ts };
  if (event.kind === "assistant") return { id: String(event.id), role: "assistant", text, ts: event.ts };
  if (event.kind === "info") return { id: String(event.id), role: "info", text, ts: event.ts };
  if (event.kind === "error") return { id: String(event.id), role: "error", text, ts: event.ts };
  if (event.kind === "progress") return { id: String(event.id), role: "progress", text, ts: event.ts };
  if (event.kind === "approval") return { id: String(event.id), role: "approval", card: event.payload.card as ApprovalCard, ts: event.ts };
  if (event.kind === "tool_start") {
    return { id: String(event.id), role: "progress", text: `Working: ${String(event.payload.label ?? event.payload.name ?? "tool")}`, ts: event.ts };
  }
  if (event.kind === "tool_end") {
    return { id: String(event.id), role: "progress", text: `${event.payload.ok ? "Finished" : "Failed"}: ${String(event.payload.name ?? "tool")}`, ts: event.ts };
  }
  if (event.kind === "done") return { id: String(event.id), role: "info", text: "Turn complete.", ts: event.ts };
  return null;
}

function cls(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export function App() {
  const [startup, setStartup] = useState<Startup | null>(null);
  const [models, setModels] = useState<Record<string, string>>({});
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [lastEventId, setLastEventId] = useState(0);
  const [input, setInput] = useState("");
  const [activeApproval, setActiveApproval] = useState<ApprovalCard | null>(null);
  const [status, setStatus] = useState("Connecting to local backend...");
  const [error, setError] = useState<string | null>(null);
  const [toolActivity, setToolActivity] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getStartup()
      .then((data) => {
        setStartup(data.startup);
        setModels(data.models);
        setSession(data.session);
        setStatus("Ready");
      })
      .catch((err: Error) => {
        setError(err.message);
        setStatus("Backend unavailable");
      });
  }, []);

  useEffect(() => {
    if (!session?.id) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const data = await getEvents(session.id, lastEventId);
        if (cancelled) return;
        setSession(data.session);
        if (data.events.length) {
          setLastEventId(data.events[data.events.length - 1].id);
          const next = data.events.map(textFromEvent).filter(Boolean) as TranscriptItem[];
          setItems((prev) => [...prev, ...next]);
          for (const event of data.events) {
            if (event.kind === "approval") setActiveApproval(event.payload.card as ApprovalCard);
            if (event.kind === "tool_start") setToolActivity(String(event.payload.label ?? event.payload.name ?? ""));
            if (event.kind === "tool_end") setToolActivity(null);
            if (event.kind === "done") setSending(false);
          }
        }
        setError(null);
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    };
    const timer = window.setInterval(poll, session.busy ? 550 : 1100);
    void poll();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [session?.id, session?.busy, lastEventId]);

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight, behavior: "smooth" });
  }, [items.length]);

  const tokenTotal = session?.token_totals?.total_tokens ?? 0;
  const modelLabel = startup?.model ?? "unknown";
  const canSend = Boolean(input.trim()) && !session?.busy && !sending;

  const groupedItems = useMemo(() => items.filter((item) => item.role !== "approval"), [items]);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!session || !input.trim()) return;
    setSending(true);
    const text = input.trim();
    setInput("");
    try {
      await sendMessage(session.id, text);
    } catch (err) {
      setSending(false);
      setError((err as Error).message);
    }
  }

  async function approve(answer: "yes" | "cancel") {
    if (!session || !activeApproval) return;
    const generation = activeApproval.generation;
    setActiveApproval(null);
    try {
      await answerApproval(session.id, generation, answer);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function newSession() {
    const data = await createSession();
    setSession(data.session);
    setItems([]);
    setLastEventId(0);
    setActiveApproval(null);
  }

  async function setMode(model: string) {
    const data = await updateSettings({ model });
    setStartup(data.startup);
  }

  async function toggleDryRun() {
    const data = await updateSettings({ dry_run: !startup?.dry_run });
    setStartup(data.startup);
  }

  async function toggleVerbose() {
    const data = await updateSettings({ verbose: !startup?.verbose });
    setStartup(data.startup);
  }

  async function undo() {
    try {
      const result = await undoLast();
      setItems((prev) => [
        ...prev,
        { id: `undo-${Date.now()}`, role: result.ok ? "info" : "error", text: `Undo Last: ${JSON.stringify(result)}` }
      ]);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><Monitor size={18} /></div>
          <div>
            <strong>xai-computer</strong>
            <span>local computer agent</span>
          </div>
        </div>
        <div className="top-status">
          <span className={cls("live-dot", session?.busy && "busy")} />
          <span>{session?.busy ? "Working" : status}</span>
          <span className="chip">{modelLabel}</span>
          <span className="chip">{tokenTotal.toLocaleString()} tokens</span>
        </div>
      </header>

      <section className="layout">
        <aside className="sidebar">
          <section className="side-section">
            <h2>Workspace</h2>
            <p className="muted">{startup?.desktop ?? "Loading desktop..."}</p>
          </section>

          <section className="side-section">
            <h3>Model</h3>
            <div className="segmented">
              {Object.entries(models).map(([key, value]) => (
                <button key={key} className={cls(startup?.model === value && "selected")} onClick={() => void setMode(value)}>
                  {key}
                </button>
              ))}
            </div>
          </section>

          <section className="side-section">
            <h3>Options</h3>
            <label className="toggle">
              <input type="checkbox" checked={Boolean(startup?.dry_run)} onChange={() => void toggleDryRun()} />
              <span>Dry-run mode</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={Boolean(startup?.verbose)} onChange={() => void toggleVerbose()} />
              <span>Verbose output</span>
            </label>
          </section>

          <section className="side-section action-list">
            <button onClick={() => void undo()}><RotateCcw size={16} /> Undo Last</button>
            <button onClick={() => void newSession()}><Sparkles size={16} /> New Session</button>
          </section>

          <section className="side-section roots">
            <h3>Allowed Roots</h3>
            {(startup?.allowed_roots ?? []).map((root) => (
              <div className="root-row" key={root}><FolderOpen size={14} /> {root}</div>
            ))}
          </section>
        </aside>

        <section className="workspace">
          <div className="transcript-header">
            <div>
              <h1>Transcript</h1>
              <p>{session?.id ? `Session ${session.id}` : "Starting session..."}</p>
            </div>
            {toolActivity && <div className="activity"><Loader2 size={15} className="spin" /> {toolActivity}</div>}
          </div>

          <div className="transcript" ref={transcriptRef}>
            {groupedItems.length === 0 ? (
              <div className="welcome">
                <Shield size={26} />
                <h2>Ready when you are.</h2>
                <p>Ask for a local task. The backend will narrate its plan, show progress, and request approval before sensitive or mutating actions.</p>
                <div className="quick-grid">
                  {quickPrompts.map((prompt) => (
                    <button key={prompt} onClick={() => setInput(prompt)}>{prompt}</button>
                  ))}
                </div>
              </div>
            ) : (
              groupedItems.map((item) => (
                <article key={item.id} className={cls("message", item.role)}>
                  <div className="message-role">{item.role}</div>
                  <div className="message-text">{item.text}</div>
                </article>
              ))
            )}
          </div>

          {error && (
            <div className="error-strip">
              <AlertTriangle size={16} /> {error}
            </div>
          )}

          <form className="composer" onSubmit={(event) => void submit(event)}>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder="Tell the agent what to do..."
            />
            <button type="submit" disabled={!canSend}>
              <Send size={17} />
              Send
            </button>
          </form>
        </section>
      </section>

      {activeApproval && (
        <div className="approval-backdrop">
          <section className={cls("approval-card", activeApproval.risk_level)}>
            <header>
              <div>
                <h2>Approval Required</h2>
                <p>{activeApproval.summary}</p>
              </div>
              <span className="risk">{activeApproval.risk_level}</span>
            </header>
            <div className="approval-actions">
              {activeApproval.actions.map((action) => (
                <div className="approval-row" key={`${action.index}-${action.label}`}>
                  <span>{action.index}</span>
                  <div>
                    <strong>{action.tool_name}</strong>
                    <p>{action.label}</p>
                  </div>
                  <em>{action.risk}</em>
                </div>
              ))}
            </div>
            {activeApproval.dry_run && <div className="dry-run"><ClipboardCheck size={15} /> Dry-run is on; actions will be simulated.</div>}
            <footer>
              <button className="approve" onClick={() => void approve("yes")}><Check size={17} /> Approve</button>
              <button className="cancel" onClick={() => void approve("cancel")}><X size={17} /> Deny</button>
            </footer>
          </section>
        </div>
      )}
    </main>
  );
}
