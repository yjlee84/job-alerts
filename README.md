# Job Alerts Pipeline

This is a `v1` local pipeline for collecting job postings from target career pages into one normalized CSV.

## Layout

- `config/sources.csv`: target sources to scan
- `data/job_listings.csv`: normalized job listings output
- `data/fetch_runs.csv`: source-level fetch log
- `config/filters.csv`: source-level rule configuration for Stage 1 filtering
- `reports/`: generated Markdown reports
- `scripts/fetch_jobs.py`: fetch and normalize job links from each source
- `scripts/filter_jobs.py`: rewrite `data/job_listings.csv` to only the currently matched candidate jobs
- `scripts/build_alert_report.py`: build the daily Markdown alert report and send it as an email notification
- `scripts/sync_job_tracker.py`: append only matched new jobs into `reports/job_tracker.csv`
- `assets/`: source resume assets used for resume compilation and application prep
- `outputs/`: per-job generated application materials
- `scripts/prepare_application.py`: generate tailored resume and cover letter drafts for a tracked job or for every tracker row whose `Status` is `Review`

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
python3 scripts/filter_jobs.py
python3 scripts/sync_job_tracker.py
python3 scripts/prepare_application.py --all-review
python3 scripts/build_alert_report.py
```

## Application Prep

The GPT-backed application prep workflow uses the existing `description` column in `data/job_listings.csv` instead of separate job-description files. GPT sees the full `.tex` files, but is only allowed to rewrite named `GPT` blocks inside those templates.

Source files:

- `assets/cv.tex`: base resume source used to build the application resume PDF
- `assets/citations.bib`: LaTeX bibliography source used during resume compilation
- `assets/letter.tex`: base cover letter source used to build the application cover letter PDF

Generated files:

- `outputs/<job_id>/source/resume.tex`
- `outputs/<job_id>/resume.pdf`
- `outputs/<job_id>/source/cover_letter.tex`
- `outputs/<job_id>/cover_letter.pdf`

Usage:

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY
python3 scripts/prepare_application.py --job-id <job_id>
```

Email notification setup:

- `JOB_ALERTS_EMAIL_TO`: recipient email address
- `JOB_ALERTS_EMAIL_FROM`: sender email address
- `JOB_ALERTS_SMTP_HOST`: SMTP host, for example `smtp.gmail.com`
- `JOB_ALERTS_SMTP_PORT`: SMTP port, usually `587`
- `JOB_ALERTS_SMTP_USERNAME`: SMTP username
- `JOB_ALERTS_SMTP_PASSWORD`: SMTP password or app password

Notes:

- The script reads the matching job description from `data/job_listings.csv`.
- It does not store generated document metadata in `reports/job_tracker.csv`.
- GPT receives the full LaTeX templates but may only rewrite named `GPT` blocks inside them.
- Tailored resume and cover-letter outputs must compile successfully and stay within a one-page PDF limit.
- The analysis and tailoring steps remain constrained by `assets/cv.tex` and `assets/letter.tex`.
- `.env` is project-local and ignored by git.

## Filter Format

Edit `config/filters.csv` with one row per source rule:

- `source_id`: source identifier from `config/sources.csv`
- `enabled`: `1` or `0`
- `allowed_grades`: pipe-separated allowed grades for sources that expose grades

Example:

```csv
source_id,enabled,allowed_grades
oecd_smartrecruiters,1,PAL1|PAL2|PAL3|PAL4|PAL5|PAL6|T5|T6
```

## Login Sync Flow

The repository workflow keeps manual runs via `workflow_dispatch` and also runs on each push to `main`.

The intended login-driven sync is:

1. macOS loads the local LaunchAgent at login
2. the local sync script waits until GitHub and `origin` are reachable
3. it commits local `reports/job_tracker.csv` changes, or creates an empty trigger commit if the tracker is unchanged
4. it runs `git pull --rebase`
5. it pushes to `main`
6. the GitHub Actions workflow runs the pipeline and commits updated outputs
7. the local sync script waits for that pipeline commit, then pulls it back down

This keeps `job_tracker.csv` flowing from local to GitHub first, then brings the generated listings and reports back from GitHub to local.

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
5. writes `reports/alerts_YYYY-MM-DD.md` for the current UTC run date and removes older alert report files so only one remains

The tracker sync:

1. reads `data/job_listings.csv`
2. reads `reports/job_tracker.csv`
3. checks whether each `Job ID` already exists in the tracker
4. appends only unseen jobs
5. uses default values `Status = Review` and `Next Action = Apply`

The review materials builder:

1. reads `reports/job_tracker.csv`
2. selects rows whose `Status` is `Review`
3. removes `outputs/<job_id>/` folders for jobs no longer marked `Review`
4. reads the matching job description from `data/job_listings.csv`
5. asks GPT for JSON block replacements against the `GPT` regions in `assets/cv.tex`
6. renders `outputs/<job_id>/source/resume.tex`
7. compiles `outputs/<job_id>/resume.pdf` and rejects outputs over one page
8. asks GPT for JSON block replacements against the `GPT` regions in `assets/letter.tex`
9. renders `outputs/<job_id>/source/cover_letter.tex`
10. compiles `outputs/<job_id>/cover_letter.pdf` and rejects outputs over one page

The filter step:

1. reads `config/filters.csv`
2. reads `data/job_listings.csv`
3. applies the source-specific grade rules
4. rewrites `data/job_listings.csv` to keep only matched candidate jobs

The intended daily workflow:

1. run `fetch_jobs.py` once per day
2. run `filter_jobs.py` to turn `job_listings.csv` into the filtered candidate list
3. run `sync_job_tracker.py` to append only unseen candidate jobs into the tracker
4. run `prepare_application.py --all-review` to keep `outputs/` aligned to current `Review` rows and create `resume.pdf` and `cover_letter.pdf` for each of them
5. run `build_alert_report.py`
6. `build_alert_report.py` sends the latest Markdown report as the email body
7. the report shows:
   `New Jobs`: jobs added to the tracker today
   `Review Jobs`: jobs currently marked `Review` in `job_tracker.csv`

## Notes

- `v1` is intentionally simple and dependency-free.
- It uses generic HTML link extraction, so some sites will work better than others.
- The next step is to add source-specific parsers for platforms like Greenhouse, Lever, SmartRecruiters, and Workday.
- SmartRecruiters sources can enrich rows with structured location, posting date, and full posting description.
- After collection is reliable, AI scoring can be added on top of `data/job_listings.csv`.
