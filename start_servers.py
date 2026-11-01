"""Run the three MCP servers as HTTP services without Docker.

    python start_servers.py          start all three, wait for /health, then keep them running
    Ctrl+C                           stop them

Then, in another terminal:
    set MCP_TRANSPORT=http           (PowerShell: $env:MCP_TRANSPORT="http")
    python chat_cli.py jobs/senior_python_backend.txt
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

from mcp_client import SERVER_SCRIPTS  # noqa: E402
from mcp_settings import load_settings  # noqa: E402


def wait_healthy(port: int, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:{0}/health".format(port), timeout=2) as response:
                return json.loads(response.read())
        except OSError:
            time.sleep(0.5)
    raise TimeoutError("server on port {0} did not become healthy".format(port))


def main() -> None:
    settings = load_settings()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    processes = []
    try:
        for name, script in SERVER_SCRIPTS.items():
            port = settings.transport.ports[name]
            processes.append(subprocess.Popen([sys.executable, script, "--transport", "http", "--port", str(port)], cwd=str(ROOT), env=env))
        for name in SERVER_SCRIPTS:
            port = settings.transport.ports[name]
            health = wait_healthy(port)
            print("{0:<14} http://127.0.0.1:{1}/mcp  healthy ({2})  discovery: http://127.0.0.1:{1}/discovery".format(name, port, health["server"]))
        print("\nAll MCP servers are up. Press Ctrl+C to stop.")
        while all(p.poll() is None for p in processes):
            time.sleep(1)
        print("A server exited; stopping the others.")
    except KeyboardInterrupt:
        print("\nStopping MCP servers...")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
