"""Filesystem MCP server: the Milestone 1 resume tools over the Model Context Protocol.

Run it on stdio (how the agent launches it) or as an HTTP service:

    python filesystem_mcp_server.py                     # stdio, JSON-RPC 2.0 on stdin/stdout
    python filesystem_mcp_server.py --transport http    # http://127.0.0.1:8101/mcp

Tools
    list_files, read_file, write_file, search_in_file   Milestone 1 tools
    watch_directory                                      wait for new, changed, or deleted resumes
    batch_process                                        read / inspect / search many files in parallel

Resources
    resumes://index, resumes://{filename}               resume listing and text
    jobs://index, jobs://{filename}                     job descriptions
    config://server                                      effective configuration
    watch://events                                       recent file-change events

Every path is resolved inside the allowed folders from mcp_config.json, so a
request for "../.env" or an absolute path elsewhere is refused with
ACCESS_DENIED instead of being read.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from mcp.server.mcpserver import Context, MCPServer

import fs_tools
import mcp_errors as E
from mcp_common import add_http_routes, guarded, run_server
from mcp_settings import PROJECT_DIR, load_settings, server_logger

SETTINGS = load_settings()
CONF = SETTINGS.filesystem
LOG = server_logger("filesystem")
SECTION_HEADINGS = ("summary", "skills", "experience", "education", "projects")


# ------------------------------------------------------------------ sandbox

class Sandbox:
    """Resolves client paths and refuses anything outside the configured folders."""

    def __init__(self, allowed: List[str], writable: List[str]):
        self.allowed = [(PROJECT_DIR / d).resolve() for d in allowed]
        self.writable = [(PROJECT_DIR / d).resolve() for d in writable]

    @staticmethod
    def _inside(path: Path, roots: List[Path]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def resolve(self, relative: str, write: bool = False) -> Path:
        if not isinstance(relative, str) or not relative.strip():
            raise E.ToolFailure(E.INVALID_PARAMS, "A non-empty path is required.")
        candidate = Path(relative.strip())
        if not candidate.is_absolute():
            candidate = PROJECT_DIR / candidate
        resolved = candidate.resolve()
        roots = self.writable if write else self.allowed
        if not self._inside(resolved, roots):
            names = ", ".join(self.display(r) for r in roots)
            if write:
                raise E.ToolFailure(E.WRITE_DENIED, "Writing to '{0}' is not allowed. Writable folders: {1}.".format(relative, names))
            raise E.ToolFailure(E.ACCESS_DENIED, "'{0}' is outside the allowed folders: {1}.".format(relative, names))
        return resolved

    @staticmethod
    def display(path: Path) -> str:
        try:
            return path.resolve().relative_to(PROJECT_DIR).as_posix()
        except ValueError:
            return path.name


SANDBOX = Sandbox(CONF.allowed_directories, CONF.writable_directories)
SUPPORTED = {ext.lower() for ext in CONF.supported_extensions}
MAX_BYTES = int(CONF.max_file_size_mb * 1024 * 1024)


def _metadata(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "path": Sandbox.display(path),
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def _readable_file(relative: str) -> Path:
    path = SANDBOX.resolve(relative)
    if not path.exists():
        raise E.ToolFailure(E.FILE_NOT_FOUND, "File not found: {0}".format(relative))
    if not path.is_file():
        raise E.ToolFailure(E.INVALID_PARAMS, "Not a file: {0}".format(relative))
    if path.suffix.lower() not in SUPPORTED:
        raise E.ToolFailure(E.UNSUPPORTED_TYPE, "Unsupported type '{0}'. Supported: {1}.".format(path.suffix, ", ".join(sorted(SUPPORTED))))
    if path.stat().st_size > MAX_BYTES:
        raise E.ToolFailure(E.FILE_TOO_LARGE, "{0} is larger than the {1} MB limit.".format(relative, CONF.max_file_size_mb))
    return path


def _directory(relative: str) -> Path:
    path = SANDBOX.resolve(relative)
    if not path.is_dir():
        raise E.ToolFailure(E.FILE_NOT_FOUND, "Directory not found: {0}".format(relative))
    return path


def _read_text(relative: str) -> dict:
    path = _readable_file(relative)
    loaded = fs_tools.read_file(str(path))
    if not loaded.get("success"):
        error = loaded.get("error", "Could not read file.")
        code = E.NOT_A_RESUME if "does not look like a resume" in error else E.READ_FAILED
        raise E.ToolFailure(code, "{0}: {1}".format(Sandbox.display(path), error))
    return {"path": Sandbox.display(path), "content": loaded["content"], "metadata": _metadata(path)}


def _pattern(keyword: str, whole_word: bool) -> re.Pattern:
    body = re.escape(keyword)
    if whole_word:
        body = r"(?<!\w)" + body + r"(?!\w)"
    return re.compile(body, re.IGNORECASE)


# ------------------------------------------------------------ watching

class DirectoryWatcher:
    """Polls watched folders on a background thread and keeps a numbered event log.

    Polling instead of OS file events keeps behaviour identical on Windows,
    macOS, Linux, and Docker bind mounts (which do not forward inotify). A
    file is reported only after its size and timestamp hold steady for one
    poll, so a resume that is still being copied is not read half-written.
    """

    def __init__(self, interval: float, max_events: int = 500):
        self.interval = interval
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=max_events)
        self._next_id = 1
        self._emitted: Dict[Path, Dict[str, tuple]] = {}
        self._last_seen: Dict[Path, Dict[str, tuple]] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @staticmethod
    def _scan(directory: Path) -> Dict[str, tuple]:
        found = {}
        try:
            for entry in directory.iterdir():
                if entry.is_file() and entry.suffix.lower() in SUPPORTED:
                    stat = entry.stat()
                    found[entry.name] = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            pass
        return found

    def watch(self, directory: Path) -> None:
        with self._lock:
            if directory not in self._emitted:
                snapshot = self._scan(directory)
                self._emitted[directory] = dict(snapshot)
                self._last_seen[directory] = dict(snapshot)
                LOG.info("watching %s (%d files)", Sandbox.display(directory), len(snapshot))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="directory-watcher", daemon=True)
                self._thread.start()

    @property
    def last_event_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    def _record(self, kind: str, directory: Path, name: str) -> None:
        path = directory / name
        event = {
            "id": self._next_id,
            "type": kind,
            "name": name,
            "path": Sandbox.display(path),
            "directory": Sandbox.display(directory),
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        if kind != "deleted" and path.exists():
            event["size_bytes"] = path.stat().st_size
        self._next_id += 1
        self._events.append(event)
        LOG.info("watch event %s %s", kind, event["path"])

    def poll_once(self) -> None:
        with self._lock:
            for directory in list(self._emitted):
                current = self._scan(directory)
                emitted = self._emitted[directory]
                last_seen = self._last_seen[directory]
                for name, signature in current.items():
                    if signature == last_seen.get(name) and signature != emitted.get(name):
                        self._record("created" if name not in emitted else "modified", directory, name)
                        emitted[name] = signature
                for name in [n for n in emitted if n not in current]:
                    self._record("deleted", directory, name)
                    del emitted[name]
                self._last_seen[directory] = current

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception:
                LOG.exception("watcher poll failed")

    def events_since(self, after_id: int, directory: Optional[Path] = None) -> List[dict]:
        wanted = Sandbox.display(directory) if directory else None
        with self._lock:
            return [dict(e) for e in self._events if e["id"] > after_id and (wanted is None or e["directory"] == wanted)]


WATCHER = DirectoryWatcher(CONF.watch_poll_interval_seconds)


# ------------------------------------------------------------- batching

def _summary_of(content: str) -> dict:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    lowered = [line.lower() for line in lines]
    sections = [heading for heading in SECTION_HEADINGS if heading in lowered]
    skills = ""
    if "skills" in lowered:
        index = lowered.index("skills")
        skills = lines[index + 1] if index + 1 < len(lines) else ""
    years = re.search(r"(\d+)\s*\+?\s*years", content, flags=re.IGNORECASE)
    return {
        "name": lines[0] if lines else "",
        "headline": lines[1] if len(lines) > 1 else "",
        "sections": sections,
        "skills": skills,
        "years_of_experience": int(years.group(1)) if years else None,
        "word_count": len(content.split()),
    }


def _process_one(operation: str, relative: str, keywords: List[str], whole_word: bool, max_chars: Optional[int]) -> dict:
    if operation == "metadata":
        path = _readable_file(relative)
        return _metadata(path)
    loaded = _read_text(relative)
    content = loaded["content"]
    if operation == "read":
        if max_chars and len(content) > max_chars:
            loaded["content"] = content[:max_chars]
            loaded["truncated"] = True
        return loaded
    if operation == "summary":
        summary = _summary_of(content)
        summary["path"] = loaded["path"]
        return summary
    lines = content.splitlines()
    matches = {}
    counts = {}
    for keyword in keywords:
        pattern = _pattern(keyword, whole_word)
        hits = [{"line_number": n + 1, "line": line.strip()[:200]} for n, line in enumerate(lines) if pattern.search(line)]
        counts[keyword] = len(hits)
        matches[keyword] = hits[:5]
    return {"path": loaded["path"], "match_counts": counts, "matches": matches}


# --------------------------------------------------------------- server

server = MCPServer(
    CONF.server_name,
    title="Resume filesystem",
    description="Sandboxed access to resumes, job descriptions, and reports.",
    instructions=(
        "Use list_files or the resumes://index resource to discover resumes, read_file to read one, "
        "batch_process for many at once, and watch_directory to wait for new resumes. "
        "Paths are relative to the project, for example resumes/resume_nagaraj.txt."
    ),
    version="1.0.0",
    log_level=SETTINGS.logging.level,
)


@server.tool()
@guarded
def list_files(directory: str, extension: Optional[str] = None) -> dict:
    """List files in an allowed folder such as 'resumes' or 'jobs', optionally filtered by extension like '.pdf'."""
    path = _directory(directory)
    files = fs_tools.list_files(str(path), extension)
    items = [_metadata(Path(f["path"])) for f in files]
    return {"directory": Sandbox.display(path), "count": len(items), "files": items}


@server.tool()
@guarded
def read_file(filepath: str) -> dict:
    """Read a .pdf, .txt, or .docx resume or job description and return its text plus file metadata."""
    return _read_text(filepath)


@server.tool()
@guarded
def write_file(filepath: str, content: str) -> dict:
    """Write a text file (.md, .txt, .json) inside a writable folder such as 'reports'."""
    path = SANDBOX.resolve(filepath, write=True)
    if path.suffix.lower() not in {e.lower() for e in CONF.writable_extensions}:
        raise E.ToolFailure(E.UNSUPPORTED_TYPE, "Cannot write '{0}' files. Allowed: {1}.".format(path.suffix, ", ".join(CONF.writable_extensions)))
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise E.ToolFailure(E.FILE_TOO_LARGE, "Content is larger than the {0} MB limit.".format(CONF.max_file_size_mb))
    result = fs_tools.write_file(str(path), content)
    if not result.get("success"):
        raise E.ToolFailure(E.READ_FAILED, result.get("error", "Write failed."))
    return {"path": Sandbox.display(path), "bytes_written": result["bytes_written"]}


@server.tool()
@guarded
def search_in_file(filepath: str, keyword: str) -> dict:
    """Case-insensitive keyword search in one file. Returns each matching line with one line of context."""
    path = _readable_file(filepath)
    result = fs_tools.search_in_file(str(path), keyword)
    if not result.get("success"):
        error = result.get("error", "Search failed.")
        code = E.INVALID_PARAMS if "Keyword" in error else (E.NOT_A_RESUME if "resume" in error else E.READ_FAILED)
        raise E.ToolFailure(code, error)
    return {"path": Sandbox.display(path), "keyword": keyword, "match_count": result["match_count"], "matches": result["matches"]}


@server.tool()
@guarded
async def watch_directory(
    ctx: Context,
    directory: str = "resumes",
    timeout_seconds: float = 30.0,
    since_event_id: Optional[int] = None,
) -> dict:
    """Monitor a folder for new, modified, or deleted resumes.

    Waits up to timeout_seconds for changes after since_event_id (by default,
    changes from now on) and returns them with last_event_id, which you pass
    to the next call to keep monitoring without missing anything. New resumes
    also trigger a resources/list_changed notification.
    """
    path = _directory(directory)
    timeout = max(0.0, min(float(timeout_seconds), CONF.watch_max_timeout_seconds))
    WATCHER.watch(path)
    cursor = WATCHER.last_event_id if since_event_id is None else int(since_event_id)
    started = time.monotonic()
    events: List[dict] = []
    while True:
        events = WATCHER.events_since(cursor, path)
        elapsed = time.monotonic() - started
        if events or elapsed >= timeout:
            break
        await ctx.report_progress(round(elapsed, 1), timeout, "watching {0}".format(Sandbox.display(path)))
        await asyncio.sleep(min(CONF.watch_poll_interval_seconds / 2, max(0.05, timeout - elapsed)))
    new_resumes = [e["path"] for e in events if e["type"] == "created"]
    if new_resumes:
        await ctx.notify_resources_changed()
        LOG.info("watch_directory reported new resumes: %s", ", ".join(new_resumes))
    return {
        "directory": Sandbox.display(path),
        "watched_seconds": round(time.monotonic() - started, 1),
        "timed_out": not events,
        "events": events,
        "new_resumes": new_resumes,
        "last_event_id": events[-1]["id"] if events else cursor,
    }


@server.tool()
@guarded
async def batch_process(
    ctx: Context,
    operation: Literal["read", "metadata", "search", "summary"],
    paths: Optional[List[str]] = None,
    directory: Optional[str] = None,
    extension: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    whole_word: bool = True,
    max_chars: Optional[int] = None,
    max_workers: Optional[int] = None,
) -> dict:
    """Run one operation over many files in parallel.

    operation: read (text + metadata), metadata (size, dates), search (keyword
    hits per file), or summary (name, headline, sections, skills, years).
    Give either paths or a directory (optionally filtered by extension).
    One bad file does not fail the batch: each result has success and either
    data or an error with a code.
    """
    if bool(paths) == bool(directory):
        raise E.ToolFailure(E.INVALID_PARAMS, "Give either paths or directory, not both or neither.")
    if operation == "search" and not keywords:
        raise E.ToolFailure(E.INVALID_PARAMS, "operation 'search' needs at least one keyword.")
    if directory:
        folder = _directory(directory)
        paths = [Sandbox.display(Path(f["path"])) for f in fs_tools.list_files(str(folder), extension)]
    paths = list(paths or [])
    if len(paths) > CONF.max_batch_size:
        raise E.ToolFailure(E.BATCH_LIMIT_EXCEEDED, "{0} files requested; the limit is {1}.".format(len(paths), CONF.max_batch_size),
                            {"requested": len(paths), "limit": CONF.max_batch_size})
    workers = max(1, min(int(max_workers or CONF.batch_max_workers), CONF.batch_max_workers))
    started = time.perf_counter()
    results: List[Optional[dict]] = [None] * len(paths)

    def run(index: int, relative: str) -> tuple:
        try:
            return index, {"path": relative, "success": True, "data": _process_one(operation, relative, keywords or [], whole_word, max_chars)}
        except E.ToolFailure as failure:
            return index, {"path": relative, "success": False, "error": failure.to_dict()}
        except Exception as exc:
            LOG.exception("batch item failed: %s", relative)
            return index, {"path": relative, "success": False, "error": {"code": E.READ_FAILED, "name": "READ_FAILED", "message": str(exc)}}

    loop = asyncio.get_running_loop()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [loop.run_in_executor(pool, run, i, p) for i, p in enumerate(paths)]
        for future in asyncio.as_completed(futures):
            index, item = await future
            results[index] = item
            done += 1
            await ctx.report_progress(done, len(paths), "{0} {1}/{2}".format(operation, done, len(paths)))

    elapsed = time.perf_counter() - started
    succeeded = sum(1 for r in results if r and r["success"])
    LOG.info("batch %s: %d files, %d ok, %.0f ms", operation, len(paths), succeeded, elapsed * 1000)
    return {
        "operation": operation,
        "total": len(paths),
        "succeeded": succeeded,
        "failed": len(paths) - succeeded,
        "workers": workers,
        "elapsed_ms": round(elapsed * 1000, 1),
        "files_per_second": round(len(paths) / elapsed, 1) if elapsed > 0 else None,
        "results": results,
    }


# ------------------------------------------------------------ resources

def _resource_read(relative: str) -> str:
    try:
        return _read_text(relative)["content"]
    except E.ToolFailure as failure:
        raise E.protocol_error(failure.code, failure.message, {"path": relative}) from None


def _index(folder: str) -> dict:
    path = PROJECT_DIR / folder
    files = [_metadata(Path(f["path"])) for f in fs_tools.list_files(str(path))] if path.is_dir() else []
    files = [f for f in files if f["extension"] in SUPPORTED]
    return {"directory": folder, "count": len(files), "files": files}


@server.resource("resumes://index", name="resume_index", description="Every resume file with size and modified time.", mime_type="application/json")
def resume_index() -> dict:
    return _index("resumes")


@server.resource("resumes://{filename}", name="resume", description="Plain text of one resume, e.g. resumes://resume_nagaraj.txt.", mime_type="text/plain")
def resume(filename: str) -> str:
    return _resource_read("resumes/" + filename)


@server.resource("jobs://index", name="job_index", description="Every job description file.", mime_type="application/json")
def job_index() -> dict:
    return _index("jobs")


@server.resource("jobs://{filename}", name="job", description="Text of one job description, e.g. jobs://frontend_react.txt.", mime_type="text/plain")
def job(filename: str) -> str:
    return _resource_read("jobs/" + filename)


@server.resource("config://server", name="server_config", description="Effective filesystem server configuration.", mime_type="application/json")
def server_config() -> dict:
    return describe()


@server.resource("watch://events", name="watch_events", description="Recent file-change events seen by watch_directory.", mime_type="application/json")
def watch_events() -> dict:
    return {"last_event_id": WATCHER.last_event_id, "events": WATCHER.events_since(0)}


@server.prompt(name="screen_resume", description="Ask a model to screen one resume against one job description.")
def screen_resume(resume_file: str, job_file: str) -> str:
    return (
        "Read jobs://{1} and resumes://{0}. List which must-have skills the resume shows with a quoted line as evidence, "
        "what is missing, and whether the candidate meets the minimum years. End with hire, borderline, or no-hire."
    ).format(resume_file, job_file)


def describe() -> dict:
    return {
        "server_name": CONF.server_name,
        "allowed_directories": CONF.allowed_directories,
        "writable_directories": CONF.writable_directories,
        "supported_extensions": sorted(SUPPORTED),
        "writable_extensions": CONF.writable_extensions,
        "max_file_size_mb": CONF.max_file_size_mb,
        "max_batch_size": CONF.max_batch_size,
        "batch_max_workers": CONF.batch_max_workers,
        "watch_poll_interval_seconds": CONF.watch_poll_interval_seconds,
        "watch_max_timeout_seconds": CONF.watch_max_timeout_seconds,
        "error_codes": {name: code for code, name in E.ERROR_NAMES.items()},
    }


add_http_routes(server, "filesystem", describe)


if __name__ == "__main__":
    run_server(server, "filesystem")
