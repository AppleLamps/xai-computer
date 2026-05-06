import { FormEvent, ReactNode, RefObject, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  Copy,
  FolderOpen,
  History,
  Lock,
  Monitor,
  Plus,
  PanelRightOpen,
  RotateCcw,
  Send,
  Settings2,
  Shield,
  Sparkles,
  Square,
  X
} from "lucide-react";
import { ApprovalCard, SavedSession, SessionInfo, Startup, localFileUrl } from "./api";

export type TranscriptItem =
  | { id: string; role: "user" | "assistant" | "info" | "error" | "progress" | "auto_approved" | "result" | "stopped"; text?: string; ts?: string; name?: string; result?: Record<string, unknown> }
  | { id: string; role: "approval"; card: ApprovalCard; ts?: string };

export type ProgressItem = { id: string; role: "progress"; text: string; ts?: string };

export type Artifact = {
  id: string;
  kind: string;
  title?: string;
  path?: string;
  tool?: string;
  chars?: number;
  preview?: string;
};

export function cls(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

function cleanProgress(text: string): string {
  return text.replace(/^\s*[↳└\-]+\s*/, "").trim();
}

function compactPath(path: string): string {
  if (path.length <= 48) return path;
  const parts = path.split(/[\\/]/).filter(Boolean);
  if (parts.length < 3) return path;
  return `${parts[0]}\\...\\${parts.slice(-2).join("\\")}`;
}

function InlineText({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g).filter(Boolean);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
        if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
        const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
        return <span key={index}>{part}</span>;
      })}
    </>
  );
}

export function MarkdownText({ text }: { text: string }) {
  const lines = text.split(/\r?\n/);
  let inFence = false;
  const fenceLines: string[] = [];
  return (
    <div className="markdown-text">
      {lines.map((line, index) => {
        const trimmed = line.trimEnd();
        if (trimmed.trim().startsWith("```")) {
          if (!inFence) {
            inFence = true;
            fenceLines.length = 0;
            return null;
          }
          inFence = false;
          return <pre className="md-codeblock" key={index}><code>{fenceLines.join("\n")}</code></pre>;
        }
        if (inFence) {
          fenceLines.push(line);
          return null;
        }
        if (!trimmed) return <div className="md-space" key={index} />;
        const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
        if (heading) {
          const level = Math.min(heading[1].length, 4);
          return <div className={`md-heading md-h${level}`} key={index}><InlineText text={heading[2]} /></div>;
        }
        const bullet = trimmed.match(/^\s*[-*•]\s+(.*)$/);
        if (bullet) {
          return (
            <div className="md-bullet" key={index}>
              <span className="md-dot" />
              <span><InlineText text={bullet[1]} /></span>
            </div>
          );
        }
        const numbered = trimmed.match(/^\s*(\d+)[.)]\s+(.*)$/);
        if (numbered) {
          return (
            <div className="md-numbered" key={index}>
              <span>{numbered[1]}.</span>
              <span><InlineText text={numbered[2]} /></span>
            </div>
          );
        }
        return <p key={index}><InlineText text={trimmed.trim()} /></p>;
      })}
    </div>
  );
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function smallTable(rows: Record<string, unknown>[], columns: string[]) {
  return (
    <div className="result-table-wrap">
      <table className="result-table">
        <thead><tr>{columns.map((col) => <th key={col}>{col}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, 8).map((row, index) => (
            <tr key={index}>
              {columns.map((col) => <td key={col}>{String(row[col] ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 8 && <p className="result-more">{rows.length - 8} more result(s)</p>}
    </div>
  );
}

function resultSummary(name: string, result: Record<string, unknown>): { title: string; detail: string; rows?: Record<string, unknown>[]; columns?: string[] } {
  if (result.ok === false) return { title: name, detail: String(result.error ?? "Tool failed") };
  if (name === "list_directory") {
    const files = asRecordArray(result.files);
    const folders = asRecordArray(result.folders);
    return {
      title: "Directory contents",
      detail: `${String(result.path ?? "")} · ${folders.length} folder(s), ${files.length} file(s)`,
      rows: [...folders, ...files],
      columns: ["name", "size_display"]
    };
  }
  if (name === "search_file_contents") {
    return {
      title: "Content search",
      detail: `${String(result.count ?? 0)} match(es), ${String(result.scanned_files ?? 0)} file(s) scanned`,
      rows: asRecordArray(result.matches),
      columns: ["name", "line", "snippet"]
    };
  }
  if (name === "recursive_find_files") {
    return {
      title: "File search",
      detail: `${String(result.count ?? 0)} match(es)`,
      rows: asRecordArray(result.matches),
      columns: ["name", "path"]
    };
  }
  if (name === "recent_files" || name === "largest_files") {
    return {
      title: name === "recent_files" ? "Recent files" : "Largest files",
      detail: `${asRecordArray(result.files).length} file(s) returned`,
      rows: asRecordArray(result.files),
      columns: ["name", "size_display", "modified"]
    };
  }
  if (name.includes("screenshot") && typeof result.path === "string") {
    return { title: "Screenshot saved", detail: result.path };
  }
  if (name === "copy_to_clipboard") {
    return { title: "Clipboard updated", detail: `${String(result.chars ?? 0)} character(s) copied.` };
  }
  if (result.destination || result.path) {
    return { title: name, detail: String(result.destination ?? result.path) };
  }
  return { title: name, detail: "Completed successfully." };
}

export function StructuredResult({
  name,
  result,
  expanded,
  onToggle,
}: {
  name: string;
  result: Record<string, unknown>;
  expanded: boolean;
  onToggle: () => void;
}) {
  const summary = resultSummary(name, result);
  const hasDetails = Boolean(summary.rows?.length || (name.includes("screenshot") && typeof result.path === "string"));
  return (
    <div className={cls("structured-result", result.ok === false && "failed")}>
      <button className="result-summary" type="button" onClick={hasDetails ? onToggle : undefined}>
        <span className="result-icon">{hasDetails ? (expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />) : <Check size={14} />}</span>
        <span>
          <strong>{summary.title}</strong>
          <em>{summary.detail}</em>
        </span>
      </button>
      {expanded && summary.rows && summary.columns && smallTable(summary.rows, summary.columns)}
      {expanded && name.includes("screenshot") && typeof result.path === "string" && (
        <img className="result-image" src={localFileUrl(result.path)} alt="Screenshot preview" />
      )}
    </div>
  );
}

export function ProgressCluster({
  items,
  expanded,
  onToggle,
}: {
  items: ProgressItem[];
  expanded: boolean;
  onToggle: () => void;
}) {
  if (!items.length) return null;
  const latest = cleanProgress(items[items.length - 1].text);
  const shown = expanded ? items : items.slice(-1);
  return (
    <div className="progress-cluster">
      <button className="progress-title" type="button" onClick={onToggle}>
        <span className="activity-mark" />
        <span>{latest || "Tool activity"}</span>
        <em>{items.length > 1 ? `${items.length} steps` : "1 step"}</em>
        {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
      {expanded && (
        <div className="progress-list">
          {shown.map((item) => (
            <div className="progress-row" key={item.id}>
              <span className="progress-node" />
              <span>{cleanProgress(item.text)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Transcript({
  items,
  streamingText,
  quickPrompts,
  onPrompt,
  transcriptRef,
  expandedToolGroups,
  expandedResultIds,
  onToggleToolGroup,
  onToggleResult,
}: {
  items: TranscriptItem[];
  streamingText?: string;
  quickPrompts: string[];
  onPrompt: (prompt: string) => void;
  transcriptRef: RefObject<HTMLDivElement | null>;
  expandedToolGroups: Set<string>;
  expandedResultIds: Set<string>;
  onToggleToolGroup: (id: string) => void;
  onToggleResult: (id: string) => void;
}) {
  const showStreamingBubble = Boolean(streamingText && streamingText.length > 0);
  return (
    <div className="transcript" ref={transcriptRef}>
      <div className="transcript-inner">
        {items.length === 0 && !showStreamingBubble ? (
          <div className="welcome">
            <Shield size={28} />
            <h2>Ready when you are.</h2>
            <p>Ask for a local task. I’ll narrate the plan, show only useful progress, and ask before sensitive or mutating actions.</p>
            <div className="quick-grid">
              {quickPrompts.map((prompt) => <button key={prompt} onClick={() => onPrompt(prompt)}>{prompt}</button>)}
            </div>
          </div>
        ) : (
          items.map((item, index) => {
            if (item.role === "progress") {
              const previous = items[index - 1];
              if (previous?.role === "progress") return null;
              const cluster: ProgressItem[] = [];
              for (let i = index; i < items.length; i += 1) {
                const candidate = items[i];
                if (candidate.role !== "progress") break;
                cluster.push(candidate as ProgressItem);
              }
              const groupId = `progress-${item.id}`;
              return (
                <ProgressCluster
                  key={item.id}
                  items={cluster}
                  expanded={expandedToolGroups.has(groupId)}
                  onToggle={() => onToggleToolGroup(groupId)}
                />
              );
            }
            if (item.role === "result" && item.name && item.result) {
              return (
                <StructuredResult
                  key={item.id}
                  name={item.name}
                  result={item.result}
                  expanded={expandedResultIds.has(item.id)}
                  onToggle={() => onToggleResult(item.id)}
                />
              );
            }
            if (item.role === "approval") return null;
            return (
              <article key={item.id} className={cls("message", item.role)}>
                <div className="message-role">{item.role === "auto_approved" ? "auto-approved" : item.role}</div>
                <div className="message-text">
                  {item.role === "assistant" ? <MarkdownText text={item.text ?? ""} /> : item.text}
                </div>
              </article>
            );
          })
        )}
        {showStreamingBubble && (
          <article className={cls("message", "assistant", "streaming")} aria-live="polite">
            <div className="message-role">assistant</div>
            <div className="message-text">
              <MarkdownText text={streamingText ?? ""} />
              <span className="streaming-caret" aria-hidden="true" />
            </div>
          </article>
        )}
      </div>
    </div>
  );
}

export function CompactRail({
  startup,
  models,
  selectedModelKnown,
  outputsCount,
  onModel,
  onUndo,
  onNewSession,
  onOpenControls,
  onOpenOutputs,
}: {
  startup: Startup | null;
  models: Record<string, string>;
  selectedModelKnown: boolean;
  outputsCount: number;
  onModel: (model: string) => void;
  onUndo: () => void;
  onNewSession: () => void;
  onOpenControls: () => void;
  onOpenOutputs: () => void;
}) {
  return (
    <aside className="control-rail">
      <section className="rail-section workspace-chip">
        <span>Workspace</span>
        <strong title={startup?.desktop}>{startup?.desktop ? compactPath(startup.desktop) : "Loading..."}</strong>
      </section>
      <section className="rail-section">
        <label className="select-wrap">
          <span className="rail-label">Model</span>
          <select value={startup?.model ?? ""} onChange={(event) => onModel(event.target.value)}>
            {!selectedModelKnown && startup?.model && <option value={startup.model}>{startup.model}</option>}
            {Object.entries(models).map(([key, value]) => <option key={key} value={value}>{key}</option>)}
          </select>
        </label>
      </section>
      <section className="rail-actions">
        <button onClick={onNewSession}><Sparkles size={16} /> New</button>
        <button onClick={onUndo}><RotateCcw size={16} /> Undo</button>
        <button onClick={onOpenControls}><Settings2 size={16} /> Controls</button>
        <button onClick={onOpenOutputs}><PanelRightOpen size={16} /> Outputs {outputsCount > 0 && <span>{outputsCount}</span>}</button>
      </section>
    </aside>
  );
}

export function Drawer({
  title,
  open,
  onClose,
  children,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="drawer" onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <h2>{title}</h2>
          <button onClick={onClose} aria-label={`Close ${title}`}><X size={18} /></button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}

export function ControlsDrawer({
  startup,
  savedSessions,
  onModelLock,
  onDryRun,
  onVerbose,
  onBypass,
  onRestoreSession,
  onAddRoot,
  onRemoveRoot,
  onResetRoots,
}: {
  startup: Startup | null;
  savedSessions: SavedSession[];
  onModelLock: () => void;
  onDryRun: () => void;
  onVerbose: () => void;
  onBypass: () => void;
  onRestoreSession: (id: string) => void;
  onAddRoot: (path: string) => void;
  onRemoveRoot: (path: string) => void;
  onResetRoots: () => void;
}) {
  return (
    <div className="drawer-stack">
      <section className="drawer-section">
        <h3>Session Controls</h3>
        <label className="toggle">
          <input type="checkbox" checked={!Boolean(startup?.auto_switch_models ?? true)} onChange={onModelLock} />
          <span><Lock size={14} /> Keep selected model for all tasks</span>
        </label>
        <label className="toggle"><input type="checkbox" checked={Boolean(startup?.dry_run)} onChange={onDryRun} /><span>Dry-run mode</span></label>
        <label className="toggle"><input type="checkbox" checked={Boolean(startup?.verbose)} onChange={onVerbose} /><span>Verbose output</span></label>
        <label className="toggle danger-toggle"><input type="checkbox" checked={Boolean(startup?.bypass_approvals)} onChange={onBypass} /><span><AlertOctagon size={14} /> BYPASS ALL</span></label>
        <p className="setting-note">Session-only. Auto-approves approval cards; hard safety blocks, path validation, dry-run, undo, and logging still apply.</p>
      </section>
      <RootsEditor roots={startup?.allowed_roots ?? []} onAdd={onAddRoot} onRemove={onRemoveRoot} onReset={onResetRoots} />
      <SessionsList sessions={savedSessions} onRestore={onRestoreSession} />
      <section className="drawer-section diagnostics">
        <h3>Diagnostics</h3>
        <dl>
          <div><dt>Max tool loops</dt><dd>{startup?.max_tool_loops ?? "..."}</dd></div>
          <div><dt>Desktop</dt><dd>{startup?.desktop ?? "..."}</dd></div>
        </dl>
      </section>
    </div>
  );
}

function RootsEditor({ roots, onAdd, onRemove, onReset }: { roots: string[]; onAdd: (path: string) => void; onRemove: (path: string) => void; onReset: () => void }) {
  const [path, setPath] = useState("");
  return (
    <section className="drawer-section roots">
      <h3>Allowed Roots</h3>
      {roots.map((root) => (
        <div className="root-row" key={root}>
          <FolderOpen size={14} /> <span>{root}</span>
          <button className="mini-button" onClick={() => onRemove(root)}>Remove</button>
        </div>
      ))}
      <div className="root-editor">
        <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="Add folder path..." />
        <button onClick={() => { onAdd(path); setPath(""); }}>Add</button>
      </div>
      <button className="link-button" onClick={onReset}>Reset roots</button>
    </section>
  );
}

function SessionsList({ sessions, onRestore }: { sessions: SavedSession[]; onRestore: (id: string) => void }) {
  return (
    <section className="drawer-section sessions-list">
      <h3><History size={15} /> Recent Sessions</h3>
      {sessions.length === 0 ? <p className="muted">(none yet)</p> : sessions.slice(0, 8).map((session) => (
        <button key={session.id} onClick={() => session.id && onRestore(session.id)}>
          <strong>{session.title || "(untitled)"}</strong>
          <span>{session.updated || session.created || session.id}</span>
        </button>
      ))}
    </section>
  );
}

export function Composer({
  input,
  canSend,
  busy,
  onInput,
  onSubmit,
  onStop,
  onTools,
}: {
  input: string;
  canSend: boolean;
  busy: boolean;
  onInput: (value: string) => void;
  onSubmit: (event?: FormEvent) => void;
  onStop: () => void;
  onTools: () => void;
}) {
  return (
    <form className="composer" onSubmit={(event) => onSubmit(event)}>
      <div className="composer-surface">
        <textarea
          value={input}
          onChange={(event) => onInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
          placeholder="Ask xai-computer..."
        />
        <div className="composer-toolbar">
          <div className="composer-tools">
            <button type="button" className="composer-icon" onClick={onTools} aria-label="Open tools">
              <Plus size={20} />
            </button>
            <button type="button" className="composer-tool-button" onClick={onTools}>
              <Settings2 size={16} /> Tools
            </button>
          </div>
          {busy ? (
            <button type="button" className="composer-send stop-button" onClick={onStop} aria-label="Stop">
              <Square size={15} />
            </button>
          ) : (
            <button type="submit" className="composer-send" disabled={!canSend} aria-label="Send">
              <Send size={17} />
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

export function ApprovalModal({ approval, onApprove }: { approval: ApprovalCard | null; onApprove: (answer: "yes" | "cancel") => void }) {
  if (!approval) return null;
  return (
    <div className="approval-backdrop">
      <section className={cls("approval-card", approval.risk_level)}>
        <header>
          <div><h2>Approval Required</h2><p>{approval.summary}</p></div>
          <span className="risk">{approval.risk_level}</span>
        </header>
        <div className="approval-actions">
          {approval.actions.map((action) => (
            <div className="approval-row" key={`${action.index}-${action.label}`}>
              <span>{action.index}</span>
              <div><strong>{action.tool_name}</strong><p>{action.label}</p></div>
              <em>{action.risk}</em>
            </div>
          ))}
        </div>
        {approval.dry_run && <div className="dry-run"><ClipboardCheck size={15} /> Dry-run is on; actions will be simulated.</div>}
        <footer>
          <button className="approve" onClick={() => onApprove("yes")}><Check size={17} /> Approve</button>
          <button className="cancel" onClick={() => onApprove("cancel")}><X size={17} /> Deny</button>
        </footer>
      </section>
    </div>
  );
}

export function TaskDrawer({ session, activity, lastPhase }: { session: SessionInfo | null; activity: string | null; lastPhase: string }) {
  const phase = session?.busy ? (activity || lastPhase || "Working") : session?.stopped ? "Stopped" : "Ready";
  return (
    <section className="drawer-section task-panel">
      <h3>Task</h3>
      <div className={cls("task-state", session?.busy && "working")}>
        <span className={cls("live-dot", session?.busy && "busy")} />
        <span>{phase}</span>
      </div>
      <p>{session?.busy ? "The agent is working cooperatively and can be stopped from the composer." : "No active task."}</p>
    </section>
  );
}

export function OutputsDrawer({ artifacts }: { artifacts: Artifact[] }) {
  return (
    <div className="drawer-stack outputs-panel">
      {artifacts.length === 0 ? <p className="muted">No outputs yet.</p> : artifacts.slice().reverse().map((artifact) => (
        <div className="artifact-card" key={artifact.id}>
          <strong>{artifact.title || artifact.kind}</strong>
          {artifact.path && <span>{artifact.path}</span>}
          {artifact.kind === "screenshot" && artifact.path && <img src={localFileUrl(artifact.path)} alt="Output preview" />}
          {artifact.preview && <p>{artifact.preview}</p>}
          {artifact.path && <button onClick={() => void navigator.clipboard?.writeText(artifact.path || "")}><Copy size={13} /> Copy path</button>}
        </div>
      ))}
    </div>
  );
}

export function ErrorRecovery({
  error,
  isAuthError,
  bypassOn,
  onRetry,
  onSwitchModel,
  onDisableBypass,
  onOpenLogs,
  onNewSession,
  onReload,
}: {
  error: string | null;
  isAuthError?: boolean;
  bypassOn: boolean;
  onRetry: () => void;
  onSwitchModel: () => void;
  onDisableBypass: () => void;
  onOpenLogs: () => void;
  onNewSession: () => void;
  onReload?: () => void;
}) {
  if (!error) return null;
  if (isAuthError) {
    return (
      <div className="error-strip auth">
        <AlertTriangle size={16} />
        <span>
          Authentication needed. Copy the launch URL from your terminal — the line that
          starts with <code>http://...?token=...</code> — and paste it into this tab.
        </span>
        {onReload && <button onClick={onReload}>Reload tab</button>}
      </div>
    );
  }
  return (
    <div className="error-strip">
      <AlertTriangle size={16} />
      <span>{error}</span>
      <button onClick={onRetry}>Retry</button>
      <button onClick={onSwitchModel}>Switch model</button>
      {bypassOn && <button onClick={onDisableBypass}>Turn off BYPASS</button>}
      <button onClick={onOpenLogs}>Open logs</button>
      <button onClick={onNewSession}>New session</button>
    </div>
  );
}

export function Topbar({
  session,
  status,
  model,
  tokens,
  bypass,
  dryRun,
  activity,
  outputsCount,
  onOpenControls,
  onOpenOutputs,
  onOpenTask,
}: {
  session: SessionInfo | null;
  status: string;
  model: string;
  tokens: number;
  bypass: boolean;
  dryRun: boolean;
  activity: string | null;
  outputsCount: number;
  onOpenControls: () => void;
  onOpenOutputs: () => void;
  onOpenTask: () => void;
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark"><Monitor size={17} /></div>
        <div><strong>xai-computer</strong><span>local computer agent</span></div>
      </div>
      <button className={cls("task-pill", session?.busy && "busy")} onClick={onOpenTask}>
        <span className={cls("live-dot", session?.busy && "busy")} />
        <span>{session?.busy ? (activity || "Working") : status}</span>
      </button>
      <div className="top-actions">
        {bypass && <button className="safety-pill danger" onClick={onOpenControls}><AlertOctagon size={14} /> BYPASS ALL · session only</button>}
        {dryRun && <button className="safety-pill" onClick={onOpenControls}>Dry-run</button>}
        <button className="chip" onClick={onOpenControls}>{model}</button>
        <button className="chip quiet" onClick={onOpenTask}>{tokens.toLocaleString()} tokens</button>
        <button className="icon-button" onClick={onOpenOutputs} aria-label="Open outputs"><PanelRightOpen size={17} />{outputsCount > 0 && <span>{outputsCount}</span>}</button>
        <button className="icon-button" onClick={onOpenControls} aria-label="Open controls"><Settings2 size={17} /></button>
      </div>
    </header>
  );
}
