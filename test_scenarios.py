"""Eight scripted conversations run against the LangGraph agent and its MCP servers.

python test_scenarios.py            run all with the Groq model
python test_scenarios.py 3 5        run scenarios 3 and 5
python test_scenarios.py --offline  rule-based fallbacks only, no API calls

Every scenario talks to the real MCP servers over stdio and checks which MCP
tools were called. Each run writes a transcript, including every MCP call,
to transcripts/<scenario>.md (transcripts/offline/ with --offline).
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if "--offline" in sys.argv:
    os.environ["USE_LLM"] = "0"

from matching_agent import MatchingSession  # noqa: E402

TRANSCRIPTS = Path("transcripts/offline") if "--offline" in sys.argv else Path("transcripts")


class Conversation:
    def __init__(self, name: str):
        self.name = name
        self.session = MatchingSession()
        self.lines = ["# {0}".format(name), ""]
        self.last_events = []

    def say(self, text: str) -> str:
        print("  You: {0}".format(text))
        self.lines += ["## You", "", text, ""]
        events = self.session.send(text)
        self.last_events = events
        trace = [e for e in events if e["kind"] in ("trace", "tool_result", "mcp")]
        answer = "\n\n".join(e["text"] for e in events if e["kind"] == "answer")
        if trace:
            self.lines += ["<details><summary>Agent reasoning ({0} steps)</summary>".format(len(trace)), ""]
            self.lines += ["- `{0}` {1}".format(e["node"], e["text"].replace("\n", " ")[:400]) for e in trace]
            self.lines += ["", "</details>", ""]
        self.lines += ["## Agent", "", answer, ""]
        print("  Agent: {0}".format(" ".join(answer.split())[:160]))
        return answer

    def tools_called(self) -> list:
        return [e["text"].split(" -> ")[0] for e in self.last_events if e["kind"] == "tool_result"]

    def mcp_calls(self) -> list:
        """server.tool for every MCP call made during the last turn."""
        return [e["text"].split(" ")[0] for e in self.last_events if e["kind"] == "mcp"]

    def intent(self) -> str:
        for e in self.last_events:
            if e["node"] == "human_feedback" and "-> intent" in e["text"]:
                return e["text"].rsplit("-> intent", 1)[1].strip(" .")
        return ""

    @property
    def state(self) -> dict:
        return self.session.state

    def save(self) -> None:
        TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
        (TRANSCRIPTS / "{0}.md".format(self.name)).write_text("\n".join(self.lines), encoding="utf-8")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def answered_with(c: Conversation, tool: str) -> bool:
    """The agent used the tool, or (offline) answered through the matching rule-based path."""
    return tool in c.tools_called() or any("rule-based" in e["text"] for e in c.last_events)


def scenario_1_full_pipeline(c: Conversation) -> None:
    """Job file -> three screening rounds -> report -> done."""
    answer = c.say("jobs/senior_python_backend.txt")
    s = c.state
    check(s["requirements"]["must_have"] == ["Python", "Django"], "must-haves come from the JD header")
    check(s["requirements"]["min_years"] == 5, "minimum years is 5")
    check(s["pool_size"] == 100, "round 1 screens all 100 resumes")
    check(len(s["round1"]) == 10, "round 1 keeps 10")
    check(all("evidence" in i and i.get("analyst_note") for i in s["shortlist"]), "round 2 analyses all 10")
    check(all(i["recommendation"] in ("hire", "borderline", "no-hire") for i in s["shortlist"]), "round 3 gives every candidate a verdict")
    check(Path(s["report_path"]).is_file(), "report written to disk")
    check("hire" in answer.lower(), "chat summary lists verdicts")
    calls = c.mcp_calls()
    for expected in ("filesystem.read_file", "resume_search.rank_resumes", "filesystem.batch_process",
                     "filesystem.write_file", "candidate_db.save_shortlist"):
        check(expected in calls, "pipeline used {0} over MCP (calls: {1})".format(expected, calls))
    check(s.get("shortlist_id"), "shortlist saved in the candidate database")
    nagaraj = next((i for i in s["shortlist"] if i["candidate_id"] == "nagaraj"), None)
    if nagaraj:
        check(nagaraj["recommendation"] == "borderline", "Nagaraj (4 of 5 years) is borderline, not hire")
        check(any("year" in tip for tip in nagaraj["suggestions"]), "borderline candidate gets an improvement suggestion")
    c.say("done")
    check(c.session.finished, "graph reaches END")

def scenario_2_natural_language_compare(c: Conversation) -> None:
    """Free-text request, then a side-by-side comparison of the top 3."""
    c.say("Find me candidates with React and 3+ years experience")
    s = c.state
    check("React" in s["requirements"]["must_have"], "React is a must-have")
    check(s["requirements"]["min_years"] == 3, "3+ years parsed")
    top3 = [i["candidate_name"].split()[0] for i in s["shortlist"][:3]]
    answer = c.say("Compare the top 3 matches side by side")
    check(c.intent() == "compare", "routed to compare")
    check(answered_with(c, "compare_candidates"), "compare_candidates tool used")
    check(all(name in answer for name in top3), "answer covers the top 3: {0}".format(top3))

def scenario_3_refine_and_rerank(c: Conversation) -> None:
    """Change requirements mid-conversation; the agent re-ranks and explains what moved."""
    c.say("jobs/frontend_react.txt")
    before = [i["candidate_id"] for i in c.state["shortlist"]]
    answer = c.say("Make Jest a must-have and raise the minimum to 5 years")
    s = c.state
    check(c.intent() == "refine", "routed to refine")
    check("Jest" in s["requirements"]["must_have"], "Jest became a must-have")
    check(s["requirements"]["min_years"] == 5, "minimum years raised to 5")
    check(len(s["requirement_history"]) == 2, "requirement history keeps both versions")
    check(s["ranking_changes"], "ranking changes were computed")
    check([i["candidate_id"] for i in s["shortlist"]] != before, "shortlist order changed")
    check("How the ranking moved" in answer, "answer explains the movement")
    answer = c.say("Actually Jest is only nice-to-have, keep the 5 years")
    s = c.state
    check("Jest" not in s["requirements"]["must_have"] and "Jest" in s["requirements"]["nice_to_have"], "Jest moved to nice-to-have")
    check(s["requirements"]["min_years"] == 5, "years kept at 5")
    check(len(s["requirement_history"]) == 3, "third requirement version recorded")

def scenario_4_explain_ranking(c: Conversation) -> None:
    """Why did one candidate rank higher than another, and why is someone not hired."""
    c.say("jobs/data_engineer.txt")
    s = c.state
    first, second = s["shortlist"][0]["candidate_name"], s["shortlist"][1]["candidate_name"]
    answer = c.say("Why did {0} rank higher than {1}?".format(first, second))
    check(c.intent() == "explain", "routed to explain")
    check(answered_with(c, "explain_ranking"), "explain_ranking tool used")
    check(first.split()[0] in answer and second.split()[0] in answer, "answer names both candidates")
    lowest = s["shortlist"][-1]
    answer = c.say("Why is {0} not a hire? What are the gaps?".format(lowest["candidate_name"]))
    check(lowest["candidate_name"].split()[0] in answer, "answer is about the right candidate")

def scenario_5_multi_round_borderline(c: Conversation) -> None:
    """Three rounds on the ML role, then interview questions and suggestions for a borderline candidate."""
    c.say("jobs/ml_engineer.txt")
    s = c.state
    check(s["pool_size"] == 100 and len(s["shortlist"]) == 10, "100 -> 10")
    borderline = [i for i in s["shortlist"] if i["recommendation"] == "borderline"]
    check(borderline, "at least one borderline candidate")
    name = borderline[0]["candidate_name"]
    check(borderline[0]["suggestions"], "borderline candidate has improvement suggestions")
    answer = c.say("Generate interview questions for {0}".format(name))
    check(c.intent() == "questions", "routed to questions")
    check(answered_with(c, "generate_interview_questions"), "generate_interview_questions tool used")
    check(answer.count("?") >= 4, "at least 4 questions")
    answer = c.say("How could {0} improve to become a hire?".format(name))
    check(name.split()[0] in answer, "suggestions are about the borderline candidate")

def scenario_6_edge_cases(c: Conversation) -> None:
    """Impossible requirements, unknown candidates, then relaxing the search."""
    answer = c.say("Find me candidates with Rust and Haskell and 10+ years experience")
    s = c.state
    check(s["qualified"] == 0, "nobody meets impossible requirements")
    check(not any(i["recommendation"] == "hire" for i in s["shortlist"]), "no hire verdicts")
    answer = c.say("Compare John Doe and Jane Smith")
    check(answer.strip(), "unknown candidates get an answer instead of a crash")
    check("John Doe" not in [i["candidate_name"] for i in c.state["shortlist"]], "no invented candidates")
    answer = c.say("Drop Rust and Haskell, require Golang with 4+ years instead")
    s = c.state
    check(s["requirements"]["must_have"] == ["Golang"], "must-haves replaced with Golang, got {0}".format(s["requirements"]["must_have"]))
    check(s["requirements"]["min_years"] == 4, "years lowered to 4")
    check(s["qualified"] >= 2, "relaxed search finds qualified candidates")
    top_two = {i["candidate_id"] for i in s["shortlist"][:2]}
    check(top_two == {"jaya_prakash", "pranav_hegde"}, "the two Golang engineers rank first, got {0}".format(top_two))
    check(all(i["recommendation"] != "no-hire" for i in s["shortlist"][:2]), "relaxed search turns them into hire or borderline")

def scenario_7_watch_new_resume(c: Conversation) -> None:
    """watch_directory picks up a resume dropped mid-conversation; the agent indexes and re-ranks it, then handles its deletion."""
    import shutil
    import threading

    target = Path("resumes/resume_ravi_teja.txt")
    target.unlink(missing_ok=True)
    try:
        c.say("jobs/senior_python_backend.txt")
        before = [i["candidate_id"] for i in c.state["shortlist"]]
        check("ravi_teja" not in before, "new candidate is not on the first shortlist")
        threading.Timer(3.0, lambda: shutil.copyfile("samples/resume_ravi_teja.txt", target)).start()
        answer = c.say("Watch for new resumes")
        check(c.intent() == "watch", "routed to watch")
        calls = c.mcp_calls()
        for expected in ("filesystem.watch_directory", "filesystem.batch_process", "resume_search.upsert_resumes", "resume_search.rank_resumes"):
            check(expected in calls, "watch flow used {0} (calls: {1})".format(expected, calls))
        s = c.state
        check(s["pool_size"] == 101, "index grew to 101 resumes")
        check(s["shortlist"][0]["candidate_id"] == "ravi_teja", "the strong new resume ranks first")
        check("newly indexed" in answer, "answer explains why the new candidate appeared")
        target.unlink()
        answer = c.say("Any new resumes? Keep watching")
        check("resume_search.remove_resumes" in c.mcp_calls(), "deleted resume removed from the index")
        check("ravi_teja" not in [i["candidate_id"] for i in c.state["shortlist"]], "deleted candidate left the shortlist")
        check(c.state["pool_size"] == 100, "index back to 100 resumes")
    finally:
        if target.exists():
            target.unlink()
        from mcp_client import get_hub
        get_hub().call_tool("resume_search", "remove_resumes", {"paths": ["resumes/resume_ravi_teja.txt"]})
        import screening
        screening.invalidate_index_caches()


def scenario_8_multi_mcp_history(c: Conversation) -> None:
    """One conversation uses all three MCP servers: search, hiring history, decisions, and semantic search."""
    c.say("jobs/data_engineer.txt")
    top = c.state["shortlist"][0]
    name = top["candidate_name"]
    answer = c.say("Have we shortlisted {0} before? Check the candidate history.".format(name))
    check("candidate_db.get_candidate_history" in c.mcp_calls() or "candidate_db.list_shortlists" in c.mcp_calls(),
          "history came from the candidate database (calls: {0})".format(c.mcp_calls()))
    check(name.split()[0] in answer, "answer is about {0}".format(name))
    c.say("Record a decision: advance {0} to the interview round.".format(name))
    check("candidate_db.record_decision" in c.mcp_calls(), "decision recorded through MCP (calls: {0})".format(c.mcp_calls()))
    from mcp_client import get_hub
    history = get_hub().call_tool("candidate_db", "get_candidate_history", {"candidate_id": top["candidate_id"]})
    check(any(d["decision"] == "advance" for d in history["decisions"]), "decision is stored in SQLite")
    answer = c.say("Who has Kafka streaming experience?")
    check("resume_search.semantic_search" in c.mcp_calls(), "open question answered with semantic_search (calls: {0})".format(c.mcp_calls()))
    check(any(n in answer for n in ("Waseem", "Joseph", "Suresh", "Neha", "Esha")), "answer names a Kafka candidate")


SCENARIOS = [
    scenario_1_full_pipeline,
    scenario_2_natural_language_compare,
    scenario_3_refine_and_rerank,
    scenario_4_explain_ranking,
    scenario_5_multi_round_borderline,
    scenario_6_edge_cases,
    scenario_7_watch_new_resume,
    scenario_8_multi_mcp_history,
]


def main() -> None:
    wanted = {int(a) for a in sys.argv[1:] if a.isdigit()}
    results = []
    for number, scenario in enumerate(SCENARIOS, start=1):
        if wanted and number not in wanted:
            continue
        print("\nScenario {0}: {1}".format(number, scenario.__doc__.strip()))
        started = time.perf_counter()
        conversation = Conversation(scenario.__name__.replace("scenario_", ""))
        try:
            scenario(conversation)
            status, detail = "PASS", ""
        except AssertionError as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:
            status, detail = "ERROR", "{0}: {1}".format(type(exc).__name__, exc)
            traceback.print_exc()
        conversation.save()
        seconds = time.perf_counter() - started
        results.append((number, scenario.__name__, status, detail, seconds))
        print("  -> {0} {1} ({2:.0f}s)".format(status, detail, seconds))

    print("\nSummary")
    for number, name, status, detail, seconds in results:
        print("  {0}. {1:<40} {2:<5} {3:>4.0f}s {4}".format(number, name, status, seconds, detail))
    if any(r[2] != "PASS" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
