# 2_natural_language_compare

## You

Find me candidates with React and 3+ years experience

<details><summary>Agent reasoning (10 steps)</summary>

- `parse_jd` Parse JD: read job description from the message (53 characters, free text).
- `extract_requirements` Extract Requirements: must-have React; nice-to-have none; minimum 3 years (via llm).
- `search_resumes` resume_search.rank_resumes 68.9 ms ok
- `search_resumes` Search Resumes (round 1): scored all 100 resumes with hybrid RAG; 10 meet every hard requirement; kept the top 10: Fiona Dsilva (77), Irfan Pathan (74), Rhea Banerjee (71), Sneha Kulkarni (70), Ishaan Kapoor (63), ...
- `rank_candidates` filesystem.batch_process 128.6 ms ok
- `rank_candidates` filesystem.batch_process 114.6 ms ok
- `rank_candidates` Rank Candidates (round 2): read 10 resumes in full, found 10 evidence lines, wrote strengths, gaps, and analyst notes (via LLM).
- `generate_report` filesystem.write_file 40.0 ms ok
- `generate_report` candidate_db.save_shortlist 13.5 ms ok
- `generate_report` Generate Report (round 3): 4 hire, 6 borderline, 0 no-hire; 0 ranking changes; saved reports/react_engineer_v1.md (filesystem.write_file) and shortlist #34 (candidate_db.save_shortlist).

</details>

## Agent

**React Engineer**: must-have React; nice-to-have none; 3+ years.
Screened 100 resumes, kept the top 10, and deep-reviewed them: **4 hire, 6 borderline, 0 no-hire**.

| # | Candidate | Score | Years | Verdict | Main gap |
|---|---|---|---|---|---|
| 1 | Fiona Dsilva | 77 | 7 | HIRE | - |
| 2 | Irfan Pathan | 74 | 5 | HIRE | - |
| 3 | Rhea Banerjee | 71 | 5 | HIRE | - |
| 4 | Sneha Kulkarni | 70 | 6 | HIRE | - |
| 5 | Ishaan Kapoor | 63 | 3 | BORDERLINE | fit score below the hire bar of 65 |
| 6 | Gopal Krishnan | 62 | 1 | BORDERLINE | 1 year of experience, 2 short of the 3-year minimum |
| 7 | Keshav Murthy | 60 | 4 | BORDERLINE | fit score below the hire bar of 65 |
| 8 | Lavanya Suresh | 59 | 8 | BORDERLINE | fit score below the hire bar of 65 |
| 9 | Elango Ramesh | 56 | 4 | BORDERLINE | fit score below the hire bar of 65 |
| 10 | Divya Sharma | 53 | 4 | BORDERLINE | fit score below the hire bar of 65 |

**Agent's take:** We should interview Fiona Dsilva first because she is the top‑scoring senior React engineer with proven migration leadership and testing expertise. Among the borderline pool, the most promising candidate is Ishaan Kapoor, who has no identified gaps.

Report saved to `reports/react_engineer_v1.md` and shortlist #34 recorded in the candidate database. Try: "Compare the top 3", "Why did Fiona rank higher than Irfan?", "How could Ishaan Kapoor improve?", "Make <skill> a must-have", "Watch for new resumes", "done".

## You

Compare the top 3 matches side by side

<details><summary>Agent reasoning (5 steps)</summary>

- `human_feedback` Human Feedback: "Compare the top 3 matches side by side" -> intent compare.
- `agent` Agent: calling compare_candidates({"candidate_ids": ["fiona_dsilva", "irfan_pathan", "rhea_banerjee"]}).
- `tools` resume_search.rank_resumes 79.7 ms ok
- `tools` compare_candidates -> { "requirements": { "must_have": [ "React" ], "nice_to_have": [], "min_years": 3 }, "candidates": [ { "candidate_id": "fiona_dsilva", "name": "Fiona Dsilva", "rank": 1, "score": 77, "score_parts": { "similarity": 37.5, "skills": 25.0, "years": 15.0, "penalty": -0.0 }, "years": 7, "matched_must_have"
- `agent` Agent: answered after 1 tool round(s).

</details>

## Agent

### Side‑by‑Side Comparison – Top 3 React Engineers  

| Feature | **Fiona Dsilva** (#1) | **Irfan Pathan** (#2) | **Rhea Banerjee** (#3) |
|---|---|---|---|
| **Score** | 77 | 74 | 71 |
| **Score breakdown** | Similarity 37.5 · Skills 25 · Years 15 · Penalty 0 | Similarity 34.2 · Skills 25 · Years 15 · Penalty 0 | Similarity 30.6 · Skills 25 · Years 15 · Penalty 0 |
| **Years of React experience** | 7 y (4 y above min) | 5 y (2 y above min) | 5 y (2 y above min) |
| **Must‑have match** | ✅ React (all) | ✅ React (all) | ✅ React (all) |
| **Nice‑to‑have match** | – (none required) | – | – |
| **Key strengths** | • Full‑stack (Next.js, GraphQL, Jest) <br>• Led a Next.js migration <br>• Highest similarity to JD (66 %) | • End‑to‑end React + Node.js delivery <br>• PostgreSQL experience (full‑stack) <br>• Strong similarity (64 %) | • Dashboard migration to React/TS <br>• GraphQL integration <br>• Good JD similarity (62 %) |
| **Gaps / missing items** | None identified | No explicit Next.js or testing tools mentioned | No testing frameworks or performance‑optimisation details |
| **Verdict** | **Hire** | **Hire** | **Hire** |
| **Why ranked higher?** | Highest similarity score (most wording overlap) and longest experience, giving the biggest boost in the “years” component. | Slightly lower similarity and 2 y less experience than Fiona, resulting in a 3‑point drop. | Lowest similarity of the three and same experience as Irfan, so its score is 3 points lower. |

**Takeaway:** All three meet the React + 3‑year requirement and are recommended for hire. Fiona leads due to the strongest resume‑JD match and the most experience, while Irfan and Rhea are very close alternatives with minor gaps (lack of explicit testing/Next.js details).
