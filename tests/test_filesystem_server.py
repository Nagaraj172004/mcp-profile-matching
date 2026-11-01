"""filesystem_mcp_server.py through a real MCP client (in-process transport)."""

import asyncio
import json
import threading
import time

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

import filesystem_mcp_server as fsm
from conftest import ROOT


def run(coro):
    return asyncio.run(coro)


async def _call(tool: str, arguments: dict, progress=None):
    async with Client(fsm.server, mode="legacy") as client:
        return await client.call_tool(tool, arguments, progress_callback=progress)


def call(tool: str, **arguments) -> dict:
    result = run(_call(tool, arguments))
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def call_error(tool: str, **arguments) -> dict:
    result = run(_call(tool, arguments))
    assert result.is_error, "expected an error"
    text = result.content[0].text
    return json.loads(text[text.index("{"):])["error"]


# ------------------------------------------------------- Milestone 1 tools

def test_list_files_with_extension_filter():
    body = call("list_files", directory="resumes", extension=".docx")
    assert body["count"] > 0
    assert all(f["extension"] == ".docx" for f in body["files"])
    assert all(f["path"].startswith("resumes/") for f in body["files"])


@pytest.mark.parametrize("name", ["resume_nagaraj.txt", "resume_karan_singh.pdf", "resume_arjun_menon.docx"])
def test_read_file_all_formats(name):
    body = call("read_file", filepath="resumes/" + name)
    assert body["content"].strip()
    assert body["metadata"]["name"] == name
    assert "Skills" in body["content"]


def test_search_in_file_returns_context():
    body = call("search_in_file", filepath="resumes/resume_nagaraj.txt", keyword="django")
    assert body["match_count"] >= 2
    assert "context" in body["matches"][0]


def test_write_file_inside_reports(tmp_path):
    body = call("write_file", filepath="reports/test_mcp_write.md", content="# hello")
    assert body["path"] == "reports/test_mcp_write.md" and body["bytes_written"] > 0
    (ROOT / "reports" / "test_mcp_write.md").unlink()


# ----------------------------------------------------------- error codes

@pytest.mark.parametrize("path", ["dataset.py", "resumes/../dataset.py", "../outside.txt", "C:/Windows/win.ini"])
def test_paths_outside_sandbox_are_denied(path):
    error = call_error("read_file", filepath=path)
    assert error["code"] == -32002 and error["name"] == "ACCESS_DENIED"


def test_missing_file():
    error = call_error("read_file", filepath="resumes/nobody.txt")
    assert error["code"] == -32001 and error["name"] == "FILE_NOT_FOUND"


def test_unsupported_type():
    target = ROOT / "reports" / "notes.csv"
    target.parent.mkdir(exist_ok=True)
    target.write_text("a,b\n", encoding="utf-8")
    try:
        error = call_error("read_file", filepath="reports/notes.csv")
        assert error["code"] == -32003 and error["name"] == "UNSUPPORTED_TYPE"
    finally:
        target.unlink()


def test_write_outside_writable_folders_is_denied():
    error = call_error("write_file", filepath="resumes/injected.txt", content="x")
    assert error["code"] == -32007 and error["name"] == "WRITE_DENIED"


def test_write_executable_extension_is_refused():
    error = call_error("write_file", filepath="reports/run.py", content="print(1)")
    assert error["name"] == "UNSUPPORTED_TYPE"


def test_empty_keyword_is_invalid_params():
    error = call_error("search_in_file", filepath="resumes/resume_nagaraj.txt", keyword="  ")
    assert error["code"] == -32602


def test_unknown_tool_is_reported():
    result = run(_call("format_disk", {}))
    assert result.is_error


# ---------------------------------------------------------- batch_process

def test_batch_read_whole_directory_with_progress():
    updates = []

    async def progress(value, total, message):
        updates.append((value, total))

    result = run(_call("batch_process", {"operation": "read", "directory": "resumes"}, progress))
    body = json.loads(result.content[0].text)
    assert body["total"] == body["succeeded"] >= 100
    assert body["failed"] == 0
    assert all(r["data"]["content"] for r in body["results"])
    assert len(updates) == body["total"] and updates[-1][0] == updates[-1][1]


def test_batch_partial_failure_keeps_going():
    body = call("batch_process", operation="metadata",
                paths=["resumes/resume_nagaraj.txt", "resumes/ghost.txt", "dataset.py", "jobs/ml_engineer.txt"])
    assert body["total"] == 4 and body["succeeded"] == 2 and body["failed"] == 2
    errors = {r["path"]: r["error"]["name"] for r in body["results"] if not r["success"]}
    assert errors == {"resumes/ghost.txt": "FILE_NOT_FOUND", "dataset.py": "ACCESS_DENIED"}
    assert [r["path"] for r in body["results"]] == ["resumes/resume_nagaraj.txt", "resumes/ghost.txt", "dataset.py", "jobs/ml_engineer.txt"]


def test_batch_search_uses_whole_words():
    body = call("batch_process", operation="search", paths=["resumes/resume_alok_pandey.pdf"], keywords=["Java", "JavaScript"])
    data = body["results"][0]["data"]
    assert data["match_counts"]["Java"] > 0
    assert data["match_counts"]["JavaScript"] == 0


def test_batch_summary():
    body = call("batch_process", operation="summary", paths=["resumes/resume_nagaraj.txt"])
    data = body["results"][0]["data"]
    assert data["name"] == "Nagaraj" and data["years_of_experience"] == 4
    assert {"skills", "experience", "education"} <= set(data["sections"])


def test_batch_validation_errors():
    assert call_error("batch_process", operation="read")["code"] == -32602
    assert call_error("batch_process", operation="search", directory="resumes")["code"] == -32602
    too_many = ["resumes/x{0}.txt".format(i) for i in range(fsm.CONF.max_batch_size + 1)]
    error = call_error("batch_process", operation="metadata", paths=too_many)
    assert error["name"] == "BATCH_LIMIT_EXCEEDED" and error["data"]["limit"] == fsm.CONF.max_batch_size


# --------------------------------------------------------- watch_directory

def test_watch_directory_reports_new_and_deleted_resume():
    target = ROOT / "resumes" / "resume_watch_test.txt"
    source = (ROOT / "samples" / "resume_ravi_teja.txt").read_text(encoding="utf-8")
    target.unlink(missing_ok=True)
    threading.Timer(1.5, lambda: target.write_text(source, encoding="utf-8")).start()
    try:
        started = time.monotonic()
        body = call("watch_directory", directory="resumes", timeout_seconds=20)
        assert not body["timed_out"]
        assert body["new_resumes"] == ["resumes/resume_watch_test.txt"]
        assert time.monotonic() - started < 15
        cursor = body["last_event_id"]
    finally:
        target.unlink(missing_ok=True)
    body = call("watch_directory", directory="resumes", timeout_seconds=20, since_event_id=cursor)
    assert [(e["type"], e["path"]) for e in body["events"]] == [("deleted", "resumes/resume_watch_test.txt")]


def test_watch_directory_times_out_quietly():
    body = call("watch_directory", directory="jobs", timeout_seconds=1.5)
    assert body["timed_out"] and body["events"] == []


def test_watch_directory_outside_sandbox():
    assert call_error("watch_directory", directory="..")["name"] == "ACCESS_DENIED"


# --------------------------------------------------------------- resources

async def _read(uri: str):
    async with Client(fsm.server, mode="legacy") as client:
        return await client.read_resource(uri)


def test_resume_index_resource():
    body = json.loads(run(_read("resumes://index")).contents[0].text)
    assert body["count"] >= 100


def test_config_resource_lists_error_codes():
    body = json.loads(run(_read("config://server")).contents[0].text)
    assert body["writable_directories"] == ["reports"]
    assert body["error_codes"]["ACCESS_DENIED"] == -32002


def test_missing_resource_is_protocol_error():
    async def go():
        async with Client(fsm.server, mode="legacy") as client:
            try:
                await client.read_resource("resumes://ghost.txt")
            except MCPError as exc:
                return exc
        return None

    error = run(go())
    assert error is not None
    assert error.error.code == -32001
    assert error.error.data["name"] == "FILE_NOT_FOUND"


def test_prompt_is_discoverable():
    async def go():
        async with Client(fsm.server, mode="legacy") as client:
            prompts = await client.list_prompts()
            prompt = await client.get_prompt("screen_resume", {"resume_file": "resume_nagaraj.txt", "job_file": "ml_engineer.txt"})
            return prompts, prompt

    prompts, prompt = run(go())
    assert "screen_resume" in [p.name for p in prompts.prompts]
    assert "resumes://resume_nagaraj.txt" in prompt.messages[0].content.text
