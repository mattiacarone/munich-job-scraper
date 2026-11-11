# munichScraper upgrade brief 4 (13 Oct 2026) - final polish

Brief 3 worked: 31 tests pass, every passed row now has `company_display` (215/215), the Yext/RobCo German leaks and the Freiburg location leak are gone, 0 senior leaks, 0 duplicates. Audit now: 45,143 rows, 215 passed. What is left, in priority order. Keep everything that works. No paid services, no LLM calls.

## 1. LinkedIn: the employer apply link is still not extracted (HIGH)
Audit of the 15 passed LinkedIn rows: 9 have `apply_url`, but each one is the LinkedIn page itself (`https://www.linkedin.com/jobs/view/<id>`), `ats` is NULL for all, and only 5 of 15 have a description > 300 chars.
- `apply_url` must hold the EMPLOYER's link, not LinkedIn's. On the guest job page (`https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}` and the public `https://www.linkedin.com/jobs/view/{id}`) look for: `<code id="applyUrl">` / `<!--"https://www.linkedin.com/jobs/view/externalApply/...?url=...-->`, an `<a>` whose `href` contains `/safety/go/?url=` or `externalApply`, and `data-tracking-control-name="public_jobs_apply-link-offsite"`. Take the `url` query parameter and URL-decode it twice if needed. If the page only offers "Easy Apply" (no off-site link), leave `apply_url` NULL and set `ats = 'linkedin-easy-apply'`.
- If the decoded host is greenhouse.io / ashbyhq.com / lever.co / personio.de|com / workable.com / recruitee.com / smartrecruiters.com, set `ats` and `ats_slug` and add the slug to `data/discovered_boards.json`.
- Fetch the detail page for EVERY LinkedIn row that passes the cheap gates (not only some), still max 150 per cycle, with the existing pause logic; persist the description even when the gate later rejects the row.
- Re-run the German gate on the fetched description and reject accordingly.
- Test with two saved fixtures in `tests/fixtures/` (one off-site job, one Easy Apply job).

## 2. German titles and out-of-profile titles on LinkedIn
Passed rows that should not have passed: "Senior SuccessFactors Consultant & Entwickler" (German word), "Technischer Systemingenieur ÖPNV (m/w/d)" (German title), "Systems Engineer - Weapon Integration" (defense hardware), "Avionics Integration Engineer" (hardware), "SAP MDG Consultant" (SAP-specific; Mattia has no SAP).
- German title words: add `entwickler|systemingenieur|technischer|techniker|ingenieur|ingenieurin|fachkraft|sachbearbeiter|berater(in)?|beratung|leiter(in)?|referent(in)?|mitarbeiter(in)?|kaufm\w+|projektleiter(in)?|verkäufer|monteur|elektriker|mechaniker|angestellte|stelle|werkstudent` and reject when any appears in the title.
- Domain gate (title): reject `avionics|weapon|defen[cs]e|ordnance|aerospace systems|powertrain|chassis|homologation|SAP (MDG|FICO|S/4|BTP|ABAP|MM|SD|HCM|SuccessFactors)|ABAP|SuccessFactors|Workday|Oracle (EBS|Fusion)|NetSuite` UNLESS the title also contains MuleSoft|Boomi|Salesforce|ServiceNow|HubSpot|integration platform. (Mattia has no SAP/Workday/defense background.)
- Make the engineer filter typo-tolerant: `engi?ee?r|enginer|enginee?r`; "Senior Process Engieer" (hardware) must be rejected.
- `fullstack` / `full stack` titles are rejected even when "Forward Deployed" is present.
- Tests: each example above.

## 3. Keep the daemon alive and the DB useful
- The daemon is not running right now (no `main.py` process). In `run_daemon.bat` also write a heartbeat file `daemon.heartbeat` (ISO timestamp) at the end of every cycle so another program can tell if the scraper is alive; print the age in `python main.py --health` and warn when older than 60 minutes.
- Database growth: 45k rows, 33k of them location-rejected. Add `python main.py --prune` that deletes rejected rows older than 30 days (keep passed rows and `applied_history` matches) and `VACUUM`; call it automatically once a day.
- Cycle time is 5-10 minutes; log per-source timing in `cycle_metrics` and skip any source that took > 120 s twice in a row for the next 3 cycles (log it).

## 4. LinkedIn cadence
Health warns "LinkedIn hit maximum rate-limit pauses (4)". Make the LinkedIn search run at most every 6 hours (persist `last_linkedin_run` in `scraper_metadata`), randomise the start by ±20 minutes, and process the queries in a rotating order (resume where the last run stopped) instead of always starting with the same ones, so the whole query list gets covered across runs even when throttled.

## 5. Delivery
All existing tests plus the new ones pass. Run `python main.py --rescore` then `python main.py --run-once`, then `python run_audit_brief3.py` (or your updated audit) and print: rows, passed, passed per source, passed LinkedIn rows with description and with an employer `apply_url`, passed rows matching German-title / SAP / defense / engineering leak patterns (must be 0), daemon heartbeat age.
