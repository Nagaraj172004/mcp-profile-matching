"""Pieces shared by the three MCP servers: error conversion, HTTP routes, startup."""

from __future__ import annotations

import argparse
import functools
import inspect
import os
import time
from typing import Callable, Optional

import mcp_errors
from mcp_settings import load_settings, server_logger

STARTED_AT = time.time()


def guarded(fn: Callable) -> Callable:
    """Turn a ToolFailure raised by a tool into the SDK's ToolError (an isError result)."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except mcp_errors.ToolFailure as failure:
                raise mcp_errors.as_tool_error(failure) from None

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except mcp_errors.ToolFailure as failure:
            raise mcp_errors.as_tool_error(failure) from None

    return wrapper


def add_http_routes(server, server_key: str, describe: Optional[Callable[[], dict]] = None) -> None:
    """GET /health and GET /discovery for the streamable-http transport."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": server.name, "uptime_seconds": round(time.time() - STARTED_AT, 1)})

    @server.custom_route("/discovery", methods=["GET"])
    async def discovery(request: Request) -> JSONResponse:
        tools = await server.list_tools()
        resources = await server.list_resources()
        templates = await server.list_resource_templates()
        prompts = await server.list_prompts()
        body = {
            "server": server.name,
            "version": server.version,
            "protocol": "Model Context Protocol",
            "message_format": "JSON-RPC 2.0",
            "transport": "streamable-http",
            "endpoint": "/mcp",
            "tools": [{"name": t.name, "description": (t.description or "").split("\n")[0]} for t in tools],
            "resources": [{"uri": str(r.uri), "name": r.name, "mime_type": r.mime_type} for r in resources],
            "resource_templates": [{"uri_template": t.uri_template, "name": t.name} for t in templates],
            "prompts": [{"name": p.name, "description": p.description} for p in prompts],
        }
        if describe:
            body["config"] = describe()
        return JSONResponse(body)


def run_server(server, server_key: str, argv=None) -> None:
    """Start a server on stdio (default) or streamable HTTP, from CLI flags or MCP_TRANSPORT."""
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run the {0} MCP server.".format(server.name))
    parser.add_argument("--transport", choices=["stdio", "http"], default=settings.transport.mode)
    parser.add_argument("--host", default=settings.transport.host)
    parser.add_argument("--port", type=int, default=settings.transport.ports[server_key])
    args = parser.parse_args(argv)
    logger = server_logger(server_key)

    if args.transport == "stdio":
        logger.info("starting %s on stdio", server.name)
        server.run("stdio")
        return

    from mcp.server.transport_security import TransportSecuritySettings

    allowed = {"{0}:{1}".format(h, args.port) for h in ("127.0.0.1", "localhost", args.host)}
    allowed.update(h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip())
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=sorted(allowed))
    logger.info("starting %s on http://%s:%s/mcp (allowed hosts: %s)", server.name, args.host, args.port, ", ".join(sorted(allowed)))
    server.run("streamable-http", host=args.host, port=args.port, transport_security=security)
