# Munich Tech & Integration Job Scraper (Mattia Carone Edition)

## How to run (5 Nov 2026)

Nothing starts automatically (no autostart task; `install_autostart.ps1` is renamed `.disabled`). You choose:

Simplest way (7 Nov 2026): double-click **Start Scraper**, **Stop Scraper** or **Scraper Status** on the Desktop. Each opens a small window that says whether the scraper is running or stopped (Stop also checks that nothing is left running); press any key to close it. Same commands from a terminal:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 once     # one cycle in this window, then exits
powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 start    # continuous in the background
powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 stop     # stop continuous mode
powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 status   # running? last activity?
```

Changes on 5 Nov 2026 (old files backed up in `backup_2026-11-05/`):
- Arbeitsagentur searches all role-family titles (`scrapers/family_titles.py`), over the last 14 days within 50 km, and fetches details only for unseen postings.
- Himalayas uses the targeted search API, limited to jobs hireable from Germany.
- EURES is off: its region filter is broken and it stored wrong "Munich" locations.
- The location gate accepts "<German city> (Remote)" and decides vague locations (Germany / Europe / EMEA / Hybrid) from the posting text.
- A years ask blocks only at 10+ years, or 5+ with a Senior title (v7).
- Ashby, Jitterbit and Apideck boards were added to the registry.

An automated, local Python web scraping application designed to monitor job opportunities in **Munich, Germany** (and remote roles across Germany), strictly tailored to **Mattia Carone's** application kit, CV, ATS keyword bank, and target role library.

---

## 🎯 Profile & Matching Specification

- **Candidate:** Mattia Carone (Digital Integration & Technology Consultant at Deloitte Netherlands -> Relocating to Munich).
- **Core Direction:** Functional / customer-facing technology consulting with strong integration knowledge; less emphasis on pure hands-on development over time.
- **Language Gate:** 
  - ✅ **English is sufficient** / English-speaking roles accepted.
  - ✅ **German as a plus / optional** accepted.
  - ❌ **Mandatory German fluency strictly filtered out** (roles requiring C1/C2, fluent German, or having German job titles are automatically discarded).
- **Target Roles:** Enterprise Integration Consultant, Solutions Engineer, Solution Consultant, Pre-Sales, Customer Success Manager/Architect, AI Implementation / Agentic AI Consultant, Marketing Technology / HubSpot / Salesforce Consulting.
- **ATS Keyword Bank:**
  - *Integration & iPaaS:* MuleSoft, Anypoint, Boomi, AtomSphere, DataWeave, API-led connectivity, CloudHub 1.0/2.0, iPaaS, RAML.
  - *Enterprise Platforms:* Salesforce, ServiceNow, HubSpot, CRM, SaaS.
  - *AI & Security:* Agentic AI, MCP (Model Context Protocol), API security, Akamai, AI transformation, GenAI.
  - *Consulting & Functional:* Requirements analysis, discovery workshops, stakeholder management, business-to-tech translation, UAT, solution design, pre-sales, customer success.

---

## 🤖 What the Claude Pipeline Reads (`jobs.db`)

The scraper serves as the single source of truth for candidate postings. The Claude application pipeline reads `jobs.db` directly (read-only).

### Exact Candidate Query SQL
A job is considered an active candidate if and only if it passed all qualification gates, scored above zero, has not been closed, and was posted within the last 14 days:

```sql
SELECT 
    id,
    title,
    company,
    company_display,
    location,
    url,
    apply_url,
    ats,
    ats_slug,
    source,
    score,
    matched_skills,
    matched_role,
    matched_categories,
    language_status,
    fit_summary,
    posted_at,
    created_at
FROM jobs 
WHERE gate_rejected IS NULL 
  AND score > 0 
  AND closed_at IS NULL 
  AND (
    posted_at >= datetime('now', '-14 days') 
    OR (posted_at IS NULL AND created_at >= datetime('now', '-14 days'))
  )
ORDER BY score DESC;
```

### Table Schema & Column Definitions

| Column | Type | Description |
|---|---|---|
| `id` | `TEXT` | Primary key (`{source}:{slug}:{postingId}`, `li:{id}`, `arbeitnow:{slug}`, `jobicy:{id}`) |
| `title` | `TEXT` | Job title |
| `company` | `TEXT` | Normalized company name |
| `company_display` | `TEXT` | Human-friendly display name from board API or card text (guaranteed non-NULL) |
| `location` | `TEXT` | Posting location (Munich or verified remote) |
| `url` | `TEXT` | Primary job URL |
| `apply_url` | `TEXT` | Direct off-site ATS application link (decoded from LinkedIn or ATS) |
| `ats` | `TEXT` | Provider name (`greenhouse`, `ashby`, `lever`, `personio`, `workable`, `recruitee`, `smartrecruiters`) |
| `ats_slug` | `TEXT` | ATS board slug |
| `source` | `TEXT` | Ingestion source name |
| `score` | `REAL` | Match score based on Mattia's ATS keyword banks and target roles |
| `matched_skills` | `TEXT` | JSON list of matched keywords |
| `matched_role` | `TEXT` | Primary matched target role category |
| `matched_categories`| `TEXT` | JSON dictionary of points scored per category |
| `language_status` | `TEXT` | `English Friendly` language classification |
| `fit_summary` | `TEXT` | Summary of matched keywords and categories |
| `gate_rejected` | `TEXT` | `NULL` for passed candidates; gate rejection reason or `duplicate_of:<canonical_id>` |
| `rejection_reason` | `TEXT` | Detailed reason why a gate was triggered |
| `is_notified` | `INTEGER` | `1` if alerted via Telegram; `0` otherwise |
| `exported_at` | `TEXT` | ISO timestamp when candidate was exported |
| `posted_at` | `TEXT` | Original posting date from source API/card |
| `created_at` | `TEXT` | ISO timestamp when first ingested |
| `dedupe_key` | `TEXT` | Normalized title + company key for cross-source deduplication |
| `closed_at` | `TEXT` | Timestamp when board removed the posting or aggregator exceeded 21 days (`NULL` = active) |
| `user_status` | `TEXT` | User application status (`new`, `saved`, `applied`, `archived`) |
| `applied_at` | `TEXT` | Timestamp when marked as applied |
| `notes` | `TEXT` | Personal notes for this job application |

> [!IMPORTANT]
> Always include `closed_at IS NULL` in queries. When an ATS board removes a posting or an aggregator posting exceeds 21 days, `closed_at` is set to the closure timestamp.

### Live Memory Paths
The scraper dynamically loads live application history and ATS registries on each cycle from:
- `C:\Users\Mattia\Desktop\Job Application material\memory\applied_history.csv`
- `C:\Users\Mattia\Desktop\Job Application material\memory\ger_skip_list.csv`
- `C:\Users\Mattia\Desktop\Job Application material\memory\ats_registry.json` (merged with `data/discovered_boards.json`)
- `C:\Users\Mattia\Desktop\Job Application material\memory\linkedin_queries.txt`

*(Falls back gracefully to local `data/` directory if memory folder is absent).*

---

## 📱 Telegram Alerts Setup

To receive notifications for high-scoring fresh postings:

1. **Create your Bot:**
   - Open Telegram and message `@BotFather`.
   - Send `/newbot`, choose a display name and username (e.g., `MattiaMunichJobBot`).
   - Copy the HTTP API token provided.

2. **Obtain your Chat ID:**
   - Start a chat with your new bot (send `/start`).
   - Message `@userinfobot` to view your numeric Telegram user ID, or check `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`.

3. **Configure Environment:**
   - Create or edit `.env` in the project root:
     ```env
     TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
     TELEGRAM_CHAT_ID=123456789
     ```

4. **Alert Filtering Rules:**
   - Only canonical rows (after cross-source deduplication).
   - `score >= 60.0`.
   - Posted within the last 3 days (`posted_at` <= 3 days).
   - Not closed (`closed_at IS NULL`).
   - Max 10 alerts per cycle to prevent spam.

---

## 🚀 Start automatically

> [!IMPORTANT]
> The scraper must be running continuously for the Claude application pipeline to receive fresh candidate postings and updated board discoveries.

### 1. Register Task at Windows Logon
To automatically start `run_daemon.bat` in the background whenever you log on to Windows, run this one-line command in PowerShell or Command Prompt:

```cmd
schtasks /create /tn "MunichScraper" /tr "c:\Users\Mattia\munichScraper\run_daemon.bat" /sc onlogon
```

### 2. Remove Automatic Startup Task
To remove the scheduled logon task:

```cmd
schtasks /delete /tn "MunichScraper" /f
```

### 3. Daemon Behavior
- `run_daemon.bat` redirects all stdout and stderr logs to `daemon.log`.
- Automatically restarts after a 60-second cooldown if the process exits or encounters a transient failure.


---

## 🚀 CLI Commands

### Run a Single Scrape Cycle
```powershell
python main.py --run-once
```

### Health & Metrics Check
Displays the status of the last 10 scraping cycles, new rows discovered, error rates, and failure warnings:
```powershell
python main.py --health
```

### Recalculate Scores & Gates
Recalculates scoring and filtering across all existing SQLite rows without fetching new network data:
```powershell
python main.py --rescore
```

### Interactive Web Dashboard & Application Tracker
Launch the local web UI at `http://127.0.0.1:8000`:
```powershell
python main.py --web
# Or double-click run_dashboard.bat
```

### Authoritative Liveness Verification
Check candidate jobs against employer ATS endpoints & detect closed/expired positions:
```powershell
python main.py --check-liveness
```

### Run Continuous Daemon
```powershell
python main.py
```

### Run Test Suite
```powershell
pytest -v
```
