#!/usr/bin/env python3

from __future__ import annotations

import csv
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

from prepare_application import DOTENV_PATH, _load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
JOB_LISTINGS_PATH = DATA_DIR / "job_listings.csv"
JOB_TRACKER_PATH = REPORTS_DIR / "job_tracker.csv"
DEFAULT_EMAIL_TO = "iyongjae6@gmail.com"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _count_jobs_in_section(markdown_text: str, heading: str) -> int:
    lines = markdown_text.splitlines()
    in_section = False
    count = 0
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == heading
            continue
        if in_section and line.startswith("### "):
            count += 1
    return count


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _inline_format(text: str) -> str:
    escaped = escape(text)
    escaped = escaped.replace("**", "\0")
    parts = escaped.split("\0")
    for index in range(1, len(parts), 2):
        parts[index] = f"<strong>{parts[index]}</strong>"
    return "".join(parts)


def _markdown_to_html(markdown_text: str) -> str:
    blocks: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
        list_items = []

    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush_list()
            continue
        if stripped.startswith("# "):
            flush_list()
            blocks.append(f"<h1>{_inline_format(stripped[2:].strip())}</h1>")
            continue
        if stripped.startswith("## "):
            flush_list()
            blocks.append(f"<h2>{_inline_format(stripped[3:].strip())}</h2>")
            continue
        if stripped.startswith("### "):
            flush_list()
            blocks.append(f"<h3>{_inline_format(stripped[4:].strip())}</h3>")
            continue
        if stripped.startswith("- "):
            list_items.append(_inline_format(stripped[2:].strip()))
            continue
        flush_list()
        blocks.append(f"<p>{_inline_format(stripped)}</p>")

    flush_list()
    content = "\n".join(blocks)
    return f"""\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Job Alerts</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f1ea;color:#1f2933;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
    <div style="max-width:720px;margin:0 auto;padding:32px 20px;">
      <div style="background:#fffdf8;border:1px solid #e5dccd;border-radius:18px;padding:32px;box-shadow:0 10px 30px rgba(40,32,20,0.08);">
        <div style="margin-bottom:24px;padding-bottom:18px;border-bottom:1px solid #eadfce;">
          <div style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#8a6f45;">Automated Job Digest</div>
        </div>
        <style>
          h1 {{ margin: 0 0 18px; font-size: 28px; line-height: 1.15; color: #1b2430; }}
          h2 {{ margin: 28px 0 10px; font-size: 18px; line-height: 1.3; color: #7c2d12; }}
          h3 {{ margin: 18px 0 8px; font-size: 16px; line-height: 1.35; color: #1f2933; }}
          p {{ margin: 0 0 12px; font-size: 15px; line-height: 1.65; }}
          ul {{ margin: 0 0 18px; padding-left: 20px; }}
          li {{ margin: 0 0 8px; font-size: 15px; line-height: 1.6; }}
          strong {{ color: #111827; }}
          a {{ color: #0f766e; }}
        </style>
        {content}
      </div>
    </div>
  </body>
</html>
"""


def _sort_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        row.get("date_posted", ""),
        row.get("company", ""),
        row.get("job_title", ""),
    )


def _tracker_sort_key(row: dict[str, str]) -> tuple[str, str]:
    return (
        row.get("Company", ""),
        row.get("Position", ""),
    )


def _listing_context(
    tracker_row: dict[str, str],
    listings_by_job_id: dict[str, dict[str, str]],
    listings_by_url: dict[str, dict[str, str]],
) -> dict[str, str]:
    job_id = tracker_row.get("Job ID", "").strip()
    website = tracker_row.get("Website", "").strip()
    if job_id and job_id in listings_by_job_id:
        return listings_by_job_id[job_id]
    if website and website in listings_by_url:
        return listings_by_url[website]
    return {}


def _write_job_section(lines: list[str], heading: str, rows: list[dict[str, str]]) -> None:
    lines.extend([heading, ""])
    if not rows:
        if heading == "## New Jobs":
            lines.append("No new jobs.")
        else:
            lines.append("No jobs in this section.")
        return
    for row in sorted(rows, key=_sort_key, reverse=True):
        company = row.get("company", "").strip() or "Unknown"
        title = row.get("job_title", "").strip() or "Untitled role"
        posted = row.get("date_posted", "").strip() or "Unknown"
        url = row.get("job_url", "").strip()

        lines.append(f"### {company} - {title}")
        lines.append(f"- Posted: {posted}")
        if url:
            lines.append(f"- URL: {url}")
        lines.append("")


def _write_review_section(
    lines: list[str],
    tracker_rows: list[dict[str, str]],
    listings_by_job_id: dict[str, dict[str, str]],
    listings_by_url: dict[str, dict[str, str]],
) -> None:
    lines.extend(["## Review Jobs", ""])
    if not tracker_rows:
        lines.append("No jobs currently marked Review.")
        return
    for tracker_row in sorted(tracker_rows, key=_tracker_sort_key):
        listing = _listing_context(tracker_row, listings_by_job_id, listings_by_url)
        company = tracker_row.get("Company", "").strip() or listing.get("company", "").strip() or "Unknown"
        title = tracker_row.get("Position", "").strip() or listing.get("job_title", "").strip() or "Untitled role"
        posted = listing.get("date_posted", "").strip() or "Unknown"
        url = tracker_row.get("Website", "").strip() or listing.get("job_url", "").strip()
        next_action = tracker_row.get("Next Action", "").strip() or "Unknown"

        lines.append(f"### {company} - {title}")
        lines.append(f"- Posted: {posted}")
        lines.append(f"- Next Action: {next_action}")
        if url:
            lines.append(f"- URL: {url}")
        lines.append("")


def build_report() -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    today = _today_utc()
    listings = _read_csv(JOB_LISTINGS_PATH)
    tracker_rows = _read_csv(JOB_TRACKER_PATH)
    listings_by_job_id = {row["job_id"]: row for row in listings if row.get("job_id")}
    listings_by_url = {row["job_url"]: row for row in listings if row.get("job_url")}
    new_jobs = []
    for tracker_row in tracker_rows:
        if tracker_row.get("Date Added", "").strip() != today:
            continue
        listing = _listing_context(tracker_row, listings_by_job_id, listings_by_url)
        if not listing:
            continue
        listing_copy = dict(listing)
        listing_copy["_tracker_row"] = tracker_row
        new_jobs.append(listing_copy)
    new_jobs = [row for row in new_jobs if row]

    report_path = REPORTS_DIR / f"alerts_{today}.md"
    lines: list[str] = [f"# Job Alerts - {today}", ""]
    _write_job_section(lines, "## New Jobs", new_jobs)
    lines.append("")
    review_jobs = [
        row
        for row in tracker_rows
        if row.get("Status", "").strip() == "Review" and row.get("Date Added", "").strip() != today
    ]
    _write_review_section(lines, review_jobs, listings_by_job_id, listings_by_url)

    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    for existing_report in REPORTS_DIR.glob("alerts_*.md"):
        if existing_report != report_path:
            existing_report.unlink()
    return report_path


def send_report_email(report_path: Path) -> None:
    report_text = report_path.read_text(encoding="utf-8")
    smtp_host = os.environ.get("JOB_ALERTS_SMTP_HOST", "").strip() or "smtp.gmail.com"
    smtp_port = int(os.environ.get("JOB_ALERTS_SMTP_PORT", "").strip() or "587")
    smtp_username = os.environ.get("JOB_ALERTS_SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("JOB_ALERTS_SMTP_PASSWORD", "").strip()
    email_to = os.environ.get("JOB_ALERTS_EMAIL_TO", DEFAULT_EMAIL_TO).strip() or DEFAULT_EMAIL_TO
    email_from = os.environ.get("JOB_ALERTS_EMAIL_FROM", smtp_username).strip()
    use_starttls = _bool_env("JOB_ALERTS_SMTP_STARTTLS", True)

    missing = [
        name
        for name, value in (
            ("JOB_ALERTS_SMTP_USERNAME", smtp_username),
            ("JOB_ALERTS_SMTP_PASSWORD", smtp_password),
            ("JOB_ALERTS_EMAIL_FROM", email_from),
        )
        if not value
    ]
    if missing:
        print(f"Skipping alert email: missing {', '.join(missing)}")
        return

    date_token = report_path.stem.removeprefix("alerts_")
    new_jobs = _count_jobs_in_section(report_text, "## New Jobs")
    review_jobs = _count_jobs_in_section(report_text, "## Review Jobs")
    subject = f"Job Alerts: {new_jobs} new, {review_jobs} review - {date_token}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(report_text)
    message.add_alternative(_markdown_to_html(report_text), subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if use_starttls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)

    print(f"Alert email sent to: {email_to}")


def main() -> None:
    _load_dotenv(DOTENV_PATH)
    report_path = build_report()
    print(f"Report written to: {report_path}")
    send_report_email(report_path)


if __name__ == "__main__":
    main()
