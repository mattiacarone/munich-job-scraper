# munichScraper upgrade brief 3 (13 Oct 2026)

Briefs 1 and 2 are implemented (17 tests pass, health check OK). This brief fixes what an audit of `jobs.db` found. Audit numbers: 28,325 rows, 221 passed, 0 duplicates among the passed ones, 0 senior-title leaks. Keep everything that works. No paid services, no LLM calls, never invent facts about Mattia. The Claude pipeline reads `jobs.db` read-only with `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`.

## 1. German gate leaks (HIGH - these cause wasted applications)
Audit found three passed postings that need German:
- Yext "Sales Coordinator (Bilingual - English/German, M/F/X)": the text says "Fluency in English and German (written and spoken)".
- RobCo "(Senior) Robotic Sales Engineer": "You speak German and English fluently".
- Sungrow "Senior Infrastructure & Network Engineer": "German at B2 level is an advantage" is correctly optional, keep that as a pass.
Fix in `matcher.py`:
- Title gate: reject when the title contains `bilingual`, `English/German`, `German/English`, `German-speaking`, `German speaker`, `DACH`, `Deutschsprachig`.
- Mandatory-sentence gate: before matching, strip HTML tags AND decode entities (`&lt;li&gt;`, `&amp;`, `&nbsp;`), split into sentences/list items, then reject if ANY sentence mentioning German/Deutsch contains: fluent, fluently, fluency, native, bilingual, proficient, C1, C2, B2 (unless the same sentence also has advantage/plus/nice to have/bonus/beneficial/preferred/ideally/desirable/von Vorteil/wünschenswert), "speak German", "German speaking", "German-speaking", "excellent/very good/good German", "German (is) required/mandatory/essential/a must", "Deutschkenntnisse" with sehr gut/fließend/verhandlungssicher/erforderlich.
- Unit tests for the three examples above (Yext reject, RobCo reject, Sungrow accept).

## 2. Engineering roles leak through
Passed rows included "C++ Developer", "Backend Java Engineer", "Senior MS Dynamics Business Central Developer", "Sr. Forward Deployed Engineer - FDE (Fullstack...)", "Senior Computer Vision Engineer", "Senior Process Engineer" (hardware). Mattia's profile is integration/consulting/customer-facing; he is NOT a software engineer.
- Reject titles matching `developer|software engineer|backend|back-end|frontend|front-end|full[- ]?stack|java\b|c\+\+|golang|rust\b|devops|sre\b|site reliability|platform engineer|test engineer|qa engineer|computer vision|machine learning|ml engineer|data engineer|data scientist|embedded|firmware|hardware|mechanical|electrical|process engineer|network engineer|infrastructure engineer|cloud engineer|security engineer|robotics? engineer`
  UNLESS the title also contains one of: `solutions?|sales engineer|pre-?sales|customer|success|technical account|implementation|integration|consultant|onboarding|forward deployed|support|enablement|professional services|delivery`.
- Exception list for roles that ARE relevant even though they say Developer: titles containing `MuleSoft|Boomi|Salesforce|ServiceNow|HubSpot|Workato|iPaaS|API` are allowed.
- Tests: "Backend Java Engineer" rejected, "C++ Developer" rejected, "Customer Solutions Engineer, EMEA" accepted, "MuleSoft Developer" accepted, "Forward Deployed Engineer" accepted, "Senior Product Designer" rejected (add `designer` to off-function; "UX writer", "illustrator" too).

## 3. Location leaks
- "Freiburg (Germany)" and "Germany" alone pass when the text merely mentions remote. Rule: a posting passes the location gate only if (a) the location field or title names Munich/a Munich suburb, or (b) the location field says Remote AND (Germany|DACH|EMEA|Europe|EU), or (c) the location field is just "Germany"/"Deutschland" AND the TITLE contains remote/home office. A description that merely says "remote-first" or lists several office cities (Freiburg / San Francisco) must NOT qualify a non-Munich location.
- Test: "Freiburg (Germany)" with "remote-first" in the text is rejected; "Remote - Munich" accepted.

## 4. Stop storing and scoring what can never match
20,363 of 28,325 rows are rejected for location. Do the cheap gates BEFORE any database write or description processing: location -> seniority -> age -> skip-list -> title off-function, and write only a compact rejected-row record (id, source, gate) instead of the full description. This cuts the 4-minute cycle and the DB size. Keep the full record only for rows that pass the location gate.

## 5. LinkedIn detail pages are not working yet
Audit: 33 LinkedIn rows passed, 0 have a description (>300 chars), 0 have `apply_url`, 0 have `ats`. Brief 2 section 4 is not effective.
- Debug with one posting: GET `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}` with a browser User-Agent and `Accept-Language: en`. Print the status code and the first 300 characters; if it is 429, honour the pause logic; if it is 200 but the markers are missing, inspect the HTML and fix the selectors: description is in `div.show-more-less-html__markup` (or `.description__text`), the off-site apply link is in an `<a>` with `data-tracking-control-name` containing `apply` or in `code#applyUrl` / `<!--"https://...-->` comments (decode `externalApply` URLs: take the `url` query parameter and URL-decode it).
- Set `apply_url`, and when its host is greenhouse.io / ashbyhq.com / lever.co / personio.de / workable.com / recruitee.com / smartrecruiters.com set `ats` and `ats_slug`.
- Apply the German gate to the fetched text and reject accordingly.
- Test with a saved HTML fixture (save one real response to `tests/fixtures/linkedin_job.html`).

## 6. Company names (only 102 of 221 have `company_display`)
- Fill `company_display` for every row: ATS rows from the board name API (already cached per slug); Arbeitnow rows use its `company_name`; LinkedIn rows use the card's company text; fallback = slug title-cased. Never leave it NULL. Backfill the existing rows.

## 7. Prober sanity
`discovered_boards.json` grew by 160 boards in one cycle, including slugs that look like false positives (e.g. `front` answered 200 on SmartRecruiters).
- A slug counts only if the API returns at least one job AND at least one of those jobs is located in Germany or is remote, otherwise record it in `probed_companies` as "no Germany jobs" so it is not rescraped every cycle.
- Log each accepted board with company name, ATS, slug, job count.
- Cap discovered boards scraped per cycle to those with a Germany/Munich job in their last scrape (store `last_germany_jobs` count).

## 8. Reliability
- `main.py --health` shows only 1 cycle row although the log shows 8 cycles: make sure every cycle (run-once, rescore and daemon) writes a `cycle_metrics` row.
- Workable returned HTTP 429 during probing: add a per-host rate limit (max 3 requests/second per host) and a retry with 5 s backoff.
- README: add a "What the Claude pipeline reads" section with the exact SQL and the column list.

## 9. Tests and delivery
All existing tests plus the new ones above must pass. Run `python main.py --rescore` and `python main.py --run-once`, then run the audit again and print: rows, passed, passed per source, passed with company_display, passed LinkedIn rows with description and apply_url, number of passed rows matching the German/engineering/location leak patterns (must be 0).
