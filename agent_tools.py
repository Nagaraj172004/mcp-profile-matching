"""Tools the matching agent can call.

There are no direct file-system or database tools here any more. The agent
gets two kinds of tools:

- MCP tools, discovered from the servers at startup (tools/list) and
  wrapped by mcp_client.MCPHub with the server's own JSON schema:
  filesystem (list_files, read_file, search_in_file), resume_search
  (semantic_search), candidate_db (get_candidate_history, list_shortlists,
  record_decision, query_records).
- Screening tools defined below (compare, explain, interview questions,
  candidate report). They read requirements and the shortlist from graph
  state through InjectedState, and every file or index access they make goes
  through MCP via screening.py.
"""

from __future__ import annotations

import json
from typing import Annotated, List, Optional

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

import screening
from mcp_client import get_hub

AGENT_MCP_TOOLS = {
    "filesystem": ["list_files", "read_file", "search_in_file"],
    "resume_search": ["semantic_search"],
    "candidate_db": ["get_candidate_history", "list_shortlists", "record_decision", "query_records"],
}


def _dump(data) -> str:
    return json.dumps(data, indent=1, default=str)


def _requirements(state: dict) -> Optional[dict]:
    return state.get("requirements") if state else None


def _shortlist(state: dict) -> list:
    return (state or {}).get("shortlist") or []


def _resolve_many(refs: List[str], state: dict) -> tuple:
    found, unknown = [], []
    for ref in refs:
        cid = screening.resolve_candidate(ref, _shortlist(state))
        if cid and cid not in found:
            found.append(cid)
        elif not cid:
            unknown.append(ref)
    return found, unknown


# -------------------------------------------------------- screening tools

@tool
def extract_requirements(jd: str) -> str:
    """Parse a job description or request into must-have skills, nice-to-have skills, and minimum years."""
    req = screening.extract_requirements(jd)
    return _dump({k: req[k] for k in ("title", "must_have", "nice_to_have", "min_years", "summary")})


@tool
def compare_candidates(candidate_ids: List[str], state: Annotated[dict, InjectedState]) -> str:
    """Head-to-head comparison of 2-5 candidates against the current requirements.
    Accepts candidate ids, names, first names, or ranks like '#1'."""
    req = _requirements(state)
    if not req:
        return _dump({"error": "No active search. Give a job description first."})
    ids, unknown = _resolve_many(candidate_ids, state)
    if not ids:
        return _dump({"error": "No matching candidates", "unknown": unknown})
    shortlist = {item["candidate_id"]: item for item in _shortlist(state)}
    rows = []
    for cid in ids:
        item = shortlist.get(cid) or screening.round_three(screening.round_two(req, [screening.evaluate_candidate(req, cid)]))[0]
        b = item["breakdown"]
        rows.append(
            {
                "candidate_id": cid,
                "name": item["candidate_name"],
                "rank": item["rank"],
                "score": item["match_score"],
                "score_parts": {
                    "similarity": b["similarity_points"],
                    "skills": b["skill_points"],
                    "years": b["years_points"],
                    "penalty": -b["penalty"],
                },
                "years": b["years"],
                "matched_must_have": b["matched_must"],
                "missing_must_have": b["missing_must"],
                "matched_nice_to_have": b["matched_nice"],
                "recommendation": item["recommendation"],
                "strengths": item["strengths"] + item.get("ai_strengths", []),
                "gaps": item["gaps"] + item.get("ai_concerns", []),
            }
        )
    return _dump({"requirements": {k: req[k] for k in ("must_have", "nice_to_have", "min_years")}, "candidates": rows, "unknown": unknown})


@tool
def explain_ranking(higher_candidate: str, lower_candidate: str, state: Annotated[dict, InjectedState]) -> str:
    """Explain point by point why one candidate ranks above another under the current requirements.
    Use for questions like 'Why did X rank higher than Y?' or 'Why is X not in the list?' (compare with the lowest shortlisted candidate)."""
    req = _requirements(state)
    if not req:
        return _dump({"error": "No active search. Give a job description first."})
    ids, unknown = _resolve_many([higher_candidate, lower_candidate], state)
    if len(ids) < 2:
        return _dump({"error": "Could not identify both candidates", "unknown": unknown})
    return _dump(screening.explain_pair(req, ids[0], ids[1]))


@tool
def generate_interview_questions(candidate_id: str, state: Annotated[dict, InjectedState]) -> str:
    """Create screening interview questions for one candidate, targeting their strengths and gaps."""
    req = _requirements(state)
    if not req:
        return _dump({"error": "No active search. Give a job description first."})
    cid = screening.resolve_candidate(candidate_id, _shortlist(state))
    if not cid:
        return _dump({"error": "Unknown candidate: {0}".format(candidate_id)})
    return _dump(screening.interview_questions(req, cid))


@tool
def get_candidate_report(candidate_id: str, state: Annotated[dict, InjectedState]) -> str:
    """Detailed match report for one candidate: score breakdown, evidence, strengths, gaps, verdict, and improvement suggestions."""
    req = _requirements(state)
    if not req:
        return _dump({"error": "No active search. Give a job description first."})
    cid = screening.resolve_candidate(candidate_id, _shortlist(state))
    if not cid:
        return _dump({"error": "Unknown candidate: {0}".format(candidate_id)})
    shortlist = {item["candidate_id"]: item for item in _shortlist(state)}
    item = shortlist.get(cid) or screening.round_three(screening.round_two(req, [screening.evaluate_candidate(req, cid)]))[0]
    keep = ("candidate_name", "resume_path", "rank", "match_score", "breakdown", "evidence", "strengths", "gaps",
            "ai_strengths", "ai_concerns", "analyst_note", "recommendation", "recommendation_reason", "suggestions")
    return _dump({k: item.get(k) for k in keep})


SCREENING_TOOLS = [
    extract_requirements,
    compare_candidates,
    explain_ranking,
    generate_interview_questions,
    get_candidate_report,
]

_tools: Optional[list] = None


def get_tools() -> list:
    """MCP tools discovered from the servers plus the screening tools, built once."""
    global _tools
    if _tools is None:
        _tools = get_hub().langchain_tools(AGENT_MCP_TOOLS) + SCREENING_TOOLS
    return _tools
