# Report — Brief 14 (7 Nov 2026)

## 1. Pre-Change Backup & Hard Rules Compliance
- **Scraper stopped before changes:** `powershell -ExecutionPolicy Bypass -File scraper.ps1 stop` confirmed no running scraper processes, followed by `PRAGMA wal_checkpoint(TRUNCATE)`.
- **Pre-change backup created:** `jobs.db`, `main.py`, `matcher.py`, `config.py`, `database.py`, `supervisor.py`, `company_discovery.py`, and `web_app.py` were copied into `backup_2026-11-07/`.
- **Hard rules:** Only public pages and public JSON/XML endpoints were queried; `robots.txt` and per-host rate limits were respected; zero logins, account creations, captcha workarounds, or stealth/proxy modes; zero changes made to `C:\Users\Mattia\Desktop\Job Application material` (read-only access to `memory/role_rules.json`, `memory/linkedin_employers.json`, `memory/applied_history.csv`, and `memory/ger_skip_list.csv`).
- **Daemon left stopped at completion:** As requested, the scraper daemon was not restarted after the final `--run-once` verification cycle.

---

## 2. Test Suite Results
- **After Part A:** `177 passed in 44.70s`
- **After Part B & Part C (`tests/test_brief_14.py` added, tests for removed modules deleted):** **`158 passed in 26.95s` (0 failures)**
- **New tests in `tests/test_brief_14.py`:**
  1. `test_a1_greenhouse_and_ashby_never_skipped_and_240s_threshold` — verifies `greenhouse` and `ashby` are exempt (`SLOW_SOURCE_EXEMPT`) and never skipped even after repeated > 240s runs, while non-exempt sources are skipped for 3 cycles only after exceeding `240.0s` twice in a row.
  2. `test_a2_vague_locations_and_remote_ok_and_150_cap` — verifies `"Remote (Germany eligible)"` passes at Gate 2 (`remote_ok`), a vague location (`"Hybrid"`) with Munich in the description passes (`location_source == 'description'`, `location_scope == 'munich'`), `"Remote"` with a US-only description (`"Candidates must reside in the United States"`) is rejected, `"Hybrid"` without Munich/remote-EU signal is rejected with `"vague location, no Munich or remote-EU signal in description"`, `"Germany"` without another city passes (`location_scope == 'germany-wide'`), and the 150-fetch per-cycle cap and per-`job_id` cache hold.
  3. `test_a3_eight_plus_years_never_blocks` — verifies an `"8+ years"` posting with a fitting title passes (`gate_rejected is None`, `years_required == 8`, `score > 0`).
  4. `test_a4_linkedin_employer_seeds_skip_agencies_and_probed` — verifies `load_linkedin_employer_seeds` reads `linkedin_employers.json` first, strips legal/geographic suffixes, skips staffing/recruiting agencies (`recruit`, `staffing`, `personal`, `talent`, `jobs`, `karriere`, `career`), skips companies already in `companies` or `probed_companies`, and `run_daily_discovery` probes them first.
  5. `test_a5_resolve_employer_link_via_registry` — verifies an aggregator posting rejected as `no-employer-link` resolves its canonical ATS `apply_url` via the `companies` table registry (`apply_url_source == 'registry'`) and passes all gates upon re-evaluation.
  6. `test_b5_scraper_log_written_and_daemon_log_not_used` — verifies `web_app.py` does not hijack `logging.basicConfig`, `main.py` attaches its `RotatingFileHandler` (`scraper.log`, 5 MB x 3) and writes to `scraper.log`, and `supervisor.start_daemon()` routes `stdout`/`stderr` to `subprocess.DEVNULL` without creating `daemon.log`.
  7. `test_part_b_removed_sources_firecrawl_exports_and_db_rules` — verifies all removed scrapers, `firecrawl_client.py`, `exporter.py`, and `.bat` launchers are deleted, `config.py` points exclusively to the live `memory/` files, and `Database.save_jobs` sets `description = NULL` on rejected rows while preserving descriptions on passing rows.

---

## 3. Cycle Time per Source (Before vs. After) & Greenhouse Performance

### Greenhouse Summary (A1)
- **Skip count:** **`0`** (previously skipped **58 times** between 4 Nov 19:11 and 7 Nov 02:11 under the 120s rule; now permanently exempt in `SLOW_SOURCE_EXEMPT = {"greenhouse", "ashby"}` and executed first in every cycle alongside `ashby`).
- **Cycle time:** **`80.9s`** (down from **132.0s** average / **171.3s** in Brief 13 — well under the **90s** target).
- **Board cadence & volume in `--run-once`:** `172` active boards (polled every cycle), `508` inactive boards (polled once every 24h), **`449` boards polled this cycle**, **`34,102` postings listed**, **`541` detail calls made**, **`66` passed postings**.

### Per-Source Cycle Timings & Funnel (`python main.py --run-once`)

| Source | Before (Avg `daemon.log`) | After (`--run-once`) | Postings Listed | Details Fetched | Passed (Cycle) | Total Open Passing in DB | Status / Cadence |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `greenhouse` | 132.0s (skipped 58x) | **80.9s** | 34,102 | 541 | 66 | 66 | Polled first every cycle (exempt) |
| `ashby` | 37.0s | **43.4s** | 18,886 | 0 | 80 | 80 | Polled first every cycle (exempt) |
| `lever` | 87.4s | **45.0s** | 6,507 | 0 | 14 | 14 | Every 3rd cycle (`SOURCE_CYCLE_MODULO = 3`) |
| `personio` | 14.5s | **19.3s** | 6,651 | 0 | 17 | 17 | Every cycle (board cadence) |
| `workable` | 26.9s | **20.4s** | 7,825 | 0 | 0 | 0 | Every cycle (board cadence) |
| `recruitee` | 6.1s | **7.4s** | 888 | 0 | 0 | 33 | Every cycle (board cadence) |
| `smartrecruiters` | 5.8s | **5.1s** | 2,380 | 0 | 21 | 9 | Every cycle (board cadence) |
| `teamtailor` | 12.7s | **18.7s** | 479 | 0 | 5 | 5 | Every cycle (board cadence) |
| `englishjobsde` | 6.1s | **6.1s** | 20 | 0 | 1 | 1 | Every cycle |
| `remotive` | 0.5s | **0.6s** | 9 | 0 | 2 | 2 | Every cycle |
| `weworkremotely` | 5.2s | **6.3s** | 204 | 0 | 26 | 26 | Every cycle |
| `jobicy` | 0.6s | **0.4s** | 50 | 0 | 3 | 22 | Every cycle |
| `remoteok` | 1.5s | **1.1s** | 52 | 0 | 4 | 4 | Every cycle |
| `arbeitnow` | — | *skipped (not due)* | — | — | — | 7 | Low-yield cadence (6h) |
| `stepstone` | — | *skipped (not due)* | — | — | — | 0 | Low-yield cadence (6h) |
| `arbeitsagentur` | 10.9s | *skipped (24h cadence)* | — | — | — | 4 | Once a day (`24.0h`) |
| `munichstartup` | 10.1s | *skipped (24h cadence)* | — | — | — | 1 | Once a day (`24.0h`) |

- **Vague-location description fetches in `--run-once`:** `150 / 150` (capped at 150 per cycle, cached by `job.id`).
- **Total open passing candidates in `jobs.db`:** **`307`** (including `119` Munich roles and `84` startup roles).

---

## 4. Rows Recovered by Part A Re-Checks (A2, A3, A5)

### A2 Re-Check (Vague Locations & `remote_ok` / `munich` Patterns)
- **Candidates re-checked:** **`577`** unique open rows from the last 14 days that were previously rejected at the location gate with a vague location and a title passing `role_rules` and negative keywords.
- **Passed location gate after description fetch / `remote_ok` / `munich` rules:** **`319`** rows (`188` via `germany-wide`, `126` via `remote-de` / `remote-eu`, `5` via `munich` in description).
- **Passed ALL gates (`gate_rejected IS NULL`):** **`25`** rows.

### A3 Re-Check (`years_required` Never Blocks)
- **Candidates re-checked:** **`232`** total open rows previously rejected on `years_required` (`109` with `coalesce(posted_at, created_at)` within 14 days; `158` with `created_at` within 14 days).
- **Remaining rows blocked on `years_required`:** **`0`**.
- **Passed ALL gates (`gate_rejected IS NULL`):** **`35`** rows (all 35 from the last-14-days cohort; the rest were caught by subsequent content gates such as `german_required` or `deep_stack` while preserving `years_required` for scoring).

### A5 Re-Check & Cycle Resolution (Employer Links via Registry, Zero Firecrawl)
- **Open `no-employer-link` rows (last 14 days) checked against `companies` registry:** **`252`** rows.
- **Links resolved (`apply_url_source = 'registry'`):** **`40`** rows resolved to direct ATS posting URLs (`16` passed all gates immediately during the Part A5 re-check; after the full `--run-once` cycle and new board discoveries, **`39` active rows** have `apply_url_source = 'registry'` and **`24` of them pass all gates**).

---

## 5. Employer Names Probed & Boards Found (A4)
- **Seed source priority:** `C:\Users\Mattia\Desktop\Job Application material\memory\linkedin_employers.json` is read first (`MAX_LINKEDIN_EMPLOYERS_PER_DAY = 300`), before `unternehmertum` and `clera_munich`, with `max_names` raised to **`600`**.
- **Filtering:** Normalized names strip legal/country suffixes (`GmbH`, `AG`, `SE`, `Inc`, `Ltd`, `LLC`, `BV`, `KG`, `Group`, `Holding`, `Deutschland`, `Germany`) and punctuation, skip any name containing `recruit`, `staffing`, `personal`, `talent`, `jobs`, `karriere`, or `career`, and skip names already present in `companies` or `probed_companies`.
- **Final cycle (`run_daily_discovery`) results:**
  - **Total names probed:** **`600`** (`300` from `linkedin_employers.json` + `300` from startup/DB seed lists).
  - **LinkedIn employer names probed:** **`300`**
  - **New boards found from LinkedIn employers:** **`52`**
  - **Total hits / new boards registered across all 600 probed names:** **`268` hits** (`332` misses marked for 7-day retry).

---

## 6. Sources Removed (B1)
The following **16** sources were completely removed from `main.py`, `scrapers/`, `config.py`, and the test suite:
1. `softgarden` (`scrapers/softgarden.py`)
2. `join` (`scrapers/join.py`)
3. `xing` (`scrapers/xing.py`)
4. `workday` (`scrapers/workday.py`)
5. `bamboohr` (`scrapers/bamboohr.py`)
6. `breezy` (`scrapers/breezy.py`)
7. `pinpoint` (`scrapers/pinpoint.py`)
8. `wellfound` (`WellfoundScraper` in `scrapers/english_boards.py`)
9. `workingnomads` (`WorkingNomadsScraper` in `scrapers/remote_boards.py`)
10. `germantechjobs` (`GermanTechJobsScraper` in `scrapers/english_boards.py`)
11. `englishjobsingermany` (`EnglishJobsInGermanyScraper` in `scrapers/english_boards.py`)
12. `himalayas` (`HimalayasScraper` in `scrapers/remote_boards.py`)
13. `amazon_jobs` (`scrapers/amazon_jobs.py`)
14. `eures` (`scrapers/eures.py`)
15. `vc` (`VCBoardsScraper` in `scrapers/vc_boards.py`)
16. `firecrawl_search` (`scrapers/firecrawl_search.py` & `firecrawl_client.py`)
17. `linkedin` (`scrapers/linkedin.py`)

- **Database cleanup for removed sources:**
  - Deleted **`706`** boards from `companies` (`join`: 605, `softgarden`: 87, `breezy`: 7, `pinpoint`: 6, `bamboohr`: 1).
  - Deleted **`11,702`** non-passing `jobs` rows from removed sources (saving their dedupe keys into `seen_keys`).
  - Deleted **`110`** `fc_map_*` / `firecrawl_*` keys from `scraper_metadata`.

---

## 7. Files and Folders Removed (with Sizes)

| Removed Path | Type | Size Before Removal |
| :--- | :--- | ---: |
| `daemon.log` | File | `93,328,532` B (`89.01 MB`) |
| `exports/` | Folder | `15,039,414` B (`14.34 MB`) |
| `.firecrawl/` (147 files) | Folder | `3,045,618` B (`2.90 MB`) |
| `__pycache__/` (all occurrences) | Folders | `1,442,751` B (`1.38 MB`) |
| `backup_2026-11-05/` | Folder | `327,422` B (`319.75 KB`) |
| `data/applied_history.csv` (stale copy) | File | `128,347` B (`125.34 KB`) |
| `matches.csv` | File | `76,807` B (`75.01 KB`) |
| `scratch/` | Folder | `45,500` B (`44.43 KB`) |
| `firecrawl_client.py` | File | `37,869` B (`36.98 KB`) |
| `scrapers/linkedin.py` | File | `31,171` B (`30.44 KB`) |
| `data/ger_skip_list.csv` (stale copy) | File | `25,716` B (`25.11 KB`) |
| `legacy/` | Folder | `24,259` B (`23.69 KB`) |
| `scrapers/xing.py` | File | `15,340` B (`14.98 KB`) |
| `config.py.bak_2026-11-04` | File | `14,771` B (`14.42 KB`) |
| `scrapers/firecrawl_search.py` | File | `14,357` B (`14.02 KB`) |
| `exporter.py` | File | `9,704` B (`9.48 KB`) |
| `scrapers/vc_boards.py` | File | `7,819` B (`7.64 KB`) |
| `scrapers/join.py` | File | `7,059` B (`6.89 KB`) |
| `scrapers/softgarden.py` | File | `6,686` B (`6.53 KB`) |
| `scrapers/workday.py` | File | `6,372` B (`6.22 KB`) |
| `run_audit_brief3.py` | File | `6,211` B (`6.07 KB`) |
| `.pytest_cache/` | Folder | `6,137` B (`5.99 KB`) |
| `scrapers/eures.py` | File | `5,642` B (`5.51 KB`) |
| `run_audit_brief4.py` | File | `5,286` B (`5.16 KB`) |
| `scrapers/bamboohr.py` | File | `4,520` B (`4.41 KB`) |
| `scrapers/amazon_jobs.py` | File | `4,258` B (`4.16 KB`) |
| `scrapers/pinpoint.py` | File | `3,778` B (`3.69 KB`) |
| `scrapers/breezy.py` | File | `3,659` B (`3.57 KB`) |
| `data/verified-career-facts-2026-10-10.md` | File | `3,321` B (`3.24 KB`) |
| `scraper.ps1.bak_2026-11-07` | File | `3,109` B (`3.04 KB`) |
| `view_results.py` | File | `1,693` B (`1.65 KB`) |
| `data/unsupported-cv-claims-2026-10-10.md` | File | `1,523` B (`1.49 KB`) |
| `data/firecrawl_report.json` | File | `565` B (`0.55 KB`) |
| `run_daemon.bat` | File | `392` B (`0.38 KB`) |
| `run_once.bat` | File | `117` B (`0.11 KB`) |
| `data/jobs.db` (empty stale file) | File | `0` B (`0 KB`) |
| `agy_upgrade.log` | File | `0` B (`0 KB`) |
| `tests/test_firecrawl.py` & `tests/test_brief_13_s6.py` | Files | `10,835` B (`10.58 KB`) |
| **Total Removed Files & Folders** | | **`113,701,489` B (`108.43 MB`)** |

*(In addition, all `IMPROVEMENT_BRIEF_*.md` and `REPORT_BRIEF_*.md` files were moved from the root directory into `docs/briefs/`.)*

---

## 8. Database, Log, and Folder Sizes (Before vs. After)

| Metric | Before Brief 14 | After Brief 14 | Target / Notes |
| :--- | ---: | ---: | :--- |
| **`jobs.db`** | `236,105,728` B (**`225.17 MB`**) | `59,289,600` B (**`56.54 MB`**) | **< 80 MB target met** (`-74.9%`, 98,705+ old rejected rows pruned, rejected descriptions set to `NULL`, redundant `seen_keys` indexes dropped, `VACUUM` executed) |
| **`jobs.db-wal`** | `1,520,312` B (`1.45 MB`) | `0` B (`0.00 MB`) | Checkpointed with `TRUNCATE` |
| **`daemon.log`** | `93,328,532` B (**`89.01 MB`**) | **Deleted (`0 MB`)** | `start_daemon()` now uses `subprocess.DEVNULL` |
| **`scraper.log`** | `3,171,504` B (`3.02 MB`, stale since 13 Oct) | `5,217,855` B (`4.98 MB`, active) | `RotatingFileHandler` (5 MB x 3) attached and verified |
| **`watchdog.log`** | `2,076` B (`0.00 MB`) | `2,497` B (`0.00 MB`) | `RotatingFileHandler` (1 MB x 2) attached |
| **Workspace folder (excl. `backup_2026-11-07/`)** | `353,296,812` B (**`336.93 MB`**) | `65,902,960` B (**`62.85 MB`**) | **`-274.08 MB` (`-81.3%`)** |

---

## 9. Anything That Could Not Be Done
- **Nothing.** Every item in Part A (A1–A5), Part B (B1–B6), and Part C (tests, full `--run-once` cycle, and report in `docs/briefs/REPORT_BRIEF_14.md`) was implemented and verified completely.
