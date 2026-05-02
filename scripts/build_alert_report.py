#!/usr/bin/env python3

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
JOB_LISTINGS_PATH = DATA_DIR / "job_listings.csv"
JOB_TRACKER_PATH = REPORTS_DIR / "job_tracker.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _snippet(description: str, *, limit: int = 450) -> str:
    description = " ".join(description.split())
    if len(description) <= limit:
        return description
    return description[: limit - 3].rstrip() + "..."


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
        description = _snippet(row.get("description", "").strip())

        lines.append(f"### {company} - {title}")
        lines.append(f"- Posted: {posted}")
        if url:
            lines.append(f"- URL: {url}")
        if description:
            lines.append(f"- Summary: {description}")
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
    new_jobs = [
        _listing_context(row, listings_by_job_id, listings_by_url)
        for row in tracker_rows
        if row.get("Date Added", "").strip() == today
    ]
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


def main() -> None:
    report_path = build_report()
    print(f"Report written to: {report_path}")


if __name__ == "__main__":
    main()
