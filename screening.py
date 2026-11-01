"""Screening logic behind the agent: requirements, rounds, verdicts, diffs.

Scores and verdicts are rule-based so every ranking can be explained point by
point. The LLM extracts requirements from free text, applies requirement
changes, and writes analyst notes and interview questions; each of those has
a rule-based fallback.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

import llm
from job_matcher import candidate_id as candidate_id_of, mentioned, parse_job_text
from mcp_client import get_hub

PROJECT_DIR = Path(__file__).resolve().parent

SCORING_RULE = (
    "score = 60 x calibrated similarity + 25 x share of job skills + 15 x years ratio, "
    "minus 15 per missing must-have, minus 10 plus 5 per missing year when below the years bar"
)

HIRE_SCORE = 65
NEAR_MISS_SCORE = 35
MAX_YEARS_SHORT = 2

ALIASES = {
    "reactjs": "React",
    "react.js": "React",
    "ts": "TypeScript",
    "js": "JavaScript",
    "k8s": "Kubernetes",
    "postgres": "PostgreSQL",
    "go": "Golang",
    "nextjs": "Next.js",
    "node": "Node.js",
    "nodejs": "Node.js",
    "ml": "Machine Learning",
    "torch": "PyTorch",
    "sklearn": "scikit-learn",
    "tf": "TensorFlow",
    "vue": "Vue.js",
}


class Requirements(BaseModel):
    title: str = Field(description="Short role title, for example 'Frontend Engineer'")
    must_have: List[str] = Field(description="Skills a candidate must have. Short skill names only.")
    nice_to_have: List[str] = Field(default_factory=list, description="Skills that help but are not required")
    min_years: int = Field(default=0, description="Minimum years of experience, 0 if not stated")
    summary: str = Field(default="", description="One sentence on what the role does")


class RequirementsUpdate(Requirements):
    change_summary: str = Field(description="One sentence describing what changed and why")


class CandidateNote(BaseModel):
    candidate_id: str
    strengths: List[str] = Field(description="Up to 3 strengths grounded in the resume text")
    concerns: List[str] = Field(description="Up to 3 concerns or gaps grounded in the resume text")
    note: str = Field(description="One-sentence analyst assessment")


class DeepAnalysis(BaseModel):
    candidates: List[CandidateNote]


class InterviewQuestions(BaseModel):
    questions: List[str] = Field(description="5 or 6 screening questions")


# ---------------------------------------------------------------- vocabulary

@lru_cache(maxsize=1)
def vocabulary() -> tuple:
    return tuple(get_hub().call_tool("resume_search", "skill_vocabulary")["skills"])


def canonical_skill(skill: str) -> str:
    cleaned = skill.strip().strip(".")
    lowered = cleaned.lower()
    if lowered in ALIASES:
        return ALIASES[lowered]
    for known in vocabulary():
        if known.lower() == lowered:
            return known
    return cleaned


def _dedupe(skills) -> list:
    seen = set()
    out = []
    for skill in skills:
        skill = canonical_skill(skill)
        if skill and skill.lower() not in seen:
            seen.add(skill.lower())
            out.append(skill)
    return out


def normalize_requirements(req: dict) -> dict:
    must = _dedupe(req.get("must_have") or [])
    must_lower = {s.lower() for s in must}
    nice = [s for s in _dedupe(req.get("nice_to_have") or []) if s.lower() not in must_lower]
    return {
        "title": (req.get("title") or "Open role").strip(),
        "must_have": must,
        "nice_to_have": nice,
        "min_years": max(0, int(req.get("min_years") or 0)),
        "summary": (req.get("summary") or "").strip(),
        "description": (req.get("description") or "").strip(),
        "structured": bool(req.get("structured")),
    }


# -------------------------------------------------------------- requirements

def _years_in(text: str) -> Optional[int]:
    match = re.search(r"(\d+)\s*\+?\s*(?:years|yrs|year)", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


AMBIGUOUS_ALIASES = {"go", "ts", "js", "ml", "node", "tf"}


def _skill_pattern(skill: str) -> str:
    return r"(?<!\w)" + re.escape(skill) + r"(?!\w)"


def _skills_in(text: str) -> list:
    """Known skills named in text. Longer names are matched first, so
    'React Native' is not also counted as 'React'."""
    names = [(skill, skill) for skill in vocabulary()]
    names += [(alias, skill) for alias, skill in ALIASES.items() if alias not in AMBIGUOUS_ALIASES]
    names.sort(key=lambda pair: -len(pair[0]))
    remaining = text
    found = []
    for name, skill in names:
        if re.search(_skill_pattern(name), remaining, flags=re.IGNORECASE):
            if skill.lower() not in {s.lower() for s in found}:
                found.append(skill)
            remaining = re.sub(_skill_pattern(name), " ", remaining, flags=re.IGNORECASE)
    return found


LIST_STOPWORDS = {"experience", "skills", "skill", "knowledge", "background", "the", "a", "an", "strong", "good", "solid"}


def _listed_skills(text: str) -> list:
    """Skills named after 'with' / 'in' even when no resume lists them ('with Rust and Haskell')."""
    match = re.search(r"\b(?:with|in|knows?|having|skilled in)\s+(.+?)(?:\s+(?:and\s+)?(?:\d+\s*\+?\s*(?:years|yrs))|[.;]|$)", text, flags=re.IGNORECASE)
    if not match:
        return []
    found = []
    for part in re.split(r",|\band\b|\bor\b|/", match.group(1)):
        words = [w for w in part.strip().split() if w.lower() not in LIST_STOPWORDS]
        if 1 <= len(words) <= 3:
            found.append(" ".join(words))
    return found


def rule_based_requirements(jd: str) -> dict:
    if "Must-have skills:" in jd:
        try:
            job = parse_job_text(jd)
            return normalize_requirements(
                {
                    "title": job["title"],
                    "must_have": job["must_have_skills"],
                    "nice_to_have": [s for s in _skills_in(job["description"]) if s.lower() not in {m.lower() for m in job["must_have_skills"]}],
                    "min_years": job["min_years"],
                    "summary": job["description"].splitlines()[0] if job["description"] else "",
                    "description": job["description"],
                    "structured": True,
                }
            )
        except ValueError:
            pass
    lowered = jd.lower()
    split_at = None
    for marker in ("nice to have", "nice-to-have", "bonus", "preferred", "plus"):
        index = lowered.find(marker)
        if index != -1 and (split_at is None or index < split_at):
            split_at = index
    must_text = jd if split_at is None else jd[:split_at]
    nice_text = "" if split_at is None else jd[split_at:]
    must = _dedupe(_skills_in(must_text) + _listed_skills(must_text))
    nice = _skills_in(nice_text)
    title = "Candidates with " + ", ".join(must) if must else "Open role"
    return normalize_requirements(
        {
            "title": title,
            "must_have": must,
            "nice_to_have": nice,
            "min_years": _years_in(jd) or 0,
            "summary": jd.strip().splitlines()[0][:200] if jd.strip() else "",
            "description": jd.strip(),
        }
    )


EXTRACT_PROMPT = """You turn a job description or a recruiter's request into structured hiring requirements.
Rules:
- must_have: only skills the text clearly requires ("must", "required", "need", "with X").
- nice_to_have: skills described as preferred, bonus, plus, or only mentioned as part of the work.
- Use short canonical skill names (React, TypeScript, Python, Django, Kubernetes).
- min_years: the minimum years of experience stated, otherwise 0.
Known skills in the resume database (prefer these spellings): {vocab}"""


def extract_requirements(jd: str) -> dict:
    """Must-have vs nice-to-have skills, minimum years, and a title."""
    baseline = rule_based_requirements(jd)
    result = llm.structured(
        Requirements,
        [
            ("system", EXTRACT_PROMPT.format(vocab=", ".join(vocabulary()))),
            ("human", jd),
        ],
        where="extract_requirements",
    )
    if result is None:
        baseline["source"] = "rules"
        return baseline
    req = result.model_dump()
    if "Must-have skills:" in jd:
        # A structured JD header is authoritative for must-haves and years.
        req["must_have"] = baseline["must_have"]
        req["min_years"] = baseline["min_years"]
        req["title"] = baseline["title"]
    req["description"] = baseline["description"]
    req["structured"] = baseline["structured"]
    req = normalize_requirements(req)
    if not req["must_have"]:
        req["must_have"] = baseline["must_have"]
    req["source"] = "llm"
    return req


REFINE_PROMPT = """You update hiring requirements based on the recruiter's latest instruction.
Return the complete updated requirements. Change only what the recruiter asked for and keep everything else.
Moving a skill to nice_to_have removes it from must_have, and the reverse.
Current requirements (JSON):
{current}
Known skills in the resume database: {vocab}"""


def rule_based_refine(current: dict, instruction: str) -> dict:
    req = dict(current)
    must = list(req["must_have"])
    nice = list(req["nice_to_have"])
    lowered = instruction.lower()
    changes = []
    years = _years_in(instruction)
    if years is not None:
        req["min_years"] = years
        changes.append("minimum years set to {0}".format(years))
    named = _skills_in(instruction)
    for skill in must + nice:
        if mentioned(instruction, skill) and skill.lower() not in {s.lower() for s in named}:
            named.append(skill)
    for skill in named:
        pattern = r"(remove|drop|without|don't need|do not need|no longer|not required)[^.,;]*" + re.escape(skill.lower())
        if re.search(pattern, lowered):
            must = [s for s in must if s.lower() != skill.lower()]
            nice = [s for s in nice if s.lower() != skill.lower()]
            changes.append("removed {0}".format(skill))
        elif re.search(re.escape(skill.lower()) + r"[^.,;]*(nice|optional|bonus|preferred)", lowered) or re.search(
            r"(nice|optional|bonus|preferred)[^.,;]*" + re.escape(skill.lower()), lowered
        ):
            must = [s for s in must if s.lower() != skill.lower()]
            if skill.lower() not in {s.lower() for s in nice}:
                nice.append(skill)
            changes.append("{0} is now nice-to-have".format(skill))
        else:
            nice = [s for s in nice if s.lower() != skill.lower()]
            if skill.lower() not in {s.lower() for s in must}:
                must.append(skill)
                changes.append("added {0} as a must-have".format(skill))
    req["must_have"] = must
    req["nice_to_have"] = nice
    if not req.get("structured"):
        req["description"] = ""
        req["title"] = "Candidates with " + ", ".join(must) if must else "Open role"
    req = normalize_requirements(req)
    req["change_summary"] = "; ".join(changes) or "no recognizable change"
    req["source"] = "rules"
    return req


def refine_requirements(current: dict, instruction: str) -> dict:
    """Apply a natural-language change ("make TypeScript a must-have") to requirements."""
    shown = {k: current.get(k) for k in ("title", "must_have", "nice_to_have", "min_years", "summary")}
    result = llm.structured(
        RequirementsUpdate,
        [
            ("system", REFINE_PROMPT.format(current=json.dumps(shown), vocab=", ".join(vocabulary()))),
            ("human", instruction),
        ],
        where="refine_requirements",
    )
    if result is None:
        return rule_based_refine(current, instruction)
    data = result.model_dump()
    change_summary = data.pop("change_summary", "")
    # A free-text request is only the original wording; re-embedding it would keep matching the old skills.
    data["description"] = current.get("description", "") if current.get("structured") else ""
    data["structured"] = current.get("structured", False)
    req = normalize_requirements(data)
    req["change_summary"] = change_summary
    req["source"] = "llm"
    return req


def describe_requirement_change(old: dict, new: dict) -> list:
    lines = []
    old_must = {s.lower(): s for s in old.get("must_have", [])}
    new_must = {s.lower(): s for s in new.get("must_have", [])}
    old_nice = {s.lower(): s for s in old.get("nice_to_have", [])}
    new_nice = {s.lower(): s for s in new.get("nice_to_have", [])}
    for key, skill in new_must.items():
        if key not in old_must:
            lines.append("{0} is now a must-have".format(skill))
    for key, skill in old_must.items():
        if key not in new_must:
            lines.append("{0} is no longer a must-have".format(skill) + (" (moved to nice-to-have)" if key in new_nice else ""))
    for key, skill in new_nice.items():
        if key not in old_nice and key not in old_must:
            lines.append("{0} added as nice-to-have".format(skill))
    for key, skill in old_nice.items():
        if key not in new_nice and key not in new_must:
            lines.append("{0} removed from nice-to-have".format(skill))
    if old.get("min_years") != new.get("min_years"):
        lines.append("minimum years changed from {0} to {1}".format(old.get("min_years"), new.get("min_years")))
    return lines


# ------------------------------------------------------------------- ranking

def _cache_key(req: dict) -> str:
    keep = {k: req.get(k) for k in ("title", "must_have", "nice_to_have", "min_years", "description")}
    return json.dumps(keep, sort_keys=True)


@lru_cache(maxsize=32)
def _rank_cached(key: str) -> dict:
    result = get_hub().call_tool("resume_search", "rank_resumes", {"requirements": json.loads(key), "strict": False})
    return {"pool_size": result["pool_size"], "all": result["ranked"]}


def invalidate_index_caches() -> None:
    """Forget cached rankings and skills after the resume index changes."""
    _rank_cached.cache_clear()
    vocabulary.cache_clear()


def full_ranking(req: dict) -> dict:
    """Pool size and every resume scored against req, best first."""
    return _rank_cached(_cache_key(req))


def round_one(req: dict, k: int = 10) -> dict:
    ranking = full_ranking(req)
    qualified = sum(1 for item in ranking["all"] if item["breakdown"]["meets_must_haves"])
    return {"pool_size": ranking["pool_size"], "qualified": qualified, "top": [dict(item) for item in ranking["all"][:k]]}


def evaluate_candidate(req: dict, cid: str) -> Optional[dict]:
    for item in full_ranking(req)["all"]:
        if item["candidate_id"] == cid:
            return dict(item)
    return None


def all_candidates() -> list:
    """(candidate_id, name) for every indexed resume."""
    return [(item["candidate_id"], item["candidate_name"]) for item in full_ranking({"title": "any", "must_have": [], "nice_to_have": [], "min_years": 0, "description": ""})["all"]]


def resolve_candidate(ref: str, shortlist: Optional[list] = None) -> Optional[str]:
    """Turn 'Meera', 'meera_krishnan', '#2', or 'rank 3' into a candidate id."""
    ref = (ref or "").strip()
    if not ref:
        return None
    shortlist = shortlist or []
    rank_match = re.fullmatch(r"(?:#|rank\s*|no\.?\s*|top\s*)?(\d{1,2})", ref.lower())
    if rank_match and shortlist:
        position = int(rank_match.group(1))
        if 1 <= position <= len(shortlist):
            return shortlist[position - 1]["candidate_id"]
    lowered = ref.lower().replace("_", " ")
    lowered = re.sub(r"^resume\s+", "", lowered)
    people = all_candidates()
    for cid, name in people:
        if lowered in (cid.replace("_", " "), name.lower()):
            return cid
    in_list = {item["candidate_id"] for item in shortlist}
    partial = [(cid, name) for cid, name in people if all(part in name.lower().split() for part in lowered.split())]
    if partial:
        partial.sort(key=lambda pair: (pair[0] not in in_list, pair[1]))
        return partial[0][0]
    return None


# ------------------------------------------------------- round 2: deep dive

def resume_texts(resume_paths: list) -> dict:
    """Full text of several resumes in one filesystem.batch_process call."""
    if not resume_paths:
        return {}
    batch = get_hub().call_tool("filesystem", "batch_process", {"operation": "read", "paths": list(resume_paths)})
    return {r["path"]: (r["data"]["content"] if r["success"] else "") for r in batch["results"]}


def resume_text(resume_path: str) -> str:
    return resume_texts([resume_path]).get(resume_path, "")


def evidence_for(resume_paths: list, skills: list) -> dict:
    """Best line proving each skill in each resume, from one batch search.

    Lines from the experience or summary are preferred over the comma-separated skills list.
    """
    if not resume_paths or not skills:
        return {path: {} for path in resume_paths}
    batch = get_hub().call_tool(
        "filesystem",
        "batch_process",
        {"operation": "search", "paths": list(resume_paths), "keywords": list(skills), "whole_word": True},
    )
    evidence = {}
    for result in batch["results"]:
        found = {}
        if result["success"]:
            for skill, hits in result["data"]["matches"].items():
                lines = [hit["line"] for hit in hits if mentioned(hit["line"], skill)]
                if lines:
                    narrative = [line for line in lines if len(line.split(",")) < 3]
                    found[skill] = (narrative or lines)[0][:180]
        evidence[result["path"]] = found
    return evidence


def find_evidence(resume_path: str, skills: list) -> dict:
    return evidence_for([resume_path], skills).get(resume_path, {})


def _years(n: int) -> str:
    return "{0} year{1}".format(n, "" if n == 1 else "s")


def rule_strengths_and_gaps(item: dict) -> tuple:
    b = item["breakdown"]
    strengths = []
    gaps = []
    if b["matched_must"]:
        strengths.append("Has every listed must-have: {0}".format(", ".join(b["matched_must"])) if not b["missing_must"]
                         else "Has must-haves: {0}".format(", ".join(b["matched_must"])))
    if b["matched_nice"]:
        strengths.append("Brings nice-to-have skills: {0}".format(", ".join(b["matched_nice"])))
    if b["min_years"] and b["years"] >= b["min_years"]:
        extra = b["years"] - b["min_years"]
        strengths.append("{0} of experience{1}".format(_years(b["years"]), " ({0} above the bar)".format(extra) if extra else " (meets the bar)"))
    if b["similarity"] >= 0.6:
        strengths.append("Resume wording closely matches the role ({0}% similarity)".format(int(b["similarity"] * 100)))
    for skill in b["missing_must"]:
        gaps.append("Missing must-have skill: {0}".format(skill))
    if b["min_years"] and b["years"] < b["min_years"]:
        gaps.append("{0} of experience, {1} short of the {2}-year minimum".format(
            _years(b["years"]), b["min_years"] - b["years"], b["min_years"]))
    if b["missing_nice"]:
        gaps.append("No evidence of nice-to-have: {0}".format(", ".join(b["missing_nice"])))
    return strengths, gaps


DEEP_PROMPT = """You are a senior technical recruiter doing a second-round review of shortlisted resumes.
Requirements: {req}
For each candidate, give up to 3 strengths and up to 3 concerns grounded only in their resume text, and a one-sentence note.
Do not invent experience that is not in the resume. Use the candidate_id values exactly as given."""


def round_two(req: dict, top: list) -> list:
    """Read every shortlisted resume, collect evidence, and add analyst notes."""
    analysed = []
    packets = []
    skills = list(req["must_have"]) + list(req["nice_to_have"])
    paths = [item["resume_path"] for item in top]
    texts = resume_texts(paths)
    evidence = evidence_for(paths, skills)
    for item in top:
        item = dict(item)
        text = texts.get(item["resume_path"], "")
        item["evidence"] = evidence.get(item["resume_path"], {})
        strengths, gaps = rule_strengths_and_gaps(item)
        item["strengths"] = strengths
        item["gaps"] = gaps
        item["ai_strengths"] = []
        item["ai_concerns"] = []
        item["analyst_note"] = ""
        analysed.append(item)
        packets.append("candidate_id: {0}\n{1}".format(item["candidate_id"], " ".join(text.split())[:900]))

    shown = {k: req.get(k) for k in ("title", "must_have", "nice_to_have", "min_years")}
    result = llm.structured(
        DeepAnalysis,
        [
            ("system", DEEP_PROMPT.format(req=json.dumps(shown))),
            ("human", "\n\n---\n\n".join(packets)),
        ],
        where="round_two deep analysis",
    )
    notes = {note.candidate_id: note for note in (result.candidates if result else [])}
    for item in analysed:
        note = notes.get(item["candidate_id"])
        if note:
            item["ai_strengths"] = note.strengths[:3]
            item["ai_concerns"] = note.concerns[:3]
            item["analyst_note"] = note.note
        else:
            item["analyst_note"] = _fallback_note(item)
    return analysed


def _fallback_note(item: dict) -> str:
    b = item["breakdown"]
    if b["meets_must_haves"]:
        return "{0} meets every hard requirement with {1} years of experience.".format(item["candidate_name"], b["years"])
    problems = list(b["missing_must"])
    if not b["meets_years"]:
        problems.append("{0}+ years".format(b["min_years"]))
    return "{0} is relevant but falls short on {1}.".format(item["candidate_name"], ", ".join(problems))


# -------------------------------------------------- round 3: recommendation

def recommend(item: dict) -> tuple:
    """hire / borderline / no-hire and the rule that produced it."""
    b = item["breakdown"]
    score = item["match_score"]
    years_short = max(0, b["min_years"] - b["years"]) if b["min_years"] else 0
    gap_count = len(b["missing_must"]) + (1 if years_short else 0)
    if gap_count == 0:
        if score >= HIRE_SCORE:
            return "hire", "Meets every must-have and the years bar with a score of {0} (hire bar {1}).".format(score, HIRE_SCORE)
        return "borderline", "Meets every hard requirement, but the overall fit score of {0} is below the hire bar of {1}; review the resume before deciding.".format(score, HIRE_SCORE)
    if gap_count == 1 and years_short <= MAX_YEARS_SHORT and score >= NEAR_MISS_SCORE:
        gap = b["missing_must"][0] if b["missing_must"] else "{0} more of experience needed".format(_years(years_short))
        return "borderline", "One gap ({0}); otherwise a relevant profile with a score of {1}.".format(gap, score)
    return "no-hire", "{0} hard-requirement gaps and a score of {1}.".format(gap_count, score)


def improvement_suggestions(item: dict) -> list:
    b = item["breakdown"]
    tips = []
    for skill in b["missing_must"]:
        tips.append("Show hands-on {0} (a shipped project, open-source work, or certification); it is a must-have.".format(skill))
    if b["min_years"] and b["years"] < b["min_years"]:
        short = b["min_years"] - b["years"]
        tips.append("Has {0} of {1} required years; consider for a mid-level opening, or revisit after {2} more.".format(b["years"], b["min_years"], _years(short)))
    for skill in b["missing_nice"][:2]:
        tips.append("Adding {0} experience would strengthen the profile.".format(skill))
    if not b["missing_must"] and b["similarity"] < 0.6:
        tips.append("Rewrite the summary and experience bullets around the role's core work so the relevant experience is easy to see.")
    return tips


def round_three(analysed: list) -> list:
    final = []
    for item in analysed:
        item = dict(item)
        verdict, reason = recommend(item)
        item["recommendation"] = verdict
        item["recommendation_reason"] = reason
        item["suggestions"] = improvement_suggestions(item) if verdict != "hire" else []
        final.append(item)
    return final


# ------------------------------------------------------------ explanations

def explain_pair(req: dict, cid_a: str, cid_b: str) -> dict:
    """Point-by-point reason why one candidate outranks the other."""
    a = evaluate_candidate(req, cid_a)
    b = evaluate_candidate(req, cid_b)
    if a is None or b is None:
        return {"error": "Unknown candidate id: {0}".format(cid_a if a is None else cid_b)}
    higher, lower = (a, b) if a["rank"] < b["rank"] else (b, a)
    hb, lb = higher["breakdown"], lower["breakdown"]
    factors = []
    for label, key in (("semantic similarity", "similarity_points"), ("skill coverage", "skill_points"), ("years of experience", "years_points")):
        diff = round(hb[key] - lb[key], 1)
        if abs(diff) >= 0.5:
            factors.append({"factor": label, "higher": hb[key], "lower": lb[key], "difference": diff})
    if hb["penalty"] != lb["penalty"]:
        factors.append({"factor": "hard-requirement penalty", "higher": -hb["penalty"], "lower": -lb["penalty"], "difference": round(lb["penalty"] - hb["penalty"], 1)})
    factors.sort(key=lambda f: -abs(f["difference"]))
    return {
        "higher": {"name": higher["candidate_name"], "rank": higher["rank"], "score": higher["match_score"], "breakdown": hb},
        "lower": {"name": lower["candidate_name"], "rank": lower["rank"], "score": lower["match_score"], "breakdown": lb},
        "score_gap": higher["match_score"] - lower["match_score"],
        "factors": factors,
        "only_higher_has": [s for s in hb["matched_must"] + hb["matched_nice"] if s not in lb["matched_must"] + lb["matched_nice"]],
        "only_lower_has": [s for s in lb["matched_must"] + lb["matched_nice"] if s not in hb["matched_must"] + hb["matched_nice"]],
        "scoring_rule": SCORING_RULE,
    }


def diff_rankings(old_req: dict, new_req: dict, old_list: list, new_list: list, new_ids: Optional[list] = None) -> list:
    """What moved between two shortlists and why. new_ids are resumes indexed since old_list was made."""
    new_ids = set(new_ids or [])
    old_pos = {item["candidate_id"]: item for item in old_list}
    new_pos = {item["candidate_id"]: item for item in new_list}
    old_all = {item["candidate_id"]: item for item in full_ranking(old_req)["all"]}
    new_all = {item["candidate_id"]: item for item in full_ranking(new_req)["all"]}
    removed = [cid for cid in old_pos if cid not in new_all]
    changes = []
    for cid in list(new_pos) + [c for c in old_pos if c not in new_pos]:
        before = old_pos.get(cid) or old_all.get(cid)
        after = new_all.get(cid)
        if after is None and cid in old_pos:
            changes.append({
                "candidate_id": cid, "candidate_name": old_pos[cid]["candidate_name"],
                "old_rank": old_pos[cid]["rank"], "new_rank": None,
                "old_score": old_pos[cid]["match_score"], "new_score": 0,
                "movement": "dropped out of the top {0} (was #{1})".format(len(old_list), old_pos[cid]["rank"]),
                "reasons": ["resume was deleted and removed from the index"],
            })
            continue
        if before is None or after is None:
            continue
        old_rank = old_pos[cid]["rank"] if cid in old_pos else None
        new_rank = new_pos[cid]["rank"] if cid in new_pos else None
        if old_rank == new_rank and before["match_score"] == after["match_score"]:
            continue
        reasons = []
        ob, nb = before["breakdown"], after["breakdown"]
        for skill in nb["missing_must"]:
            if skill not in ob["missing_must"]:
                reasons.append("does not have new must-have {0}".format(skill))
        for skill in ob["missing_must"]:
            if skill not in nb["missing_must"]:
                reasons.append("{0} is no longer required".format(skill))
        for skill in nb["matched_must"] + nb["matched_nice"]:
            if skill not in ob["matched_must"] + ob["matched_nice"]:
                reasons.append("has newly listed skill {0}".format(skill))
        if ob["meets_years"] and not nb["meets_years"]:
            reasons.append("{0} is below the new {1}-year minimum".format(_years(nb["years"]), nb["min_years"]))
        if not ob["meets_years"] and nb["meets_years"]:
            reasons.append("now meets the {0}-year minimum".format(nb["min_years"]))
        if old_rank and new_rank and new_rank < old_rank and after["match_score"] < before["match_score"]:
            reasons.append("moved up because candidates above lost more points")
        elif old_rank and new_rank and new_rank > old_rank and after["match_score"] > before["match_score"]:
            reasons.append("moved down because others gained more points")
        if cid in new_ids:
            reasons = ["newly indexed resume picked up by watch_directory"]
        elif not reasons and new_ids and new_rank and old_rank and new_rank > old_rank:
            reasons.append("pushed down by a newly indexed resume")
        elif not reasons and removed and new_rank and old_rank and new_rank < old_rank:
            reasons.append("moved up after a deleted resume left the list")
        if not reasons:
            reasons.append("relative shift as other candidates gained or lost points")
        if old_rank is None:
            movement = "entered the top {0} at #{1}".format(len(new_list), new_rank)
        elif new_rank is None:
            movement = "dropped out of the top {0} (was #{1})".format(len(old_list), old_rank)
        elif new_rank < old_rank:
            movement = "moved up from #{0} to #{1}".format(old_rank, new_rank)
        elif new_rank > old_rank:
            movement = "moved down from #{0} to #{1}".format(old_rank, new_rank)
        else:
            movement = "stayed at #{0}".format(new_rank)
        changes.append(
            {
                "candidate_id": cid,
                "candidate_name": after["candidate_name"],
                "old_rank": old_rank,
                "new_rank": new_rank,
                "old_score": before["match_score"],
                "new_score": after["match_score"],
                "movement": movement,
                "reasons": reasons,
            }
        )
    changes.sort(key=lambda c: (c["new_rank"] is None, c["new_rank"] or 99))
    return changes


QUESTIONS_PROMPT = """You write screening interview questions for one candidate.
Role requirements: {req}
Candidate: {name}. Strengths: {strengths}. Gaps: {gaps}.
Resume:
{resume}
Write 5 or 6 specific questions: 2 that verify claimed strengths with depth, 2 that probe the gaps, 1 scenario question about the role's core work, 1 behavioral question."""


def interview_questions(req: dict, cid: str) -> dict:
    item = evaluate_candidate(req, cid)
    if item is None:
        return {"error": "Unknown candidate id: {0}".format(cid)}
    strengths, gaps = rule_strengths_and_gaps(item)
    shown = {k: req.get(k) for k in ("title", "must_have", "nice_to_have", "min_years")}
    result = llm.structured(
        InterviewQuestions,
        [
            ("system", QUESTIONS_PROMPT.format(
                req=json.dumps(shown),
                name=item["candidate_name"],
                strengths="; ".join(strengths) or "none listed",
                gaps="; ".join(gaps) or "none",
                resume=" ".join(resume_text(item["resume_path"]).split())[:1200],
            )),
            ("human", "Write the questions."),
        ],
        where="interview_questions",
    )
    if result is not None and result.questions:
        questions = result.questions
        source = "llm"
    else:
        b = item["breakdown"]
        questions = []
        for skill in b["matched_must"][:2]:
            questions.append("Walk me through the most complex {0} work you have shipped. What trade-offs did you make?".format(skill))
        for skill in b["missing_must"]:
            questions.append("This role requires {0}. What related experience do you have, and how quickly could you ramp up?".format(skill))
        if not b["meets_years"]:
            questions.append("You have {0} years of experience and the role asks for {1}. Which projects show senior-level ownership?".format(b["years"], b["min_years"]))
        for skill in b["missing_nice"][:1]:
            questions.append("Have you worked with {0}? If not, what have you used instead?".format(skill))
        questions.append("Describe how you would approach the first 90 days as a {0}.".format(req.get("title", "team member")))
        questions.append("Tell me about a production incident you owned end to end. What did you change afterwards?")
        source = "rules"
    return {"candidate_id": cid, "candidate_name": item["candidate_name"], "questions": questions, "source": source}


# ------------------------------------------------------------------ report

VERDICT_LABEL = {"hire": "HIRE", "borderline": "BORDERLINE", "no-hire": "NO-HIRE"}


def render_report(req: dict, shortlist: list, pool_size: int, qualified: int, changes: list, version: int) -> str:
    lines = [
        "# Match report: {0}".format(req["title"]),
        "",
        "Version {0}".format(version),
        "",
        "## Requirements",
        "",
        "- Must-have: {0}".format(", ".join(req["must_have"]) or "none"),
        "- Nice-to-have: {0}".format(", ".join(req["nice_to_have"]) or "none"),
        "- Minimum years: {0}".format(req["min_years"]),
        "",
        "## Screening rounds",
        "",
        "1. Initial screen: scored all {0} resumes and kept the top {1}. {2} of {0} meet every hard requirement.".format(pool_size, len(shortlist), qualified),
        "2. Deep analysis: read each shortlisted resume in full, collected evidence lines, and wrote strengths, gaps, and analyst notes.",
        "3. Recommendation: hire / borderline / no-hire from the rules below, with improvement suggestions for borderline candidates.",
        "",
        "Scoring: " + SCORING_RULE + ".",
        "Hire needs every must-have, the years bar, and a score of {0}+. Meeting every hard requirement with a lower score, or one gap (a single missing must-have, or up to {1} years short) with a score of {2}+, is borderline.".format(HIRE_SCORE, MAX_YEARS_SHORT, NEAR_MISS_SCORE),
        "",
    ]
    if changes:
        lines += ["## Ranking changes since the last version", ""]
        for change in changes:
            lines.append("- **{0}** {1} (score {2} -> {3}): {4}".format(
                change["candidate_name"], change["movement"], change["old_score"], change["new_score"], "; ".join(change["reasons"])))
        lines.append("")
    lines += [
        "## Shortlist",
        "",
        "| Rank | Candidate | Score | Years | Missing must-haves | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for item in shortlist:
        b = item["breakdown"]
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            item["rank"], item["candidate_name"], item["match_score"], b["years"],
            ", ".join(b["missing_must"]) or "-", VERDICT_LABEL[item["recommendation"]]))
    lines.append("")
    for item in shortlist:
        b = item["breakdown"]
        lines += [
            "### #{0} {1} - {2}".format(item["rank"], item["candidate_name"], VERDICT_LABEL[item["recommendation"]]),
            "",
            "Resume: `{0}`".format(item["resume_path"]),
            "",
            "Score {0} = similarity {1} + skills {2} + years {3} - penalty {4}".format(
                item["match_score"], b["similarity_points"], b["skill_points"], b["years_points"], b["penalty"]),
            "",
            "Verdict reason: {0}".format(item["recommendation_reason"]),
            "",
        ]
        if item.get("analyst_note"):
            lines += ["Analyst note: {0}".format(item["analyst_note"]), ""]
        lines.append("Strengths:")
        lines += ["- {0}".format(s) for s in item["strengths"] + item.get("ai_strengths", [])] or ["- none"]
        lines.append("")
        lines.append("Gaps:")
        gaps = item["gaps"] + item.get("ai_concerns", [])
        lines += ["- {0}".format(g) for g in gaps] if gaps else ["- none found"]
        lines.append("")
        if item.get("evidence"):
            lines.append("Evidence from the resume:")
            lines += ["- {0}: \"{1}\"".format(skill, line) for skill, line in item["evidence"].items()]
            lines.append("")
        if item.get("suggestions"):
            lines.append("Improvement suggestions:")
            lines += ["- {0}".format(tip) for tip in item["suggestions"]]
            lines.append("")
    return "\n".join(lines)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50] or "role"
