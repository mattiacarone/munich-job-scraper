# munichScraper brief 9 (19 Oct 2026): learn from real outcomes, gate earlier, resolve apply links

Apply brief 8 first if it is not applied yet. Briefs 1-8 stay as they are.

## Evidence (85 applications sent 12-19 Oct, replies read from Gmail)
- About 25% got any reply, 14 were rejections within 1-7 days, 1 interview (Tangent, an entry-level Customer Success role). The rejected roles were mostly poor fits that slipped through: Content Manager, Social Media Manager, Procurement Manager, Business Development Representative, Revenue Operations Manager, Inside Sales (Portuguese/Spanish), Product Engineer, C++ product owner, ERP project manager, Quantum Integration Engineer.
- In roles that scored 50+ the same gaps repeat: German required (16 of 61), a years-of-experience ask (12 of 61), deep AWS (8 of 61), Kubernetes/Docker/DevOps (3). Gemini and preflight catch these AFTER the scraper has already fetched and handed them over, which wastes fetches, Gemini quota and tailoring time.
- Hand-off blockers: LinkedIn postings whose real application link was never resolved (Publicis, YO AI Labs x2, Animore) and account-only portals (Workday, SuccessFactors, JOIN, Phenom) that Mattia only discovers at apply time.

## 1. Close the loop: read the outcomes
`C:\Users\Mattia\Desktop\Job Application material\memory\outcomes.csv` (columns date,company,title,status,note; status is Replied, Interview, Rejected, Offer or Withdrawn) and `...\memory\applied_history.csv` (columns company,role,status,date,link,source) are written by the apply pipeline. Read them, read-only, at every scoring run:
- Build a title-keyword weight table: for each normalised title word or bigram, count applications sent vs Rejected, Replied/Interview. With at least 3 samples, a word with 0 positive outcomes and 3+ rejections gets a score penalty (-10 per word, cap -25); a word with an Interview or Offer gets +10. Never apply this to sources or companies, only title terms and role families.
- Role-family prior, applied unless the table disagrees: boost implementation, solutions/solution consultant, integration, technical consultant, customer success (technical), technical account manager, delivery, onboarding/implementation consultant, business analyst (systems), pre-sales engineer (junior). Penalise marketing, social media, content, community, procurement/purchasing, business development representative, inside sales, recruiting/HR, finance/accounting, hardware or embedded engineering, pure software engineering.
- Print the 15 most penalised and 15 most boosted terms in the run report.

## 2. Gate earlier, with a gate code and the quote
Add these gate codes (store the matched quote in `gate_reason`; rows with a gate stay in the DB for audit):
- `german_required`: English-language postings that require German: "fluent German", "German (C1|C2|native|business[- ]level|written and spoken)", "Deutschkenntnisse (erforderlich|sehr gut|verhandlungssicher)", "excellent command of German", "native German speaker". Do NOT gate "German is a plus / nice to have / advantageous". Mattia speaks A1-A2.
- `years_required`: parse the minimum years the posting asks ("5+ years", "at least 6 years", "minimum of 4 years", "3-5 years" = minimum 3). Store the number in a new nullable column `years_required`. Gate when the minimum is 6 or more, or 5 or more together with Senior/Sr./Staff/Lead/Principal in the title. Mattia has 3+ years and targets mid and associate roles. A "preferred" or "nice to have" years mention never gates.
- `contract`: fixed-term/befristet, temporary, maternity or parental cover, part-time/Teilzeit, internship/working student, freelance-only. Mattia wants permanent full-time. Store `contract_type` (permanent, fixed_term, part_time, intern, freelance, unknown).
- `deep_stack`: the posting makes one of these central and not just listed: Kubernetes/Terraform/Linux administration, SAP ABAP, embedded C/C++, kernel, ML model training, SOC/pentest, mainframe. Use the existing niche gate wording if one exists.
- `clearance`: security clearance (SUEG, Sicherheitsueberpruefung, SC/DV, "German citizenship required").

## 3. Resolve the real application link (biggest hand-off blocker)
- For every LinkedIn row, try to resolve the external apply URL: on the public job page look for the off-site apply link (`applyUrl`, `data-tracking-control-name="public_jobs_apply-link-offsite"`, or the `href` of the "Apply" button) and unwrap `linkedin.com/safety/go?url=...`. Save it in `apply_url` (the pipeline already prefers `apply_url`). Follow at most 3 redirects; never log in, never solve a captcha, never use cookies.
- If the company has an ATS board we already know (Greenhouse, Ashby, Lever, Personio, SmartRecruiters, Teamtailor, Recruitee, Workable), search that board's public JSON by title and use its URL.
- Add a column `apply_route`: greenhouse, ashby, lever, personio, smartrecruiters, teamtailor, recruitee, workable, linkedin_easy, amazon_jobs, workday, successfactors, phenom, join, taleo, icims, email, company_site, unknown. Rows routed to workday, successfactors, phenom, join, taleo or icims get `needs_account = 1` (do not gate them, the pipeline just parks them for Mattia).

## 4. Freshness
Replies often arrive within hours and early applicants are read first. Keep `posted_at` accurate (first-seen time when the board gives no date) and add `age_hours` to the run report. Score +8 for under 48 hours, +4 for under 7 days. Re-check liveness of anything older than 14 days before handing it over (brief 7 does this for 22+ days; use 14).

## 5. Dedupe against what was already sent
Read `applied_history.csv` (status starting with "Submitted"), skip any row whose normalised company plus the first 14 characters of the normalised title match, and cap each company at 3 sent applications in total (Amazon exempt). Report skipped counts.

## 6. Additive columns (the pipeline reads them if present, never breaks if absent)
`years_required INTEGER`, `contract_type TEXT`, `apply_route TEXT`, `needs_account INTEGER`, `german_required INTEGER`, `title_weight REAL` (the adjustment from section 1). Do not change the contract `gate_rejected IS NULL AND score > 0 AND closed_at IS NULL`.

## Report and tests
- `--rescore`, then print rows passed before and after, grouped by new gate code, plus 10 example titles for each gate so Mattia can spot false positives.
- Tests: "Fluent German required" gates; "German is a plus" passes; "5+ years" in a Senior title gates; "3-5 years" passes; "Temporary, 6 months" gates; a LinkedIn guest page with an off-site apply link resolves to the Greenhouse URL; a title containing "Social Media Manager" with an earlier Rejected outcome is penalised; "Implementation Consultant" is boosted.

## Do not change
Briefs 1-8, the German-in-title gate, skip list, aggregator blocks, posting-language gate, location gates and the DB contract above.
