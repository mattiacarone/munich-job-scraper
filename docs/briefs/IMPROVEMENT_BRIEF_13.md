# munichScraper brief 13 (4 Nov 2026): one set of title rules, stop re-downloading, never stay down, clean the database

## Evidence (jobs.db and daemon.log, 4 Nov 2026)
- 5,453 postings passed every gate since 21 Oct. Checked against the application pipeline's own rules, 4,115 (75%) are outside Mattia's role families, 594 (11%) have senior titles, and only 711 (13%) would be considered. Gate work, Firecrawl credits and pipeline reads are spent on the other 87%.
- The gates are not applied to every source. Passing rows by source: arbeitnow 3,225, recruitee 709, jobicy 288, greenhouse 270, xing 246, stepstone 238, englishjobsingermany 144, join 123, ashby 79. Examples that passed: arbeitnow "Sales Director, EMEA", "Account Executive (German Speaking)", "Senior Account Executive, DACH (German-speaking, Hamburg)", "Marketing Director - Project Manager"; englishjobsingermany "Director of Cloud infrastructure and security", "Product Lead - AI Investing App"; join "AI Tech Lead". check_gate_seniority (matcher.py:1039) blocks these titles when it runs, so some ingestion path writes rows with gate_rejected NULL without running Matcher's gate chain (matcher.py:1610 onwards). 201 Director and 182 Lead titles got through this way.
- Every cycle (about every 25 minutes) re-downloads every board in full: greenhouse 56,789 postings with content=true (scrapers/greenhouse.py:57), ashby 23,188 (about 5.5 minutes), lever 13,199, personio 8,845, workable 11,642. About 104,000 of the ~117,000 postings are then rejected by Gate 2 (location). The harvest takes about 13 minutes of each cycle.
- On 2-3 Nov the daemon and supervisor were down for about 25 hours (watchdog.log: "heartbeat 1509.5 min old"), because nothing starts supervisor.py after Windows starts.
- config.py negative_keywords (line 335) is defined but never read anywhere.
- jobs.db is 283 MB and jobs.db-wal 165 MB; the WAL is never checkpointed.
- Firecrawl: 442 credits left.

## Hard rules (unchanged from briefs 9-12)
Only public pages and public JSON endpoints. Respect robots.txt and rate limits. Never log in, create accounts, solve or work around a captcha, or use stealth or proxy modes. Never write keys to logs. Keep the existing tests passing and add a test for each change below. Do not touch anything in C:\Users\Mattia\Desktop\Job Application material except reading memory/role_rules.json.

## 1. One set of title rules, applied to every source
- The application pipeline now writes `C:\Users\Mattia\Desktop\Job Application material\memory\role_rules.json` at the start of every run. Fields (Python `re`-compatible, use re.I): `family`, `family_not`, `seniority`, `seniority_ok`, `years_block`, `german_title`.
- A title fits when `family` matches, `family_not` does not, and `seniority` does not match unless `seniority_ok` also matches. Example: "Associate Solutions Architect" fits, "Senior Software Engineer" and "Sales Director, EMEA" do not.
- Add this as a gate (code "role-rules") that runs right after Gate 2 for EVERY source, and use `years_block` on the description in Gate 4c. Read the file at the start of each cycle; if it is missing or invalid, log a warning and keep the existing gates (never pass everything).
- Find the ingestion path that writes rows without running the gate chain (arbeitnow, englishjobsingermany, join, jobicy, recruitee, xing, stepstone) and route it through `Matcher` like the ATS sources. Test: an arbeitnow row titled "Sales Director, EMEA" must end up with gate_rejected set.
- Re-gate the rows that are currently passing and still open, so the 4,700 wrong passes are cleared now, not only for new rows.
- The new role families include software engineer/developer, backend/Python/API developer and AI/LLM developer titles (Mattia, 4 Nov). The existing off-function gate (matcher.py:1454) and ENGINEERING_ROLE_PAT block those; the role-rules gate must take precedence for titles it accepts, while the deep-stack gate (4e) still blocks Java/C++/Go/Rust/embedded-centred postings.
- Remove the broad titles already deleted from config.py target_roles (Chief of Staff, Founder Associate, Generalist, Business Operations, BizOps, Strategy & Operations) from any other allow lists such as STARTUP_GENERALIST_ALLOW.

## 2. Stop re-downloading what is already known
- Greenhouse: fetch `/v1/boards/{slug}/jobs` WITHOUT content=true, apply the location gate and the role-rules gate on title and location, and fetch `/v1/boards/{slug}/jobs/{id}` content only for new IDs (not already in jobs.db) that pass both. Re-fetch content for a known ID only when its `updated_at` changed.
- Ashby, Lever, Personio, Workable, Recruitee, SmartRecruiters: same principle where the API allows it (list first, details only for new and passing IDs). Where the list endpoint already includes the description, keep one request per board but skip parsing and storing descriptions for postings that fail location or role-rules.
- Per-board cadence: a board that produced no posting passing location in the last 30 days is polled once a day, not every cycle. Log how many boards are on each cadence.
- Target: a full cycle under 4 minutes, and the cycle interval set to 10 minutes so new postings reach jobs.db sooner (applying in the first 48 hours matters most). Keep the single-instance guard so cycles never overlap.
- Log per source per cycle: postings listed, details fetched, passed, seconds taken.

## 3. Start automatically and stay up
- Add a script `install_autostart.ps1` that registers a Windows Task Scheduler task "MunichScraper" at user logon running `run_daemon.bat` (hidden window), and `uninstall_autostart.ps1` to remove it. Do NOT run the install script; Mattia decides whether to run it.
- supervisor.py: if the daemon has crashed 3 times within an hour, wait 15 minutes before the next restart and log why, instead of a tight restart loop.

## 4. Use negative_keywords
- Apply config.py negative_keywords as a title gate (whole words, case-insensitive) right after the role-rules gate, code "negative-keyword". Also add: Construction, Bauarbeiter, Site Manager, Electrician, Mechanic, Dental, Recruiter, Recruitment Consultant, Procurement, Purchasing, Thermal, Propulsion, Powertrain.
- Match only the negative_keywords list itself, not target_locations, which follows it in config.py.

## 5. Database housekeeping
- After each cycle run `PRAGMA wal_checkpoint(TRUNCATE)`. Once a day: delete rows rejected for location, role-rules or negative-keyword that are older than 30 days, but keep their dedupe_key/url in a small `seen_keys` table so they are not re-inserted as new; then VACUUM at most once a week.
- Never delete rows that passed, rows with user_status/applied_at set, or rows the pipeline exported.

## 6. Firecrawl
- Only spend Firecrawl credits on postings that already passed location and role-rules. Log credits used per cycle and stop using Firecrawl (without failing the cycle) below 50 remaining credits.

## Done when
- Tests pass, including new ones for each section.
- One full cycle has run: report per-source counts, cycle time, passing rows before and after re-gating, DB size before and after, and the share of passing rows that fit role_rules.json (target above 90%).
- Write REPORT_BRIEF_13.md with those numbers.
