"""Match a job description against the resume index.

Hybrid search takes semantic neighbors and resumes that contain every
must-have skill, drops anyone below the years bar or missing a must-have
skill, then scores the rest from 0 to 100.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from resume_rag import get_collection, get_model


def parse_job_text(text: str) -> dict:
    """Read the Title, Must-have skills, and Minimum years header."""
    title = ""
    skills = []
    min_years = 0
    saw_header = False
    body = []
    in_header = True
    for line in text.splitlines():
        if in_header and line.startswith("Title:"):
            title = line.split(":", 1)[1].strip()
            saw_header = True
            continue
        if in_header and line.startswith("Must-have skills:"):
            raw = line.split(":", 1)[1]
            skills = [part.strip() for part in raw.split(",") if part.strip()]
            saw_header = True
            continue
        if in_header and line.startswith("Minimum years:"):
            min_years = int(line.split(":", 1)[1].strip())
            saw_header = True
            continue
        if in_header and line.strip() == "" and saw_header:
            in_header = False
            continue
        in_header = False
        body.append(line)
    if not title or not skills:
        raise ValueError("Job file needs a Title and Must-have skills line.")
    return {
        "title": title,
        "must_have_skills": skills,
        "min_years": min_years,
        "description": "\n".join(body).strip(),
        "text": text.strip(),
    }


def parse_job_file(path) -> dict:
    return parse_job_text(Path(path).read_text(encoding="utf-8"))


def skill_set(skills_text: str) -> set:
    return {part.strip().lower() for part in skills_text.split(",") if part.strip()}


def mentioned(text: str, skill: str) -> bool:
    """True when skill appears as its own word, so Java does not match JavaScript."""
    pattern = r"(?<!\w)" + re.escape(skill) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def violates_must_have(meta: dict, job: dict) -> bool:
    """True when years are too low or a required skill is missing."""
    have = skill_set(meta["skills"])
    if int(meta["experience_years"]) < int(job["min_years"]):
        return True
    return any(skill.lower() not in have for skill in job["must_have_skills"])


def _excerpt(text: str, limit: int = 280) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3] + "..."


def _calibrated_similarity(similarity: float) -> float:
    """Stretch MiniLM cosine into 0-1.

    A real section match on this model usually lands between 0.45 and 0.80,
    so that band is what the 60% similarity weight uses.
    """
    return max(0.0, min(1.0, (similarity - 0.45) / 0.35))


def _reasoning(score: int, section: str, similarity: float, matched: list, years: int, min_years: int) -> str:
    if score >= 80:
        tone = "Strong match"
    elif score >= 60:
        tone = "Partial match"
    else:
        tone = "Weak match"
    skills_bit = ", ".join(matched) if matched else "none"
    if min_years:
        if years >= min_years:
            years_bit = "{0} years of experience meets the {1}+ year requirement".format(years, min_years)
        else:
            years_bit = "{0} years of experience is below the {1}+ year requirement".format(years, min_years)
    else:
        years_bit = "{0} years of experience".format(years)
    return (
        "{0} on the {1} section ({2}% semantic similarity). "
        "Matched skills: {3}. {4}."
    ).format(tone, section, int(round(similarity * 100)), skills_bit, years_bit)


def _jd_skills(job: dict, vocab: list) -> list:
    """Must-have skills plus other known skills named in the job text."""
    chosen = list(job["must_have_skills"])
    seen = {skill.lower() for skill in chosen}
    for skill in vocab:
        if skill.lower() in seen:
            continue
        if mentioned(job["text"], skill):
            chosen.append(skill)
            seen.add(skill.lower())
    return chosen


def _group_hits(payload: dict) -> dict:
    grouped = {}
    for document, meta, distance in zip(
        payload["documents"][0],
        payload["metadatas"][0],
        payload["distances"][0],
    ):
        path = meta["resume_path"]
        similarity = max(0.0, min(1.0, 1.0 - float(distance)))
        slot = grouped.setdefault(
            path,
            {
                "meta": meta,
                "similarity": -1.0,
                "section": meta["section"],
                "excerpts": [],
            },
        )
        slot["excerpts"].append((similarity, document))
        if similarity > slot["similarity"]:
            slot["similarity"] = similarity
            slot["section"] = meta["section"]
    return grouped


def match_job(job: dict, k: int = 10, hybrid: bool = True, persist_dir: str = "chroma_db") -> dict:
    """Return the brief's JSON object for one parsed job."""
    collection = get_collection(persist_dir)
    count = collection.count()
    if count == 0:
        raise SystemExit("The resume index is empty. Run python resume_rag.py first.")

    embedding = get_model().encode(job["text"]).tolist()
    payload = collection.query(
        query_embeddings=[embedding],
        n_results=count,
        include=["documents", "metadatas", "distances"],
    )
    grouped = _group_hits(payload)

    vocab = []
    seen_skills = set()
    for slot in grouped.values():
        for skill in slot["meta"]["skills"].split(","):
            skill = skill.strip()
            if skill and skill.lower() not in seen_skills:
                seen_skills.add(skill.lower())
                vocab.append(skill)
    jd_skills = _jd_skills(job, vocab)

    if hybrid:
        semantic_pool = {
            path
            for path, _slot in sorted(grouped.items(), key=lambda item: -item[1]["similarity"])[:15]
        }
        keyword_pool = set()
        for path, slot in grouped.items():
            have = skill_set(slot["meta"]["skills"])
            if all(skill.lower() in have for skill in job["must_have_skills"]):
                keyword_pool.add(path)
        pool = semantic_pool | keyword_pool
    else:
        pool = set(grouped)

    matches = []
    for path in pool:
        slot = grouped[path]
        meta = slot["meta"]
        if hybrid and violates_must_have(meta, job):
            continue
        have = skill_set(meta["skills"])
        matched = [skill for skill in jd_skills if skill.lower() in have]
        skill_frac = (len(matched) / float(len(jd_skills))) if jd_skills else 1.0
        years = int(meta["experience_years"])
        min_years = int(job["min_years"])
        years_frac = min(1.0, years / float(min_years)) if min_years else 1.0
        similarity = max(0.0, float(slot["similarity"]))
        if hybrid:
            raw = 0.60 * _calibrated_similarity(similarity) + 0.25 * skill_frac + 0.15 * years_frac
        else:
            raw = similarity
        score = max(0, min(100, int(round(100 * raw))))
        excerpts = [
            _excerpt(text)
            for _similarity, text in sorted(slot["excerpts"], key=lambda item: -item[0])[:2]
        ]
        matches.append(
            {
                "candidate_name": meta["candidate_name"],
                "resume_path": path,
                "match_score": score,
                "matched_skills": matched,
                "relevant_excerpts": excerpts,
                "reasoning": _reasoning(
                    score,
                    str(slot["section"]).title(),
                    similarity,
                    matched,
                    years,
                    min_years,
                ),
                "similarity": similarity,
            }
        )

    matches.sort(key=lambda item: (-item["match_score"], -item["similarity"], item["candidate_name"]))
    top = []
    for item in matches[:k]:
        top.append(
            {
                "candidate_name": item["candidate_name"],
                "resume_path": item["resume_path"],
                "match_score": item["match_score"],
                "matched_skills": item["matched_skills"],
                "relevant_excerpts": item["relevant_excerpts"],
                "reasoning": item["reasoning"],
            }
        )
    return {"job_description": job["text"], "top_matches": top}


MISSING_MUST_PENALTY = 15
YEARS_SHORT_PENALTY = 10
YEARS_SHORT_PER_YEAR = 5


def candidate_id(resume_path: str) -> str:
    """resumes/resume_meera_krishnan.txt -> meera_krishnan"""
    stem = Path(resume_path).stem
    return stem[len("resume_"):] if stem.startswith("resume_") else stem


def requirements_query(req: dict) -> str:
    """Text that gets embedded for semantic search."""
    parts = []
    if req.get("title"):
        parts.append(req["title"])
    if req.get("must_have"):
        parts.append("Must have: " + ", ".join(req["must_have"]))
    if req.get("nice_to_have"):
        parts.append("Nice to have: " + ", ".join(req["nice_to_have"]))
    if req.get("min_years"):
        parts.append("{0}+ years of experience".format(req["min_years"]))
    if req.get("description"):
        parts.append(req["description"])
    return "\n".join(parts)


def skill_vocabulary(persist_dir: str = "chroma_db") -> list:
    """Every skill listed on any indexed resume, in its original casing."""
    payload = get_collection(persist_dir).get(include=["metadatas"])
    vocab = []
    seen = set()
    for meta in payload.get("metadatas") or []:
        for skill in meta["skills"].split(","):
            skill = skill.strip()
            if skill and skill.lower() not in seen:
                seen.add(skill.lower())
                vocab.append(skill)
    return vocab


def score_breakdown(meta: dict, similarity: float, req: dict, strict: bool = False) -> dict:
    """Score one resume against requirements and say where every point came from.

    Base score is 60% calibrated similarity, 25% share of the job's skills
    (must-have plus nice-to-have), 15% years versus the minimum. Outside
    strict mode, each missing must-have costs 15 points and being below the
    years bar costs 10 plus 5 per missing year, so near misses still rank
    instead of vanishing.
    """
    have = skill_set(meta["skills"])
    must = list(req.get("must_have") or [])
    nice = [s for s in (req.get("nice_to_have") or []) if s.lower() not in {m.lower() for m in must}]
    job_skills = must + nice
    matched_must = [s for s in must if s.lower() in have]
    missing_must = [s for s in must if s.lower() not in have]
    matched_nice = [s for s in nice if s.lower() in have]
    missing_nice = [s for s in nice if s.lower() not in have]
    matched_count = len(matched_must) + len(matched_nice)
    skill_frac = matched_count / float(len(job_skills)) if job_skills else 1.0

    years = int(meta["experience_years"])
    min_years = int(req.get("min_years") or 0)
    years_frac = min(1.0, years / float(min_years)) if min_years else 1.0

    similarity_points = 60.0 * _calibrated_similarity(similarity)
    skill_points = 25.0 * skill_frac
    years_points = 15.0 * years_frac
    penalty = 0.0
    if not strict:
        penalty += MISSING_MUST_PENALTY * len(missing_must)
        if min_years and years < min_years:
            penalty += YEARS_SHORT_PENALTY + YEARS_SHORT_PER_YEAR * (min_years - years)
    raw = similarity_points + skill_points + years_points - penalty
    return {
        "score": max(0, min(100, int(round(raw)))),
        "similarity": round(similarity, 3),
        "similarity_points": round(similarity_points, 1),
        "skill_points": round(skill_points, 1),
        "years_points": round(years_points, 1),
        "penalty": round(penalty, 1),
        "matched_must": matched_must,
        "missing_must": missing_must,
        "matched_nice": matched_nice,
        "missing_nice": missing_nice,
        "years": years,
        "min_years": min_years,
        "meets_years": (years >= min_years) if min_years else True,
        "meets_must_haves": not missing_must and ((years >= min_years) if min_years else True),
    }


def rank_resumes(req: dict, k: int = 10, strict: bool = False, persist_dir: str = "chroma_db") -> dict:
    """Score every indexed resume against structured requirements.

    req has title, must_have, nice_to_have, min_years, description.
    strict=True drops anyone missing a must-have or below the years bar,
    like match_job. Returns the pool size and the top k with breakdowns.
    """
    collection = get_collection(persist_dir)
    count = collection.count()
    if count == 0:
        raise SystemExit("The resume index is empty. Run python resume_rag.py first.")

    embedding = get_model().encode(requirements_query(req)).tolist()
    payload = collection.query(
        query_embeddings=[embedding],
        n_results=count,
        include=["documents", "metadatas", "distances"],
    )
    grouped = _group_hits(payload)

    ranked = []
    for path, slot in grouped.items():
        meta = slot["meta"]
        breakdown = score_breakdown(meta, max(0.0, float(slot["similarity"])), req, strict=strict)
        if strict and not breakdown["meets_must_haves"]:
            continue
        excerpts = [
            _excerpt(text)
            for _similarity, text in sorted(slot["excerpts"], key=lambda item: -item[0])[:2]
        ]
        ranked.append(
            {
                "candidate_id": candidate_id(path),
                "candidate_name": meta["candidate_name"],
                "resume_path": path,
                "match_score": breakdown["score"],
                "best_section": str(slot["section"]).title(),
                "skills": meta["skills"],
                "relevant_excerpts": excerpts,
                "breakdown": breakdown,
            }
        )

    ranked.sort(key=lambda item: (-item["match_score"], -item["breakdown"]["similarity"], item["candidate_name"]))
    for position, item in enumerate(ranked, start=1):
        item["rank"] = position
    return {"pool_size": len(grouped), "ranked": ranked[:k], "all": ranked}


def match_job_file(path, k: int = 10, hybrid: bool = True) -> dict:
    return match_job(parse_job_file(path), k=k, hybrid=hybrid)


def main() -> None:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    flags = {arg for arg in sys.argv[1:] if arg.startswith("--")}
    path = args[0] if args else "jobs/senior_python_backend.txt"
    result = match_job_file(path, hybrid="--semantic" not in flags)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
