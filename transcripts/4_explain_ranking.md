# 4_explain_ranking

## You

jobs/data_engineer.txt

<details><summary>Agent reasoning (11 steps)</summary>

- `parse_jd` filesystem.read_file 8.3 ms ok
- `parse_jd` Parse JD: read job description from jobs/data_engineer.txt via filesystem.read_file (304 characters, structured header).
- `extract_requirements` Extract Requirements: must-have Python, Spark, SQL; nice-to-have Airflow; minimum 3 years (via llm).
- `search_resumes` resume_search.rank_resumes 94.0 ms ok
- `search_resumes` Search Resumes (round 1): scored all 100 resumes with hybrid RAG; 9 meet every hard requirement; kept the top 10: Rahul Iyer (88), Chetan Bhat (86), Aisha Khan (86), Waseem Akram (86), Zubin Mistry (79), ...
- `rank_candidates` filesystem.batch_process 116.5 ms ok
- `rank_candidates` filesystem.batch_process 145.8 ms ok
- `rank_candidates` Rank Candidates (round 2): read 10 resumes in full, found 34 evidence lines, wrote strengths, gaps, and analyst notes (via LLM).
- `generate_report` filesystem.write_file 9.2 ms ok
- `generate_report` candidate_db.save_shortlist 21.4 ms ok
- `generate_report` Generate Report (round 3): 8 hire, 1 borderline, 1 no-hire; 0 ranking changes; saved reports/data_engineer_v1.md (filesystem.write_file) and shortlist #38 (candidate_db.save_shortlist).

</details>

## Agent

**Data Engineer**: must-have Python, Spark, SQL; nice-to-have Airflow; 3+ years.
Screened 100 resumes, kept the top 10, and deep-reviewed them: **8 hire, 1 borderline, 1 no-hire**.

| # | Candidate | Score | Years | Verdict | Main gap |
|---|---|---|---|---|---|
| 1 | Rahul Iyer | 88 | 6 | HIRE | - |
| 2 | Chetan Bhat | 86 | 6 | HIRE | No evidence of nice-to-have: Airflow |
| 3 | Aisha Khan | 86 | 7 | HIRE | - |
| 4 | Waseem Akram | 86 | 5 | HIRE | - |
| 5 | Zubin Mistry | 79 | 9 | HIRE | No evidence of nice-to-have: Airflow |
| 6 | Tarun Malhotra | 78 | 3 | HIRE | No evidence of nice-to-have: Airflow |
| 7 | Neelam Choudhary | 74 | 5 | HIRE | - |
| 8 | Joseph Mathew | 67 | 4 | HIRE | No evidence of nice-to-have: Airflow |
| 9 | Deepika Rawat | 64 | 3 | BORDERLINE | fit score below the hire bar of 65 |
| 10 | Yamini Krishna | 45 | 2 | NO-HIRE | Missing must-have skill: Spark |

**Agent's take:** We should interview Rahul Iyer first (verdict: hire) because he has the highest score of 88, meets all must‑have skills and includes Airflow experience. The most promising borderline candidate is Deepika Rawat, whose main gap is no evidence of nice‑to‑have: Airflow.

Report saved to `reports/data_engineer_v1.md` and shortlist #38 recorded in the candidate database. Try: "Compare the top 3", "Why did Rahul rank higher than Chetan?", "How could Deepika Rawat improve?", "Make <skill> a must-have", "Watch for new resumes", "done".

## You

Why did Rahul Iyer rank higher than Chetan Bhat?

<details><summary>Agent reasoning (4 steps)</summary>

- `human_feedback` Human Feedback: "Why did Rahul Iyer rank higher than Chetan Bhat?" -> intent explain.
- `agent` Agent: calling explain_ranking({"higher_candidate": "Rahul Iyer", "lower_candidate": "Chetan Bhat"}).
- `tools` explain_ranking -> { "higher": { "name": "Rahul Iyer", "rank": 1, "score": 88, "breakdown": { "score": 88, "similarity": 0.731, "similarity_points": 48.2, "skill_points": 25.0, "years_points": 15.0, "penalty": 0.0, "matched_must": [ "Python", "Spark", "SQL" ], "missing_must": [], "matched_nice": [ "Airflow" ], "missin
- `agent` Agent: answered after 1 tool round(s).

</details>

## Agent

### Why Rahul Iyer ranks higher than Chetan Bhat  

| Factor | Rahul Iyer | Chetan Bhat | Impact on ranking |
|--------|------------|-------------|-------------------|
| **Overall score** | **88** | **86** | Rahul’s total is 2 points higher. |
| **Must‑have skills (Python, Spark, SQL)** | All 3 matched → **25 pts** | All 3 matched → **25 pts** | No difference. |
| **Nice‑to‑have skill (Airflow)** | Present → **no penalty** (adds 0 pts but avoids the –10 pts penalty) | **Missing** → **‑10 pts** penalty applied. |
| **Years of experience** | 6 y (≥ 3) → **15 pts** | 6 y (≥ 3) → **15 pts** | No difference. |
| **Semantic similarity to the JD** | 0.731 → **48.2 pts** | 0.752 → **51.9 pts** | Chetan scores slightly higher on similarity, but the Airflow penalty outweighs this (‑10 pts). |
| **Penalty for missing must‑haves** | None | None | No impact. |

#### Bottom‑line explanation
- Both candidates meet all **must‑have** requirements and have the same years of experience, so those components are equal.
- **Airflow** is a **nice‑to‑have** skill. Rahul lists it, so he avoids the **‑10 pts** penalty that Chetan incurs for lacking it.
- Although Chetan’s resume is a bit more semantically similar to the job description, the Airflow penalty more than offsets that advantage, resulting in Rahul’s **2‑point** lead (88 vs 86).  

Thus, Rahul Iyer ranks higher primarily because he satisfies the nice‑to‑have Airflow requirement, while Chetan Bhat does not.

## You

Why is Yamini Krishna not a hire? What are the gaps?

<details><summary>Agent reasoning (4 steps)</summary>

- `human_feedback` Human Feedback: "Why is Yamini Krishna not a hire? What are the gaps?" -> intent candidate_detail.
- `agent` Agent: calling get_candidate_report({"candidate_id": "yamini_krishna"}).
- `tools` get_candidate_report -> { "candidate_name": "Yamini Krishna", "resume_path": "resumes/resume_yamini_krishna.docx", "rank": 10, "match_score": 45, "breakdown": { "score": 45, "similarity": 0.723, "similarity_points": 46.7, "skill_points": 18.8, "years_points": 10.0, "penalty": 30.0, "matched_must": [ "Python", "SQL" ], "mis
- `agent` Agent: answered after 1 tool round(s).

</details>

## Agent

### Why Yamini Krishna is a **no‑hire**

| Aspect | What the resume shows | Requirement | Gap |
|--------|----------------------|-------------|-----|
| **Must‑have skills** | Python, SQL (present) | **Spark** (must‑have) | **Missing** – no evidence of Spark experience. |
| **Years of experience** | 2 years | Minimum 3 years | **Below threshold** – 1 year short. |
| **Nice‑to‑have skill** | Airflow (present) | Airflow | Satisfied, but it cannot compensate for missing must‑haves. |
| **Overall match score** | 45 / 100 | Hire‑threshold ≈ 65 | Far below the hiring bar. |
| **Penalty applied** | –30 pts | Missing Spark & insufficient years | Large penalty drops the score. |

#### Summary of gaps
1. **Missing Spark** – a core, non‑negotiable skill for the Data Engineer role.  
2. **Insufficient experience** – only 2 years, while the role requires at least 3 years.  

Because both hard requirements are unmet, the system recommends **no‑hire** despite her Airflow experience and good resume similarity.  

**Improvement suggestions** (if you want to keep her in the pipeline for a future role):
- Add a Spark project (e.g., a batch processing job, certification, or open‑source contribution).  
- Gain at least one more year of relevant data‑engineering work.  

Until those gaps are closed, Yamini Krishna does not qualify for the current Data Engineer opening.
