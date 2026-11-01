"""The bonus servers, the client hub across all three servers, and the HTTP transport."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from mcp import Client

from conftest import ROOT


def run(coro):
    return asyncio.run(coro)


async def _call(server, tool: str, arguments: dict):
    async with Client(server, mode="legacy") as client:
        return await client.call_tool(tool, arguments)


def call(server, tool: str, **arguments):
    result = run(_call(server, tool, arguments))
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def call_error(server, tool: str, **arguments) -> dict:
    result = run(_call(server, tool, arguments))
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{"):])["error"]


# ------------------------------------------------------------ candidate_db

@pytest.fixture()
def db(tmp_path, monkeypatch):
    import candidate_db_mcp_server as dbm

    monkeypatch.setattr(dbm, "DB_PATH", tmp_path / "candidates.db")
    dbm._init()
    return dbm.server


def test_shortlist_history_and_decisions(db):
    saved = call(db, "save_shortlist", role="Data Engineer", requirements={"must_have": ["Spark"]},
                 candidates=[{"rank": 1, "candidate_id": "rahul_iyer", "candidate_name": "Rahul Iyer", "score": 88, "verdict": "hire"},
                             {"rank": 2, "candidate_id": "chetan_bhat", "candidate_name": "Chetan Bhat", "score": 86, "verdict": "hire"}],
                 report_path="reports/data_engineer_v1.md")
    assert saved["shortlist_id"] == 1
    call(db, "record_decision", candidate_id="rahul_iyer", decision="advance", notes="strong Spark", role="Data Engineer")
    history = call(db, "get_candidate_history", candidate_id="rahul_iyer")
    assert history["seen_before"]
    assert history["shortlist_appearances"][0]["rank"] == 1
    assert history["decisions"][0]["decision"] == "advance"
    assert not call(db, "get_candidate_history", candidate_id="nobody")["seen_before"]
    listing = call(db, "list_shortlists", limit=5)
    assert listing["shortlists"][0]["top_candidates"][0]["candidate_name"] == "Rahul Iyer"


def test_query_records_is_read_only(db):
    call(db, "save_shortlist", role="ML", requirements={}, candidates=[{"rank": 1, "candidate_id": "a", "candidate_name": "A", "score": 70, "verdict": "hire"}])
    rows = call(db, "query_records", sql="SELECT role, COUNT(*) FROM shortlists GROUP BY role")
    assert rows["rows"] == [["ML", 1]]
    for sql in ("DELETE FROM shortlists", "SELECT 1; DROP TABLE shortlists", "UPDATE decisions SET notes = 'x'"):
        assert call_error(db, "query_records", sql=sql)["code"] == -32602


def test_invalid_decision_value_is_rejected(db):
    result = run(_call(db, "record_decision", {"candidate_id": "a", "decision": "maybe"}))
    assert result.is_error


# ----------------------------------------------------------- resume_search

@pytest.fixture(scope="module")
def search():
    import resume_search_mcp_server as rsm

    if call(rsm.server, "index_stats")["chunks"] == 0:
        pytest.skip("Index is empty: run python ingest.py first.")
    return rsm.server


def test_index_stats_and_vocabulary(search):
    stats = call(search, "index_stats")
    assert stats["resumes"] >= 100 and stats["embedding_model"] == "all-MiniLM-L6-v2"
    assert "Django" in call(search, "skill_vocabulary")["skills"]


def test_rank_resumes_returns_breakdowns(search):
    body = call(search, "rank_resumes", requirements={"title": "Backend", "must_have": ["Python", "Django"], "min_years": 5})
    assert body["pool_size"] >= 100
    top = body["ranked"][0]
    assert {"similarity_points", "skill_points", "years_points", "penalty"} <= set(top["breakdown"])
    assert top["breakdown"]["meets_must_haves"]


def test_semantic_search(search):
    hits = call(search, "semantic_search", query="Kubernetes clusters with Terraform", k=3)["hits"]
    assert len(hits) == 3
    assert any("Kubernetes" in h["skills"] for h in hits)


def test_semantic_search_rejects_empty_query(search):
    assert call_error(search, "semantic_search", query="  ")["code"] == -32602


# --------------------------------------------------- hub across 3 servers

def test_hub_connects_to_all_servers_over_stdio():
    from mcp_client import MCPHub

    hub = MCPHub(transport="stdio").start()
    try:
        assert set(hub.server_info) == {"filesystem", "resume_search", "candidate_db"}
        assert "batch_process" in hub.tool_names("filesystem")
        assert "semantic_search" in hub.tool_names("resume_search")
        assert "get_candidate_history" in hub.tool_names("candidate_db")
        text = hub.call_tool("filesystem", "read_file", {"filepath": "resumes/resume_nagaraj.txt"})["content"]
        hits = hub.call_tool("resume_search", "semantic_search", {"query": text[:300], "k": 1})["hits"]
        assert hits[0]["candidate_id"] == "nagaraj"
        schema = hub.read_resource("candidate_db", "db://schema")
        assert "CREATE TABLE" in schema
        tools = hub.langchain_tools({"filesystem": ["read_file"], "candidate_db": ["list_shortlists"]})
        assert [t.name for t in tools] == ["read_file", "list_shortlists"]
        assert "Nagaraj" in tools[0].invoke({"filepath": "resumes/resume_nagaraj.txt"})
        from mcp_client import MCPToolError
        with pytest.raises(MCPToolError) as info:
            hub.call_tool("filesystem", "read_file", {"filepath": "dataset.py"})
        assert info.value.name == "ACCESS_DENIED"
        assert all(entry["ms"] >= 0 for entry in hub.call_log)
    finally:
        hub.close()


# ------------------------------------------------------- HTTP transport

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_filesystem_server_over_streamable_http():
    port = _free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    process = subprocess.Popen([sys.executable, str(ROOT / "filesystem_mcp_server.py"), "--transport", "http", "--port", str(port)],
                               cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:{0}".format(port)
        deadline = time.monotonic() + 30
        health = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as response:
                    health = json.loads(response.read())
                    break
            except OSError:
                time.sleep(0.3)
        assert health and health["status"] == "ok" and health["server"] == "resume-filesystem"
        with urllib.request.urlopen(base + "/discovery", timeout=5) as response:
            discovery = json.loads(response.read())
        assert discovery["message_format"] == "JSON-RPC 2.0" and discovery["endpoint"] == "/mcp"
        assert "watch_directory" in [t["name"] for t in discovery["tools"]]
        assert discovery["config"]["error_codes"]["FILE_NOT_FOUND"] == -32001

        async def over_http():
            async with Client(base + "/mcp", mode="legacy") as client:
                result = await client.call_tool("list_files", {"directory": "jobs"})
                return json.loads(result.content[0].text)

        assert run(over_http())["count"] == 6
    finally:
        process.terminate()
        process.wait(timeout=10)
