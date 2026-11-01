"""Render the diagrams in docs/.

    docs/state_machine.png    the compiled LangGraph (generated from the graph itself)
    docs/mcp_architecture.png agent, client hub, and the three MCP servers
    docs/mcp_sequence.png     one conversation as MCP calls
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

os.chdir(Path(__file__).resolve().parent)

from tls import use_system_certificates  # noqa: E402

DOCS = Path("docs")


def render(mermaid: str, target: Path) -> None:
    encoded = base64.urlsafe_b64encode(mermaid.encode("utf-8")).decode("ascii")
    response = httpx.get("https://mermaid.ink/img/{0}?type=png&bgColor=white".format(encoded), timeout=60)
    response.raise_for_status()
    target.write_bytes(response.content)
    print("wrote {0} ({1} bytes)".format(target.as_posix(), len(response.content)))


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    use_system_certificates()
    from matching_agent import build_graph
    from mcp_client import get_hub

    graph_mermaid = build_graph().get_graph().draw_mermaid()
    (DOCS / "state_machine.mmd").write_text(graph_mermaid, encoding="utf-8")
    render(graph_mermaid, DOCS / "state_machine.png")
    for name in ("mcp_architecture", "mcp_sequence"):
        render((DOCS / "{0}.mmd".format(name)).read_text(encoding="utf-8"), DOCS / "{0}.png".format(name))
    get_hub().close()


if __name__ == "__main__":
    main()
