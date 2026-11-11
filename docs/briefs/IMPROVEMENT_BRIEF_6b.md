# munichScraper brief 6b (14 Oct 2026) - urgent fix after brief 6

Result of the first pipeline run on the brief-6 database: 37 eligible roles, 0 applications sent, about USD 3.50 of tailoring wasted.
- 24 of the 37 were **Jobgether** postings (jobs.lever.co/jobgether/...). Jobgether is a staffing/aggregator company that re-posts other companies' remote jobs; applying there does not reach the employer, and none of its forms submitted.
- 12 were **WeWorkRemotely** pages (weworkremotely.com/remote-jobs/...): aggregator pages without the employer's form, several US-only ("Customer Success Associate (Denver, CO)", "Netlify ... Denver, CO").
- Only 1 real Munich employer role remained (Proxima Fusion).

Fix, in this order:
1. **Block staffing/aggregator re-posters** as companies, whatever source they come from: jobgether, crossover, toptal, braintrust, turing, mercor, outlier, remotasks, dataannotation, and any company whose posting text says it recruits "on behalf of" / "for our client". Gate code `aggregator`. Remove `jobgether` from `data/discovered_boards.json` (lever) and never re-add it.
2. **WeWorkRemotely / Remotive / Himalayas / Working Nomads**: a row is a candidate ONLY if (a) the posting explicitly allows Germany/DACH/EU/Europe/EMEA (US/Americas/"Denver, CO"/time-zone-only-US rows are rejected) AND (b) the employer's apply URL was resolved (follow the board's "Apply" button; it must land on the employer's site or ATS, not on the board) and stored in `apply_url`. Otherwise `gate_rejected = 'no-employer-link'` or `'location'`.
3. Same rule for Wellfound, germantechjobs, englishjobsingermany, StepStone and XING: no resolved employer `apply_url` = not a candidate (it can still go to `exports/manual_leads.csv`).
4. Re-run `--rescore`; print passed per source before/after. Expected: WeWorkRemotely and Jobgether drop to (near) 0 passed; the rest unchanged.
5. Tests: a Jobgether Lever posting is rejected; a WWR posting with "USA only" is rejected; a WWR posting with an employer Greenhouse link and "Europe" eligibility is accepted with that link in `apply_url`.
