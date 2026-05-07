#!/usr/bin/env python3

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
JOB_LISTINGS_PATH = BASE_DIR / "data" / "job_listings.csv"
JOB_TRACKER_PATH = BASE_DIR / "reports" / "job_tracker.csv"

TRACKER_FIELDS = [
    "Company",
    "Position",
    "Date Added",
    "Status",
    "Application Date",
    "Next Action",
    "Website",
    "Contact",
    "Contacted",
    "Job ID",
]


def _ensure_tracker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=TRACKER_FIELDS)
        writer.writeheader()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _migrate_tracker_schema(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    migrated: list[dict[str, str]] = []
    for row in rows:
        migrated.append({field: row.get(field, "") for field in TRACKER_FIELDS})
    return migrated


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def sync_tracker() -> tuple[Path, int, int]:
    _ensure_tracker(JOB_TRACKER_PATH)
    today = _today_utc()
    listings = _read_csv(JOB_LISTINGS_PATH)
    tracker_rows = _migrate_tracker_schema(_read_csv(JOB_TRACKER_PATH))
    live_job_ids = {row.get("job_id", "").strip() for row in listings if row.get("job_id", "").strip()}
    live_websites = {row.get("job_url", "").strip() for row in listings if row.get("job_url", "").strip()}
    listings_by_website = {
        row.get("job_url", "").strip(): row
        for row in listings
        if row.get("job_url", "").strip()
    }

    for tracker_row in tracker_rows:
        if not tracker_row.get("Date Added", "").strip():
            tracker_row["Date Added"] = today
        if not tracker_row.get("Job ID", "").strip():
            website = tracker_row.get("Website", "").strip()
            listing = listings_by_website.get(website)
            if listing:
                tracker_row["Job ID"] = listing.get("job_id", "").strip()
    kept_tracker_rows: list[dict[str, str]] = []
    removed_count = 0
    for tracker_row in tracker_rows:
        if tracker_row.get("Status", "").strip() != "Review":
            kept_tracker_rows.append(tracker_row)
            continue
        job_id = tracker_row.get("Job ID", "").strip()
        website = tracker_row.get("Website", "").strip()
        matches_live_listing = (job_id and job_id in live_job_ids) or (website and website in live_websites)
        if matches_live_listing:
            kept_tracker_rows.append(tracker_row)
        else:
            removed_count += 1

    tracker_rows = kept_tracker_rows
    existing_job_ids = {row.get("Job ID", "").strip() for row in tracker_rows if row.get("Job ID", "").strip()}
    existing_websites = {row.get("Website", "").strip() for row in tracker_rows if row.get("Website", "").strip()}

    new_rows: list[dict[str, str]] = []
    for listing in listings:
        job_id = listing.get("job_id", "").strip()
        website = listing.get("job_url", "").strip()
        if (job_id and job_id in existing_job_ids) or not website or website in existing_websites:
            continue
        if job_id:
            existing_job_ids.add(job_id)
        existing_websites.add(website)
        new_rows.append(
            {
                "Company": listing.get("company", "").strip(),
                "Position": listing.get("job_title", "").strip(),
                "Date Added": today,
                "Status": "Review",
                "Application Date": "",
                "Next Action": "Apply",
                "Website": website,
                "Contact": "",
                "Contacted": "",
                "Job ID": job_id,
            }
        )

    if new_rows:
        tracker_rows.extend(new_rows)
        _write_csv(JOB_TRACKER_PATH, TRACKER_FIELDS, tracker_rows)
    else:
        _write_csv(JOB_TRACKER_PATH, TRACKER_FIELDS, tracker_rows)

    return JOB_TRACKER_PATH, len(new_rows), removed_count


def main() -> None:
    output_path, added_count, removed_count = sync_tracker()
    print(f"Tracker synced: {output_path}")
    print(f"Rows added: {added_count}")
    print(f"Review rows removed: {removed_count}")


if __name__ == "__main__":
    main()
