"""Local process management helpers."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import is_dry_run
from logger import log_event
from shell_guard import validate_working_dir


@dataclass(frozen=True)
class ProcessLaunchVerdict:
    ok: bool
    executable: str
    reason: str = ""


_BLOCKED_PROCESS_EXECUTABLES: frozenset[str] = frozenset({
    # Shells and script hosts
    "cmd", "powershell", "pwsh", "bash", "sh",
    "python", "python3", "py", "node", "npm", "npx",
    "wscript", "cscript", "mshta",
    # System modification / control
    "reg", "regedit", "rundll32", "regsvr32", "schtasks",
    "sc", "wmic", "dism", "sfc", "bcdedit", "bcdboot",
    # Package/download vectors
    "pip", "pip3", "curl", "wget", "bitsadmin",
})


def _process_executable_name(executable: str) -> str:
    cleaned = executable.strip().strip('"').strip("'")
    if not cleaned:
        return ""
    return Path(cleaned).stem.casefold()


def classify_process_launch(executable: str) -> ProcessLaunchVerdict:
    name = _process_executable_name(executable)
    if not name:
        return ProcessLaunchVerdict(False, "", "Executable is required.")
    if name in _BLOCKED_PROCESS_EXECUTABLES:
        return ProcessLaunchVerdict(
            False,
            name,
            f"Executable '{name}' is blocked for start_process; use the constrained run_command tool where appropriate.",
        )
    return ProcessLaunchVerdict(True, name)


def _load_psutil() -> Any:
    try:
        import psutil
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("psutil is required for process tools.") from e
    return psutil


def list_processes(query: str | None = None, limit: int = 25) -> dict[str, Any]:
    try:
        psutil = _load_psutil()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    query_l = (query or "").casefold()
    out: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "status", "cmdline"]):
        try:
            info = proc.info
        except Exception:  # noqa: BLE001
            continue
        name = info.get("name") or ""
        cmdline = " ".join(info.get("cmdline") or [])
        if query_l and query_l not in name.casefold() and query_l not in cmdline.casefold():
            continue
        out.append({"pid": info["pid"], "name": name, "status": info.get("status"), "cmdline": cmdline})
        if len(out) >= max(1, min(limit, 100)):
            break
    return {"ok": True, "processes": out, "count": len(out)}


def start_process(executable: str, args: list[str] | None = None, working_dir: str | None = None) -> dict[str, Any]:
    args = args or []
    verdict = classify_process_launch(executable)
    if not verdict.ok:
        log_event(
            "start_process_blocked",
            {"executable": verdict.executable, "reason": verdict.reason},
            phase="blocked",
        )
        return {
            "ok": False,
            "blocked": True,
            "error": f"Process launch blocked: {verdict.reason}",
            "executable": verdict.executable,
        }
    try:
        cwd = validate_working_dir(working_dir)
    except (PermissionError, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}

    command = [executable, *args]
    if is_dry_run():
        return {"ok": True, "dry_run": True, "command": command, "working_dir": str(cwd)}
    try:
        proc = subprocess.Popen(command, cwd=str(cwd), shell=False)  # noqa: S603
    except FileNotFoundError:
        return {"ok": False, "error": f"Executable not found: {executable}"}
    except OSError as e:
        return {"ok": False, "error": f"Failed to start process: {e}"}
    log_event("start_process", {"command": command, "working_dir": str(cwd), "pid": proc.pid}, phase="executed")
    return {"ok": True, "pid": proc.pid, "command": command, "working_dir": str(cwd)}


def stop_process(pid: int, force: bool = False) -> dict[str, Any]:
    try:
        psutil = _load_psutil()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    if is_dry_run():
        return {"ok": True, "dry_run": True, "pid": pid, "force": force}
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            proc.terminate()
    except psutil.NoSuchProcess:
        return {"ok": False, "error": f"Process not found: {pid}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to stop process {pid}: {e}"}
    log_event("stop_process", {"pid": pid, "force": force}, phase="executed")
    return {"ok": True, "pid": pid, "force": force}


def wait_for_process_exit(pid: int, timeout_sec: float = 10.0) -> dict[str, Any]:
    try:
        psutil = _load_psutil()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    deadline = time.time() + max(0.1, timeout_sec)
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"ok": True, "pid": pid, "exited": True}

    while time.time() < deadline:
        if not proc.is_running():
            return {"ok": True, "pid": pid, "exited": True}
        time.sleep(0.1)
    return {"ok": False, "error": f"Process {pid} still running after {timeout_sec}s.", "pid": pid}
