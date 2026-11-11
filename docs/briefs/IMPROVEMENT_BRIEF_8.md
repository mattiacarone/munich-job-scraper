# munichScraper brief 8 (17 Oct 2026): add fully remote EU/EMEA roles (Mattia's decision)

## Why
The Munich pool is about 30 new fitting roles a day once the backlog is used up. Mattia (EU citizen, moving to Munich) decided on 17 Oct 2026 to also target **fully remote roles open to candidates anywhere in the EU/EMEA**, in English.

## Change the location gate
1. Keep everything that passes today (Munich area on-site/hybrid, Germany-remote).
2. ALSO pass a row when the posting is **fully remote** AND open to a region that includes Germany: "Remote - Europe", "Remote (EU)", "EMEA remote", "Remote, any EU country", "Remote within CET +/- 2 hours", "Europe (Remote)". Store `location_scope = 'remote-eu'` (Munich rows `munich`, Germany-remote `remote-de`).
3. Still REJECT (gate `location`):
   - remote restricted to one other country ("Remote - UK", "Remote, Spain only", "must reside in Poland", "US remote", "Americas", "APAC");
   - hybrid or on-site anywhere outside the Munich area (Berlin, Hamburg, Amsterdam, London, Paris ... even if the title says "remote-friendly");
   - "remote" postings that require a residence/work permit Mattia cannot have (he is an EU citizen, so EU work rights are fine; UK/US/CH need sponsorship -> reject).
4. Read the full posting text, not just the location field: many boards write "Remote" in the location and "candidates must be based in the UK" in the text.
5. New sources worth enabling now that remote EU counts (verify URLs first): We Work Remotely, Remotive, Himalayas and Working Nomads *Europe/EMEA* filters, EU Remote Jobs, Remote OK (Europe tag), Wellfound remote Europe. Brief 6b still applies: a row only counts if the employer's own apply URL is resolved; staffing/aggregator re-posters (Jobgether, Crossover, Toptal, rocken.jobs ...) stay blocked.
6. Seniority gate: also reject titles with Senior/Sr./Staff/Lead when the posting asks for 5+ years (Mattia has 3+ years; target mid and associate).

## Report and tests
- `--rescore`, then print passed rows by `location_scope` before/after, and the number of new remote-eu rows from the last 7 days.
- Tests: "Remote - Europe" Ashby posting passes as remote-eu; "Remote (UK only)" fails; "Hybrid - Berlin" fails; a posting with location "Remote" and text "must be based in the United States" fails; "Senior Solutions Engineer, 6+ years" fails; "Solutions Engineer, 3+ years, Remote EMEA" passes.

## Do not change
Briefs 1-7b, German gate, skip list, aggregator blocks, posting-language gate, and the `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL` contract.
