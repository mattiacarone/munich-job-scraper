# munichScraper brief 10 (21 Oct 2026): spend the Firecrawl credits where the application pipeline loses roles

Briefs 1-9 stay as they are (apply brief 9 first if it is not applied yet). Contract unchanged: `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`.

## Evidence (jobs.db and 3 apply runs, 19-21 Oct 2026)
- 555 postings pass the gates. 196 of them (35%) have a description under 300 characters, so scoring and Gemini matching work from almost nothing for a third of the pool.
- About 86 passing rows come from aggregators and still have NO employer apply link: weworkremotely 70 (63 route `unknown`), germantechjobs 11, wellfound 5, englishjobsingermany 4, jobicy 3. The pipeline drops or parks them (no-employer-link, or the aggregator page shows a bot check), so they never become applications. Today's enrichment (`enrich_candidates` limit 15, 25 credits per cycle) resolves only a few per cycle.
- `apply_kind='captcha'` is predicted for 108 Ashby rows. In practice Ashby forms were submitted without any visible captcha in the last three runs (Camunda, Clickhouse, Haut AI, True Talent, Constructor, Benepass-style forms), so the prediction is far too pessimistic and makes those roles look unreachable.
- Firecrawl search runs `queries_per_cycle=2` of 8 discovery queries, so every employer-ATS query gets covered only every fourth cycle.
- Spend is not directed: credits go to whichever rows come first, including rows the pipeline already prepared or sent.

## Hard rules for everything below
- Only public pages. Respect robots.txt (use `tools/robots_check.py` style logic or Firecrawl's own policy). Do NOT enable stealth, residential-proxy or any "bypass bot protection" mode, never solve or work around a captcha, never log in. If a page returns a challenge or 403, record `apply_url_status='blocked'` and move on.
- Never write secrets or keys to logs. Never touch Mattia's accounts.

## 1. Spend credits by value, not by order
Sort every Firecrawl job (description fetch, apply-link resolution) by this priority and stop when the cycle budget is spent:
1. passing rows with `score >= 55` that lack a description (<300 chars) or lack a resolved employer link, newest first;
2. rows with score 45-54 in the same state;
3. everything else (never).
Skip rows that were already prepared or sent: read `C:\Users\Mattia\Desktop\Job Application material\memory\applied_history.csv` (status starts with "Submitted") and the folder names in `C:\Users\Mattia\Desktop\Job Application material\ready\` (company and title prefix) and do not spend a credit on them. Use the existing `resolved_apply_urls` table and `firecrawl_cache` so one URL is never paid for twice.

## 2. Budget
- Make the budget configurable: env `FIRECRAWL_MAX_CREDITS_PER_CYCLE` (default 60) and `FIRECRAWL_MAX_CREDITS_PER_DAY` (default 400). Print remaining credits at the start of every cycle (use the CLI's credit-usage command if available) and refuse to exceed the daily cap.
- Add a one-off backfill command `python main.py --firecrawl-backfill [--max-credits 150] [--dry-run]` that works through the priority list from section 1 until the credits or the list run out. `--dry-run` prints how many credits it would use and which rows, without spending any. Run the dry run first and show the estimate.

## 3. Resolve the 86 aggregator rows
For rows from weworkremotely, germantechjobs, wellfound, englishjobsingermany, jobicy, remotive and arbeitnow without `apply_url`: scrape the posting page with `formats=["markdown","links"]`, take the first external link whose anchor text or surrounding text says apply (apply now, apply for this position, jetzt bewerben, view on company site), unwrap redirect wrappers (utm, `linkedin.com/safety/go?url=`, `l.facebook`, `out.` URLs), and store it in `apply_url` with `apply_url_source='firecrawl'`. Then run the existing `classify_apply_route` on it so `apply_route` and `apply_kind` update. If the employer is on a known ATS (greenhouse, ashby, lever, personio, smartrecruiters, teamtailor, recruitee, workable), also record the board slug in `data/discovered_boards.json`. If no external link exists, set `apply_url_status='none'` so the pipeline stops retrying the row.

## 4. Fill the 196 short descriptions
For passing rows with `LENGTH(description) < 300`, fetch clean markdown from the employer page (not the aggregator when an employer link exists) and store it in `description` (cap 20,000 characters, strip navigation, cookie banners and footers). Mark `description_source='firecrawl'`. Then rescore those rows so `score`, `matched_skills` and the German gate use the full text. The application pipeline reuses the stored description when it is longer than 200 characters, so this also saves it a fetch per role.

## 5. Recalibrate `apply_kind` from real results
The application pipeline writes one folder per role under `C:\Users\Mattia\Desktop\Job Application material\ready\<company>_<title>\` with `result.json` (`status`: SUBMITTED, CAPTCHA, NEEDS-HUMAN, NO-FORM, ACCOUNT-ONLY, ERROR ...) and `meta.json` (`url`, `apply_url`). Read them read-only, derive the route from the host (greenhouse.io, ashbyhq.com, lever.co, personio, smartrecruiters, join.com, recruitee, teamtailor, workable), and compute per route: tried, SUBMITTED, CAPTCHA, ACCOUNT-ONLY. Rules:
- If a route has at least 5 tries and at least 50% SUBMITTED, set `apply_kind='auto'` for its rows (this fixes Ashby) unless the page was already flagged as having a visible captcha challenge.
- If a route has at least 5 tries and 0 SUBMITTED with mostly CAPTCHA or ACCOUNT-ONLY, keep `captcha` or `account`.
- Write the table to `data/route_success.json` and print it in the run report.
Never delete a row because of its `apply_kind`; it is a hint.

## 6. Discovery coverage
- Run all 8 `FIRECRAWL_DISCOVERY_QUERIES` every 24 hours instead of 2 per cycle (keep a `last_run` per query in `scraper_metadata`), within the credit budget.
- Add 4 queries for roles the pipeline now also applies to (Mattia's "not a clean fit" strategy): `Business Analyst`, `Technical Account Manager`, `Solutions Consultant`, `Project Manager` on the same ATS domains, each with `Munich OR "Germany" OR "Remote Europe"`.
- Add `site:` queries for `jobs.eu.greenhouse.io`, `*.recruitee.com`, `*.softgarden.io` (softgarden has 71 passing rows already).
- Use Firecrawl map on career pages listed in `data/discovered_boards.json` that were last polled more than 7 days ago, only to list new job URLs (cheap), never to crawl whole sites.

## 7. Report
After every cycle write `data/firecrawl_report.json` with: credits used and remaining, rows enriched (description and apply link), links resolved by source, rows marked blocked or none, new boards found, and the route success table from section 5. Also print a 6-line summary. The application pipeline reads this file in its retrospective.

## Tests
- Priority: a score-70 row with no description is fetched before a score-50 row; a row whose company+title is in `ready\` or `applied_history.csv` is skipped without a credit.
- Aggregator row with an "Apply for this position" link to `https://boards.greenhouse.io/acme/jobs/123?utm_source=x` stores the clean URL and route `greenhouse`; a page with only a Cloudflare challenge sets `apply_url_status='blocked'` and spends no retry credits.
- `--firecrawl-backfill --dry-run` prints an estimate and spends nothing.
- Route table: 6 Ashby results with 5 SUBMITTED turns Ashby rows to `auto`.
- Daily cap: the 401st credit is refused.

## Report back to Mattia
Credits spent, rows with a newly resolved employer link, rows with a newly filled description, number of Ashby rows changed to `auto`, and the new count of passing rows without an apply route.
