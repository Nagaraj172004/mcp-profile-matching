"""Terminal chat for the matching agent.

python chat_cli.py                 start with a prompt
python chat_cli.py jobs/ml_engineer.txt   start from a job file
python chat_cli.py --quiet         hide the node-by-node reasoning trace
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from matching_agent import MatchingSession  # noqa: E402
from mcp_client import get_hub  # noqa: E402

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

EXAMPLES = """Start with a job file (jobs/frontend_react.txt), a pasted job description,
or a request like: Find me candidates with React and 3+ years experience
Then try: Compare the top 3 | Why did X rank higher than Y? | Make Jest a must-have
          Watch for new resumes | Have we shortlisted Nagaraj before? | done"""


def main() -> None:
    if os.name == "nt":
        os.system("")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    quiet = "--quiet" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print("Connecting to MCP servers...")
    session = MatchingSession()
    hub = get_hub()
    for name, info in hub.server_info.items():
        print("{0}  [mcp] {1}: {2} v{3} over {4}, protocol {5}, {6} tools{7}".format(
            MAGENTA, name, info["name"], info["version"], info["transport"], info["protocol_version"], len(hub.tools.get(name, [])), RESET))
    print()

    def show(event: dict) -> None:
        if event["kind"] == "answer":
            print("\n{0}Agent:{1}\n{2}".format(CYAN, RESET, event["text"]))
        elif quiet:
            return
        elif event["kind"] == "trace":
            print("{0}  [{1}] {2}{3}".format(DIM, event["node"], event["text"], RESET))
        elif event["kind"] == "tool_result":
            print("{0}  [tool] {1}{2}".format(YELLOW, event["text"][:220], RESET))
        elif event["kind"] == "mcp":
            print("{0}  [mcp] {1}{2}".format(MAGENTA, event["text"], RESET))

    print("Candidate matching agent (LangGraph). Type 'done' to finish, 'new' to start over.\n")
    print(EXAMPLES)
    pending = " ".join(args) if args else None
    while True:
        if pending is None:
            try:
                pending = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
        if not pending:
            pending = None
            continue
        if pending.lower() in {"quit", "exit"} and not session.started:
            break
        if pending.lower() == "new":
            session = MatchingSession()
            print("Started a new conversation.")
            pending = None
            continue
        session.send(pending, show)
        pending = None
        if session.finished:
            again = input("\nStart another search? (y/n): ").strip().lower()
            if again != "y":
                break
            session = MatchingSession()


if __name__ == "__main__":
    main()
