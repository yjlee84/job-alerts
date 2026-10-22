# Daily job applications

GitHub Actions runs the pipeline daily at **09:00 Europe/Berlin**, with automatic
summer/winter time handling. The Mac can be closed. The local login application
runner is disabled; there are no login or periodic Mac submission runs.

The workflow discovers jobs, applies `config/filters.csv`, updates
`reports/job_tracker.csv`, prepares missing tailored documents, fills the OECD
Easy Apply form in a cloud Chromium browser, submits valid applications, saves
confirmed receipts, and updates the tracker. Company confirmation emails are
sufficient; this project’s duplicate submitted-application emails are disabled.
Separate needs-attention emails remain enabled for blocked or failed attempts. A manual workflow Run now button
is also available. Scheduled starts can be delayed by GitHub.

## Familiar project records

- `reports/job_tracker.csv`: application status, application date, follow-up action
  and the job description. Manual tracker changes on main are retained.
- `outputs/<job_id>/resume.pdf` and `cover_letter.pdf`: per-job tailored documents.
- `outputs/<job_id>/job_description.txt`: saved description for interview preparation.
- `outputs/<job_id>/source/`: editable tailored TeX sources.

The cloud runner commits these outputs back to main, as the earlier project did.
Existing PDFs and historical job descriptions are retained rather than deleted
when a job leaves Review. Already-applied and closed postings are skipped even
when tracking parameters or the listing slug change.

## Private cloud state

The repository is public. Profile information, reference contacts, screening
answers, browser session state and submission receipts are therefore encrypted
before being saved in `cloud-state.enc` on the `application-state` branch. The
encryption key is a GitHub Actions secret, with a backup at
`.local/cloud/state-key` on this Mac. These records are never published as plain
text or uploaded as public Actions artifacts.

A submission intent and the exact materials reach durable cloud storage **before
Submit is clicked**. A positive portal confirmation plus a successful application
response is required before marking Applied. Interrupted or uncertain attempts
never automatically resubmit. Confirmed receipts recover interrupted tracker
updates; pending exception emails retry without resubmitting. Later manual Interview or
Rejected statuses are preserved. SMTP acknowledgement failures can cause a
repeated exception email, but do not cause a repeated application.

To download cloud receipts and private interview archives into this project:

```bash
.venv/bin/python scripts/cloud_state.py download
```

Private answers and receipts then appear under
`.local/applications/<job_id>/interview/`. Public documents remain in `outputs/`.
Keep `.local/cloud/state-key`; it is required to decrypt the downloaded state.
Do not reseed cloud state from an old local tracker: the deploy script refuses
to overwrite an existing cloud application ledger.

## Browser behavior and exceptions

The worker uses ordinary Playwright Chromium in the cloud. It does not spoof a
browser identity, rotate proxies, or bypass CAPTCHAs or access restrictions.
Blocked access, changed forms and unknown factual answers produce a distinct
needs-attention email and never an application-confirmation email. The cloud
workflow includes a probe mode that only opens the public Easy Apply form and
checks visibility; it never uploads documents or submits an application.

The personal profile supplies verified facts. GPT drafts free-text answers from
the job description and resume. The hiring-team message uses an email format
and the confirmed signature, without a blank line after Kind regards. The form
worker’s API ledger is capped at $0.03 per job across reruns; existing CV and
cover-letter generation has separate API usage. Failed or unknown paid requests
retain their reserved budget. Cloud runner/storage usage is separate from API
usage.

## Development and deployment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-browser.txt
.venv/bin/python -m playwright install chromium --only-shell
.venv/bin/python -m unittest discover -s tests -v
```

`config/sources.csv` enables sources. `config/filters.csv` currently matches OECD
PAL1–PAL6 and T5–T6. `.env` holds local OpenAI/SMTP credentials; GitHub Actions
uses repository secrets. The initial cloud deployment is provisioned with
`scripts/deploy_cloud.py`, followed by pushing the workflow and running probe
mode. The production workflow serializes scheduled and manual runs to prevent
concurrent submissions. A failed encrypted checkpoint stops any later submit
click or form-answer API call.

The earlier supervised Safari worker remains available for explicit local work.
Its Junior Policy Analyst trial on 22 October 2026 returned a confirmed portal
receipt, updated the tracker and sent a confirmation email. Cloud browser
compatibility is verified separately through the deployed workflow.
