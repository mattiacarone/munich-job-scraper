# munichScraper upgrade brief 5 (14 Oct 2026) - make the scraper decide what is worth applying to

Briefs 1-4 are in (gates, dedupe, company names, LinkedIn detail fetch, liveness, health, web dashboard). This brief comes from the outcome of the first big run that used the scraper's database as its source. Keep everything that works. No paid services, no LLM calls, never invent facts about Mattia. The Claude pipeline reads `jobs.db` read-only with `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`; keep that contract and the existing columns.

## What the run showed (numbers)
- The scraper had 248 candidates. After the pipeline's own checks 46 were eligible, 39 passed a pre-check, about 31 were submitted in total over two days.
- Eligible roles that could not be sent: 4 behind account-only sites (Salesforce/Workday, NXP/Workday, BESTSECRET/SuccessFactors, Amazon), 9 stopped by captchas (Recruitee x5, BambooHR, Ashby x3), several by questions only Mattia can answer. Gemini matching had already been spent on all of them.
- 70 of the 248 candidates came from Arbeitnow; it produced almost all of the junk (minijob, courier "Paketzusteller", "Grocery Associate", "Stagiair(e) Influencer" internship, "Office Management Assistant - Minijob", "Expert Model Trains", "Open Application", "Talent Community") and every one of its URLs is an aggregator page, not the employer's form.
- 3 passed rows still contain German-fluency text and 1 passed row has a German title. The pipeline's own German scan dropped 47 of 162 fetched postings (29%).
- LinkedIn: 4 passed rows, 0 with an employer `apply_url`.
- The scraper has not run for 13 hours (heartbeat 776 min old).

## 1. Classify how each posting can be applied to (HIGH)
New columns `apply_kind` (TEXT) and `apply_host` (TEXT), filled at scrape time from the final apply URL (see 2) or the ATS:
- `auto` : greenhouse.io, job-boards.greenhouse.io, job-boards.eu.greenhouse.io, lever.co (no visible captcha), personio.de/.com, workable.com, smartrecruiters.com, teamtailor.com, join.com listings that apply without login.
- `captcha` : recruitee.com, bamboohr.com, ashbyhq.com (captcha often shows), any page whose HTML contains `hcaptcha` or `h-captcha`.
- `account` : myworkdayjobs.com / *.wd1-5.myworkdayjobs.com, successfactors / sapsf, careersportal.*, amazon.jobs, jobs.sap.com, oraclecloud.com hcm, taleo, icims, phenom, eightfold.
- `easy_apply` : LinkedIn jobs with no off-site link.
- `unknown` otherwise.
Rules: rows with `apply_kind = 'account'` get `gate_rejected = 'account-only'` (the pipeline cannot apply there). `captcha` rows stay candidates but get a score penalty of -15 and are marked, so the pipeline can sort them after the `auto` ones. Add a small bonus +6 for `auto`. Export `apply_kind` in `exports/candidates_latest.csv`. Test each host class.

## 2. Resolve the real apply link at scrape time (HIGH)
- Arbeitnow: for each Arbeitnow row that passes the cheap gates, GET `<job url>/apply` with redirects followed (httpx, `follow_redirects=True`, 15 s timeout, 3 concurrent) and store the FINAL URL (strip `utm_source`, `ref`, `utm_*` query parameters) in `apply_url`; set `ats`, `ats_slug`, `apply_kind` from it. If the final URL is still on arbeitnow.*, set `gate_rejected = 'no-employer-link'`. Cache results so a job is resolved once. Also add the resolved ATS slug to `data/discovered_boards.json` (new boards = more direct coverage, and these jobs then arrive from the ATS scraper with real descriptions).
- LinkedIn: finish the employer-link extraction. `apply_url` must hold the employer's URL (decode `externalApply`/`safety/go` `url=` parameters, double-decode if needed), never a linkedin.com URL; rows that only offer Easy Apply get `apply_kind = 'easy_apply'`. Fetch details for every LinkedIn row that passes the cheap gates, over several cycles (resume where the last cycle stopped), and persist the description even if the gate later rejects the row.
- Dedupe by resolved `apply_url` (same employer URL = same job, keep the ATS-sourced row).

## 3. One filter, shared with the pipeline (HIGH)
The pipeline's German/off-profile logic caught what the scraper missed. Make `matcher.py` apply the same rules:
- German text: strip HTML tags, decode entities, split into sentences/list items (`<li>`, `<br>`, newlines, `.;!?`); reject when ANY sentence mentioning German/Deutsch contains fluent|fluently|fluency|native|bilingual|proficient|C1|C2|B2|"speak German"|"German speaking"|"German-speaking"|verhandlungssicher|fließend|"sehr gute Deutsch" and does NOT also contain plus|advantage|nice to have|bonus|beneficial|preferred|ideally|desirable|"von Vorteil"|wünschenswert. A "German is a plus" sentence elsewhere must not cancel it. Also reject when the description is written in German (German function words und|der|die|das|wir|sie|ihre|mit|für|bei|von|eine|auf|werden|wird|sind|als|auch|oder|dich|unser|aufgaben|erfahrung|kenntnisse exceed 25 and 1.2x the English words the|and|you|our|with|for|will|are|your|we|to|of|experience|team|work|role).
- Title gate for German words: Handelsvertreter, Zusteller, Berater(in), Mitarbeiter(in), Sachbearbeiter, Kaufmann/Kauffrau, Leiter(in), Referent(in), Entwickler, Vertrieb, Projektleiter(in), Spezialist, Fachinformatiker, Ingenieur(in), Techniker, Fachkraft, selbstständig, Verkäufer.
- Off-profile titles: assistant, assistenz, minijob, mini-job, werkstudent, stagiair*, intern, internship, praktik*, trainee, zusteller, driver, fahrer, grocery, cashier, kassier*, operator, "expert model", "open application", "initiative application", unsolicited, spontan*, "talent community", "talent pool", "office management", cleaner, kitchen, waiter, babysit*, tutor, "model trains". Exception list: titles containing MuleSoft|Boomi|Salesforce|ServiceNow|HubSpot|integration|solutions|customer success|implementation|consultant.
- Required fix list from the audit (must be 0 after `--rescore`): Handelsvertreter title, Agile Robots "New Product Introduction Engineer", Baringa "(Senior) Manager (m/w/d)", Impossiblecloud "(Senior) Sales Manager" German-fluency leaks.
- Write a `data/filters.json` exported from these rules so the Node pipeline can load the same lists later (keys: german_title_words, off_profile_title_words, mandatory_german_patterns, soft_words).

## 4. Metrics that tell you whether a cycle was productive (MEDIUM)
- `cycle_metrics.passed` currently reads 0 for every cycle although 248 rows pass: fix the count (rows with `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL` that were new or newly passing this cycle) and add columns `passed_total`, `by_apply_kind` (JSON), `by_source` (JSON), `exported`.
- `python main.py --health` also prints: rows passed per apply_kind, per source, newest passed row age, and WARNING when no new passing row appeared in 24 hours or the heartbeat is older than 60 minutes.
- Outcome feedback: read `memory/applied_history.csv` (Mattia's applications; columns company,role,status,date,link,source) at the start of each cycle. Build a per-source and per-ats table of how many of the scraper's rows ended with a status starting `Submitted` and how many with `Rejected`; show it in `--health` ("source yield"). Use it to raise the score by +3 for sources/ATS with at least 3 submissions and no captcha, and lower sources whose rows are nearly all unsendable.

## 5. Stop duplicating the pipeline's work (MEDIUM)
- The Claude pipeline rescans the same ~440 ATS boards every run. Add `python main.py --export-candidates --days 14` that prints nothing but writes `exports/candidates_full.csv` (all rows with `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL AND apply_kind != 'account'`, columns key,src,company,title,location,date,url,apply_url,apply_kind,score,description_excerpt) and the posting text files, so the pipeline can read one complete file instead of the incremental `candidates_latest.csv`.
- `data/heartbeat.json` written at the end of each cycle: `{"time": "...", "cycle_seconds": n, "passed_total": n, "newest_posting": "..."}` so the pipeline can decide to skip its own board scan when the heartbeat is younger than 90 minutes.

## 6. Keep it running (MEDIUM)
- README section "Start automatically": give Mattia the exact one-line Task Scheduler command to run `run_daemon.bat` at logon, a second command that removes it, and note that the scraper must be running for the pipeline to be fresh. Do NOT register anything automatically.
- `run_daemon.bat` must write `daemon.log` and restart after a crash (already present: verify).
- On start, if the heartbeat is older than 6 hours, run one full cycle immediately before waiting for the scheduler.

## 7. Prune and speed (LOW)
- 38k of 52k rows are location-rejected. Before inserting, apply the cheap gates (location, seniority, age, off-profile title, skip-list) and store only a compact record (id, source, gate, first_seen) for rejected rows; keep full descriptions only for rows that pass the location gate. Make `--prune` run once a day automatically.
- Log per-source timing in `cycle_metrics.per_source_timing` and skip a source for 3 cycles after two runs over 120 seconds.

## 8. Tests and delivery
New tests (tests/test_brief_5.py): `apply_kind` for each host class; Arbeitnow `/apply` redirect resolved to a Greenhouse URL (mock httpx); an Arbeitnow row left on arbeitnow.* is rejected as `no-employer-link`; Workday URL rejected as `account-only`; the four audit leaks rejected; "Customer Solutions Engineer, EMEA" and "Senior Customer Success Manager" still accepted; `cycle_metrics.passed` equals the audit count; `data/filters.json` contains the keys listed. All existing tests must pass.
Run `python main.py --rescore` then `python main.py --run-once`, then print: rows, passed, passed per source, passed per apply_kind, passed with employer `apply_url`, count of passed rows matching German-text/German-title/off-profile patterns (must be 0), heartbeat time.
