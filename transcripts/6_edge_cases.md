# 6_edge_cases

## You

Find me candidates with Rust and Haskell and 10+ years experience

<details><summary>Agent reasoning (11 steps)</summary>

- `parse_jd` Parse JD: read job description from the message (65 characters, free text).
- `extract_requirements` resume_search.skill_vocabulary 1083.4 ms ok
- `extract_requirements` Extract Requirements: must-have Rust, Haskell; nice-to-have none; minimum 10 years (via llm).
- `search_resumes` resume_search.rank_resumes 122.0 ms ok
- `search_resumes` Search Resumes (round 1): scored all 100 resumes with hybrid RAG; 0 meet every hard requirement; kept the top 10: Chetan Bhat (0), Zubin Mistry (0), Irfan Pathan (0), Gopal Krishnan (0), Girish Nair (0), ...
- `rank_candidates` filesystem.batch_process 244.4 ms ok
- `rank_candidates` filesystem.batch_process 28.0 ms ok
- `rank_candidates` Rank Candidates (round 2): read 10 resumes in full, found 0 evidence lines, wrote strengths, gaps, and analyst notes (via LLM).
- `generate_report` filesystem.write_file 5.4 ms ok
- `generate_report` candidate_db.save_shortlist 9.3 ms ok
- `generate_report` Generate Report (round 3): 0 hire, 0 borderline, 10 no-hire; 0 ranking changes; saved reports/software_engineer_v1.md (filesystem.write_file) and shortlist #41 (candidate_db.save_shortlist).

</details>

## Agent

**Software Engineer**: must-have Rust, Haskell; nice-to-have none; 10+ years.
Screened 100 resumes, kept the top 10, and deep-reviewed them: **0 hire, 0 borderline, 10 no-hire**.

| # | Candidate | Score | Years | Verdict | Main gap |
|---|---|---|---|---|---|
| 1 | Chetan Bhat | 0 | 6 | NO-HIRE | Missing must-have skill: Rust |
| 2 | Zubin Mistry | 0 | 9 | NO-HIRE | Missing must-have skill: Rust |
| 3 | Irfan Pathan | 0 | 5 | NO-HIRE | Missing must-have skill: Rust |
| 4 | Gopal Krishnan | 0 | 1 | NO-HIRE | Missing must-have skill: Rust |
| 5 | Girish Nair | 0 | 6 | NO-HIRE | Missing must-have skill: Rust |
| 6 | Neelam Choudhary | 0 | 5 | NO-HIRE | Missing must-have skill: Rust |
| 7 | Devendra Singh | 0 | 7 | NO-HIRE | Missing must-have skill: Rust |
| 8 | Abhishek Tiwari | 0 | 4 | NO-HIRE | Missing must-have skill: Rust |
| 9 | Siddharth Jain | 0 | 5 | NO-HIRE | Missing must-have skill: Rust |
| 10 | Vivek Nambiar | 0 | 7 | NO-HIRE | Missing must-have skill: Rust |

**Agent's take:** We should not move forward with any of the top candidates, as each has a no-hire verdict due to missing the required Rust and Haskell expertise. Since none meet the seniority or mandatory skill criteria, there is no suitable first interview. There are no borderline cases.

Report saved to `reports/software_engineer_v1.md` and shortlist #41 recorded in the candidate database. Try: "Compare the top 3", "Why did Chetan rank higher than Zubin?", "Make <skill> a must-have", "Watch for new resumes", "done".

## You

Compare John Doe and Jane Smith

<details><summary>Agent reasoning (9 steps)</summary>

- `human_feedback` Human Feedback: "Compare John Doe and Jane Smith" -> intent compare.
- `agent` Agent: forced compare_candidates failed (Error code: 400 - Tool call validation failed: tool call validation failed: attempted to call tool 'get_candidate_histor); retrying.
- `agent` Agent: calling semantic_search({"k": 5, "query": "John Doe"}).
- `tools` resume_search.semantic_search 62.9 ms ok
- `tools` semantic_search -> {"query": "John Doe", "hits": [{"candidate_id": "joseph_mathew", "candidate_name": "Joseph Mathew", "resume_path": "resumes/resume_joseph_mathew.txt", "similarity": 0.323, "section": "header", "excerpt": "Joseph Mathew Data Engineer | joseph.mathew@email.com", "years": 4, "skills": "Python, Spark, K
- `agent` Agent: calling compare_candidates({"candidate_ids": ["John Doe", "Jane Smith"]}).
- `tools` resume_search.rank_resumes 40.8 ms ok
- `tools` compare_candidates -> { "error": "No matching candidates", "unknown": [ "John Doe", "Jane Smith" ] }
- `agent` Agent: answered after 2 tool round(s).

</details>

## Agent

I’m sorry—I can’t find any candidates named **John Doe** or **Jane Smith** in the current shortlists or candidate database.  

If you’d like to compare specific candidates, please provide their exact IDs (e.g., `chetan_bhat`, `zubin_mistry`) or full names as they appear in the system. Alternatively, let me know if you’d like to adjust the job requirements or run a new search for other candidates.

## You

Drop Rust and Haskell, require Golang with 4+ years instead

<details><summary>Agent reasoning (10 steps)</summary>

- `human_feedback` Human Feedback: "Drop Rust and Haskell, require Golang with 4+ years instead" -> intent refine.
- `extract_requirements` Extract Requirements: applied "Drop Rust and Haskell, require Golang with 4+ years instead" -> Golang is now a must-have; Rust is no longer a must-have; Haskell is no longer a must-have; minimum years changed from 10 to 4 (via llm).
- `search_resumes` resume_search.rank_resumes 37.1 ms ok
- `search_resumes` Search Resumes (round 1): scored all 100 resumes with hybrid RAG; 2 meet every hard requirement; kept the top 10: Jaya Prakash (55), Pranav Hegde (50), Gopal Krishnan (0), Nagaraj (0), Charu Goel (0), ...
- `rank_candidates` filesystem.batch_process 211.6 ms ok
- `rank_candidates` filesystem.batch_process 45.4 ms ok
- `rank_candidates` Rank Candidates (round 2): read 10 resumes in full, found 2 evidence lines, wrote strengths, gaps, and analyst notes (via LLM).
- `generate_report` filesystem.write_file 5.2 ms ok
- `generate_report` candidate_db.save_shortlist 14.7 ms ok
- `generate_report` Generate Report (round 3): 0 hire, 2 borderline, 8 no-hire; 17 ranking changes; saved reports/software_engineer_v2.md (filesystem.write_file) and shortlist #42 (candidate_db.save_shortlist).

</details>

## Agent

**Software Engineer**: must-have Golang; nice-to-have none; 4+ years.
Screened 100 resumes, kept the top 10, and deep-reviewed them: **0 hire, 2 borderline, 8 no-hire**.

**Requirement change:** Golang is now a must-have; Rust is no longer a must-have; Haskell is no longer a must-have; minimum years changed from 10 to 4.

**How the ranking moved:**
- Jaya Prakash entered the top 10 at #1 (score 0 -> 55): Rust is no longer required; Haskell is no longer required; has newly listed skill Golang; now meets the 4-year minimum
- Pranav Hegde entered the top 10 at #2 (score 0 -> 50): Rust is no longer required; Haskell is no longer required; has newly listed skill Golang; now meets the 4-year minimum
- Gopal Krishnan moved up from #4 to #3 (score 0 -> 0): does not have new must-have Golang; Rust is no longer required; Haskell is no longer required
- Nagaraj entered the top 10 at #4 (score 0 -> 0): does not have new must-have Golang; Rust is no longer required; Haskell is no longer required; now meets the 4-year minimum
- Charu Goel entered the top 10 at #5 (score 0 -> 0): does not have new must-have Golang; Rust is no longer required; Haskell is no longer required; now meets the 4-year minimum
- Harish Gupta entered the top 10 at #6 (score 0 -> 0): does not have new must-have Golang; Rust is no longer required; Haskell is no longer required
- Yamini Krishna entered the top 10 at #8 (score 0 -> 0): does not have new must-have Golang; Rust is no longer required; Haskell is no longer required
- Bharat Varma entered the top 10 at #9 (score 0 -> 0): does not have new must-have Golang; Rust is no longer required; Haskell is no longer required; now meets the 4-year minimum

| # | Candidate | Score | Years | Verdict | Main gap |
|---|---|---|---|---|---|
| 1 | Jaya Prakash | 55 | 5 | BORDERLINE | fit score below the hire bar of 65 |
| 2 | Pranav Hegde | 50 | 9 | BORDERLINE | fit score below the hire bar of 65 |
| 3 | Gopal Krishnan | 0 | 1 | NO-HIRE | Missing must-have skill: Golang |
| 4 | Nagaraj | 0 | 4 | NO-HIRE | Missing must-have skill: Golang |
| 5 | Charu Goel | 0 | 5 | NO-HIRE | Missing must-have skill: Golang |
| 6 | Harish Gupta | 0 | 11 | NO-HIRE | Missing must-have skill: Golang |
| 7 | Devendra Singh | 0 | 7 | NO-HIRE | Missing must-have skill: Golang |
| 8 | Yamini Krishna | 0 | 2 | NO-HIRE | Missing must-have skill: Golang |
| 9 | Bharat Varma | 0 | 4 | NO-HIRE | Missing must-have skill: Golang |
| 10 | Esha Gupta | 0 | 10 | NO-HIRE | Missing must-have skill: Golang |

**Agent's take:** Interview Jaya Prakash first, as he has the highest score and a solid Golang backend background despite limited breadth and leadership exposure. The most promising borderline candidate is Jaya Prakash, and he has no identified gap.

Report saved to `reports/software_engineer_v2.md` and shortlist #42 recorded in the candidate database. Try: "Compare the top 3", "Why did Jaya rank higher than Pranav?", "How could Jaya Prakash improve?", "Make <skill> a must-have", "Watch for new resumes", "done".
