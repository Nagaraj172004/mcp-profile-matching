"""MCP client hub: one long-lived MCP session per server, usable from synchronous code.

The LangGraph agent, the CLI, Streamlit, and the tests all call
hub.call_tool("filesystem", "read_file", {...}) and get plain Python data back.
Underneath, each server gets its own MCP Client inside a single background
asyncio loop, so:

- stdio servers are launched once and stay up (the embedding model in the
  resume search server loads once, not on every call);
- HTTP servers (MCP_TRANSPORT=http, e.g. under docker compose) keep one session;
- every call is logged with server, tool, duration, and outcome, which the
  CLI prints as part of the agent's reasoning trace.

Servers only receive MCP_*, HF_*, and a few Python settings from the
environment. The Groq API key stays in the agent process.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, Callable, Dict, Iterable, List, Optional

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

import mcp_errors as E
from mcp_settings import PROJECT_DIR, SERVER_NAMES, load_settings

SERVER_SCRIPTS = {
    "filesystem": "filesystem_mcp_server.py",
    "resume_search": "resume_search_mcp_server.py",
    "candidate_db": "candidate_db_mcp_server.py",
}
PASS_THROUGH_PREFIXES = ("MCP_", "HF_", "SSL_CERT", "REQUESTS_CA_BUNDLE")
STARTUP_TIMEOUT_SECONDS = 180
CALL_TIMEOUT_SECONDS = 180
MAX_TOOL_TEXT = 6000

# The SDK warns when the server has already closed an HTTP session the client is ending at shutdown.
logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)


class MCPToolError(Exception):
    """A tool or protocol error returned by an MCP server, with its code."""

    def __init__(self, server: str, tool: str, error: dict):
        self.server = server
        self.tool = tool
        self.error = error
        self.code = error.get("code", E.INTERNAL_ERROR)
        self.name = error.get("name", E.ERROR_NAMES.get(self.code, "ERROR"))
        super().__init__("{0}.{1} failed [{2} {3}]: {4}".format(server, tool, self.code, self.name, error.get("message", "")))


def _server_env() -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.startswith(PASS_THROUGH_PREFIXES)}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "ANONYMIZED_TELEMETRY": "False", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"})
    for key in ("SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _parse_result(server: str, tool: str, result) -> Any:
    text = "\n".join(getattr(item, "text", "") for item in (result.content or []) if getattr(item, "text", None))
    if result.is_error:
        raise MCPToolError(server, tool, E.parse_tool_error(text))
    data = result.structured_content
    if data is not None:
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data
    try:
        return json.loads(text)
    except ValueError:
        return text


class MCPHub:
    def __init__(self, servers: Optional[Iterable[str]] = None, transport: Optional[str] = None):
        self.settings = load_settings()
        self.transport = (transport or self.settings.transport.mode).lower()
        self.servers = list(servers or SERVER_NAMES)
        self.tools: Dict[str, list] = {}
        self.server_info: Dict[str, dict] = {}
        self.notifications: List[dict] = []
        self.call_log: List[dict] = []
        self._clients: Dict[str, Client] = {}
        self._stops: Dict[str, asyncio.Event] = {}
        self._tasks: Dict[str, "asyncio.Future"] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "MCPHub":
        with self._lock:
            if self._started:
                return self
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-hub", daemon=True)
            self._thread.start()
            ready = {name: asyncio.run_coroutine_threadsafe(self._open(name), self._loop) for name in self.servers}
            errors = []
            for name, future in ready.items():
                try:
                    future.result(STARTUP_TIMEOUT_SECONDS)
                except Exception as exc:
                    errors.append("{0}: {1}".format(name, exc))
            self._started = True
            if errors:
                self.close()
                raise RuntimeError("Could not connect to MCP servers: " + "; ".join(errors))
            atexit.register(self.close)
            return self

    async def _open(self, name: str) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        self._stops[name] = asyncio.Event()
        self._tasks[name] = asyncio.ensure_future(self._run(name, ready))
        await ready

    async def _run(self, name: str, ready: asyncio.Future) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self.transport == "http":
                    target: Any = self.settings.server_url(name)
                else:
                    log_dir = self.settings.project_path(self.settings.logging.directory)
                    log_dir.mkdir(parents=True, exist_ok=True)
                    errlog = stack.enter_context(open(log_dir / "{0}.stderr.log".format(name), "a", encoding="utf-8"))
                    params = StdioServerParameters(
                        command=sys.executable,
                        args=[str(PROJECT_DIR / SERVER_SCRIPTS[name])],
                        cwd=str(PROJECT_DIR),
                        env=_server_env(),
                    )
                    target = stdio_client(params, errlog=errlog)

                async def on_message(message) -> None:
                    if isinstance(message, Exception):
                        return
                    root = getattr(message, "root", message)
                    method = getattr(root, "method", type(root).__name__)
                    self.notifications.append({"server": name, "method": method, "time": time.time()})

                # "legacy" is the initialize handshake every MCP server and client supports; the SDK's
                # experimental 2026-07-28 "auto" mode drops streamable-HTTP connections in mcp 2.3.
                client = await stack.enter_async_context(
                    Client(target, mode="legacy", message_handler=on_message, read_timeout_seconds=CALL_TIMEOUT_SECONDS))
                listed = await client.list_tools()
                self.tools[name] = list(listed.tools)
                info = client.server_info
                self.server_info[name] = {
                    "name": getattr(info, "name", name),
                    "version": getattr(info, "version", ""),
                    "protocol_version": client.protocol_version,
                    "transport": self.transport,
                }
                self._clients[name] = client
                ready.set_result(True)
                await self._stops[name].wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

    def close(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        for stop in self._stops.values():
            loop.call_soon_threadsafe(stop.set)

        async def wait_all():
            tasks = [t for t in self._tasks.values() if not t.done()]
            if tasks:
                await asyncio.wait(tasks, timeout=10)

        try:
            asyncio.run_coroutine_threadsafe(wait_all(), loop).result(15)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._loop = None
        self._clients.clear()
        self._started = False

    # ---------------------------------------------------------------- calls

    def _client(self, server: str) -> Client:
        if not self._started:
            self.start()
        if server not in self._clients:
            raise KeyError("MCP server '{0}' is not connected (connected: {1}).".format(server, ", ".join(self._clients)))
        return self._clients[server]

    def _run_sync(self, coro, timeout: Optional[float] = None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout or CALL_TIMEOUT_SECONDS)

    def call_tool(self, server: str, tool: str, arguments: Optional[dict] = None,
                  on_progress: Optional[Callable[[float, Optional[float], Optional[str]], None]] = None,
                  timeout: Optional[float] = None) -> Any:
        client = self._client(server)
        started = time.perf_counter()
        entry = {"server": server, "tool": tool, "ok": False}

        async def progress(value, total, message):
            if on_progress:
                on_progress(value, total, message)

        async def call():
            return await client.call_tool(tool, arguments or {}, progress_callback=progress if on_progress else None)

        try:
            result = self._run_sync(call(), timeout)
            data = _parse_result(server, tool, result)
            entry["ok"] = True
            return data
        except MCPToolError as exc:
            entry["error"] = exc.name
            raise
        except MCPError as exc:
            entry["error"] = E.ERROR_NAMES.get(exc.error.code, "ERROR")
            raise MCPToolError(server, tool, {"code": exc.error.code, "message": exc.error.message}) from None
        finally:
            entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.call_log.append(entry)

    def read_resource(self, server: str, uri: str) -> Any:
        client = self._client(server)
        try:
            result = self._run_sync(client.read_resource(uri))
        except MCPError as exc:
            data = exc.error.data if isinstance(exc.error.data, dict) else {}
            raise MCPToolError(server, "resources/read", {"code": exc.error.code, "name": data.get("name"), "message": exc.error.message}) from None
        text = result.contents[0].text if result.contents else ""
        try:
            return json.loads(text)
        except ValueError:
            return text

    def list_resources(self, server: str) -> List[str]:
        client = self._client(server)
        return [str(r.uri) for r in self._run_sync(client.list_resources()).resources]

    def list_resource_templates(self, server: str) -> List[str]:
        client = self._client(server)
        return [t.uri_template for t in self._run_sync(client.list_resource_templates()).resource_templates]

    def list_prompts(self, server: str) -> List[str]:
        client = self._client(server)
        return [p.name for p in self._run_sync(client.list_prompts()).prompts]

    def tool_names(self, server: str) -> List[str]:
        self._client(server)
        return [t.name for t in self.tools.get(server, [])]

    # ----------------------------------------------------- LangChain bridge

    def langchain_tools(self, include: Dict[str, List[str]]) -> list:
        """Wrap MCP tools as LangChain tools whose schema is the server's own inputSchema."""
        from langchain_core.tools import StructuredTool

        wrapped = []
        for server, names in include.items():
            self._client(server)
            for tool in self.tools.get(server, []):
                if tool.name not in names:
                    continue
                wrapped.append(
                    StructuredTool(
                        name=tool.name,
                        description="[MCP {0}] {1}".format(server, (tool.description or "").strip()),
                        args_schema=dict(tool.input_schema or {"type": "object", "properties": {}}),
                        func=self._tool_runner(server, tool.name),
                    )
                )
        return wrapped

    def _tool_runner(self, server: str, tool: str) -> Callable[..., str]:
        def run(**kwargs) -> str:
            try:
                data = self.call_tool(server, tool, kwargs)
            except MCPToolError as exc:
                return json.dumps({"error": exc.error})
            text = json.dumps(data, default=str)
            return text if len(text) <= MAX_TOOL_TEXT else text[:MAX_TOOL_TEXT] + " ... (truncated)"

        return run


_hub: Optional[MCPHub] = None
_hub_lock = threading.Lock()


def get_hub() -> MCPHub:
    """The process-wide hub, connected to every server on first use."""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = MCPHub()
        return _hub.start()
