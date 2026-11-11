# Improvement Brief 12 — Implementation & Verification Report

**Date:** 2026-10-31  
**Workspace:** `C:\Users\Mattia\munichScraper`

---

## 1. Test Results

All existing tests and 37 new Brief 12 unit/integration tests pass:

```text
python -m pytest -q
162 passed in 83.11s
```

### Summary of New Tests Added (`tests/test_brief_12.py` — 37 tests)
1. **Continuous Runner, Heartbeat & Watchdog (`supervisor.py`)**
   - `test_touch_heartbeat_and_age`: Verifies `touch_heartbeat()` writes an ISO-8601 UTC timestamp to `daemon.heartbeat` and `heartbeat_age_minutes()` calculates elapsed minutes accurately.
   - `test_write_pid_and_pid_is_running`: Verifies `write_pid()` writes the process ID to `daemon.pid` and `pid_is_running()` detects live vs. non-existent PIDs.
   - `test_watchdog_check_restarts_when_stale`: Verifies `watchdog_check()` detects a stale heartbeat (`> 30 min`) or dead PID, spawns a fresh daemon process, and logs the restart reason and PIDs to `watchdog.log`.
   - `test_watchdog_check_healthy_no_restart`: Verifies `watchdog_check()` does not restart a healthy daemon with a fresh heartbeat and live PID.
   - `test_source_cadence_stepstone_xing_weekly_arbeitnow_daily`: Verifies `source_is_due()` and `mark_source_run()` enforce 168h (weekly) cadence for `stepstone` and `xing`, 24h (daily) cadence for `arbeitnow`, and every-cycle execution for ATS boards.
   - `test_linkedin_removed_from_scheduled_cycle`: Verifies `LinkedInScraper` is removed from scheduled `JobEngine.scrapers`.

2. **Munich Company Discovery & ATS Board Probing (`company_discovery.py`)**
   - `test_known_munich_slugs_contains_all_26_brief_slugs`: Verifies all 26 required Munich company slugs (`celonis`, `personio`, `helsing`, `deepdrive`, `isar-aerospace`, `lilium`, `tado`, `konux`, `simscale`, `bryter`, `kaia-health`, `holidu`, `egym`, `finanzcheck`, `scalable-capital`, `idnow`, `freeletics`, `alasco`, `capmo`, `ryd`, `finn`, `navvis`, `proglove`, `crashtest-security`, `air-up`, `mambu`) are included in `KNOWN_MUNICH_SLUGS`.
   - `test_slug_variants_generation`: Verifies slug normalization (`GmbH`/`AG`/`SE` stripping, umlaut normalization, hyphenated and compact variants).
   - `test_ats_probes_cover_all_8_vendors`: Verifies public read-only probes for `greenhouse`, `ashby`, `lever`, `personio`, `workable`, `recruitee`, `teamtailor`, and `smartrecruiters`.
   - `test_import_known_slugs_idempotent`: Verifies importing known Munich slugs into `companies` is idempotent.
   - `test_discover_names_records_hits_and_misses_with_7_day_retry`: Verifies discovered boards are persisted to `companies` and `data/discovered_boards.json`, while misses are recorded in `probed_companies` and skipped until their 7-day retry window expires.
   - `test_all_seed_names_fetches_unternehmertum_and_clera_respecting_robots`: Verifies `all_seed_names()` checks `robots.txt` before fetching UnternehmerTUM and Clera `/jobs/munich` seed pages.

3. **Real Descriptions at Intake (`intake_checks.py`, `intake_fixer.py`, `matcher.py`)**
   - `test_fetch_description_follows_json_ld_and_article_selectors`: Verifies `fetch_description()` extracts job descriptions from `JobPosting` JSON-LD and semantic article containers (`article`, `main`, `[class*="description"]`).
   - `test_gate_no_description_rejects_short_descriptions`: Verifies postings with `< 300` chars after detail-page fetch are gated with `gate_rejected = 'no-description'`.
   - `test_gate_no_description_passes_300_plus_chars`: Verifies postings with $\ge 300$ chars pass the description length gate.

4. **Real Employer Apply Links (`intake_checks.py`, `scrapers/arbeitsagentur.py`, `scrapers/remote_boards.py`)**
   - `test_is_aggregator_host_identifies_aggregators`: Verifies `remotive.com`, `arbeitnow.com`, `weworkremotely.com`, `remoteok.com`, `himalayas.app`, `workingnomads.com`, `arbeitsagentur.de`, `jobicy.com`, `wellfound.com`, `munich-startup.de`, `englishjobs.de`, `englishjobsingermany.com`, `germantechjobs.de`, `stepstone.de`, `xing.com`, and `join.com` are recognized as aggregators.
   - `test_check_employer_link_direct_ats_passes`: Verifies direct ATS postings (`greenhouse`, `ashby`, `lever`, `personio`, `workday`, `smartrecruiters`, `recruitee`, `teamtailor`, `bamboohr`, `breezy`, `pinpoint`, `softgarden`, `amazon_jobs`, `vc`, `firecrawl`) pass with `apply_url = url`.
   - `test_check_employer_link_aggregator_extracts_external_apply_link`: Verifies external employer/ATS apply links are extracted from aggregator HTML/JSON payloads.
   - `test_check_employer_link_aggregator_without_employer_link_rejected`: Verifies aggregator rows without an external employer link are rejected with `gate_rejected = 'no-employer-link'`.
   - `test_arbeitsagentur_uses_externe_url_when_present`: Verifies `ArbeitsagenturScraper` extracts `externeURL` / `externeUrl` when provided by the public API.
   - `test_remote_boards_extract_employer_url_from_raw_html`: Verifies `RemotiveScraper`, `HimalayasScraper`, `WeWorkRemotelyScraper`, `WorkingNomadsScraper`, and `RemoteOkScraper` extract external employer URLs before stripping HTML.

5. **Real Liveness Checking & Timestamps (`liveness.py`, `database.py`)**
   - `test_liveness_marks_404_and_410_closed`: Verifies HTTP `404` and `410` set `closed_at`.
   - `test_liveness_marks_closed_phrase_in_200_page_closed`: Verifies HTTP `200` pages containing closed-posting phrases (`"this job is no longer available"`, `"position has been filled"`, `"stellenangebot ist nicht mehr verfügbar"`, etc.) set `closed_at`.
   - `test_liveness_marks_redirect_to_generic_careers_root_closed`: Verifies redirects from a specific posting URL to a bare `/careers` or `/jobs` index page set `closed_at`.
   - `test_liveness_keeps_403_and_429_open`: Verifies HTTP `403` and `429` do not mark postings closed.
   - `test_first_seen_never_null_on_insert_and_backfill`: Verifies `first_seen` is populated on insert and backfilled across existing rows (`0` null rows in `jobs`).

6. **Tighter Hard Blocks (`intake_checks.py`, `matcher.py`)**
   - `test_hard_block_german_required_phrases`: Verifies `"fluent German"`, `"German (C1)"`, `"C1 German"`, `"German is mandatory"`, `"German required"`, `"business fluent in German"`, `"verhandlungssicheres Deutsch"`, `"Deutschkenntnisse (C1)"`, `"fließend Deutsch"` reject as `german_required`.
   - `test_hard_block_german_is_a_plus_allowed`: Verifies `"German is a plus"` / `"German is a bonus"` / `"German is nice to have"` are not blocked by `german_required`.
   - `test_hard_block_years_required_5_plus_and_founder_titles`: Verifies `"5+ years"`, `"6+ years"`, `"7+ years"`, `"8+ years"`, `"10+ years"`, `"minimum of 5 years"`, `"at least 6 years"` in descriptions and `Founder` / `Co-Founder` / `Founding` / `CTO` in titles reject as `years_required`, while `"3-5 years"` passes.
   - `test_hard_block_contract_phrases`: Verifies `"fixed-term"`, `"fixed term"`, `"befristet"`, `"maternity cover"`, `"parental leave cover"`, `"elternzeitvertretung"`, `"12-month contract"`, `"18-month"`, `"24-month"`, `"temporary position"`, `"part-time"`, `"teilzeit"`, `"werkstudent"`, `"working student"`, `"praktikum"`, and `"internship"` reject as `contract`.

7. **Clera Handling & Company Row Cap (`intake_checks.py`, `matcher.py`)**
   - `test_clera_allows_target_role_in_munich_or_remote_eu`: Verifies `Clera` postings pass only when the title matches target role families (Product, Strategy, Operations, BizOps, Chief of Staff, Founder's Associate, AI/Data/Analytics, Program/Project, Commercial/RevOps/GTM, Solutions/Implementation, Venture/Portfolio) AND location is Munich or remote-EU.
   - `test_clera_rejects_non_target_role_or_non_munich`: Verifies non-target roles or non-Munich/non-remote-EU locations from `Clera` are rejected (`off-function` / `location`).
   - `test_clera_not_blocked_by_ger_skip_list`: Verifies `Clera` is evaluated by `check_clera` and the per-posting German gate rather than blanket-blocked by `ger_skip_list.csv`.
   - `test_dedupe_and_cap_enforces_5_row_cap_for_capped_companies`: Verifies `prolificacademic`, `true-talent-solutions`, and `jobgether` are capped at 5 active postings per company (`gate_rejected = 'company_row_cap'`).
   - `test_dedupe_and_cap_collapses_normalized_triple_duplicates`: Verifies duplicate `(company, title, location)` triples are collapsed before insert (`gate_rejected = 'dup_triple_at_intake'`), keeping the row with the longest description and direct employer apply link.

8. **Export Quality Guard (`exporter.py`)**
   - `test_exporter_excludes_missing_description_or_missing_employer_link`: Verifies `export_candidates_csv()` and `export_to_csv()` never export any row without a description ($\ge 300$ chars) or without a verified employer apply link.

---

## 2. First Full Cycle After the Change (Cycle 46)

- **Cycle ID:** `46`
- **Duration:** `1437.4 seconds` (`23.96 minutes`)
- **Scheduled Cadence Enforcement:**
  - `linkedin`: Removed from scheduled cycle (`0` calls).
  - `stepstone` & `xing`: Weekly cadence (`168h`) — skipped in Cycle 46 because last run was `< 168h` ago.
  - `arbeitnow`: Daily cadence (`24h`) — skipped in Cycle 46 because last run was `< 24h` ago.
- **Total Postings Discovered in Cycle 46:** `110,721` across 30 active sources
- **Pre-Insert Intake Deduplication & Caps (Cycle 46):**
  - `dup_triple_at_intake` (normalized `company + title + location` duplicates collapsed): `2,380`
  - `company_row_cap` (`prolificacademic`, `true-talent-solutions`, `jobgether` capped at 5): `7,668`
- **Total Active Passing Postings (`gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`):** `526` (down from `1,036` prior to Brief 12)

### Per-Source Discovered & Active Passed Counts

| Source | Postings Discovered (Cycle 46) | Active Passed Postings (`jobs.db`) | Cadence / Notes |
| :--- | ---: | ---: | :--- |
| `greenhouse` | 53,177 | 302 | Every cycle (ATS) |
| `ashby` | 22,154 | 102 | Every cycle (ATS) |
| `lever` | 12,530 | 21 | Every cycle (ATS) |
| `personio` | 8,756 | 32 | Every cycle (ATS) |
| `workable` | 5,571 | 0 | Every cycle (ATS) |
| `softgarden` | 2,887 | 0 | Every cycle (ATS) |
| `smartrecruiters` | 1,978 | 0 | Every cycle (ATS) |
| `recruitee` | 1,116 | 16 | Every cycle (ATS) |
| `join` | 760 | 1 | Every cycle |
| `teamtailor` | 415 | 6 | Every cycle (ATS) |
| `germantechjobs` | 398 | 0 | Every cycle |
| `pinpoint` | 240 | 0 | Every cycle (ATS) |
| `weworkremotely` | 214 | 12 | Every cycle |
| `arbeitsagentur` | 104 | 2 | Every cycle |
| `amazon_jobs` | 100 | 9 | Every cycle (Direct) |
| `remoteok` | 58 | 2 | Every cycle |
| `jobicy` | 50 | 0 | Every cycle |
| `workday` | 45 | 2 | Every cycle (ATS) |
| `wellfound` | 44 | 0 | Every cycle |
| `breezy` | 40 | 0 | Every cycle (ATS) |
| `englishjobsingermany` | 20 | 0 | Every cycle |
| `englishjobsde` | 19 | 0 | Every cycle |
| `munichstartup` | 15 | 0 | Every cycle |
| `firecrawl` | 12 | 0 | Every cycle (Direct career pages) |
| `remotive` | 12 | 0 | Every cycle |
| `bamboohr` | 5 | 0 | Every cycle (ATS) |
| `eures` | 1 | 0 | Every cycle |
| `himalayas` | 0 | 0 | Every cycle |
| `vc` | 0 | 0 | Every cycle |
| `workingnomads` | 0 | 0 | Every cycle |
| `arbeitnow` | *(skipped — 24h cadence)* | 17 | Daily (24h) |
| `stepstone` | *(skipped — 168h cadence)* | 1 | Weekly (168h) |
| `xing` | *(skipped — 168h cadence)* | 1 | Weekly (168h) |
| `linkedin` | *(removed)* | 0 | Removed from scheduled cycle |
| **Total** | **110,721** | **526** | |

### Rejected Gates by Reason (Cycle 46)

| Gate Reason | Rejected Count (Cycle 46) | Description |
| :--- | ---: | :--- |
| `location` | 92,534 | Non-Munich / non-remote-EU location |
| `cross_source_dup` | 29,595 | Cross-source deduplication (`duplicate_of:<id>`) |
| `company_row_cap` | 7,668 | Per-company cap (max 5 rows for `prolificacademic`, `true-talent-solutions`, `jobgether`) |
| `dup_triple_at_intake` | 2,380 | Normalized `(company, title, location)` duplicate at intake |
| `off-function` | 1,601 | Non-target function (engineering/devops/nursing/etc. or non-target Clera role) |
| `seniority` | 1,590 | Director, VP, Head of, Principal, Staff, C-level, Lead |
| `german` | 1,330 | German-language title/body detection |
| `skip-list` | 1,197 | German-only employer skip list |
| `years_required` | 390 | `5+`/`6+`/`7+`/`8+`/`10+` years required or `Founder`/`Co-Founder`/`Founding`/`CTO` title |
| `age` | 365 | Posting older than 14 days |
| `no-description` | 345 | Description `< 300` characters after detail-page fetch |
| `dup` | 326 | URL / ID duplicate |
| `german_required` | 282 | Explicit German requirement phrase (`fluent German`, `German (C1)`, `verhandlungssicheres Deutsch`, etc.) |
| `no-employer-link` | 171 | Aggregator row with no verifiable external employer/ATS apply link (`161` in cycle + `10` post-dedupe promoted rows) |
| `contract` | 91 | Fixed-term, `befristet`, maternity/parental cover, 12/18/24-month, temporary, part-time, `Werkstudent`, `Praktikum` |
| `language` | 54 | Non-English / non-German foreign language posting |
| `clearance` | 33 | Security clearance / defense citizenship requirement |
| `deep_stack` | 18 | Deep software engineering stack requirement |
| `account-only` | 0 | Login-wall / account-only posting |

### New ATS Boards Found (`company_discovery.py`)

- **Total New/Verified Munich Boards Added in Cycle 46:** `83` (`26` imported from `KNOWN_MUNICH_SLUGS` + `57` newly discovered via live public ATS probing of `120` due seed names; `63` misses recorded in `probed_companies` for 7-day weekly retry).
- **Seed Sources Used & `robots.txt` Compliance:**
  - `KNOWN_MUNICH_SLUGS` (26 curated Munich companies)
  - `MUNICH_SEED_COMPANIES` (`scrapers/ats_discovery.py`)
  - UnternehmerTUM portfolio (`https://www.unternehmertum.de/en/start-ups` — `robots.txt` checked and allowed, 24 names extracted)
  - Clera Munich job index (`https://www.clera.io/jobs/munich` — `robots.txt` checked and allowed, 12 client company slugs extracted)
  - Existing employer names from `jobs.db`
- **Sample Newly Discovered Live Boards (`data/discovered_boards.json` & `companies` table):**
  - `greenhouse:flix` (128 jobs)
  - `ashby:the-exploration-company` (117 jobs)
  - `ashby:camunda` (50 jobs)
  - `greenhouse:parloa` (45 jobs)
  - `ashby:reonic` (32 jobs)
  - `personio:reverion` (23 jobs)
  - `ashby:quantumdiamonds` (20 jobs)
  - `greenhouse:celigo` (20 jobs)
  - `ashby:quantum` (17 jobs)
  - `greenhouse:marvelfusion` (15 jobs)
  - `greenhouse:blacklane` (7 jobs)
  - `greenhouse:numa` (7 jobs)
  - `personio:tanso` (7 jobs)
  - `teamtailor:corintis` (7 jobs)
  - `ashby:kertos` (7 jobs)
  - `teamtailor:vialytics` (5 jobs)
  - `personio:blickfeld` (4 jobs)
  - `ashby:luminovo` (4 jobs)
  - `greenhouse:bimm` (4 jobs)
  - `personio:predium` (3 jobs)
  - `greenhouse:enmacc` (2 jobs)
  - `recruitee:formo` (1 job)

---

## 3. Export Verification (Descriptions & Employer Apply Links)

All exported CSV files and active passing database rows were verified after Cycle 46:

| Export File / Dataset | Total Rows | Rows Without Description (`< 300` chars) | Rows Without Employer Apply Link |
| :--- | ---: | ---: | ---: |
| `exports/candidates_latest.csv` | 14 | **0** | **0** |
| `exports/candidates_full.csv` | 139 | **0** | **0** |
| `matches.csv` | 526 | **0** | **0** |
| `jobs.db` (`gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`) | 526 | **0** | **0** |
| `jobs.db` (`first_seen IS NULL`) | 0 | — | — |

- **Number of exported postings without a description ($\ge 300$ chars):** **`0`**
- **Number of exported postings without an employer apply link:** **`0`**

---

## 4. Daemon & Watchdog Status

- `supervisor.py` watchdog loop is running in the background (`check every 300s`, `heartbeat limit 30 min`).
- Continuous scraper daemon (`python main.py`) is running (`daemon.pid` = `1524`, `daemon.heartbeat` updated continuously before/after each source and cycle).
- `run_daemon.bat` now launches both the continuous scraper daemon (`python main.py`) and the supervisor watchdog (`python supervisor.py`).

