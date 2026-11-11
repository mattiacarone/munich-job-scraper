# munichScraper brief 11 (28 Oct 2026): reach more employers that match Mattia's profile

## Evidence (application pipeline, 1,860 matched roles from 68 runs, 12-27 Oct 2026)
- Only 13% of matched roles are good matches (PASS). The biggest losses: German required 24%, a different profession (hardware, SAP, engineering) 28%.
- Company job boards turn into good matches about twice as often as LinkedIn: Recruitee 50%, Lever 21%, Ashby 19%, Greenhouse 12%, LinkedIn 11%.
- Best-converting role families (title keywords): Business Systems Analyst, Enablement, Partner Manager / Partner Solutions, Technical Program / Project Manager, Implementation Manager / Specialist, Solution(s) Engineer / Consultant, Technical Account Manager, Customer Success Engineer, Marketing Operations / Analyst, AI Solutions / AI Deployment, Forward Deployed.
- Near-zero: "Integration Engineer" (it finds hardware integration), "Application Engineer", "ServiceNow" (mostly German-speaking), Talent Acquisition.
- Scope: Munich region (on-site or hybrid) plus fully remote roles open to someone living in Germany (EU/EMEA remote). English working language; German A1-A2 only.

## Hard rules (unchanged from briefs 9-10)
Only public pages and public JSON endpoints. Respect robots.txt and rate limits. Never log in, create accounts, solve or work around a captcha, use stealth or proxy modes. Never write keys to logs. Additive columns only; do not change existing column meanings.

## 1. Big-employer portals that list jobs publicly (listing needs no account; Mattia creates accounts himself when he applies)
- Workday: the public CXS endpoint `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with `{"searchText":"","locations":[...],"limit":20,"offset":0}`. Find tenants for Munich employers that use Workday (start with any in `companies` whose careers page links to myworkdayjobs.com; verify each tenant live). Filter location Munich/Germany/remote EMEA in code.
- SAP SuccessFactors career sites (`career{N}.successfactors.eu/career?company=...` RSS or the `jobs.{company}.com` JSON feeds where public), Eightfold (`/api/apply/v2/jobs?domain=...&location=Munich`), Phenom (`/widgets` search JSON) and Avature, where the endpoint is public.
- Own portals with public JSON: amazon.jobs (`/en/search.json?loc_query=Munich`), Google (`careers.google.com/api/v3/search/?location=Munich`), Microsoft (`gcsservices.careers.microsoft.com/search/api/v1/search?lc=Munich`), Apple (`jobs.apple.com/api/role/search`).
- Mark all of these `apply_kind = account_portal` so the pipeline hands them to Mattia instead of trying to submit.

## 2. National and EU job feeds
- Bundesagentur für Arbeit Jobsuche API (public: `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs`, header `X-API-Key: jobboerse-jobsuche`), `wo=München`, `umkreis=30`, `veroeffentlichtseit=14`, one call per title from the best-converting list. Keep only postings whose text is English (language detection on title + first 500 characters).
- EURES (`europa.eu/eures`) public search, Germany + Munich, English-language postings only.

## 3. Start-up and remote boards not yet covered
- Welcome to the Jungle / Otta (public Algolia search used by its site), Munich and remote Europe.
- Getro and Consider boards of more investors with Munich or DACH portfolios: Project A, Cherry Ventures, Speedinvest, La Famiglia, Picus, 468 Capital, Lakestar, Point Nine, btov, Atomico, Index Ventures, Accel, Balderton, Northzone, Creandum (verify each board URL first; add to `VC_BOARDS`).
- Y Combinator Work at a Startup (public listings filtered to remote EU or Munich), startup.jobs, Remote OK API, Working Nomads API, EU Remote Jobs, Landing.jobs, The Hub (remote EU).
- Keep Himalayas, but keep only jobs whose location restrictions include Germany, the EU or worldwide.

## 4. Ecosystem partners (his platform skills are the strongest signal)
- Read the public partner directories of MuleSoft, Boomi, Workato, Salesforce (AppExchange consultants, Germany), ServiceNow (partner finder, Germany), HubSpot (solutions partners, Germany) and Celonis. Store each partner company in `companies`, then detect its careers ATS with the existing prober. These firms hire integration and implementation consultants, his best fit.

## 5. Remote location parsing
- Treat as in scope: "Remote (Europe)", "Remote - EMEA", "EU timezone", "CET/CEST", "Europe (remote)", "Worldwide", "Anywhere" (unless the text says US-only or names a non-EU country as required residence). Out of scope: remote limited to one non-German country ("Remote - Spain", "Remote UK only").

## 6. Recall per title
- For each source that supports search, run the best-converting title list first (above), and stop adding new queries for a title after two runs with zero passing rows.
- Add synonyms: "Customer Enablement", "Partner Enablement", "Alliance Manager", "Channel Manager", "CRM Analyst", "Product Analyst", "GTM Engineer", "AI Deployment", "Solutions Analyst", "Partner Technical Manager", "Integration Consultant", "iPaaS".

## 7. Freshness and re-scan
- Poll company boards with 3+ passing rows in the last 30 days every 6 hours; others daily; boards with no passing row in 30 days weekly.
- Store `first_seen` per posting; postings under 72 hours old go first in `matches.csv`.

## Report back to Mattia
Per new source: postings found, postings in scope, postings passing your gates, and the 5 best examples (title, company, link). List sources that failed or were blocked and why. Add tests for each new scraper (one recorded response per source) and for the remote-location rule in section 5.
