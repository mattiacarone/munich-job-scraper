# munichScraper brief 14 (7 Nov 2026): find more jobs that fit, drop what never pays off

## Evidence (jobs.db, daemon.log and the application pipeline's handoff files, 7 Nov 2026)
- **Where applications come from.** Since 13 Oct the scraper fed 89 sent applications: ashby 36, greenhouse 32, arbeitnow 8, teamtailor 5, recruitee 3, stepstone 2, personio 1, workable 1, linkedin 1. Lever gave 27 matches and 34 tailored applications but 0 sends (hCaptcha; Mattia applies by hand at fit 75+).
- **Sources with no match at all since 13 Oct:** softgarden (786 postings reached the pipeline, 0 matches), englishjobsingermany, germantechjobs, himalayas, amazon_jobs, wellfound, pinpoint, workday, breezy, bamboohr. vc and workingnomads listed 0 postings in the last 12 cycles. join and stepstone/xing listings are account-only. XING rows have company "XING Company" and the city pasted into the title.
- **Firecrawl:** 49 credits left, below the scraper's own guard of 50 ("refusing credit" 26 times in the last 3 MB of daemon.log). It produced 1 passing row in 14 days.
- **Greenhouse keeps getting skipped.** The slow-source rule (main.py:986-1019: more than 120 s twice in a row means skip 3 cycles) skipped greenhouse 58 times between 4 Nov 19:11 and 7 Nov 02:11. Greenhouse averages 132 s per cycle (about 44,000 listed postings, 650 boards). It is the second-best source.
- **Vague locations are never examined.** In the last 14 days 782 rows (563 unique company + title) whose title passes role_rules were rejected at the location gate with no description stored. Their location was empty (267), "Germany" (62), "Remote" (59), "Hybrid" (57), "Deutschland" (43), "EMEA" (22), "Deutschlandweit" (22) or "Bundesweit" (16). Brief 13 section 2 skips details for postings that fail location, so these never get a second look. Examples: Woodmark Consulting AG "AI Solutions Engineer (m/f/d)" (Germany), Bjak "Technical Product Manager - AI Stockbroking" (Germany), Capco "Data Modelling Consultant (Insurance)" (empty).
- **Remote-EU text rejected.** 26 more fitting rows were rejected although the location itself says remote-EU: "Remote (Germany eligible)", "EU/UK - Remote", "Remote; Europe; New York", "Berlin, Germany (Hybrid) (Remote)".
- **Years still block.** matcher.py check_gate_years_required (line 1399, called at 1849-1852) rejects 6+ years, or 5+ with a senior word. Mattia's rule since 4 Nov: a years ask lowers the score, it never blocks. 158 fitting rows were rejected on years_required in 14 days.
- **Board discovery misses the employers that are hiring now.** 1,859 companies posted Munich or remote jobs on LinkedIn since 24 Oct; 1,094 of them are in neither probed_companies nor companies. Probing currently finds a board for about 43% of names (307 of 720 in the last 7 days).
- **Logs.** web_app.py:39 calls logging.basicConfig at import time. main.py imports web_app (line 87) before its own basicConfig (line 92), so main.py's 5 MB x 3 RotatingFileHandler never attaches: scraper.log was last written on 13 Oct. All output goes to stdout, which supervisor.py start_daemon appends to daemon.log with no rotation: daemon.log is 93 MB. Drive C: ran completely full on 6 Nov.
- **Database.** jobs.db is 236 MB. 132,082 of its 132,405 rows are rejected rows, which carry 61 MB of description text, and 96,992 of them are older than 7 days. scraper_metadata holds about 3,000 fc_map_* keys from Firecrawl.
- **Unused output and stale copies.** Nothing reads exports/ (21 MB) or matches.csv: the pipeline reads jobs.db directly. data/applied_history.csv, data/ger_skip_list.csv and the two data/*.md fact files are 13 Oct copies, and data/jobs.db is an empty file. main.py:114 reads Path("memory/applied_history.csv") relative to the scraper folder; that path never exists, so it falls back to the stale 13 Oct copy.

## Hard rules (unchanged from briefs 9-13)
- Only public pages and public JSON endpoints. Respect robots.txt and rate limits.
- Never log in, create accounts, solve or work around a captcha, or use stealth or proxy modes. Never write keys to logs.
- Keep all tests passing and add a test for each change. Delete tests only for modules this brief removes.
- Do not change anything in `C:\Users\Mattia\Desktop\Job Application material`. Only READ these files from its memory/ folder: role_rules.json, linkedin_employers.json, applied_history.csv, ger_skip_list.csv.
- **Before any change:**
  - Stop the scraper: `powershell -ExecutionPolicy Bypass -File scraper.ps1 stop`.
  - Run a WAL checkpoint, then copy jobs.db, main.py, matcher.py, config.py, database.py, supervisor.py, company_discovery.py and web_app.py into backup_2026-11-07/.
- **At the end:** do not restart the scraper. Mattia starts it with the "Start Scraper" icon on his Desktop.

## Part A: more jobs that fit

### A1. Greenhouse and Ashby are never skipped
- Exempt greenhouse and ashby (68 of the 89 sends) from the slow-source rule. For all other sources raise the threshold from 120 s to 240 s.
- Run greenhouse and ashby first in every cycle, so their new postings reach jobs.db earliest.
- Bring greenhouse under 90 s per cycle. Check that the brief 13 board cadence really skips inactive boards (log how many boards are polled per cycle), and keep list calls concurrent.
- Log for every source and cycle: postings listed, details fetched, rows passed, seconds.

### A2. Vague locations get a second look instead of a rejection
- role_rules.json now has two more fields, `munich` and `remote_ok` (Python `re`, use re.I). They are the same patterns the pipeline uses.
  - At Gate 2, a location passes when `munich` matches it or `remote_ok` matches it, in addition to the current logic.
  - This fixes "Remote (Germany eligible)", "EU/UK - Remote" and "Remote; Europe; ...".
  - If the fields are missing, keep today's logic.
- **What counts as vague:** after trimming and lowercasing, the location is empty, or it is one of remote, hybrid, germany, deutschland, de, europe, emea, eu, anywhere, deutschlandweit, bundesweit, home office, homeoffice, flexible, or a combination such as "remote, germany" or "germany (remote)".
- **When to fetch the description:** the location is vague, the title passes role-rules and negative keywords, and the title names no other region (latam, latin america, us, usa, north america, americas, apac, uk, united kingdom, france, spain, italy, nordics, benelux, netherlands, poland, india, canada, japan, singapore, australia, mena). Then fetch the posting's description even though the location gate would fail, and decide:
  - **Munich:** the description names a Munich-area place (the `munich` pattern). The location passes, with location_source = 'description'.
  - **Remote EU:** the description says remote work is open to Germany, the EU or EMEA (`remote_ok`) and does not limit it to another country ("US only", "must reside in the United States", "UK right to work only"). The location passes as remote.
  - **Germany-wide:** the location is "germany", "deutschland", "deutschlandweit" or "bundesweit" and the description names no other city as the place of work. The location passes with location_scope = 'germany-wide'; the pipeline's matcher judges it.
  - **Otherwise:** reject with code `location` and reason "vague location, no Munich or remote-EU signal in description".
- Fetch each posting's description only once (cache by job id). Cap these extra fetches at 150 per cycle and log the count.
- Re-check the open rows from the last 14 days that were rejected on location and have a vague location and a fitting title (about 560 unique).

### A3. A years ask never blocks
- Remove the years_required rejection (matcher.py check_gate_years_required at 1399 and its call at 1849-1852). Also make sure the role_rules `years_block` path can never reject a row.
- Keep parse_years_required and keep storing years_required on the row; the pipeline uses it for scoring.
- Re-check the open rows rejected on years_required in the last 14 days.

### A4. Discover boards from employers that are hiring in Munich now
- The pipeline now writes `memory/linkedin_employers.json` at every run. Its `employers` field is a list of `{name, postings, last_seen}`, sorted by postings, with agencies and Deloitte already removed.
- In company_discovery.run_daily_discovery, read this file as the FIRST seed source, before the startup lists.
- Skip names already in probed_companies or companies. Compare normalized names: lowercase; strip GmbH, AG, SE, Inc, Ltd, LLC, BV, KG, Group, Holding, Deutschland, Germany and punctuation.
- Probe up to 300 employer names per day, and raise max_names to 600 so the startup lists keep their share.
- Also skip any name containing recruit, staffing, personal, talent, jobs, karriere or career.
- Log the number of employer names probed and boards found.

### A5. Employer links without Firecrawl
- Rows rejected as no-employer-link (englishjobsde 56, weworkremotely 19, remoteok 13 in 14 days) may belong to a company that has a board in the companies table.
- For those rows, list that board, look for a posting whose normalized title matches, and if one is found set apply_url to it (apply_url_source = 'registry') and run the gates again.
- No Firecrawl and no search engines.

## Part B: remove what does not pay off

### B1. Sources
- **Remove completely.** Take these out of main.py (lines 179-211 and their imports), scrapers/, config.py and the tests:
  - softgarden
  - join: an account-only ATS, and Mattia never creates accounts
  - xing: broken parsing and account-only
  - workday: account-only
  - bamboohr, breezy, pinpoint, wellfound, workingnomads
  - germantechjobs, englishjobsingermany
  - himalayas: the pipeline scans Himalayas itself
  - amazon_jobs, eures (already commented out), vc (VCBoardsScraper)
  - firecrawl_search
  - scrapers/linkedin.py: the pipeline runs its own LinkedIn search, and two LinkedIn scanners risk rate limits
- **Keep, polled once a day:** arbeitsagentur (11.6 s per cycle for about 5 postings) and munichstartup (9.9 s for about 15).
- **Keep, polled every third cycle:** lever (60 s per cycle, 0 automatic sends).
- **Keep as they are:** greenhouse, ashby, personio, workable, recruitee, smartrecruiters, teamtailor, arbeitnow, stepstone, englishjobsde, remotive, weworkremotely, remoteok, jobicy, and the partner_mulesoft boards (ecosystem_partners.py).
- **Database clean-up:** delete the companies-table boards of removed sources (join 605, softgarden 87, breezy 7). Delete non-passing jobs rows of removed sources, keeping their dedupe keys in seen_keys as the daily prune does.

### B2. Firecrawl
- Delete firecrawl_client.py and scrapers/firecrawl_search.py.
- Remove every Firecrawl call in main.py (around lines 84-85, 177, 211, 491-493, 933-947, 982, 1087-1096, 1118, 1123, 1219, 1305 and 1436).
- IntakeFixer and LivenessEngine must keep working with plain HTTP and no firecrawl argument.
- Delete the .firecrawl/ folder (147 files), data/firecrawl_report.json and the fc_map_* keys in scraper_metadata.
- Remove tests that only cover Firecrawl. Do not uninstall the Firecrawl CLI.

### B3. Files and folders nobody uses
- **Delete:**
  - folders: legacy/, scratch/, backup_2026-11-05/, every __pycache__/, .pytest_cache/
  - old copies and scripts: config.py.bak_2026-11-04, scraper.ps1.bak_2026-11-07, run_audit_brief3.py, run_audit_brief4.py, view_results.py, agy_upgrade.log
  - stale data copies: data/jobs.db (empty), data/applied_history.csv, data/ger_skip_list.csv, data/verified-career-facts-2026-10-10.md, data/unsupported-cv-claims-2026-10-10.md
- **Launchers:** delete run_once.bat and run_daemon.bat. They open visible console windows, and scraper.ps1 plus the Desktop icons replace them. In install_autostart.ps1.disabled, change the action to `powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 start`.
- **Briefs:** move every IMPROVEMENT_BRIEF_*.md and REPORT_BRIEF_*.md into docs/briefs/. This brief's report goes there too.
- **Live data only:**
  - config.py reads applied_history.csv and ger_skip_list.csv only from the live memory folder. If a file is missing, log a warning and run that gate without it; never fall back to a stale copy.
  - Fix main.py:114 to use config.APPLIED_HISTORY.

### B4. Exports
- Remove the export step: exporter.py, the export calls in main.py (around lines 526, 1155 and 1299) and the --export CLI flag.
- Delete exports/ and matches.csv. Keep dashboard.html, web_app.py and notifier.py.

### B5. Logs
- Remove the basicConfig call from web_app.py:39, so main.py's RotatingFileHandler (scraper.log, 5 MB x 3) really attaches. Test that scraper.log is written.
- In supervisor.py start_daemon, send main.py's stdout and stderr to subprocess.DEVNULL, since main.py already logs everything to scraper.log.
- The supervisor's own messages go to watchdog.log through a RotatingFileHandler (1 MB x 2).
- After the change, delete the old 93 MB daemon.log.

### B6. Database
- Keep description text only for rows that pass the gates, or that wait for the A2 decision. Set description to NULL for rows rejected on location, role-rules, negative-keyword, seniority, german, german_required, skip-list, account-only or duplicate.
- Prune rejected rows after 7 days instead of 30 (prune_rejected_jobs, prune_stale_rejected_jobs, main.py:962 and 1568). Keep their dedupe keys in seen_keys, as today.
- Run one VACUUM at the end. Target: jobs.db under 80 MB.

## Part C: tests and report
- **Tests:** the full suite passes. Add new tests for:
  - A1: greenhouse is never skipped; another source is skipped after taking 240 s twice.
  - A2: a vague location with Munich in the description passes; "Remote (Germany eligible)" passes; "Remote" with a US-only description is rejected; the 150-fetch cap holds.
  - A3: an "8+ years" posting with a fitting title passes.
  - A4: seeds come from linkedin_employers.json; agencies and already-probed names are skipped.
  - A5: an employer link is resolved through the registry.
  - B5: scraper.log is written and nothing is appended to daemon.log.
- **Final cycle:** run one full cycle (`python main.py --run-once`).
- **Report:** write docs/briefs/REPORT_BRIEF_14.md with:
  - tests passed
  - cycle time per source, before and after
  - greenhouse seconds and skip count
  - rows recovered by the A2 and A3 re-checks, and how many of them now pass
  - employer names probed and boards found
  - links resolved by A5
  - sources removed
  - files and folders removed, with their sizes
  - jobs.db and log sizes, before and after
  - anything that could not be done, and why
