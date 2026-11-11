# munichScraper brief 7 (15 Oct 2026): more Munich reach, especially start-ups

## What the database shows today (jobs.db, read-only audit by the Apply Munich pipeline)
- 83,280 rows; only 2,448 pass every gate. 54,051 are rejected for location (fine: big ATS boards are global).
- Munich-located: 7,844 rows from 1,638 companies, of which 729 pass.
- Munich rows lost by gate: account-only 1,607 (almost all XING/StepStone re-posts, OK), german 1,336 (OK), **age 1,243**, seniority 978, off-function 810.
- **459 Munich postings are rejected only because they are 22-60 days old, yet still open** (closed_at is NULL). Many start-ups keep a role open for 1-3 months. Example: "Founding Customer Support Engineer", 63 days, still live.
- Start-up sources are thin: Workable 30 Munich rows, Teamtailor 19, Wellfound 19, JOIN 0 (join.py exists but produces nothing). Personio is strong (1,076).
- Some start-up-typical titles are blocked as "seniority": "Chief of Staff to the CEO", "Founding ...", "Founder Associate".

## Changes, in this order

### 1. Age gate: freshness ranks, liveness decides (biggest quick win)
- Keep rejecting postings older than 21 days ONLY if `liveness.py` cannot confirm they are still open.
- Postings 22-60 days old that liveness confirms are open: accept, with a score penalty (e.g. -1 point per week over 21 days) so fresh roles still come first.
- Over 60 days: reject as now.
- Re-check liveness for these rows at least every 48 hours; set `closed_at` when the page 404s/410s or says the role is closed.

### 2. Start-up discovery (new seed lists -> prober -> ATS boards)
Goal: find Munich start-ups and scale-ups, detect their ATS with `prober.py`, and add the slugs to `data/discovered_boards.json` so the normal ATS scrapers pick them up every run.
Seed sources (verify each URL before coding against it; a 404 means a wrong URL, not "no jobs"):
- **Munich start-up ecosystem lists:** Munich Startup (munich-startup.de: start-up database and job board), UnternehmerTUM / XPRENEURS / TUM Venture Labs portfolio pages, LMU Entrepreneurship Center, Werk1, Bayern Innovativ / BayStartUP.
- **VC portfolio job boards** of investors active in Munich (many run a Getro or Consider board listing every portfolio company's open roles): e.g. UVC Partners, HV Capital, Picus Capital, Acton Capital, 42CAP, Speedinvest, Earlybird, Cherry Ventures, Project A, La Famiglia, btov, Visionaries Club, High-Tech Gruenderfonds. Parse the board, keep only Munich / Germany-remote / EU-remote roles, and resolve the employer's own apply URL.
- **Y Combinator**: YC company directory filtered to Munich/Germany, and Work at a Startup (workatastartup.com) roles that allow Germany/EU.
- **Wellfound**: Munich location search (expand beyond the current 19 rows), employer apply link resolved as in brief 6b.
- Every new company found goes through `prober.py` (greenhouse, ashby, lever, personio, recruitee, workable, teamtailor, pinpoint, bamboohr, join, breezy). Log new boards found per run.

### 3. Start-up tag
- Add columns `is_startup` (0/1) and `startup_source` (which seed list or VC board). Mark 1 when the company came from a start-up list or VC board, or the posting says seed/Series A-C/"start-up"/"scale-up", or headcount on the ATS page is under 500.
- The Apply Munich pipeline reads this column for reporting (start-ups get equal weight to big companies, Mattia 15 Oct).

### 4. Seniority gate: allow start-up generalist titles
- Allow: "Chief of Staff", "Founder Associate", "Founder's Associate", "Founding" + a non-engineering function (Founding Customer Success / Operations / Solutions / Implementation / Account Manager), "Business Operations", "BizOps", "Generalist", "Strategy & Operations".
- Keep blocking: Head of, Director, VP, Principal, Staff (engineering), people-management Lead, intern/working student/Praktikum/Werkstudent, Founding Engineer (pure software engineering).

### 5. JOIN (join.com)
- Make `join.py` produce rows again (Munich start-ups use JOIN heavily). Mattia is creating a JOIN candidate account (15 Oct 2026), so add a config flag `join_account_ready = True`: JOIN rows are normal candidates (not `account-only`), with `apply_kind = 'join-account'` so the pipeline knows the form needs the signed-in session.

### 6. Report and tests
- Print, before/after `--rescore`: passed per source, Munich passed, Munich start-up passed (`is_startup=1`), rows rescued by the age change, new boards found by the prober.
- Tests: a 40-day-old live Ashby posting passes with a penalty; a 40-day-old 404 posting is rejected and gets `closed_at`; "Chief of Staff to the CEO" passes the seniority gate; "Head of Sales" still fails; a VC-board role with a resolved Personio apply URL passes with `is_startup=1`; a VC-board role without an employer link goes to manual_leads only.

## Do not change
- German gate, skip list, aggregator/staffing blocks (brief 6b), the `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL` contract the pipeline reads.
