"""Streamlit chat for the matching agent: streamlit run app.py"""

from __future__ import annotations

import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import streamlit as st  # noqa: E402

import llm  # noqa: E402
from matching_agent import MatchingSession  # noqa: E402
from mcp_client import get_hub  # noqa: E402

st.set_page_config(page_title="Candidate Matching Agent", layout="wide")

STARTERS = [
    "jobs/senior_python_backend.txt",
    "Find me candidates with React and 3+ years experience",
    "jobs/ml_engineer.txt",
]
FOLLOW_UPS = [
    "Compare the top 3 matches side by side",
    "Why did #1 rank higher than #2?",
    "Watch for new resumes",
    "Have we shortlisted the #1 candidate before?",
    "Make Docker a must-have",
    "done",
]

if "session" not in st.session_state:
    with st.spinner("Starting MCP servers (filesystem, resume search, candidate database)..."):
        st.session_state.session = MatchingSession()
    st.session_state.chat = []

session: MatchingSession = st.session_state.session


def run_turn(text: str) -> None:
    st.session_state.chat.append({"role": "user", "content": text})
    with st.chat_message("user"):
        st.markdown(text)
    with st.chat_message("assistant"):
        trace_lines = []
        answers = []
        with st.status("Agent reasoning...", expanded=True) as status:
            def on_event(event: dict) -> None:
                if event["kind"] == "trace":
                    line = "**{0}**: {1}".format(event["node"], event["text"])
                elif event["kind"] == "tool_result":
                    line = "`tool` {0}".format(event["text"][:240])
                elif event["kind"] == "mcp":
                    line = "`mcp` {0}".format(event["text"])
                else:
                    answers.append(event["text"])
                    return
                trace_lines.append(line)
                st.markdown(line)

            session.send(text, on_event)
            status.update(label="Agent reasoning ({0} steps)".format(len(trace_lines)), state="complete", expanded=False)
        for answer in answers:
            st.markdown(answer)
    st.session_state.chat.append({"role": "assistant", "content": "\n\n".join(answers), "trace": trace_lines})


with st.sidebar:
    st.header("Agent state")
    st.caption("Model: {0}".format(llm.MODEL if llm.llm_enabled() else "LLM off (rule-based fallbacks)"))
    if st.button("New conversation"):
        st.session_state.session = MatchingSession()
        st.session_state.chat = []
        st.rerun()
    state = session.state if session.started else {}
    req = state.get("requirements")
    if req:
        st.subheader(req["title"])
        st.markdown("**Must-have:** {0}".format(", ".join(req["must_have"]) or "none"))
        st.markdown("**Nice-to-have:** {0}".format(", ".join(req["nice_to_have"]) or "none"))
        st.markdown("**Minimum years:** {0}".format(req["min_years"]))
        st.caption("Requirement versions: {0}".format(len(state.get("requirement_history", []))))
    shortlist = state.get("shortlist") or []
    if shortlist and "recommendation" in shortlist[0]:
        st.subheader("Shortlist")
        st.dataframe(
            [
                {
                    "#": item["rank"],
                    "Candidate": item["candidate_name"],
                    "Score": item["match_score"],
                    "Years": item["breakdown"]["years"],
                    "Verdict": item["recommendation"],
                }
                for item in shortlist
            ],
            hide_index=True,
            width="stretch",
        )
    report_path = state.get("report_path")
    if report_path and state.get("report_markdown"):
        st.download_button("Download match report", state["report_markdown"], file_name=Path(report_path).name)
    hub = get_hub()
    st.subheader("MCP servers")
    for name, info in hub.server_info.items():
        st.markdown("**{0}** `{1}` v{2} - {3}, {4} tools".format(name, info["name"], info["version"], info["transport"], len(hub.tools.get(name, []))))
    if hub.call_log:
        with st.expander("Recent MCP calls ({0} total)".format(len(hub.call_log))):
            for call in hub.call_log[-15:][::-1]:
                st.markdown("`{0}.{1}` {2} ms {3}".format(call["server"], call["tool"], call["ms"], "ok" if call["ok"] else call.get("error", "error")))
    for title, image in (("State machine", "docs/state_machine.png"), ("MCP architecture", "docs/mcp_architecture.png")):
        if Path(image).is_file():
            with st.expander(title):
                st.image(image)

st.title("Candidate Matching Agent")
st.caption("LangGraph agent over MCP: every file, index, and database access goes through the filesystem, resume search, and candidate database MCP servers.")

for entry in st.session_state.chat:
    with st.chat_message(entry["role"]):
        if entry.get("trace"):
            with st.expander("Agent reasoning ({0} steps)".format(len(entry["trace"]))):
                for line in entry["trace"]:
                    st.markdown(line)
        st.markdown(entry["content"])

suggestions = FOLLOW_UPS if session.started and not session.finished else STARTERS
clicked = None
for start in range(0, len(suggestions), 3):
    row = suggestions[start:start + 3]
    for column, suggestion in zip(st.columns(3), row):
        if column.button(suggestion, width="stretch"):
            clicked = suggestion

prompt = st.chat_input("Paste a job description, a jobs/*.txt path, or ask a question")
if clicked or prompt:
    run_turn(clicked or prompt)
    st.rerun()
