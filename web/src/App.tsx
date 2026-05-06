import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApprovalCard,
  AUTH_REQUIRED_MESSAGE,
  WebEvent,
  answerApproval,
  createSession,
  getSession,
  getStartup,
  openLogsFolder,
  sendMessage,
  stopSession,
  streamUrl,
  undoLast,
  updateAllowedRoots,
  updateSettings,
  type SavedSession,
  type SessionInfo,
  type Startup
} from "./api";
import {
  ApprovalModal,
  Artifact,
  CompactRail,
  Composer,
  ControlsDrawer,
  Drawer,
  ErrorRecovery,
  OutputsDrawer,
  TaskDrawer,
  Topbar,
  Transcript,
  TranscriptItem
} from "./components";

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
  if (event.kind === "stopped") return { id: String(event.id), role: "stopped", text: "Stopped by user.", ts: event.ts };
  if (event.kind === "auto_approved") return { id: String(event.id), role: "auto_approved", text, ts: event.ts };
  if (event.kind === "tool_result") {
    const result = event.payload.result;
    return {
      id: String(event.id),
      role: "result",
      name: String(event.payload.name ?? "tool_result"),
      result: result && typeof result === "object" ? result as Record<string, unknown> : {},
      ts: event.ts
    };
  }
  if (event.kind === "approval") return { id: String(event.id), role: "approval", card: event.payload.card as ApprovalCard, ts: event.ts };
  return null;
}

function artifactFromEvent(event: WebEvent): Artifact | null {
  const payload = event.kind === "artifact" ? event.payload : event.payload.artifact;
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  return {
    id: `${event.id}-${String(record.path ?? record.kind ?? "artifact")}`,
    kind: String(record.kind ?? "artifact"),
    title: record.title ? String(record.title) : undefined,
    path: record.path ? String(record.path) : undefined,
    tool: record.tool ? String(record.tool) : undefined,
    chars: typeof record.chars === "number" ? record.chars : undefined,
    preview: record.preview ? String(record.preview) : undefined,
  };
}

export function App() {
  const [startup, setStartup] = useState<Startup | null>(null);
  const [models, setModels] = useState<Record<string, string>>({});
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [savedSessions, setSavedSessions] = useState<SavedSession[]>([]);
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [input, setInput] = useState("");
  const [lastPrompt, setLastPrompt] = useState("");
  const [activeApproval, setActiveApproval] = useState<ApprovalCard | null>(null);
  const [status, setStatus] = useState("Connecting to local backend...");
  const [error, setError] = useState<string | null>(null);
  const [toolActivity, setToolActivity] = useState<string | null>(null);
  const [lastPhase, setLastPhase] = useState("Ready");
  const [sending, setSending] = useState(false);
  const [controlsOpen, setControlsOpen] = useState(false);
  const [outputsOpen, setOutputsOpen] = useState(false);
  const [taskOpen, setTaskOpen] = useState(false);
  const [expandedToolGroups, setExpandedToolGroups] = useState<Set<string>>(new Set());
  const [expandedResultIds, setExpandedResultIds] = useState<Set<string>>(new Set());
  const [streamingText, setStreamingText] = useState("");
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const lastEventIdRef = useRef(0);

  useEffect(() => {
    getStartup()
      .then((data) => {
        setStartup(data.startup);
        setModels(data.models);
        setSession(data.session);
        setSavedSessions(data.saved_sessions);
        setStatus("Ready");
      })
      .catch((err: Error) => {
        setError(err.message);
        setStatus("Backend unavailable");
      });
  }, []);

  const processEvent = useCallback((event: WebEvent) => {
    lastEventIdRef.current = Math.max(lastEventIdRef.current, event.id);

    if (event.kind === "stream_start") {
      setStreamingText("");
    } else if (event.kind === "stream_delta") {
      const chunk = String(event.payload.text ?? "");
      if (chunk) setStreamingText((prev) => prev + chunk);
    } else if (event.kind === "stream_cancel") {
      setStreamingText("");
    } else if (event.kind === "assistant") {
      setStreamingText("");
    }

    const transcriptEntry = textFromEvent(event);
    if (transcriptEntry) {
      setItems((prev) => [...prev, transcriptEntry]);
    }
    const artifact = artifactFromEvent(event);
    if (artifact) {
      setArtifacts((prev) => [...prev, artifact]);
    }

    if (event.kind === "approval") {
      setActiveApproval(event.payload.card as ApprovalCard);
      setLastPhase("Waiting for approval");
    } else if (event.kind === "auto_approved") {
      setLastPhase("Auto-approved");
    } else if (event.kind === "phase") {
      setLastPhase(String(event.payload.label ?? event.payload.phase ?? "Working"));
    } else if (event.kind === "tool_start") {
      setToolActivity(String(event.payload.label ?? event.payload.name ?? ""));
      setLastPhase(String(event.payload.label ?? "Running tool"));
    } else if (event.kind === "tool_end") {
      setToolActivity(null);
    } else if (event.kind === "user") {
      setSession((prev) => (prev ? { ...prev, busy: true, stopped: false, active_error: null, event_count: event.id } : prev));
    } else if (event.kind === "stopped") {
      setSending(false);
      setLastPhase("Stopped");
      setActiveApproval(null);
      setStreamingText("");
      setSession((prev) => (prev ? { ...prev, busy: false, stopped: true, event_count: event.id } : prev));
    } else if (event.kind === "done") {
      setSending(false);
      const canceled = Boolean(event.payload.canceled);
      const errorText = event.payload.error ? String(event.payload.error) : null;
      setLastPhase(canceled ? "Stopped" : errorText ? "Error" : "Done");
      setActiveApproval(null);
      setStreamingText("");
      setSession((prev) => (prev ? { ...prev, busy: false, active_error: errorText, stopped: canceled, event_count: event.id } : prev));
    } else if (event.kind === "usage") {
      const totals = (event.payload.totals as Record<string, number> | undefined) ?? null;
      if (totals) {
        setSession((prev) => (prev ? { ...prev, token_totals: totals, event_count: event.id } : prev));
      }
    } else {
      setSession((prev) => (prev ? { ...prev, event_count: event.id } : prev));
    }
  }, []);

  useEffect(() => {
    if (!session?.id) return;
    let cancelled = false;
    let source: EventSource | null = null;
    let consecutiveErrors = 0;

    const open = () => {
      if (cancelled) return;
      const after = lastEventIdRef.current;
      const url = streamUrl(session.id, after);
      const es = new EventSource(url);
      source = es;
      es.onopen = () => {
        consecutiveErrors = 0;
        setError(null);
      };
      es.onmessage = (msg) => {
        if (cancelled) return;
        try {
          const event = JSON.parse(msg.data) as WebEvent;
          processEvent(event);
        } catch {
          // ignore malformed frame
        }
      };
      es.onerror = () => {
        if (cancelled) return;
        consecutiveErrors += 1;
        if (es.readyState === EventSource.CLOSED || consecutiveErrors >= 5) {
          es.close();
          if (consecutiveErrors >= 5) {
            setError("Lost connection to local backend. Reload the page once it is back.");
          }
        }
      };
    };

    open();
    return () => {
      cancelled = true;
      source?.close();
    };
  }, [session?.id, processEvent]);

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight, behavior: "smooth" });
  }, [items.length, streamingText]);

  const isAuthError = error === AUTH_REQUIRED_MESSAGE;

  const tokenTotal = session?.token_totals?.total_tokens ?? 0;
  const modelLabel = startup?.model ?? "unknown";
  const canSend = Boolean(input.trim()) && !session?.busy && !sending;
  const knownModelValues = Object.values(models);
  const selectedModelKnown = startup?.model ? knownModelValues.includes(startup.model) : true;
  const groupedItems = useMemo(() => items.filter((item) => item.role !== "approval"), [items]);

  async function refreshStartup(currentSession = session) {
    const data = await getStartup(currentSession?.id);
    setStartup(data.startup);
    setModels(data.models);
    setSavedSessions(data.saved_sessions);
    setSession(data.session);
  }

  async function submit(event?: FormEvent, overrideText?: string) {
    event?.preventDefault();
    const text = (overrideText ?? input).trim();
    if (!session || !text) return;
    setSending(true);
    setLastPrompt(text);
    setInput("");
    setLastPhase("Starting");
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
    setArtifacts([]);
    lastEventIdRef.current = 0;
    setStreamingText("");
    setActiveApproval(null);
    setError(null);
    await refreshStartup(data.session);
  }

  async function restoreSession(id: string) {
    const data = await getSession(id);
    setStartup(data.startup);
    setSession(data.session);
    setItems(data.events.map(textFromEvent).filter(Boolean) as TranscriptItem[]);
    setArtifacts(data.events.map(artifactFromEvent).filter(Boolean) as Artifact[]);
    lastEventIdRef.current = data.session.event_count;
    setStreamingText("");
    setActiveApproval(null);
    setError(null);
  }

  async function setMode(model: string) {
    const data = await updateSettings({ session_id: session?.id, model });
    setStartup(data.startup);
  }

  async function toggleDryRun() {
    const data = await updateSettings({ session_id: session?.id, dry_run: !startup?.dry_run });
    setStartup(data.startup);
  }

  async function toggleVerbose() {
    const data = await updateSettings({ session_id: session?.id, verbose: !startup?.verbose });
    setStartup(data.startup);
  }

  async function toggleBypassApprovals() {
    const data = await updateSettings({ session_id: session?.id, bypass_approvals: !startup?.bypass_approvals });
    setStartup(data.startup);
    if (session) setSession({ ...session, bypass_approvals: Boolean(data.startup.bypass_approvals) });
  }

  async function toggleModelLock() {
    const autoSwitchOn = startup?.auto_switch_models ?? true;
    const data = await updateSettings({ session_id: session?.id, auto_switch_models: !autoSwitchOn });
    setStartup(data.startup);
  }

  async function addRoot(path: string) {
    try {
      const data = await updateAllowedRoots({ action: "add", path });
      setStartup(data.startup);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function removeRoot(path: string) {
    try {
      const data = await updateAllowedRoots({ action: "remove", path });
      setStartup(data.startup);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function resetRoots() {
    const data = await updateAllowedRoots({ action: "reset" });
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

  async function stop() {
    if (!session) return;
    try {
      await stopSession(session.id);
      setLastPhase("Stopping");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function retryLastPrompt() {
    if (lastPrompt) await submit(undefined, lastPrompt);
  }

  async function switchModelRecovery() {
    const fallback = models["grok-4.3-latest"] ?? Object.values(models)[0];
    if (fallback) await setMode(fallback);
  }

  async function openLogs() {
    try {
      await openLogsFolder();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function toggleToolGroup(id: string) {
    setExpandedToolGroups((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleResult(id: string) {
    setExpandedResultIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <main className="app-shell">
      <Topbar
        session={session}
        status={status}
        model={modelLabel}
        tokens={tokenTotal}
        bypass={Boolean(startup?.bypass_approvals)}
        dryRun={Boolean(startup?.dry_run)}
        activity={toolActivity || lastPhase}
        outputsCount={artifacts.length}
        onOpenControls={() => setControlsOpen(true)}
        onOpenOutputs={() => setOutputsOpen(true)}
        onOpenTask={() => setTaskOpen(true)}
      />
      <section className="layout">
        <CompactRail
          startup={startup}
          models={models}
          selectedModelKnown={selectedModelKnown}
          onModel={(model) => void setMode(model)}
          onUndo={() => void undo()}
          onNewSession={() => void newSession()}
          onOpenControls={() => setControlsOpen(true)}
          onOpenOutputs={() => setOutputsOpen(true)}
          outputsCount={artifacts.length}
        />
        <section className="workspace">
          <div className="transcript-header">
            <div>
              <h1>Conversation</h1>
              <p>{session?.id ? `Session ${session.id}` : "Starting session..."}</p>
            </div>
          </div>
          <Transcript
            items={groupedItems}
            streamingText={streamingText}
            quickPrompts={quickPrompts}
            onPrompt={setInput}
            transcriptRef={transcriptRef}
            expandedToolGroups={expandedToolGroups}
            expandedResultIds={expandedResultIds}
            onToggleToolGroup={toggleToolGroup}
            onToggleResult={toggleResult}
          />
          <ErrorRecovery
            error={error}
            isAuthError={isAuthError}
            bypassOn={Boolean(startup?.bypass_approvals)}
            onRetry={() => void retryLastPrompt()}
            onSwitchModel={() => void switchModelRecovery()}
            onDisableBypass={() => void (startup?.bypass_approvals ? toggleBypassApprovals() : undefined)}
            onOpenLogs={() => void openLogs()}
            onNewSession={() => void newSession()}
            onReload={() => window.location.reload()}
          />
          <Composer
            input={input}
            canSend={canSend}
            busy={Boolean(session?.busy)}
            onInput={setInput}
            onSubmit={(event) => void submit(event)}
            onStop={() => void stop()}
            onTools={() => setControlsOpen(true)}
          />
        </section>
      </section>
      <Drawer title="Controls" open={controlsOpen} onClose={() => setControlsOpen(false)}>
        <ControlsDrawer
          startup={startup}
          savedSessions={savedSessions}
          onModelLock={() => void toggleModelLock()}
          onDryRun={() => void toggleDryRun()}
          onVerbose={() => void toggleVerbose()}
          onBypass={() => void toggleBypassApprovals()}
          onRestoreSession={(id) => void restoreSession(id)}
          onAddRoot={(path) => void addRoot(path)}
          onRemoveRoot={(path) => void removeRoot(path)}
          onResetRoots={() => void resetRoots()}
        />
      </Drawer>
      <Drawer title="Outputs" open={outputsOpen} onClose={() => setOutputsOpen(false)}>
        <OutputsDrawer artifacts={artifacts} />
      </Drawer>
      <Drawer title="Task" open={taskOpen} onClose={() => setTaskOpen(false)}>
        <TaskDrawer session={session} activity={toolActivity} lastPhase={lastPhase} />
      </Drawer>
      <ApprovalModal approval={activeApproval} onApprove={(answer) => void approve(answer)} />
    </main>
  );
}
