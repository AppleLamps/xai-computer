import { FormEvent, ReactNode, RefObject, useEffect, useRef, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  Copy,
  CornerDownLeft,
  FolderOpen,
  History,
  Lock,
  Monitor,
  PanelRightOpen,
  RotateCcw,
  Send,
  Settings2,
  Shield,
  Slash,
  Sparkles,
  Square,
  Terminal,
  X
} from "lucide-react";
import { ApprovalAction, ApprovalActionDetails, ApprovalActionTextPreview, ApprovalCard, SavedSession, SessionInfo, Startup, localFileUrl } from "./api";

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

export type SlashCommand = {
  command: string;
  label: string;
  hint: string;
  prompt: string;
};

const SLASH_COMMANDS: SlashCommand[] = [
  {
    command: "/desktop",
    label: "List Desktop",
    hint: "Show contents of the Desktop folder",
    prompt: "List what is on my Desktop and tell me the 5 most recently modified files."
  },
  {
    command: "/recent",
    label: "Recent files",
    hint: "Recently modified files in a folder",
    prompt: "Show me the 10 most recently modified files in my Desktop folder."
  },
  {
    command: "/largest",
    label: "Largest files",
    hint: "Largest files in a folder",
    prompt: "Find the 10 largest files in my Documents folder and summarize their types."
  },
  {
    command: "/screenshot",
    label: "Screenshot",
    hint: "Capture the screen and describe it",
    prompt: "Take a screenshot of my current screen and tell me what visible windows are open."
  },
  {
    command: "/find",
    label: "Find files",
    hint: "Search for files by name or content",
    prompt: "Search my Desktop recursively for files whose names or contents match: "
  },
  {
    command: "/clipboard",
    label: "Read clipboard",
    hint: "Read and summarize current clipboard text",
    prompt: "Read my clipboard and summarize what it contains."
  },
  {
    command: "/windows",
    label: "List windows",
    hint: "List currently visible windows",
    prompt: "List the windows currently open on my screen and what each one appears to be."
  },
  {
    command: "/processes",
    label: "List processes",
    hint: "Show top processes by CPU/memory",
    prompt: "List the top 10 running processes by memory usage."
  }
];

function matchSlash(input: string): SlashCommand[] | null {
  if (!input.startsWith("/")) return null;
  const head = input.split(/\s/, 1)[0].toLowerCase();
  if (head.length < 1) return null;
  const matches = SLASH_COMMANDS.filter((cmd) => cmd.command.startsWith(head));
  return matches.length > 0 ? matches : null;
}

export function Composer({
  input,
  canSend,
  busy,
  onInput,
  onSubmit,
  onStop,
  onSettings,
}: {
  input: string;
  canSend: boolean;
  busy: boolean;
  onInput: (value: string) => void;
  onSubmit: (event?: FormEvent) => void;
  onStop: () => void;
  onSettings: () => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const matches = matchSlash(input);
  const slashOpen = Boolean(matches && matches.length > 0);
  const [highlight, setHighlight] = useState(0);

  useEffect(() => {
    setHighlight(0);
  }, [input]);

  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const next = Math.min(ta.scrollHeight, 360);
    ta.style.height = `${next}px`;
  }, [input]);

  function applyCommand(cmd: SlashCommand) {
    onInput(cmd.prompt);
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (ta) {
        ta.focus();
        ta.setSelectionRange(cmd.prompt.length, cmd.prompt.length);
      }
    });
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (slashOpen && matches) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setHighlight((prev) => (prev + 1) % matches.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setHighlight((prev) => (prev - 1 + matches.length) % matches.length);
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        applyCommand(matches[highlight]);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        applyCommand(matches[highlight]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        onInput("");
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  }

  return (
    <form className="composer" onSubmit={(event) => onSubmit(event)}>
      <div className="composer-surface">
        {slashOpen && matches && (
          <div className="slash-menu" role="listbox" aria-label="Slash commands">
            {matches.map((cmd, index) => (
              <button
                type="button"
                key={cmd.command}
                role="option"
                aria-selected={index === highlight}
                className={cls("slash-item", index === highlight && "active")}
                onMouseEnter={() => setHighlight(index)}
                onClick={() => applyCommand(cmd)}
              >
                <Slash size={13} />
                <div>
                  <strong>{cmd.command} <span className="slash-label">— {cmd.label}</span></strong>
                  <p>{cmd.hint}</p>
                </div>
              </button>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(event) => onInput(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask xai-computer… (try / for commands)"
          rows={1}
        />
        <div className="composer-toolbar">
          <span className="composer-hint">
            <CornerDownLeft size={12} /> <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline · <kbd>/</kbd> commands
          </span>
          <div className="composer-actions">
            <button type="button" className="composer-tool-button" onClick={onSettings} title="Open settings">
              <Settings2 size={16} /> <span>Settings</span>
            </button>
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
      </div>
    </form>
  );
}

function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="approval-field">
      <span className="approval-field-label">{label}</span>
      <div className="approval-field-value">{children}</div>
    </div>
  );
}

function PathBlock({ value }: { value: string }) {
  return <code className="approval-path">{value}</code>;
}

function PreviewBlock({
  preview,
  diff = false,
}: {
  preview: ApprovalActionTextPreview;
  diff?: boolean;
}) {
  const sizeText = preview.bytes === preview.chars
    ? `${preview.chars.toLocaleString()} chars`
    : `${preview.chars.toLocaleString()} chars · ${preview.bytes.toLocaleString()} bytes`;
  return (
    <div className="approval-preview">
      <div className="approval-preview-meta">
        {sizeText}{preview.truncated && " · truncated"}
      </div>
      <pre className={cls("approval-preview-body", diff && "diff")}>
        {diff ? colorizeDiff(preview.preview) : preview.preview}
      </pre>
    </div>
  );
}

function colorizeDiff(text: string): ReactNode {
  return text.split(/\r?\n/).map((line, index) => {
    let cls = "diff-line";
    if (line.startsWith("+++") || line.startsWith("---")) cls += " diff-meta";
    else if (line.startsWith("@@")) cls += " diff-hunk";
    else if (line.startsWith("+")) cls += " diff-add";
    else if (line.startsWith("-")) cls += " diff-del";
    return (
      <span key={index} className={cls}>
        {line || " "}
        {"\n"}
      </span>
    );
  });
}

function ActionDetail({ action }: { action: ApprovalAction }) {
  const details: ApprovalActionDetails = action.details ?? {};
  const tool = action.tool_name;

  if (tool === "run_command") {
    return (
      <div className="approval-detail">
        <DetailField label="Command">
          <code className="approval-cmd">{details.command ?? "?"}</code>
        </DetailField>
        {details.working_dir && (
          <DetailField label="Working dir"><PathBlock value={details.working_dir} /></DetailField>
        )}
        {typeof details.timeout_sec === "number" && (
          <DetailField label="Timeout">{details.timeout_sec}s</DetailField>
        )}
      </div>
    );
  }

  if (tool === "write_file" || tool === "append_file") {
    return (
      <div className="approval-detail">
        {details.path && <DetailField label="Path"><PathBlock value={details.path} /></DetailField>}
        {tool === "write_file" && (
          <DetailField label="Mode">
            {details.overwrite ? "Overwrite (.bak backup)" : "New file"}
          </DetailField>
        )}
        {details.content && <DetailField label="Content"><PreviewBlock preview={details.content} /></DetailField>}
      </div>
    );
  }

  if (tool === "replace_in_file") {
    return (
      <div className="approval-detail">
        {details.path && <DetailField label="Path"><PathBlock value={details.path} /></DetailField>}
        {typeof details.replace_all === "boolean" && (
          <DetailField label="Scope">{details.replace_all ? "All matches" : "First match"}</DetailField>
        )}
        {details.old_text && <DetailField label="Find"><PreviewBlock preview={details.old_text} /></DetailField>}
        {details.new_text && <DetailField label="Replace with"><PreviewBlock preview={details.new_text} /></DetailField>}
      </div>
    );
  }

  if (tool === "apply_patch") {
    return (
      <div className="approval-detail">
        {details.path && <DetailField label="Path"><PathBlock value={details.path} /></DetailField>}
        {typeof details.hunks === "number" && (
          <DetailField label="Hunks">{details.hunks}</DetailField>
        )}
        {details.unified_diff && (
          <DetailField label="Diff"><PreviewBlock preview={details.unified_diff} diff /></DetailField>
        )}
      </div>
    );
  }

  if (tool === "move_file" || tool === "copy_file") {
    return (
      <div className="approval-detail">
        {details.source && <DetailField label="From"><PathBlock value={details.source} /></DetailField>}
        {details.destination && <DetailField label="To"><PathBlock value={details.destination} /></DetailField>}
        {details.overwrite && <DetailField label="Overwrite">Existing destination will be replaced</DetailField>}
      </div>
    );
  }

  if (tool === "rename_file") {
    return (
      <div className="approval-detail">
        {details.source && <DetailField label="Source"><PathBlock value={details.source} /></DetailField>}
        {details.new_name && <DetailField label="New name"><code>{details.new_name}</code></DetailField>}
      </div>
    );
  }

  if (tool === "delete_file_to_recycle_bin") {
    return (
      <div className="approval-detail">
        {details.path && <DetailField label="Path"><PathBlock value={details.path} /></DetailField>}
        <DetailField label="Destination">Recycle Bin (restorable)</DetailField>
      </div>
    );
  }

  if (tool === "create_folder" || tool === "organize_desktop_by_type" || tool === "organize_folder") {
    return (
      <div className="approval-detail">
        {(details.path || details.desktop_path) && (
          <DetailField label="Path"><PathBlock value={(details.path ?? details.desktop_path) as string} /></DetailField>
        )}
        {details.mode && <DetailField label="Mode">{details.mode}</DetailField>}
      </div>
    );
  }

  if (tool === "start_process") {
    return (
      <div className="approval-detail">
        {details.executable && <DetailField label="Executable"><code>{details.executable}</code></DetailField>}
        {details.args && details.args.length > 0 && (
          <DetailField label="Args"><code>{details.args.join(" ")}</code></DetailField>
        )}
        {details.working_dir && <DetailField label="Working dir"><PathBlock value={details.working_dir} /></DetailField>}
      </div>
    );
  }

  if (tool === "stop_process") {
    return (
      <div className="approval-detail">
        {details.pid !== undefined && <DetailField label="PID">{String(details.pid)}</DetailField>}
        {details.force && <DetailField label="Force">Yes (process will be killed)</DetailField>}
      </div>
    );
  }

  if (tool === "browser_navigate") {
    return (
      <div className="approval-detail">
        {details.url && <DetailField label="URL"><code>{details.url}</code></DetailField>}
        {details.wait_for && <DetailField label="Wait for"><code>{details.wait_for}</code></DetailField>}
      </div>
    );
  }

  if (tool === "browser_click" || tool === "browser_press") {
    return (
      <div className="approval-detail">
        {details.selector && <DetailField label="Selector"><code>{details.selector}</code></DetailField>}
        {details.key && <DetailField label="Key"><code>{details.key}</code></DetailField>}
        {details.nth !== undefined && <DetailField label="nth">{details.nth}</DetailField>}
      </div>
    );
  }

  if (tool === "browser_fill") {
    return (
      <div className="approval-detail">
        {details.selector && <DetailField label="Selector"><code>{details.selector}</code></DetailField>}
        {details.text && <DetailField label="Text"><PreviewBlock preview={details.text} /></DetailField>}
      </div>
    );
  }

  if (tool === "browser_download") {
    return (
      <div className="approval-detail">
        {details.url && <DetailField label="URL"><code>{details.url}</code></DetailField>}
        {details.click_selector && <DetailField label="Trigger"><code>{details.click_selector}</code></DetailField>}
        {details.save_as && <DetailField label="Save as"><PathBlock value={details.save_as} /></DetailField>}
      </div>
    );
  }

  if (tool === "browser_screenshot") {
    return (
      <div className="approval-detail">
        {details.selector ? (
          <DetailField label="Selector"><code>{details.selector}</code></DetailField>
        ) : (
          <DetailField label="Scope">{details.full_page ? "Full page" : "Viewport"}</DetailField>
        )}
        {details.save_as && <DetailField label="Save as"><PathBlock value={details.save_as} /></DetailField>}
      </div>
    );
  }

  if (tool === "type_text") {
    return (
      <div className="approval-detail">
        {details.text && <DetailField label="Text"><PreviewBlock preview={details.text} /></DetailField>}
        {details.delay_ms !== undefined && <DetailField label="Delay">{details.delay_ms}ms</DetailField>}
      </div>
    );
  }

  if (tool === "press_hotkey") {
    return (
      <div className="approval-detail">
        {details.keys && (
          <DetailField label="Keys"><code>{details.keys.join(" + ")}</code></DetailField>
        )}
      </div>
    );
  }

  if (tool === "click" || tool === "move_mouse" || tool === "scroll") {
    return (
      <div className="approval-detail">
        {(details.x !== undefined || details.y !== undefined) && (
          <DetailField label="Position">({details.x ?? "?"}, {details.y ?? "?"})</DetailField>
        )}
        {details.button && <DetailField label="Button">{details.button}</DetailField>}
        {details.clicks !== undefined && <DetailField label="Clicks">{details.clicks}</DetailField>}
        {details.amount !== undefined && <DetailField label="Amount">{details.amount}</DetailField>}
        {details.direction && <DetailField label="Direction">{details.direction}</DetailField>}
      </div>
    );
  }

  if (tool === "focus_window") {
    return (
      <div className="approval-detail">
        {details.window_id !== undefined && <DetailField label="Window">{String(details.window_id)}</DetailField>}
        {details.title_substring && <DetailField label="Title contains"><code>{details.title_substring}</code></DetailField>}
      </div>
    );
  }

  if (tool === "read_clipboard") {
    return (
      <div className="approval-detail">
        <DetailField label="Reads">Up to {details.max_chars ?? 5000} characters of clipboard contents</DetailField>
      </div>
    );
  }

  return null;
}

function ShellExplanation({ explanation }: { explanation: Record<string, string> }) {
  const entries = Object.entries(explanation).filter(([, v]) => v && v.trim());
  if (entries.length === 0) return null;
  return (
    <div className="shell-explanation">
      <div className="shell-explanation-title">
        <Terminal size={14} /> What this command does
      </div>
      {entries.map(([key, value]) => (
        <div className="shell-explanation-row" key={key}>
          <span>{prettyShellKey(key)}</span>
          <p>{value}</p>
        </div>
      ))}
    </div>
  );
}

function prettyShellKey(key: string): string {
  if (key === "summary") return "Summary";
  if (key === "side_effects") return "Side effects";
  if (key === "reversibility") return "Reversibility";
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function ApprovalActionRow({
  action,
  expanded,
  onToggle,
}: {
  action: ApprovalAction;
  expanded: boolean;
  onToggle: () => void;
}) {
  const detail = <ActionDetail action={action} />;
  const hasDetail = detail !== null;
  return (
    <div className={cls("approval-row", `risk-${action.risk}`)}>
      <button
        className="approval-row-head"
        type="button"
        onClick={hasDetail ? onToggle : undefined}
        aria-expanded={hasDetail ? expanded : undefined}
      >
        <span className="approval-index">{action.index}</span>
        <div className="approval-row-summary">
          <strong>{action.tool_name}</strong>
          <p>{action.label}</p>
        </div>
        <em className={cls("approval-risk", `risk-${action.risk}`)}>{action.risk}</em>
        {hasDetail && (
          <span className="approval-row-toggle" aria-hidden="true">
            {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          </span>
        )}
      </button>
      {hasDetail && expanded && <div className="approval-row-detail">{detail}</div>}
    </div>
  );
}

export function ApprovalModal({ approval, onApprove }: { approval: ApprovalCard | null; onApprove: (answer: "yes" | "cancel") => void }) {
  const approveRef = useRef<HTMLButtonElement | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (!approval) return;
    setExpanded(() => {
      const next = new Set<number>();
      const expandableIndices = approval.actions
        .filter((a) => ["run_command", "write_file", "append_file", "replace_in_file", "apply_patch"].includes(a.tool_name))
        .map((a) => a.index);
      if (approval.actions.length === 1) next.add(approval.actions[0].index);
      else expandableIndices.slice(0, 2).forEach((i) => next.add(i));
      return next;
    });
    const id = window.setTimeout(() => approveRef.current?.focus(), 30);
    return () => window.clearTimeout(id);
  }, [approval]);

  useEffect(() => {
    if (!approval) return;
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      const isField = tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable;
      if (event.key === "Escape") {
        event.preventDefault();
        onApprove("cancel");
      } else if (event.key === "Enter" && !isField && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        onApprove("yes");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [approval, onApprove]);

  if (!approval) return null;

  function toggle(index: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  const counts = approval.actions.reduce<Record<string, number>>((acc, action) => {
    acc[action.risk] = (acc[action.risk] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="approval-backdrop" role="dialog" aria-modal="true" aria-label="Approval required">
      <section className={cls("approval-card", approval.risk_level)}>
        <header>
          <div>
            <h2>Approval Required</h2>
            <p>{approval.summary}</p>
          </div>
          <span className={cls("risk", `risk-${approval.risk_level}`)}>{approval.risk_level}</span>
        </header>
        <div className="approval-meta">
          {approval.affected_root && (
            <div className="approval-meta-row">
              <FolderOpen size={14} />
              <span>Scope</span>
              <code>{approval.affected_root}</code>
            </div>
          )}
          <div className="approval-meta-row">
            <Shield size={14} />
            <span>Actions</span>
            <em>
              {approval.actions.length} total
              {counts.high ? ` · ${counts.high} high` : ""}
              {counts.medium ? ` · ${counts.medium} medium` : ""}
              {counts.low ? ` · ${counts.low} low` : ""}
            </em>
          </div>
        </div>
        {approval.shell_explanation && <ShellExplanation explanation={approval.shell_explanation} />}
        <div className="approval-actions">
          {approval.actions.map((action) => (
            <ApprovalActionRow
              key={`${action.index}-${action.tool_name}`}
              action={action}
              expanded={expanded.has(action.index)}
              onToggle={() => toggle(action.index)}
            />
          ))}
        </div>
        {approval.dry_run && <div className="dry-run"><ClipboardCheck size={15} /> Dry-run is on; actions will be simulated.</div>}
        <footer>
          <span className="approval-shortcuts">
            <kbd>Esc</kbd> deny · <kbd>Ctrl</kbd>+<kbd>Enter</kbd> approve
          </span>
          <div className="approval-buttons">
            <button className="cancel" onClick={() => onApprove("cancel")}><X size={17} /> Deny</button>
            <button ref={approveRef} className="approve" onClick={() => onApprove("yes")}><Check size={17} /> Approve</button>
          </div>
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
