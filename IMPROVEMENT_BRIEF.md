# munichScraper upgrade brief (from the Claude "Apply Munich" pipeline, 13 Oct 2026)

Goal: make munichScraper the ALWAYS-ON DISCOVERY LAYER for Mattia Carone's Munich job search. It polls every 20 minutes, keeps the SQLite DB, alerts on strong fresh matches, and exports clean candidates. Tailoring, CV/letter writing and form submission stay in the Claude pipeline (`C:\Users\Mattia\Desktop\Job Application material\runs\tools\pipeline\run.mjs`), which will read this project's export. Do NOT generate cover letters here.

Keep the existing architecture (pydantic config, scrapers/ package, Database, ProfileMatcher, Notifier, apscheduler, --run-once / --rescore). Python 3.12, httpx, no paid APIs, no LLM calls.

## Data now in `data/` (read these, do not edit them)
- `ats_registry.json` - ~440 verified company career boards: `{"greenhouse": [slugs], "ashby": [...], "lever": [...], "personio": [...], "workable": [...], "recruitee": [...], "smartrecruiters": [...]}`
- `linkedin_queries.txt` - 176 title queries (one per line)
- `ger_skip_list.csv` - companies known to require German (columns company,scope,...; scope "all" = skip)
- `applied_history.csv` - already applied/reviewed (columns company,role,status,date,link,source) - never re-surface these
- `verified-career-facts-2026-10-10.md`, `unsupported-cv-claims-2026-10-10.md` - the only true facts about Mattia (for scoring context only)

## Required changes

### 1. Sources (scrapers/)
Add one scraper per ATS that iterates ALL slugs in `data/ats_registry.json` (concurrency ~12, timeout 25 s, skip failures, log ok/failed counts):
- greenhouse: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` (title, location.name, updated_at, absolute_url, content HTML)
- ashby: `https://api.ashbyhq.com/posting-api/job-board/{slug}` (jobs[].title, location, secondaryLocations[].location, isRemote, publishedAt, jobUrl, descriptionPlain)
- lever: `https://api.lever.co/v0/postings/{slug}?mode=json` (text, categories.location, categories.allLocations, workplaceType, createdAt ms, hostedUrl, descriptionPlain + lists)
- personio: `https://{slug}.jobs.personio.de/xml` (fallback `.com`) - <position><id><name><office><createdAt>, url `https://{slug}.jobs.personio.de/job/{id}`, description from <jobDescriptions>
- workable: `https://apply.workable.com/api/v1/widget/accounts/{slug}` (shortcode, title, city, country, telecommuting, published_on, url)
- recruitee: `https://{slug}.recruitee.com/api/offers/` (offers[].title, city, country, remote, created_at, careers_url, description)
- smartrecruiters: `https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&country=de` (content[].name, location.city, releasedDate, url `https://jobs.smartrecruiters.com/{slug}/{id}`)
- keep Arbeitnow (all pages until `links.next` is null) and Jobicy.
- LinkedIn guest search: `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={q}&location=Munich%2C%20Bavaria%2C%20Germany&f_TPR=r1209600&start={0,10,..40}` for each line of `linkedin_queries.txt`; parse `<li>` cards (jobPosting:ID, base-search-card__title, base-search-card__subtitle, job-search-card__location, datetime). 3 workers, 2.6-3.8 s random delay, on HTTP 429 pause ALL workers 75 s; stop LinkedIn after 4 pauses. Run LinkedIn at most every 6 hours (it throttles), the ATS scrapers every cycle.
- Job id = `{source}:{slug}:{postingId}` (LinkedIn: `li:{id}`) so the Claude pipeline can dedupe.

### 2. Filters (matcher.py) - hard gates, in this order
1. Duplicate: company+title (normalised: lowercase, strip "(m/w/d)", "(f/m/d)", "(all genders)" etc., remove non-alphanumerics) in `data/applied_history.csv` -> skip.
2. Location: MUNICH ONLY. Accept Munich/München and suburbs (Garching, Unterföhring, Ismaning, Aschheim, Feldkirchen, Planegg, Martinsried, Oberhaching, Gilching, Hallbergmoos, Unterschleißheim, Poing, Grünwald, Pullach, Neubiberg, Haar, Freising, Erding, Ottobrunn, Taufkirchen, Germering, Dachau, Starnberg) and "remote" when tied to Germany/DACH/EMEA/Europe. REJECT Berlin, Hamburg, Frankfurt, Stuttgart, Cologne, Düsseldorf etc. when Munich is not also listed.
3. Seniority: reject titles with Director, Head of, VP, Vice President, Chief, Principal, Staff, Lead (except "Lead Generation"), Team Lead, Engineering Manager, Intern, Internship, Working Student, Werkstudent, Praktikum, Trainee, Ausbildung, Thesis. "Senior" is allowed.
4. German: reject when (a) the title contains DACH or German words (m/w/d alone is fine), (b) the description is written in German (count German function words und|der|die|das|wir|sie|ihre|mit|für|bei|von|eine|auf|werden|wird|sind|als|auch|oder|dich|deine|unser|aufgaben|erfahrung|kenntnisse vs English the|and|you|our|with|for|will|are|your|we|to|of|experience|team|work|role; German if >25 and >1.2x English), or (c) any sentence mentioning German/Deutsch matches fluent|native|business fluent|C1|C2|B2|verhandlungssicher|fließend|muttersprach|sehr gute Deutsch|excellent German|German (is) required|mandatory|a must - UNLESS that same sentence contains plus|advantage|nice to have|desirable|bonus|beneficial|preferred|von Vorteil|wünschenswert|ideally. A "German is a plus" sentence elsewhere must NOT cancel a mandatory sentence.
5. Company on `ger_skip_list.csv` with scope "all" -> skip.
6. Off-function titles (pure software engineer/developer, backend/frontend/full-stack, data engineer, ML engineer, scientist, researcher, hardware/embedded/firmware/mechanical/electrical, accountant, legal, payroll, nurse, driver, warehouse, account executive, BDR/SDR) -> skip.

### 3. Scoring (after the gates)
Keep the weighted keyword bank and target roles from `config.py`, plus: +4 if posted <= 3 days ago, +2 if <= 7 days; -8 if the description asks for 6+ years, -3 for 5 years; -1.2 per mention of python/java/golang/rust/c++/kubernetes/terraform (cap -8); -2 per quota/cold call/prospecting mention. Extra tracks Mattia has real experience in: marketing (marketing, SEO, content, campaign, growth, HubSpot, WordPress, Webflow, website, CMS, e-commerce, brand: +3.5), HR (HR, people operations, talent, recruiting, HRIS: +3), Italian/Spanish market roles (italian, spanish, italy, spain, iberia, southern europe, latam: +5).

### 4. Export for the Claude pipeline
After every cycle write `exports/candidates_latest.csv` with columns exactly: `key,src,company,title,location,date,url,score,description_excerpt` for jobs that passed all gates, posted in the last 14 days, not yet exported before (track `exported_at` in the DB). Also write the full description to `exports/jd/{normalised key}.txt` (normalise: lowercase, keep a-z0-9 only, first 80 chars) - the Claude pipeline reads posting text from there.

### 5. Alerts
Notifier: send only jobs with score >= 60 and posted <= 3 days, max 10 per cycle; message = title, company, location, score, URL. Telegram/Slack/Discord stay optional (empty config = log only).

### 6. Remove
- `generate_application.py` and the `applications/` output: the master cover letter template conflicts with the rules (generic opener "I am writing to express my strong interest", unverified claims such as "several years", requirements analysis, workshops, pre-sales). Move the file to `legacy/` instead of deleting it.

### 7. Keep it honest and testable
- `python main.py --run-once` must print a funnel: postings per source, rejected per gate (dup/location/seniority/german/skip-list/off-function), passed, exported.
- `python main.py --rescore` re-applies gates and scoring to the DB.
- Add `tests/test_matcher.py` with at least: German "plus" sentence does not cancel a mandatory sentence; Berlin-only rejected; "Director" rejected; "Senior Customer Success Manager" accepted; German-language description rejected.
- Do not add any paid service or LLM call. Do not invent facts about Mattia anywhere.
