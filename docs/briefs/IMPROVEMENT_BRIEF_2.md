# munichScraper upgrade brief 2 (13 Oct 2026)

Brief 1 is implemented and its 9 tests pass. This brief fixes what an audit of the first export found and makes the scraper the single, reliable source of fresh Munich jobs for the Claude pipeline (which reads `jobs.db` directly, read-only, with `gate_rejected IS NULL AND score > 0` and the last 14 days). Keep everything from brief 1 working. No paid services, no LLM calls, never invent facts about Mattia.

## 1. Clean out the legacy code paths
- Delete `scrapers/greenhouse_scraper.py`, `scrapers/jobicy_scraper.py`, `scrapers/api_scraper.py`, `scrapers/html_scraper.py`, `scrapers/playwright_scraper.py` (move to `legacy/`), and remove them from `main.py`.
- One-off migration: delete DB rows whose `id` is not in the `{source}:{slug}:{postingId}` / `li:{id}` / `arbeitnow:{slug}` / `jobicy:{id}` form (sources "Greenhouse-flix", "GermanTechAPI", "JobicyGermany" etc.). Log how many were removed.

## 2. One posting = one row (cross-source dedupe)
- Add a column `dedupe_key` = normalised company + normalised title (lowercase; strip "(m/w/d)", "(f/m/d)", "(all genders)", "(w/m/d)", "(m/f/x)" and anything in brackets; keep a-z0-9).
- When two rows share a `dedupe_key`, keep ONE as the canonical row and set `gate_rejected = 'duplicate_of:<id>'` on the others. Priority for the canonical row: company ATS (greenhouse/ashby/lever/personio/workable/recruitee/smartrecruiters) > arbeitnow/jobicy > LinkedIn.
- Test: the same "Customer Success Engineer" at Veeam from two sources leaves exactly one row with `gate_rejected IS NULL`.

## 3. Real company names
- Add `company_display`. Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{slug}` -> `name`. Ashby: `organizationName` if present in the job-board API, else the job's `companyName`. Lever/Personio/Workable/Recruitee/SmartRecruiters: the company name field in their API if present. Fallback: the slug title-cased. Cache per slug in a `companies` table.
- Use `company_display` in `company` for new rows ("Veeam Software", not "Veeamsoftware").

## 4. LinkedIn: get the text and the real apply link
- For each LinkedIn card that passes the title/location gates, fetch `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}` (same pacing and 429 pause rules as the search), store the description (`.show-more-less-html__markup`), and run the German gate on it.
- Extract the off-site apply URL if present (look for `applyUrl`, `offsiteApplyUrl` or the `<code id="applyUrl">` element; LinkedIn wraps it as `/jobs/view/externalApply/...?url=<encoded>` — decode the `url` parameter) into a new column `apply_url`. If it points to a known ATS (greenhouse, ashby, lever, personio, workable, recruitee, smartrecruiters), also record `ats` and `ats_slug`, and append the slug to `data/discovered_boards.json` (see 7).
- Never fetch more than 150 LinkedIn detail pages per cycle; detail fetches only on the 6-hour LinkedIn cycle.

## 5. Read the project's files live, not copies
In `config.py` add paths (with fallback to `data/` if missing):
- `APPLIED_HISTORY = C:\Users\Mattia\Desktop\Job Application material\memory\applied_history.csv`
- `GER_SKIP_LIST = ...\memory\ger_skip_list.csv`
- `ATS_REGISTRY = ...\memory\ats_registry.json` (merge with `data/discovered_boards.json`)
- `LINKEDIN_QUERIES = ...\memory\linkedin_queries.txt`
Reload them at the start of every cycle, so new applications and new boards added by the Claude pipeline are picked up without copying files. Read-only: never write to that folder.

## 6. Closed postings
- Each cycle, a posting that was listed on its ATS board before but is no longer in the board's API response gets `closed_at = now`. LinkedIn/Arbeitnow rows older than 21 days get `closed_at` too.
- Add `closed_at IS NULL` to the rule that makes a row a candidate (`gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`). Document this in README so the Claude pipeline can add it to its query.

## 7. Find new company boards automatically
- After each cycle take every company name seen (all sources) that is not in the registry and was not probed in the last 14 days (table `probed_companies`), max 200 per cycle, concurrency 12.
- Slug candidates: normalised name, name without legal suffixes (GmbH, AG, SE, Inc, Ltd, Group, Germany, Deutschland, Europe), and the hyphenated form.
- Probe in order greenhouse, ashby, lever, smartrecruiters (totalFound > 0), workable, recruitee, personio; a slug counts only if the API returns at least one job. Save hits to `data/discovered_boards.json` (same shape as `ats_registry.json`) and scrape them from the next cycle.

## 8. Gate fixes from the audit
- Off-function: add tutor, teacher, nanny, babysitter, caregiver, cleaner, cook, chef, waiter, cashier, sales assistant, retail assistant, delivery, courier, call center/callcenter agent.
- Location: "Germany" alone (no city, no "remote") is NOT Munich -> reject. Accept "Remote Germany / Remote DE / Germany (Remote)" only when the posting text or title says remote.
- Age: reject postings whose `posted_at` is older than 21 days; if `posted_at` is missing use `created_at` (first seen).
- German gate: also reject when the title is in German (contains words like Berater, Mitarbeiter, Sachbearbeiter, Kaufmann, Kauffrau, Leiter, Referent, Entwickler, Vertrieb, Projektleiter, Spezialist, Fachinformatiker, Ingenieur) even if the description is English.

## 9. Metrics and health
- Table `cycle_metrics` (one row per cycle): started_at, duration_s, per-source postings and errors, new rows, rejected per gate, passed, closed, new boards found, LinkedIn pauses.
- `python main.py --health` prints the last 10 cycles and warns when a source returned 0 postings for 3 cycles in a row or LinkedIn hit 4 pauses in 3 cycles.
- Rotate `scraper.log` at 5 MB (keep 3 files).

## 10. Alerts that matter
- Telegram: document in README how Mattia creates a bot (BotFather) and gets the chat id; keep tokens in `.env` (python-dotenv), never in code.
- Alert only canonical rows (after dedupe), score >= 60, posted <= 3 days, not closed, max 10 per cycle; one message per job: company_display, title, location, score, apply_url or url.

## 11. Run it reliably on Windows
- `run_daemon.bat` must restart the daemon if it crashes (loop with a 60 s wait) and write to `daemon.log`.
- README: a one-line Task Scheduler command Mattia can run himself to start `run_daemon.bat` at logon (do not register it automatically).

## 12. Tests (add to tests/)
Dedupe keeps one row and prefers the ATS source; company_display from a mocked Greenhouse board response; LinkedIn externalApply URL decoding; closed posting gets closed_at and leaves the candidate set; "Germany" alone rejected, "Remote Germany" accepted; German title rejected with an English description; tutor posting rejected; slug probe counts only a non-empty job list. All tests and `python main.py --run-once` must pass; print the funnel at the end.
