#!/usr/bin/env python3

from __future__ import annotations

import csv
import html
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
FILTERS_PATH = BASE_DIR / "config" / "filters.csv"
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


@dataclass
class FilterRule:
    source_id: str
    enabled: bool
    allowed_grades: set[str]
    include_title_keywords: list[str]
    exclude_title_keywords: list[str]
    include_description_keywords: list[str]
    exclude_description_keywords: list[str]


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


def _split_keywords(raw_value: str) -> list[str]:
    if not raw_value.strip():
        return []
    return [part.strip().lower() for part in raw_value.replace("|", ",").split(",") if part.strip()]


def _split_grades(raw_value: str) -> set[str]:
    if not raw_value.strip():
        return set()
    return {part.strip().upper() for part in raw_value.replace(",", "|").split("|") if part.strip()}


def _read_rules(path: Path) -> dict[str, FilterRule]:
    if not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        rows = csv.DictReader(csv_file)
        rules: dict[str, FilterRule] = {}
        for row in rows:
            source_id = (row.get("source_id") or "").strip()
            if not source_id:
                continue
            rules[source_id] = FilterRule(
                source_id=source_id,
                enabled=(row.get("enabled") or "").strip() == "1",
                allowed_grades=_split_grades(row.get("allowed_grades") or ""),
                include_title_keywords=_split_keywords(row.get("include_title_keywords") or ""),
                exclude_title_keywords=_split_keywords(row.get("exclude_title_keywords") or ""),
                include_description_keywords=_split_keywords(row.get("include_description_keywords") or ""),
                exclude_description_keywords=_split_keywords(row.get("exclude_description_keywords") or ""),
            )
    return rules


def _matches_any(haystack: str, keywords: list[str]) -> bool:
    return any(keyword in haystack for keyword in keywords)


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_html_tags(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_text(text)


def _extract_grade(text: str) -> str:
    match = re.search(r"\bGrade:\s*([A-Z]{2,}\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).upper()


def _fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(content_type, errors="replace")


class DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture_depth = 0
        self._text_parts: list[str] = []

    @property
    def text(self) -> str:
        return _clean_text(" ".join(self._text_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag.lower() == "div" and attr_map.get("itemprop") == "description":
            self._capture_depth = 1
            return
        if self._capture_depth > 0:
            self._capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._capture_depth > 0:
            self._capture_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_depth > 0:
            self._text_parts.append(data)


def _description_from_html(html_text: str) -> str:
    parser = DescriptionParser()
    parser.feed(html_text)
    if parser.text:
        return parser.text
    return _strip_html_tags(html_text)


def _enrich_listing_for_rules(row: dict[str, str]) -> dict[str, str]:
    job_url = (row.get("job_url") or "").strip()
    if not job_url:
        return row
    html_text = _fetch_html(job_url)
    enriched = dict(row)
    enriched["description"] = _description_from_html(html_text)
    enriched["job_grade"] = _extract_grade(_strip_html_tags(html_text))
    return enriched


def _apply_rule(row: dict[str, str], rule: FilterRule) -> tuple[bool, str]:
    if not rule.enabled:
        return True, ""

    title = (row.get("job_title") or "").lower()
    description = (row.get("description") or "").lower()
    grade = (row.get("job_grade") or "").upper().strip()

    if rule.allowed_grades:
        if not grade:
            return False, "missing_grade"
        if grade not in rule.allowed_grades:
            return False, f"grade_not_allowed:{grade}"

    if rule.include_title_keywords and not _matches_any(title, rule.include_title_keywords):
        return False, "missing_title_keyword"

    if rule.exclude_title_keywords and _matches_any(title, rule.exclude_title_keywords):
        return False, "excluded_title_keyword"

    if rule.include_description_keywords and not _matches_any(description, rule.include_description_keywords):
        return False, "missing_description_keyword"

    if rule.exclude_description_keywords and _matches_any(description, rule.exclude_description_keywords):
        return False, "excluded_description_keyword"

    return True, ""


def sync_tracker() -> tuple[Path, int]:
    _ensure_tracker(JOB_TRACKER_PATH)
    today = _today_utc()
    listings = _read_csv(JOB_LISTINGS_PATH)
    rules = _read_rules(FILTERS_PATH)
    filtered_listings: list[dict[str, str]] = []
    for row in listings:
        rule = rules.get((row.get("source_id") or "").strip())
        if rule is None:
            filtered_listings.append(row)
            continue
        try:
            enriched_row = _enrich_listing_for_rules(row) if rule.allowed_grades else row
        except Exception:  # noqa: BLE001
            continue
        if _apply_rule(enriched_row, rule)[0]:
            filtered_listings.append(row)
    listings = filtered_listings
    tracker_rows = _migrate_tracker_schema(_read_csv(JOB_TRACKER_PATH))
    listings_by_website = {
        row.get("job_url", "").strip(): row
        for row in listings
        if row.get("job_url", "").strip()
    }

    for tracker_row in tracker_rows:
        if not tracker_row.get("Date Added", "").strip():
            tracker_row["Date Added"] = today
        if tracker_row.get("Job ID", "").strip():
            continue
        website = tracker_row.get("Website", "").strip()
        listing = listings_by_website.get(website)
        if listing:
            tracker_row["Job ID"] = listing.get("job_id", "").strip()

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

    return JOB_TRACKER_PATH, len(new_rows)


def main() -> None:
    output_path, added_count = sync_tracker()
    print(f"Tracker synced: {output_path}")
    print(f"Rows added: {added_count}")


if __name__ == "__main__":
    main()
