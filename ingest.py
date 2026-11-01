"""Build the resume index entirely through MCP.

    filesystem.batch_process(operation="read", directory="resumes")
        -> resume_search.index_resumes(resumes=[...], replace=True)

The agent process never opens a resume file itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from mcp_client import get_hub  # noqa: E402


def ingest(directory: str = "resumes", quiet: bool = False) -> dict:
    hub = get_hub()

    def progress(done, total, message):
        if not quiet and total:
            print("\r  reading {0}/{1}".format(int(done), int(total)), end="", flush=True)

    batch = hub.call_tool("filesystem", "batch_process", {"operation": "read", "directory": directory}, on_progress=progress)
    if not quiet:
        print()
    documents = [{"path": r["data"]["path"], "content": r["data"]["content"]} for r in batch["results"] if r["success"]]
    failed = [r for r in batch["results"] if not r["success"]]
    stats = hub.call_tool("resume_search", "index_resumes", {"resumes": documents, "replace": True})
    report = {
        "read": batch["succeeded"],
        "failed": [{"path": r["path"], "error": r["error"]["name"]} for r in failed],
        "batch_ms": batch["elapsed_ms"],
        "indexed_resumes": stats["resumes"],
        "chunks": stats["chunks"],
        "index_seconds": stats["seconds"],
    }
    if not quiet:
        print("filesystem.batch_process read {read} files in {batch_ms} ms ({0} failed)".format(len(report["failed"]), **report))
        print("resume_search.index_resumes stored {indexed_resumes} resumes as {chunks} chunks in {index_seconds} s".format(**report))
    return report


if __name__ == "__main__":
    ingest(sys.argv[1] if len(sys.argv) > 1 else "resumes")
