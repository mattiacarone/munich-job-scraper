# munichScraper Brief 11 Implementation Report (28 Oct 2026)

## 1. Executive Summary
Brief 11 expanded munichScraper to reach more Munich and EU/EMEA remote employers matching Mattia Carone's profile (Solutions Engineering, Partner Management, Technical Program Management, Customer Success, and AI Solutions).

All 7 core sections of the brief were implemented following the hard rules:
- **Contract preserved:** `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`.
- **Public endpoints only:** Strictly public JSON APIs and RSS endpoints; no logins, no captcha solving, no stealth/proxy modes.
- **Additive schema only:** Added `first_seen TEXT` and `query_performance` table without modifying existing column semantics.
- **Verification:** All endpoints were tested live before integration.
- **Test suite:** 125/125 unit and integration tests passing (`pytest tests/test_brief_11.py` 12/12 passed).

---

## 2. Yield per New Source

| Source | Postings Found | Postings In Scope | Postings Passing Gates | Primary Apply Route / Kind |
| :--- | :---: | :---: | :---: | :--- |
| **Amazon Jobs** (`amazon.jobs`) | 50 | 39 | **8** | `account_portal` (direct apply on amazon.jobs) |
| **Workday CXS** (`*.myworkdayjobs.com`) | 55 | 44 | **4** | `account_portal` (direct apply on Workday) |
| **Arbeitsagentur** (`rest.arbeitsagentur.de`) | 75 | 75 | **15** | `account_portal` (direct apply / jobdetail) |
| **RemoteOK** (`remoteok.com/api`) | 57 | 54 | **17** | `auto` / direct apply link |
| **EURES** (`europa.eu/eures`) | 6 | 6 | **0** | `account_portal` (roles off-function in sample) |
| **VC Boards Expansion** (Point Nine & Accel) | 0 | 0 | **0** | `vc_board` |
| **Total** | **243** | **218** | **44** | — |

---

## 3. Best Passing Examples by Source

### Amazon Jobs (Munich)
1. **Snr Data & AI GTM Specialist, AWS Specialist & P-Industries** — *Amazon* (Score: 67.0)
   - URL: `https://www.amazon.jobs/en/jobs/10567384/snr-data-ai-gtm-specialist-aws-specialist-p-industries`
2. **Senior Solutions Architect, AGS Ukraine** — *Amazon* (Score: 42.0)
   - URL: `https://www.amazon.jobs/en/jobs/10575501/senior-solutions-architect-ags-ukraine`
3. **Senior Manager, AWS Solutions Architecture, AWS Startups** — *Amazon* (Score: 33.6)
   - URL: `https://www.amazon.jobs/en/jobs/10569367/senior-manager-aws-solutions-architecture-aws-startups`
4. **Financial Analyst, Customer Fulfillment Finance** — *Amazon* (Score: 25.6)
   - URL: `https://www.amazon.jobs/en/jobs/10575356/financial-analyst-customer-fulfillment-finance`
5. **Senior Financial Analyst, Customer Fulfillment, Execution Finance** — *Amazon* (Score: 22.8)
   - URL: `https://www.amazon.jobs/en/jobs/10577211/senior-financial-analyst-customer-fulfillment-execution-finance`

### Workday CXS (Munich / Germany Remote)
1. **Solutions Architect, CSP GTM** — *NVIDIA* (Score: 41.6)
   - URL: `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Germany-Remote/Solutions-Architect--CSP-GTM_JR2025076`
2. **Partner Development Manager** — *Autodesk* (Score: 18.0)
   - URL: `https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/Barcelona-ESP/Partner-Account-Manager--French-Speaker--German-as-a-Second-Language-_26WD98029`
3. **Structured Data Foundation Model Engineer, Modern Data Stack** — *NVIDIA* (Score: 1.0)
   - URL: `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Germany-Munich/Structured-Data-Foundation-Model-Engineer--Modern-Data-Stack_JR2028306-1`
4. **Senior MLOps Engineer - DSX Enablement** — *NVIDIA* (Score: 1.0)
   - URL: `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Germany-Remote/Senior-MLOps-Engineer---DSX-Enablement_JR2024830-1`

### Bundesagentur für Arbeit (Munich, English-Filtered)
1. **AI Solution Architect - Agentic AI & Foundation Models (f/m/x)** — *BMW AG* (Score: **112.8**)
   - URL: `https://www.arbeitsagentur.de/jobsuche/jobdetail/12462-1642385-1-S`
2. **Domain Architect Digital Roadmapping and AI Solutions (f/m/div)** — *Infineon Technologies AG* (Score: **63.0**)
   - URL: `https://www.arbeitsagentur.de/jobsuche/jobdetail/12874-1646845-1-S`
3. **Sales Ops and Strategy Analyst, Global Sales operations (GSO)** — *Amazon Web Services EMEA SARL* (Score: **54.0**)
   - URL: `https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003797845-S`
4. **Product Manager AI (w/m/d)** — *Guldberg GmbH* (Score: **48.8**)
   - URL: `https://www.arbeitsagentur.de/jobsuche/jobdetail/13410-k13572.15852-S`
5. **Applied AI Architect – Semiconductor Engineering Solutions (f/m/div)** — *Infineon Technologies AG* (Score: **46.8**)
   - URL: `https://www.arbeitsagentur.de/jobsuche/jobdetail/12874-1641641-1-S`

### RemoteOK (Worldwide / Germany Eligible Remote)
1. **Solutions Engineer** — *Onebrief* (Score: **100.4**)
   - URL: `https://remoteOK.com/remote-jobs/remote-solutions-engineer-onebrief-1137568`
2. **Risk Analyst** — *Pavago* (Score: **53.6**)
   - URL: `https://remoteOK.com/remote-jobs/remote-risk-analyst-pavago-1137548`
3. **Technical Customer Support** — *QuickMail* (Score: **42.0**)
   - URL: `https://remoteOK.com/remote-jobs/remote-technical-customer-support-quickmail-1137506`
4. **SAP RE FX Consultant** — *Yopeso Romania SRL* (Score: **32.0**)
   - URL: `https://remoteOK.com/remote-jobs/remote-sap-re-fx-consultant-yopeso-romania-srl-1137535`
5. **Manager Clinical Data Management** — *EDETEK* (Score: **28.0**)
   - URL: `https://remoteOK.com/remote-jobs/remote-manager-clinical-data-management-edetek-1137481`

---

## 4. Failed, Blocked, or Deprecated Sources & Reasons
Per the brief's hard rules, every endpoint was tested live. The following sources were blocked or failed and were therefore omitted or handled cleanly:

1. **Welcome to the Jungle / Otta:**
   - *Status:* Blocked / Challenge.
   - *Reason:* The public web listing endpoint returns HTTP 202/Cloudflare Managed Challenge. Algolia search credentials embedded in site JavaScript enforce strict `Origin`/`Referer` checks and API rate limits that return HTTP 403 when queried programmatically without browser automation.
2. **Apple Jobs (`jobs.apple.com/api/role/search`):**
   - *Status:* Blocked.
   - *Reason:* Returns HTTP 301 redirecting to an authenticated portal session; direct programmatic JSON search without Apple session cookies returns HTTP 403.
3. **Microsoft Careers:**
   - *Status:* Blocked.
   - *Reason:* `apply.careers.microsoft.com` enforces Akamai Bot Protection (HTTP 403 Forbidden). The legacy `gcsservices.careers.microsoft.com/search/api/v1/search` endpoint returns HTTP 404.
4. **Google Careers (`careers.google.com/api/v3/search`):**
   - *Status:* Retired / 404.
   - *Reason:* The v3 search endpoint is deprecated; Google Careers now renders listings through internal client-side RPCs requiring Google authentication.
5. **VC Boards Tested that Failed Live:**
   - `jobs.cherry.vc`: HTTP 404 Not Found.
   - `jobs.picuscap.com`: Cloudflare Access login redirect (`bitter-salad-a262.cloudflareaccess.com`).
   - `jobs.creandum.com`, `jobs.project-a.com`, `jobs.speedinvest.com`, `jobs.balderton.com`, `jobs.atomico.com`, `jobs.indexventures.com`: `ConnectError` (no active public `jobs.*` CNAME configured).
   - Only `jobs.pointnine.com` (200 OK) and `jobs.accel.com` (200 OK) verified live and were added to `VC_BOARDS`.

---

## 5. Implementation Details by Section

### Section 1: Big-Employer Portals
- Implemented `AmazonJobsScraper` in `scrapers/amazon_jobs.py` querying `https://www.amazon.jobs/en/search.json?city=Munich&country=DEU&result_limit=100`.
- Implemented `WorkdayScraper` in `scrapers/workday.py` querying public CXS endpoints (`/wday/cxs/{tenant}/{site}/jobs`) and detail endpoints for verified tenants: NVIDIA (`wd5`), Autodesk (`wd1`), and Intel (`wd1`).
- All roles from these scrapers are tagged with `apply_kind = 'account_portal'` and `needs_account = 1`.

### Section 2: National and EU Job Feeds
- Implemented `ArbeitsagenturScraper` in `scrapers/arbeitsagentur.py`:
  - Search: `GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs` with `X-API-Key: jobboerse-jobsuche`.
  - Detail: `GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{base64(referenznummer)}`.
  - Filter: Enforces language detection on title + first 500 characters, discarding German-language postings.
- Implemented `EuresScraper` in `scrapers/eures.py`:
  - Search: `POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search`.
  - Filters to Germany (`locationCodes: ["DE"]`) and English language.

### Section 3: Start-up & Remote Boards
- Implemented `RemoteOkScraper` in `scrapers/remote_boards.py` querying `https://remoteok.com/api`.
- Updated `HimalayasScraper` in `scrapers/remote_boards.py` to keep only jobs whose location restrictions include Germany, the EU, or worldwide.
- Expanded `VC_BOARDS` in `scrapers/vc_boards.py` with live verified boards: Point Nine Capital and Accel.

### Section 4: Ecosystem Partners Seeding & Probing
- Created `ecosystem_partners.py` with 36 curated consulting and integration partners for MuleSoft, Boomi, Workato, Salesforce, ServiceNow, HubSpot, and Celonis.
- Implemented `seed_ecosystem_partners()` storing them in the SQLite `companies` table and `probe_ecosystem_partners()` to discover their ATS boards using `BoardProber`.

### Section 5: Remote Location Parsing in Matcher
Updated `matcher.check_gate_location()`:
- **In scope (`location_scope = 'remote-eu'`):**
  `"Remote (Europe)"`, `"Remote - EMEA"`, `"EU timezone"`, `"CET/CEST"`, `"Europe (remote)"`, `"Worldwide"`, `"Anywhere"`.
- **Out of scope:**
  Explicitly rejects single non-German country remotes (`"Remote - Spain"`, `"Remote UK only"`, `"Remote - France"`, `"Remote - Italy"`, `"Remote - US"`, `"Remote - Spain (EU)"`).
- **Worldwide restriction check:**
  Rejects `"Worldwide"` or `"Anywhere"` postings if description states US-only or non-EU residence requirement.

### Section 6: Recall per Title & Zero-Passing Query Suppression
- Added `get_eligible_queries()` in `Database`:
  - Runs best-converting title list first ("Business Systems Analyst", "Enablement", "Partner Manager", "Technical Program Manager", "Implementation Specialist", "Solutions Engineer", etc.).
  - Added synonyms: "Customer Enablement", "Partner Enablement", "Alliance Manager", "Channel Manager", "CRM Analyst", "Product Analyst", "GTM Engineer", "AI Deployment", "Solutions Analyst", "Partner Technical Manager", "Integration Consultant", "iPaaS".
  - Explicitly excludes near-zero titles: "Integration Engineer", "Application Engineer", "ServiceNow", "Talent Acquisition".
  - Tracks query performance in `query_performance` table: stops queries after 2 consecutive runs with 0 passing rows.

### Section 7: Freshness, Rescan & Export Priority
- Added `first_seen TEXT` column in SQLite `jobs` table (migrated additively).
- Implemented `should_poll_board(ats, slug)`:
  - Boards with 3+ passing rows in last 30 days -> poll every 6 hours.
  - Boards with 1-2 passing rows -> poll daily (24 hours).
  - Boards with 0 passing rows -> poll weekly (168 hours).
- Implemented `export_matches_csv()`:
  - Generates `matches.csv` ordering postings under 72 hours old first, sorted by score descending.

---

## 6. Test Suite Verification
A comprehensive test file `tests/test_brief_11.py` was created containing 12 unit and integration tests covering:
1. `test_brief11_remote_locations_in_scope`: All 7 in-scope remote variations pass and assign `location_scope = 'remote-eu'`.
2. `test_brief11_remote_locations_out_of_scope`: All single-country non-German foreign remote strings are rejected.
3. `test_brief11_worldwide_with_us_residence_rejected`: "Worldwide" with US-residence clause is rejected.
4. `test_amazon_jobs_scraper_recorded`: Recorded mock validation for Amazon Jobs.
5. `test_workday_scraper_recorded`: Recorded mock validation for Workday CXS search + detail.
6. `test_arbeitsagentur_scraper_recorded`: Recorded mock validation for Bundesagentur für Arbeit search + detail + English filter.
7. `test_eures_scraper_recorded`: Recorded mock validation for EURES search API.
8. `test_remoteok_scraper_recorded`: Recorded mock validation for RemoteOK API.
9. `test_ecosystem_partners_seeding`: Validates DB insertion of partner companies.
10. `test_recall_queries_priority_and_suppression`: Validates priority ordering, synonym inclusion, exclusions, and 2-run suppression.
11. `test_freshness_board_poll_decision`: Validates 6h vs daily vs weekly polling intervals.
12. `test_matches_csv_export_under_72h_first`: Validates `< 72h` postings ordered first in `matches.csv`.

**Full test suite result:** `125 passed in 85.31s` (0 failures, 100% pass rate).

