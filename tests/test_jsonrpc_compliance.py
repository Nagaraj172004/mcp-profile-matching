"""Raw JSON-RPC 2.0 conversation with filesystem_mcp_server.py over stdio.

No MCP client library is used here: the test writes newline-delimited JSON
to the server's stdin and checks every response by hand, so it shows the
wire format the protocol requires.
"""

import json
import queue
import subprocess
import sys
import threading
import time

import pytest

from conftest import ROOT

PROTOCOL_VERSION = "2025-11-25"


class RawServer:
    def __init__(self, script: str):
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "ANONYMIZED_TELEMETRY": "False"}
        for key in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME"):
            if key in __import__("os").environ:
                env[key] = __import__("os").environ[key]
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / script)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line)

    def send_raw(self, text: str) -> None:
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def send(self, message: dict) -> None:
        self.send_raw(json.dumps(message))

    def receive(self, request_id, timeout: float = 30.0) -> dict:
        """Next message with this id, skipping notifications sent in between."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            message = json.loads(line)
            assert message.get("jsonrpc") == "2.0", message
            if message.get("id") == request_id:
                return message
        raise AssertionError("no response for id {0}".format(request_id))

    def request(self, request_id, method: str, params: dict = None) -> dict:
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        self.send(body)
        return self.receive(request_id)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture(scope="module")
def server():
    raw = RawServer("filesystem_mcp_server.py")
    response = raw.request(1, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "raw-jsonrpc-test", "version": "1.0"},
    })
    raw.initialize_response = response
    raw.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield raw
    raw.close()


def test_initialize_handshake(server):
    response = server.initialize_response
    assert response["id"] == 1 and "result" in response and "error" not in response
    result = response["result"]
    assert result["serverInfo"]["name"] == "resume-filesystem"
    assert result["protocolVersion"] == PROTOCOL_VERSION
    for capability in ("tools", "resources", "prompts"):
        assert capability in result["capabilities"]


def test_tools_list_discovery(server):
    response = server.request(2, "tools/list")
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert {"list_files", "read_file", "write_file", "search_in_file", "watch_directory", "batch_process"} <= names
    batch = next(t for t in response["result"]["tools"] if t["name"] == "batch_process")
    assert batch["inputSchema"]["type"] == "object"
    assert "ctx" not in batch["inputSchema"]["properties"]
    assert batch["inputSchema"]["properties"]["operation"]["enum"] == ["read", "metadata", "search", "summary"]


def test_resource_discovery(server):
    resources = server.request(3, "resources/list")["result"]["resources"]
    assert {"resumes://index", "jobs://index", "config://server", "watch://events"} <= {r["uri"] for r in resources}
    templates = server.request(4, "resources/templates/list")["result"]["resourceTemplates"]
    assert {"resumes://{filename}", "jobs://{filename}"} <= {t["uriTemplate"] for t in templates}
    prompts = server.request(5, "prompts/list")["result"]["prompts"]
    assert "screen_resume" in {p["name"] for p in prompts}


def test_tool_call_success(server):
    response = server.request(6, "tools/call", {"name": "list_files", "arguments": {"directory": "jobs"}})
    result = response["result"]
    assert result.get("isError") in (None, False)
    body = json.loads(result["content"][0]["text"])
    assert body["count"] == 6


def test_resource_read(server):
    response = server.request(7, "resources/read", {"uri": "resumes://resume_nagaraj.txt"})
    contents = response["result"]["contents"][0]
    assert contents["uri"] == "resumes://resume_nagaraj.txt"
    assert contents["text"].startswith("Nagaraj")


def test_tool_error_is_result_with_code(server):
    """Tool failures come back as results with isError=true, carrying the application code."""
    response = server.request(8, "tools/call", {"name": "read_file", "arguments": {"filepath": "dataset.py"}})
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    assert '"code": -32002' in result["content"][0]["text"]
    assert "ACCESS_DENIED" in result["content"][0]["text"]


def test_resource_error_is_jsonrpc_error(server):
    response = server.request(9, "resources/read", {"uri": "resumes://does_not_exist.txt"})
    assert "result" not in response
    assert response["error"]["code"] == -32001
    assert response["error"]["data"]["name"] == "FILE_NOT_FOUND"


def test_method_not_found(server):
    response = server.request(10, "files/delete_everything", {})
    assert response["error"]["code"] == -32601


def test_invalid_params(server):
    response = server.request(11, "tools/call", {"arguments": {}})
    assert response["error"]["code"] == -32602


def test_string_ids_are_echoed(server):
    response = server.request("abc-123", "tools/list")
    assert response["id"] == "abc-123"


def test_malformed_json_does_not_kill_server(server):
    server.send_raw("{this is not json")
    deadline = time.monotonic() + 5
    parse_error = None
    while time.monotonic() < deadline and parse_error is None:
        try:
            message = json.loads(server.lines.get(timeout=0.5))
        except queue.Empty:
            continue
        if "error" in message and message.get("id") is None:
            parse_error = message
    if parse_error is not None:
        assert parse_error["error"]["code"] == -32700
    response = server.request(12, "tools/list")
    assert "result" in response
