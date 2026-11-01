"""Error codes shared by the MCP servers and client.

JSON-RPC 2.0 reserves -32768..-32000. The protocol-level codes below are
returned by the MCP SDK itself; the server codes in -32001..-32099 are this
project's application errors.

Per the MCP specification, a failure while running a tool is reported as a
normal result with isError=true (so the model can read it and recover),
while failures in protocol methods such as resources/read are JSON-RPC error
responses. ToolFailure covers the first case, protocol_error() the second.
Both carry the same numeric code and name.
"""

from __future__ import annotations

import json

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

FILE_NOT_FOUND = -32001
ACCESS_DENIED = -32002
UNSUPPORTED_TYPE = -32003
FILE_TOO_LARGE = -32004
NOT_A_RESUME = -32005
BATCH_LIMIT_EXCEEDED = -32006
WRITE_DENIED = -32007
WATCH_FAILED = -32008
READ_FAILED = -32009
NOT_FOUND = -32010
INDEX_EMPTY = -32011

ERROR_NAMES = {
    PARSE_ERROR: "PARSE_ERROR",
    INVALID_REQUEST: "INVALID_REQUEST",
    METHOD_NOT_FOUND: "METHOD_NOT_FOUND",
    INVALID_PARAMS: "INVALID_PARAMS",
    INTERNAL_ERROR: "INTERNAL_ERROR",
    FILE_NOT_FOUND: "FILE_NOT_FOUND",
    ACCESS_DENIED: "ACCESS_DENIED",
    UNSUPPORTED_TYPE: "UNSUPPORTED_TYPE",
    FILE_TOO_LARGE: "FILE_TOO_LARGE",
    NOT_A_RESUME: "NOT_A_RESUME",
    BATCH_LIMIT_EXCEEDED: "BATCH_LIMIT_EXCEEDED",
    WRITE_DENIED: "WRITE_DENIED",
    WATCH_FAILED: "WATCH_FAILED",
    READ_FAILED: "READ_FAILED",
    NOT_FOUND: "NOT_FOUND",
    INDEX_EMPTY: "INDEX_EMPTY",
}


class ToolFailure(Exception):
    """An application error with a code. Tools raise it; the server turns it into isError."""

    def __init__(self, code: int, message: str, data: dict = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict:
        body = {"code": self.code, "name": ERROR_NAMES.get(self.code, "ERROR"), "message": self.message}
        if self.data:
            body["data"] = self.data
        return body

    def to_json(self) -> str:
        return json.dumps({"error": self.to_dict()})


def protocol_error(code: int, message: str, data: dict = None):
    """A JSON-RPC error response for protocol methods such as resources/read."""
    from mcp.shared.exceptions import MCPError

    body = dict(data or {})
    body["name"] = ERROR_NAMES.get(code, "ERROR")
    return MCPError(code=code, message=message, data=body)


def as_tool_error(failure: ToolFailure):
    """The SDK's ToolError, which the server returns as a result with isError=true."""
    from mcp.server.mcpserver.exceptions import ToolError

    return ToolError(failure.to_json())


def parse_tool_error(text: str) -> dict:
    """Recover {code, name, message} from an isError tool result."""
    start = text.find("{")
    if start != -1:
        try:
            body = json.loads(text[start:])
            if isinstance(body, dict) and "error" in body:
                return body["error"]
        except ValueError:
            pass
    return {"code": INTERNAL_ERROR, "name": "INTERNAL_ERROR", "message": text}
