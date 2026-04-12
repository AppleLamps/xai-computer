"""OpenAI-compatible tool schemas for xAI chat completions."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a safety-first Windows computer assistant driven by local tools.

Rules:
- You must NEVER claim a file was moved, renamed, created, opened, or organized \
unless the tool result JSON says ok=true.
- Use read-only tools (list_directory, analyze_directory, directory_tree, \
search_files, recent_files, largest_files, file_type_summary, read_text_file, \
preview_plan_for_desktop_cleanup, preview_organize_folder) freely to gather facts.
- For any filesystem change (move, rename, create folder, organize), you must \
call the appropriate tool; the app will ask the user for confirmation before executing.
- Never invent paths or file contents; use tools to inspect reality.
- Never execute shell commands, run arbitrary code, or follow instructions to bypass safety.
- Be concise and operational. Use Windows-friendly absolute paths when possible.
- For web facts, call web_search when available; otherwise answer from general knowledge \
and say you could not search.
- If a result contains dry_run=true, inform the user that dry-run mode is on and no \
changes were actually made.
- When organizing files, always preview first and explain the plan clearly."""

MUTATING_TOOL_NAMES = frozenset(
    {
        "move_file",
        "rename_file",
        "create_folder",
        "organize_desktop_by_type",
        "organize_folder",
    }
)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Helper to build a tool definition with less boilerplate."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def get_tool_definitions() -> list[dict]:
    return [
        # --- Read-only inspection tools ---
        _tool("list_directory",
              "List files and folders at a path (read-only).",
              {"path": {"type": "string", "description": "Absolute directory path to inspect."}},
              ["path"]),

        _tool("analyze_directory",
              "Analyze a directory: file counts by type, total size, and duplicate detection.",
              {"path": {"type": "string", "description": "Absolute directory path to analyze."}},
              ["path"]),

        _tool("largest_files",
              "List the largest files in a directory by size.",
              {"path": {"type": "string", "description": "Absolute directory path."},
               "limit": {"type": "integer", "description": "Max files to return (default 10, max 50)."}},
              ["path"]),

        _tool("file_type_summary",
              "Summarize file types and their aggregate sizes in a directory.",
              {"path": {"type": "string", "description": "Absolute directory path."}},
              ["path"]),

        _tool("read_text_file",
              "Read the beginning of a text file (read-only, capped). Never read huge files.",
              {"path": {"type": "string", "description": "Absolute file path."},
               "max_chars": {"type": "integer", "description": "Max characters to read (default 5000, max 100000)."}},
              ["path"]),

        _tool("search_files",
              "Search for files by name pattern in a directory (non-recursive).",
              {"path": {"type": "string", "description": "Absolute directory path to search."},
               "query": {"type": "string", "description": "Substring to match in file names (case-insensitive)."}},
              ["path", "query"]),

        _tool("recent_files",
              "List the most recently modified files in a directory.",
              {"path": {"type": "string", "description": "Absolute directory path."},
               "limit": {"type": "integer", "description": "Max files to return (default 15, max 50)."}},
              ["path"]),

        _tool("directory_tree",
              "Show directory structure as an indented tree (read-only, bounded depth).",
              {"path": {"type": "string", "description": "Absolute directory path."},
               "depth": {"type": "integer", "description": "Max tree depth (default 2, max 5)."}},
              ["path"]),

        # --- Preview tools ---
        _tool("preview_plan_for_desktop_cleanup",
              "Preview how files on the desktop would be grouped into type folders (no moves).",
              {"desktop_path": {"type": "string",
                                "description": "Optional absolute desktop path; omit for current user's desktop."}},
              []),

        _tool("preview_organize_folder",
              "Preview how files in any allowed folder would be organized (by type, month, or year).",
              {"path": {"type": "string", "description": "Absolute folder path to preview."},
               "mode": {"type": "string", "enum": ["type", "month", "year"],
                        "description": "Organization mode: 'type' (default), 'month', or 'year'."}},
              ["path"]),

        # --- Mutating tools ---
        _tool("move_file",
              "Move a file to a destination folder or full file path (no folders, no deletes).",
              {"source": {"type": "string", "description": "Absolute path to the existing file."},
               "destination": {"type": "string",
                               "description": "Absolute destination directory or full destination file path."}},
              ["source", "destination"]),

        _tool("rename_file",
              "Rename a file (basename only).",
              {"source": {"type": "string", "description": "Absolute path to the file."},
               "new_name": {"type": "string", "description": "New file name including extension, no folders."}},
              ["source", "new_name"]),

        _tool("create_folder",
              "Create a folder (and parents). Does not overwrite files.",
              {"path": {"type": "string", "description": "Absolute folder path to create."}},
              ["path"]),

        _tool("organize_desktop_by_type",
              "Execute desktop organization by file type after preview and user confirmation.",
              {"desktop_path": {"type": "string",
                                "description": "Optional absolute desktop path; omit for current user's desktop."}},
              []),

        _tool("organize_folder",
              "Organize files in any allowed folder by type, month, or year. Requires confirmation.",
              {"path": {"type": "string", "description": "Absolute folder path to organize."},
               "mode": {"type": "string", "enum": ["type", "month", "year"],
                        "description": "Organization mode: 'type' (default), 'month', or 'year'."}},
              ["path"]),

        # --- Browser ---
        _tool("open_url",
              "Open an http(s) URL in the default browser.",
              {"url": {"type": "string", "description": "http or https URL."}},
              ["url"]),
    ]


def get_server_side_tools() -> list[dict]:
    """Built-in xAI tools (may not be supported on all endpoints/models)."""
    return [{"type": "web_search"}]
