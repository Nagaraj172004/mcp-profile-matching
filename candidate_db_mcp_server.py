"""Candidate database MCP server: hiring history in SQLite behind the Model Context Protocol.

Every report the agent generates is saved as a shortlist, and recruiter
decisions are recorded per candidate, so a later conversation can ask
"have we shortlisted Nagaraj before?" and get an answer from real records.

    python candidate_db_mcp_server.py                    # stdio
    python candidate_db_mcp_server.py --transport http   # http://127.0.0.1:8103/mcp

Tools: save_shortlist, record_decision, get_candidate_history, list_shortlists,
query_records (read-only SELECT). Resources: db://schema, db://shortlists/recent.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import List, Literal, Optional

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

import mcp_errors as E
from mcp_common import add_http_routes, guarded, run_server
from mcp_settings import load_settings, server_logger

SETTINGS = load_settings()
CONF = SETTINGS.candidate_db
LOG = server_logger("candidate_db")
DB_PATH = SETTINGS.project_path(CONF.database_path)
MAX_QUERY_ROWS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS shortlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL,
    requirements TEXT NOT NULL,
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS shortlist_candidates (
    shortlist_id INTEGER NOT NULL REFERENCES shortlists(id),
    rank INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    score INTEGER NOT NULL,
    verdict TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_name TEXT,
    role TEXT,
    decision TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_sc_candidate ON shortlist_candidates(candidate_id);
CREATE INDEX IF NOT EXISTS idx_decisions_candidate ON decisions(candidate_id);
"""


def _connect(readonly: bool = False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        connection = sqlite3.connect("file:{0}?mode=ro".format(DB_PATH.as_posix()), uri=True)
        connection.execute("PRAGMA query_only = ON")
    else:
        connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _init() -> None:
    with closing(_connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ShortlistEntry(BaseModel):
    rank: int
    candidate_id: str
    candidate_name: str
    score: int
    verdict: str = Field(description="hire, borderline, or no-hire")


server = MCPServer(
    CONF.server_name,
    title="Candidate database",
    description="SQLite records of shortlists and hiring decisions.",
    instructions="get_candidate_history answers 'have we seen this person before?'. query_records runs read-only SELECT statements.",
    version="1.0.0",
    log_level=SETTINGS.logging.level,
)


@server.tool()
@guarded
def save_shortlist(role: str, requirements: dict, candidates: List[ShortlistEntry], report_path: Optional[str] = None) -> dict:
    """Store a generated shortlist (role, requirements, ranked candidates with verdicts)."""
    if not candidates:
        raise E.ToolFailure(E.INVALID_PARAMS, "A shortlist needs at least one candidate.")
    with closing(_connect()) as connection:
        cursor = connection.execute(
            "INSERT INTO shortlists (created_at, role, requirements, report_path) VALUES (?, ?, ?, ?)",
            (_now(), role, json.dumps(requirements), report_path),
        )
        shortlist_id = cursor.lastrowid
        connection.executemany(
            "INSERT INTO shortlist_candidates VALUES (?, ?, ?, ?, ?, ?)",
            [(shortlist_id, c.rank, c.candidate_id, c.candidate_name, c.score, c.verdict) for c in candidates],
        )
        connection.commit()
    LOG.info("saved shortlist %s for %s (%d candidates)", shortlist_id, role, len(candidates))
    return {"shortlist_id": shortlist_id, "role": role, "candidates": len(candidates)}


@server.tool()
@guarded
def record_decision(
    candidate_id: str,
    decision: Literal["advance", "hold", "reject", "hired"],
    notes: str = "",
    candidate_name: Optional[str] = None,
    role: Optional[str] = None,
) -> dict:
    """Record a recruiter decision for a candidate (advance, hold, reject, hired) with optional notes."""
    if not candidate_id.strip():
        raise E.ToolFailure(E.INVALID_PARAMS, "candidate_id is required.")
    with closing(_connect()) as connection:
        cursor = connection.execute(
            "INSERT INTO decisions (created_at, candidate_id, candidate_name, role, decision, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), candidate_id, candidate_name, role, decision, notes),
        )
        connection.commit()
    return {"decision_id": cursor.lastrowid, "candidate_id": candidate_id, "decision": decision}


@server.tool()
@guarded
def get_candidate_history(candidate_id: str) -> dict:
    """Every shortlist a candidate appeared on (role, rank, score, verdict, date) and every recorded decision."""
    with closing(_connect()) as connection:
        appearances = connection.execute(
            "SELECT s.id AS shortlist_id, s.created_at, s.role, c.rank, c.score, c.verdict, c.candidate_name "
            "FROM shortlist_candidates c JOIN shortlists s ON s.id = c.shortlist_id "
            "WHERE c.candidate_id = ? ORDER BY s.id DESC",
            (candidate_id,),
        ).fetchall()
        decisions = connection.execute(
            "SELECT created_at, role, decision, notes FROM decisions WHERE candidate_id = ? ORDER BY id DESC",
            (candidate_id,),
        ).fetchall()
    return {
        "candidate_id": candidate_id,
        "shortlist_appearances": [dict(r) for r in appearances],
        "decisions": [dict(r) for r in decisions],
        "seen_before": bool(appearances or decisions),
    }


@server.tool()
@guarded
def list_shortlists(limit: int = 10) -> dict:
    """The most recent shortlists with their top three candidates."""
    limit = max(1, min(int(limit), 50))
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT id, created_at, role, report_path FROM shortlists ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        items = []
        for row in rows:
            top = connection.execute(
                "SELECT rank, candidate_name, score, verdict FROM shortlist_candidates WHERE shortlist_id = ? ORDER BY rank LIMIT 3",
                (row["id"],),
            ).fetchall()
            item = dict(row)
            item["top_candidates"] = [dict(t) for t in top]
            items.append(item)
    return {"count": len(items), "shortlists": items}


@server.tool()
@guarded
def query_records(sql: str) -> dict:
    """Run one read-only SELECT over the tables shortlists, shortlist_candidates, and decisions (see db://schema)."""
    statement = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", statement) or ";" in statement:
        raise E.ToolFailure(E.INVALID_PARAMS, "Only a single SELECT statement is allowed.")
    try:
        with closing(_connect(readonly=True)) as connection:
            cursor = connection.execute(statement)
            rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
            columns = [d[0] for d in cursor.description or []]
    except sqlite3.Error as exc:
        raise E.ToolFailure(E.INVALID_PARAMS, "SQL error: {0}".format(exc))
    return {
        "columns": columns,
        "rows": [list(r) for r in rows[:MAX_QUERY_ROWS]],
        "row_count": min(len(rows), MAX_QUERY_ROWS),
        "truncated": len(rows) > MAX_QUERY_ROWS,
    }


@server.resource("db://schema", name="schema", description="SQL schema of the candidate database.", mime_type="text/plain")
def schema() -> str:
    return SCHEMA.strip()


@server.resource("db://shortlists/recent", name="recent_shortlists", description="The five most recent shortlists.", mime_type="application/json")
def recent_shortlists() -> dict:
    return list_shortlists.__wrapped__(5)


def describe() -> dict:
    return {"server_name": CONF.server_name, "database_path": CONF.database_path, "max_query_rows": MAX_QUERY_ROWS}


_init()
add_http_routes(server, "candidate_db", describe)


if __name__ == "__main__":
    run_server(server, "candidate_db")
