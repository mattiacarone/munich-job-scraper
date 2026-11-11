# munichScraper upgrade brief 6 (14 Oct 2026) - more sources, more Munich roles

Goal: find MORE English-friendly roles in Munich (and remote-Germany) by adding job boards and ATS types the scraper does not cover yet. Keep everything from briefs 1-5. Every new source writes into the same `jobs` table with the same columns and goes through the same gates (location Munich/remote-DE, seniority, German, off-profile, duplicate, apply_kind). No paid services, no LLM calls, no logins, no captcha solving, no account creation. Be polite: per-host rate limit (max 1 request / 2 s for job boards, 3 / s for ATS APIs), a real browser User-Agent, `Accept-Language: en`, backoff on 429/403, and stop a source for the cycle after 3 consecutive errors.

Why: in the last run only 83 NEW candidate postings appeared in 24 hours across the 440 ATS boards, Arbeitnow and LinkedIn; after the gates 14 were eligible. The big German boards are not covered at all.

Tested from Mattia's PC on 14 Oct 2026 (plain HTTP, no browser):
| source | result |
|---|---|
| StepStone `https://www.stepstone.de/jobs/<query>/in-muenchen?radius=30&action=facet_selected%3blanguage%3ben` | 200, server-rendered, ~25 postings per page with links `/stellenangebote--...`; the `language;en` facet returns English-language postings only |
| XING `https://www.xing.com/jobs/search?keywords=<q>&location=M%C3%BCnchen` | 200, server-rendered JSON in the HTML with `"title"` fields (e.g. "Customer Success Manager (all levels)") |
| englishjobsingermany.com `/jobs?location=munich` | 200, React app; job data not in plain links (look for an embedded JSON state or the API the page calls) |
| germantechjobs.de `/jobs/all/Munich` | 200, React shell only (find its JSON API) |
| Remotive API `https://remotive.com/api/remote-jobs` | 200, 15 jobs, 13 open to EU/Germany |
| Himalayas `https://himalayas.app/jobs/api` | 200 |
| Bundesagentur für Arbeit Jobsuche API `rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/app/jobs` with `X-API-Key: jobboerse-jobsuche` | 403 (key or endpoint changed: check the current docs at bund.dev / jobsuche.api.bund.dev before giving up) |
| join.com `/api/public/jobs` | 404 (no public API; company pages `join.com/companies/<slug>` are server-rendered) |
| wellfound.com `/location/munich` | 200 (check for embedded `__NEXT_DATA__`) |

## 1. StepStone (HIGH - largest German board)
- Scraper `scrapers/stepstone.py`. For each query in `linkedin_queries.txt` (rotate like LinkedIn: max 40 queries per cycle, resume next cycle), fetch `https://www.stepstone.de/jobs/<slugified-query>/in-muenchen?radius=30&action=facet_selected%3blanguage%3ben&page=<1..3>`.
- Parse the job cards from the server-rendered HTML (or the embedded JSON state if present): title, company, location, posted date (relative dates like "vor 2 Tagen" -> date), job URL (`/stellenangebote--...-<id>-inline.html`). Job id = `stepstone:<id>`.
- Fetch the detail page only for rows that pass the cheap gates (max 80 per cycle): description text, and the apply target. StepStone "Schnellbewerbung"/"Jetzt bewerben" on stepstone.de needs a StepStone login -> `apply_kind = 'account'` (reject as account-only); if the button links to an employer site/ATS, store that URL in `apply_url` and classify it.
- English-only facet first; German-language postings still go through the German gate.

## 2. XING Jobs (HIGH)
- Scraper `scrapers/xing.py`: `https://www.xing.com/jobs/search?keywords=<q>&location=M%C3%BCnchen&radius=30&page=<n>`; extract the embedded JSON (look for the script tag holding the search results: objects with `title`, `companyInfo`/`company`, `location`, `activatedAt`/`publishedAt`, `url`/`slug`, `id`). Job id = `xing:<id>`.
- Detail page for gated rows: description and the external apply link if any (`applyUrl`, `externalUrl`); XING-internal apply needs login -> `apply_kind = 'account'`.

## 3. English-first boards (MEDIUM)
- englishjobsingermany.com: find the JSON the React app loads (browser dev tools network tab equivalent: look in the HTML for `__NEXT_DATA__`, `window.__INITIAL_STATE__`, or `fetch(` URLs) and use it; filter Munich + remote Germany.
- germantechjobs.de: same approach (it is a SPA; find the API it calls, often `/api/jobs?location=Munich`).
- Wellfound: parse `__NEXT_DATA__` on `https://wellfound.com/location/munich` and role pages; Munich + remote DE only.
- Each board: job id `<board>:<id>`, follow the apply link to the employer ATS when present (that also feeds `data/discovered_boards.json`).

## 4. Remote boards for remote-Germany roles (MEDIUM)
- Remotive (`/api/remote-jobs`), Himalayas (`/jobs/api?limit=100&offset=...`), WeWorkRemotely RSS (`https://weworkremotely.com/categories/remote-customer-support-jobs.rss`, `.../remote-sales-and-marketing-jobs.rss`, `.../remote-management-and-finance-jobs.rss`), Working Nomads (`https://www.workingnomads.com/api/exposed_jobs/`), Jobicy (already present).
- Keep ONLY postings whose location/eligibility explicitly includes Germany, DACH, EU, Europe or EMEA and whose title passes the gates; tag `location = 'Remote (Germany eligible)'`.

## 5. More ATS types (HIGH - direct employer forms, best for automatic applying)
Add fetchers + prober support (same pattern as the existing ATS scrapers, slugs in `data/discovered_boards.json`):
- BambooHR: `https://<slug>.bamboohr.com/careers/list` (JSON) -> job URL `https://<slug>.bamboohr.com/careers/<id>`; `apply_kind = 'captcha'`.
- Teamtailor: `https://<slug>.teamtailor.com/jobs` (server-rendered list; job pages `/jobs/<id>-<title>`), also try `https://career.<company-domain>/jobs` when a careers page says "Powered by Teamtailor"; `apply_kind = 'auto'`.
- Softgarden (very common in Germany): `https://<slug>.softgarden.io/de/widgets/jobs` or `https://<slug>.softgarden.io/api/rest/v3/frontend/jobslist/...` (check which responds); `apply_kind = 'auto'` unless a captcha is seen.
- Join.com: company pages `https://join.com/companies/<slug>` (server-rendered list); `apply_kind = 'auto'` if the apply form works without login, else `'account'`.
- Breezy: `https://<slug>.breezy.hr/json`; Pinpoint: `https://<slug>.pinpointhq.com/postings.json`; Factorial: `https://<slug>.factorialhr.com/` (job list page); Comeet: company page JSON if present.
- Workday listing endpoints (`https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs`, POST) for large Munich employers ARE readable without login but applying needs an account: add them only with `apply_kind = 'account'` and keep them out of the candidate set (store as "manual-apply leads" in a separate export `exports/manual_leads.csv` with the link, so Mattia can apply himself to the best ones).

## 6. Find more Munich companies to probe (MEDIUM)
- Seed list of Munich employers from public directories (respect robots.txt): munich-startup.de database (startup profiles with website), UnternehmerTUM / TUM Venture Labs portfolio pages, "Top Munich startups" lists, and every company name seen on StepStone/XING/LinkedIn/Arbeitnow.
- For each company: guess slugs (existing rule) for ALL supported ATS types (greenhouse, ashby, lever, personio, workable, recruitee, smartrecruiters, bamboohr, teamtailor, softgarden, join, breezy, pinpoint). Also fetch the company's own `/careers`, `/jobs`, `/karriere` page once and detect the ATS from links/iframes ("Powered by ..." or the ATS host). Save hits in `data/discovered_boards.json` only when the board has at least one Germany/remote job (brief 5 rule).
- Limit 300 companies per cycle; remember probed companies for 30 days.

## 7. Dedupe across all sources
The same job will now appear on StepStone, XING, LinkedIn and the company ATS. Keep the existing `dedupe_key` (normalised company + title) and the canonical-row priority, extended: company ATS > English boards > StepStone/XING > LinkedIn > aggregators. Also dedupe on the resolved `apply_url`.

## 8. Metrics and tests
- `--health` shows postings / new / passed per source and per day for the new sources, plus how many passed rows have `apply_kind = 'auto'`.
- Tests with saved HTML/JSON fixtures for StepStone, XING, one English board, BambooHR, Teamtailor, Softgarden; dedupe across StepStone + ATS for the same job; account-only StepStone posting rejected; remote board posting without EU/Germany eligibility rejected.
- Run `--rescore` and `--run-once`, then print: new postings per source, passed per source, passed per apply_kind, total passed (compare with the 263 passed before this brief).
