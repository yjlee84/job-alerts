# Job Alerts Pipeline

This is a `v1` local pipeline for collecting job postings from target career pages into one normalized CSV.

## Layout

- `config/sources.csv`: target sources to scan
- `data/job_listings.csv`: normalized job listings output
- `data/fetch_runs.csv`: source-level fetch log
- `reports/`: generated daily Markdown reports
- `scripts/fetch_jobs.py`: fetch and normalize job links from each source
- `scripts/build_alert_report.py`: build the daily Markdown alert report
- `scripts/sync_job_tracker.py`: append only new jobs into `reports/job_tracker.csv`

## Source Format

Edit `config/sources.csv` with one row per source:

- `source_id`: short stable identifier
- `company`: display name
- `careers_url`: page to scan
- `parser_type`: use `links` for the current `v1`
- `enabled`: `1` or `0`

Example:

```csv
source_id,company,careers_url,parser_type,enabled
oecd,OECD,https://example.com/careers,links,1
```

## Run

```bash
python3 scripts/fetch_jobs.py
python3 scripts/sync_job_tracker.py
python3 scripts/build_alert_report.py
```

## GitHub Actions Schedule

The repository workflow keeps manual runs via `workflow_dispatch` and also runs once per day on GitHub Actions.

- `05:00 UTC` during `April-October`
- `06:00 UTC` during `January-March` and `November-December`

This is intended to approximate `07:00` Paris time across summer/winter time. Around daylight-saving transition dates, GitHub Actions may be off by one hour for a few days because cron is UTC-only.

The script:

1. reads `config/sources.csv`
2. fetches each enabled URL
3. extracts likely job links and titles
4. appends new rows into `data/job_listings.csv`
5. records fetch status in `data/fetch_runs.csv`

The alert builder:

1. reads `data/job_listings.csv`
2. reads `reports/job_tracker.csv`
3. writes `New Jobs` from rows whose `Date Added` is today
4. writes `Review Jobs` from rows whose `Status` is `Review`

The tracker sync:

1. reads `data/job_listings.csv`
2. reads `reports/job_tracker.csv`
3. checks whether each `Job ID` already exists in the tracker
4. appends only unseen jobs
5. uses default values `Status = Review` and `Next Action = Apply`

The intended daily workflow:

1. run `fetch_jobs.py` once per day
2. run `sync_job_tracker.py` to append only new jobs into the tracker
3. run `build_alert_report.py`
4. the report shows:
   `New Jobs`: jobs added to the tracker today
   `Review Jobs`: jobs currently marked `Review` in `job_tracker.csv`

## Notes

- `v1` is intentionally simple and dependency-free.
- It uses generic HTML link extraction, so some sites will work better than others.
- The next step is to add source-specific parsers for platforms like Greenhouse, Lever, SmartRecruiters, and Workday.
- SmartRecruiters sources can enrich rows with structured location, posting date, and full posting description.
- After collection is reliable, AI scoring can be added on top of `data/job_listings.csv`.
