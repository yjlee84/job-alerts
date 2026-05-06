#!/usr/bin/env python3

from __future__ import annotations

import os

from prepare_application import (
    DOTENV_PATH,
    JOB_LISTINGS_PATH,
    JOB_TRACKER_PATH,
    _find_listing,
    _load_dotenv,
    _migrate_tracker_rows,
    prepare_application_materials,
    _read_csv,
)


def main() -> None:
    _load_dotenv(DOTENV_PATH)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required for automated review materials generation")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")

    tracker_rows = _migrate_tracker_rows(_read_csv(JOB_TRACKER_PATH))
    listings = _read_csv(JOB_LISTINGS_PATH)
    review_rows = [row for row in tracker_rows if row.get("Status", "").strip() == "Review"]

    built_resumes = 0
    built_cover_letters = 0
    skipped_rows = 0
    for row in review_rows:
        job_id = row.get("Job ID", "").strip()
        job_url = row.get("Website", "").strip()
        try:
            listing = _find_listing(
                listings,
                job_id=job_id,
                job_url=job_url,
            )
        except ValueError:
            skipped_rows += 1
            print(
                "Skipping review row without matching listing: "
                f"job_id={job_id or '<missing>'} url={job_url or '<missing>'}"
            )
            continue
        prepare_application_materials(
            tracker_row=row,
            listing=listing,
            api_key=api_key,
            model=model,
        )
        built_resumes += 1
        built_cover_letters += 1

    print(f"Tracker rows scanned: {len(tracker_rows)}")
    print(f"Review rows skipped: {skipped_rows}")
    print(f"Review resumes built: {built_resumes}")
    print(f"Review cover letters built: {built_cover_letters}")


if __name__ == "__main__":
    main()
