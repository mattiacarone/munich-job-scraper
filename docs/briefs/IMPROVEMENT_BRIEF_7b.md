# munichScraper brief 7b (16 Oct 2026): stop defaulting unknown locations to Munich

## Problem (audit of jobs.db, read-only)
- 45 rows that pass every gate have a non-German site and are not in Munich, but carry `location = 'Munich, Germany'`. The scraper seems to fill "Munich, Germany" as a default when it cannot read the real location.
- Examples: arbeitnow.fr "Account Manager Montpellier" (Deliveroo) and "Account Solutions Architect ... France" (CoreWeave); Teamtailor career sites in France (volkswagenfinancialservices.vwfs.fr: 16 rows, posted in French) and Sweden (jobb.mfj.se, karriar.prima.se, posted in Swedish).
- In today's pipeline run 12 of 20 Gemini-approved roles were French/Swedish postings and had to be dropped afterwards, so they cost Gemini matching time and a slot in the batch.

## Fix, in this order
1. **Never default a location to Munich.** If the source has no location, store `location = NULL` and `location_source = 'unknown'`. A row with an unknown location may pass only if the posting text itself names Munich/München (or Germany-remote).
2. **Arbeitnow country sites:** for arbeitnow.fr / .nl / .es / .it / .co.uk etc., use the posting's own location field; if the title or text names a non-German city (Montpellier, Paris, Lyon, Madrid, ...), or says "France"/"Spain"/..., set that as the location (gate `location` rejects it).
3. **Teamtailor:** read the location from the job's JSON-LD (`jobLocation.address`) or the "Locations" block on the job page, not from the company's default. Career sites on a non-German domain (.fr, .se, .nl, .es, .it, .pl, .dk) need an explicit Munich/Germany location in the posting to pass.
4. **Posting language gate:** reject (gate `language`) postings whose text is mostly French, Swedish, Dutch, Spanish, Italian or Polish (simple function-word ratio, like the existing German check). English or German-plus-English postings are unchanged.
5. `--rescore` and print: passing rows with a non-.de/.com TLD before/after (expected: 45 -> near 0), and passing Munich rows before/after.
6. Tests: an arbeitnow.fr "Account Manager Montpellier" row is rejected; a vwfs.fr Teamtailor French posting is rejected; a Teamtailor posting on a .com site with "Munich, Germany" in JSON-LD passes; a row with no location whose text says "based in our Munich office" passes.

## Do not change
Everything from briefs 1-7, the German gate, skip list, aggregator blocks, and the `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL` contract.
