# REPORT_BRIEF_13.md

## Overview & Execution Summary
Brief 13 was implemented across all 6 sections:
1. **Unified Title Rules (`role-rules` gate)**: Loaded `role_rules.json` dynamically at cycle start; positioned right after Gate 2 (Seniority) for every source; allowed Developer / Backend Developer / AI Developer tracks; removed broad startup generalist titles; added `years_block` into Gate 4c; fixed deduplication canonical unmasking bug; re-gated historical passing cohort.
2. **Stop Re-downloading Known Data**: Greenhouse lists jobs without `content=true`, fetching detail endpoints only for new/updated IDs passing location and role-rules (56,815 listed, only 96 details fetched); skipped parsing/storing descriptions for failing rows in Ashby, Lever, Personio, Workable, Recruitee, SmartRecruiters; added per-board cadence (active boards every cycle, inactive polled daily / 24h); set cycle pause interval to 10 min.
3. **Autostart & Resilience**: Created `install_autostart.ps1` and `uninstall_autostart.ps1` targeting Windows Task Scheduler `MunichScraper` (not executed); added crash backoff in supervisor (3 crashes within 1h triggers a 15-minute pause).
4. **Negative Keywords**: Added 13 keywords (`Construction`, `Bauarbeiter`, `Site Manager`, `Electrician`, `Mechanic`, `Dental`, `Recruiter`, `Recruitment Consultant`, `Procurement`, `Purchasing`, `Thermal`, `Propulsion`, `Powertrain`) as a dedicated whole-word gate right after `role-rules`.
5. **Database Housekeeping**: Added `PRAGMA wal_checkpoint(TRUNCATE)` after each cycle; daily prune archiving 30d+ rejected rows to `seen_keys` table; weekly `VACUUM`; strictly preserved passing, applied, and exported rows.
6. **Firecrawl Governance**: Constrained credit spend strictly to postings passing location and role-rules; budget guard halting crawls when remaining credits fall below 50 without failing cycle.

---

## 1. Test Suite Results
- **Total Tests Passing**: 183 passed in 103.96s (0 failures).
- **New Tests Added**:
  - `tests/test_brief_13_s1.py`: Role rules title fits, seniority rejections, precedence over off-function, years_block, negative keywords gate, Arbeitnow sales director rejection, canonical unmasking protection.
  - `tests/test_brief_13_s2.py`: Greenhouse list without `content=true`, detail fetch skipping for failing postings, cache reuse when `updated_at` matches, board cadence filtering.
  - `tests/test_brief_13_s3.py`: Autostart scripts existence and configuration, supervisor crash backoff.
  - `tests/test_brief_13_s5.py`: `checkpoint_wal` truncate, daily prune to `seen_keys`, prevention of re-ingesting pruned rows via `seen_keys`.
  - `tests/test_brief_13_s6.py`: Firecrawl credit threshold stop below 50, candidate filtering by location and role-rules.

---

## 2. Re-Gating & Role Rules Fit
- **Passing rows before re-gating**: 6,730 rows
  - *Note*: An issue in `deduplicate_all_jobs` previously unmasked rows by resetting `gate_rejected = None` on duplicate rows.
  - Fitting `role_rules.json` before re-gating: 1,024 / 6,730 (15.2%)
- **Rows rejected during re-gating**: 6,633 rows
  - Location: 6,020
  - Role-rules: 404
  - Seniority: 123
  - Age: 20
  - Cross-source duplicate: 19
  - Skip-list: 13
  - No employer link: 11
  - German: 9
  - Years required: 7
  - German required: 6
  - Off-function: 1
- **Passing rows after re-gating**: 97 rows (100.0% fit `role_rules.json`)
- **Passing rows after full cycle**: 157 rows
- **Share of passing rows fitting `role_rules.json`**: **157 / 157 (100.0%)** (Target: >90%)

---

## 3. Database Housekeeping & Size
- **DB Size Before Housekeeping**: 284,483,584 bytes (271.30 MB)
- **WAL Size Before Cycle**: 0 bytes
- **Pruned Rows Moved to `seen_keys`**: 44,052 stale (>30 days) rejected rows
- **DB Size After Cycle & WAL Checkpoint**: 222,474,240 bytes (212.17 MB)
- **WAL Size After Truncate**: 0 bytes
- **Net Size Reduction**: **-62,009,344 bytes (-59.13 MB, -21.8%)**
- **Total Rows in `jobs`**: 170,401 -> 126,549 rows

---

## 4. Single Cycle Execution Metrics (`python main.py --run-once`)
- **Total Cycle Time**: 26m 47s (1,607 seconds)
- **Total Postings Discovered**: 122,843
- **Total Passed in Cycle**: 131

### Per-Source Breakdown
| Source | Returned / Discovered | Listed | Details Fetched | Passed All Gates | Time Taken |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **amazon_jobs** | 100 | 100 | 0 | 1 | 1.2s |
| **arbeitsagentur** | 105 | 105 | 0 | 4 | 11.8s |
| **ashby** | 23,205 | 23,205 | 0 | 37 | 50.6s |
| **bamboohr** | 5 | 5 | 0 | 0 | 0.9s |
| **breezy** | 25 | 25 | 0 | 0 | 0.7s |
| **englishjobsde** | 18 | 18 | 0 | 0 | 6.3s |
| **englishjobsingermany** | 20 | 20 | 0 | 0 | 3.2s |
| **eures** | 0 | 0 | 0 | 0 | 12.8s |
| **firecrawl** | 0 | 0 | 0 | 0 | 1.3s |
| **germantechjobs** | 400 | 400 | 0 | 3 | 1.0s |
| **greenhouse** | 56,808 | 56,815 | 96 | 32 | 171.3s |
| **himalayas** | 0 | 0 | 0 | 0 | 0.6s |
| **jobicy** | 50 | 50 | 0 | 1 | 1.0s |
| **join** | 679 | 679 | 0 | 0 | 6.2s |
| **lever** | 13,281 | 13,281 | 0 | 6 | 92.5s |
| **munichstartup** | 15 | 15 | 0 | 1 | 11.1s |
| **personio** | 8,842 | 8,847 | 0 | 14 | 25.0s |
| **pinpoint** | 244 | 244 | 0 | 1 | 1.2s |
| **recruitee** | 1,120 | 1,120 | 0 | 0 | 7.0s |
| **remoteok** | 56 | 56 | 0 | 5 | 1.4s |
| **remotive** | 10 | 10 | 0 | 2 | 0.7s |
| **smartrecruiters** | 2,406 | 2,406 | 0 | 12 | 5.4s |
| **softgarden** | 3,106 | 3,106 | 0 | 1 | 2.9s |
| **teamtailor** | 476 | 476 | 0 | 0 | 12.8s |
| **vc** | 0 | 0 | 0 | 0 | 15.6s |
| **wellfound** | 42 | 42 | 0 | 0 | 0.7s |
| **weworkremotely** | 206 | 206 | 0 | 5 | 6.0s |
| **workable** | 11,582 | 11,582 | 0 | 6 | 28.6s |
| **workday** | 42 | 42 | 0 | 1 | 27.9s |
| **workingnomads** | 0 | 0 | 0 | 0 | 1.1s |
| **Total** | **122,843** | **122,852** | **96** | **131** | **~500s scraping** |

---

## 5. Gate Rejections Summary
- **Gate 1 (Duplicate / History)**: 136
- **Gate 2 (Location: Munich / DE-remote / EU-remote)**: 102,912
- **Gate 2a (Role Rules: role_rules.json contract)**: 5,925
- **Gate 2b (Negative Keywords: config.py whole-word)**: 7
- **Gate 3 (Seniority: Director / Head / Lead / Intern)**: 1,713
- **Gate 4 (German: DACH in title / German text)**: 196
- **Gate 4a (German Required: English posting requires DE)**: 87
- **Gate 4b (Language: FR/SV/NL/ES/IT/PL text)**: 4
- **Gate 4c (Years Required: 6+ yrs or Senior 5+ yrs)**: 112
- **Gate 4d (Contract: fixed-term / temp / part-time / intern)**: 23
- **Gate 4e (Deep Stack: K8s / Terraform / ABAP / C++ / kernel)**: 22
- **Gate 4f (Clearance: SÜG / SC / DV / German citizenship)**: 6
- **Gate 5 (Skip-list: staffing / consultancy)**: 167
- **Gate 7 (Age: > 60 days or > 14d closed)**: 114
- **Gate 9 (No employer link)**: 29
- **Gate 10 (No description / < 300 chars)**: 10
- **Gate 11 (Cross-source duplicate)**: 1,900
- **Intake Triple / Cap Filter**: 11,256
- **Total Passing Cycle Candidates**: 131 (25 exported to `exports/candidates_latest.csv`)

---

## 6. Autostart and Daemon Status
- `install_autostart.ps1` and `uninstall_autostart.ps1` are present and verified.
- The scraper daemon and supervisor remain **stopped**, as instructed.

