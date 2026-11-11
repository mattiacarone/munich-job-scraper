# munichScraper brief 12 (31 Oct 2026): keep running, find the missing companies, stop passing jobs that cannot be applied to

## Evidence (jobs.db cycles 43-45 and the application pipeline on 30 Oct 2026)
- The daemon stopped after cycle 45 (started 2026-10-30 11:55 UTC, heartbeat 12:43 UTC). No python process is running and nothing new has been written since. daemon.log also shows "skipped: maximum number of running instances reached (1)" when a cycle runs longer than the 20-minute interval.
- A manual probe of the public ATS APIs found 26 Munich boards in 2 minutes that the scraper had no rows for or had not added, for example: ashby clera, rasa, finto, growthprotocol, jua, peec, sensorfact, statista, payrails, alephalpha; greenhouse superchat, konux; lever iqm; personio appliedai, eqs-group, juna, ki-group, miles-mobility, userlane, climedo, foodforecast, tanso, unternehmertum; recruitee bluebird, kigroup; workable agile-robots. They are listed in the pipeline's memory/ats_registry.json.
- LinkedIn: per_source_errors {"linkedin": 1} in most cycles and 1 passed posting, while the pipeline's own LinkedIn guest scan returned about 2,850 of 4,926 postings in one run and produced most of the sends on 30 Oct.
- Time spent per cycle on low-yield sources: stepstone ~620 s (1,800 postings, ~3 passed), xing ~510 s (1,200 postings, ~6 passed), arbeitnow ~3,500 postings for ~22 passed.
- Passed postings that could never be applied to: weworkremotely 74, remoteok 13, arbeitsagentur 27. On 30 Oct every one of these failed at the form stage (employer link hidden behind the board's login, or an image captcha "Dargestellte Zeichen") after a CV and letter had already been written.
- After the scraper's gates, the pipeline's model still blocked 9 of 34 assessed postings for German, 4 for seniority (5+ years) and 1 for a fixed-term contract, all of which are visible in the posting text.
- 317 passed postings have no description (or under 300 characters). About 37,700 rows have no first_seen. Prolific Academic alone created 437 duplicate rows. Clera hosts 272 jobs (a recruiting marketplace for many companies), mostly founder, robotics and engineering roles.

## Hard rules (unchanged from briefs 9-11)
Only public pages and public JSON endpoints. Respect robots.txt and rate limits. Never log in, create accounts, solve or work around a captcha, or use stealth or proxy modes. Never write keys to logs. Keep the existing tests passing and add a test for each change below.

## 1. Keep the daemon alive
- Add a watchdog (Windows Task Scheduler task or a small supervisor loop) that restarts the daemon when daemon.heartbeat is older than 30 minutes, and logs each restart.
- Write the heartbeat during a cycle (every source), not only at the start, so a long cycle is not mistaken for a dead one.
- Set the cycle interval to the real cycle length (or start the next cycle when the previous one ends) instead of skipping runs with "maximum number of running instances reached".

## 2. Find the missing companies every day
- Add a daily company-discovery step: for each name in a seed list, try the slug variants (name, name-with-dashes, name without legal suffix) on Greenhouse, Ashby, Lever, Personio (xml), Workable (widget API), Recruitee, Teamtailor and SmartRecruiters. A 404 means wrong slug or wrong ATS, not "no jobs". Save hits to companies; save misses to probed_companies so they are retried weekly, not every cycle.
- Seed lists (public pages): UnternehmerTUM / TUM Venture Labs portfolios, munich-startup.de company directory, Y Combinator companies located in Munich, Clera's client company pages (getclera.com/jobs/<company>), investor portfolios already in brief 11.
- Import the 26 slugs listed in the evidence above right away.

## 3. LinkedIn: fix or hand over
- Either fix the LinkedIn guest source (it errors every cycle), or remove it from the scraper and leave LinkedIn to the pipeline's discover.mjs, which already scans all 133 role-family queries with rotation. Do not keep a source that costs time and returns nothing.

## 4. Drop or slow the low-yield sources
- Move stepstone and xing to a weekly poll (or remove them); they cost ~19 minutes per cycle for under 10 passed postings.
- Poll arbeitnow daily instead of every cycle.

## 5. Reject postings that cannot be applied to, at intake
- For aggregator sources (weworkremotely, remoteok, arbeitsagentur, englishjobs.de, arbeitnow, germantechjobs, jobicy, himalayas, workingnomads, remotive): resolve the employer's own apply URL. If there is none (login wall, captcha, email-only without an address), set gate_rejected = 'no-employer-link' and do not export it.
- arbeitsagentur: postings whose only apply path is the BA portal with the image captcha are rejected.
- Never pass a posting whose apply_url is on the aggregator's own domain.

## 6. Catch blocks in code before export
Run these on the full description (English and German wording) and set gate_rejected with the matched quote saved:
- German required: "fluent German", "fluency in German", "German (C1|C2|native)", "verhandlungssicher", "fließend Deutsch", "sehr gute Deutschkenntnisse", "Deutsch (C1|C2)", "German-speaking" in the title. "German is a plus / nice to have / von Vorteil" does NOT block.
- Seniority: "(5|6|7|8|10)\+? years" with experience/Erfahrung nearby; founder / ex-founder / CTO titles.
- Contract: "fixed-term", "befristet", "maternity cover", "Elternzeitvertretung", "(12|18|24) months", "temporary", "part-time", "Werkstudent", "Praktikum".

## 7. Data quality
- Fetch the description at intake for every posting that passes the gates; do not export one without a description of at least 300 characters.
- Set first_seen on every row (backfill missing values with created_at).
- Add a per-company cap on duplicate rows (prolificacademic, true-talent-solutions) and dedupe by normalized company + title + location before insert.
- Clera: keep only postings whose title matches the role families below and whose location is Munich or remote EU.

## Role families (unchanged from 30 Oct 2026)
Only these are exported: integration / implementation / consulting; solutions / forward-deployed engineer; technical customer success; project / product / ops (technical project or program manager, product owner/manager, RevOps / CRM / GTM systems, marketing ops).

## Report back
Write REPORT_BRIEF_12.md with: the test results, the first full cycle after the change (duration, per-source postings and passed counts, rejected gates by reason, new boards found), and the number of exported postings without a description or without an employer apply link (both should be 0).
