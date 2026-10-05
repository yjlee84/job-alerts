#!/usr/bin/env python3

from __future__ import annotations

import csv
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
FILTERS_PATH = BASE_DIR / "config" / "filters.csv"
JOB_LISTINGS_PATH = BASE_DIR / "data" / "job_listings.csv"


@dataclass
class FilterRule:
    source_id: str
    enabled: bool
    allowed_grades: set[str]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def split_grades(raw_value: str) -> set[str]:
    if not raw_value.strip():
        return set()
    return {part.strip().upper() for part in raw_value.replace(",", "|").split("|") if part.strip()}


def read_rules(path: Path = FILTERS_PATH) -> dict[str, FilterRule]:
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
                allowed_grades=split_grades(row.get("allowed_grades") or ""),
            )
    return rules


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_html_tags(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def extract_grade(text: str) -> str:
    match = re.search(r"\bGrade:\s*([A-Z]+\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).upper()


def fetch_html(url: str) -> str:
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
        return clean_text(" ".join(self._text_parts))

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


def description_from_html(html_text: str) -> str:
    parser = DescriptionParser()
    parser.feed(html_text)
    if parser.text:
        return parser.text
    return strip_html_tags(html_text)


def enrich_listing_for_rules(row: dict[str, str]) -> dict[str, str]:
    job_url = (row.get("job_url") or "").strip()
    if not job_url:
        return row
    html_text = fetch_html(job_url)
    enriched = dict(row)
    enriched["description"] = description_from_html(html_text)
    enriched["job_grade"] = extract_grade(strip_html_tags(html_text))
    return enriched


def apply_rule(row: dict[str, str], rule: FilterRule) -> tuple[bool, str]:
    if not rule.enabled:
        return True, ""

    grade = (row.get("job_grade") or "").upper().strip()

    if rule.allowed_grades:
        if not grade:
            return False, "missing_grade"
        if grade not in rule.allowed_grades:
            return False, f"grade_not_allowed:{grade}"

    return True, ""


def filter_listings(
    listings: list[dict[str, str]],
    rules: dict[str, FilterRule] | None = None,
) -> tuple[list[dict[str, str]], int, int]:
    rules = rules or read_rules()
    filtered_listings: list[dict[str, str]] = []
    matches = 0
    rejects = 0

    for row in listings:
        rule = rules.get((row.get("source_id") or "").strip())
        if rule is None:
            filtered_listings.append(row)
            matches += 1
            continue
        try:
            enriched_row = enrich_listing_for_rules(row) if rule.allowed_grades else row
        except Exception:  # noqa: BLE001
            rejects += 1
            continue
        matched, _ = apply_rule(enriched_row, rule)
        if matched:
            filtered_listings.append(row)
            matches += 1
        else:
            rejects += 1

    return filtered_listings, matches, rejects


def main() -> None:
    listings = read_csv(JOB_LISTINGS_PATH)
    if not listings:
        print(f"No listings found at: {JOB_LISTINGS_PATH}")
        return
    fieldnames = list(listings[0].keys())
    filtered_listings, matches, rejects = filter_listings(listings)
    write_csv(JOB_LISTINGS_PATH, fieldnames, filtered_listings)
    print(f"Rules loaded: {len(read_rules())}")
    print(f"Jobs matched: {matches}")
    print(f"Jobs rejected: {rejects}")
    print(f"Listings evaluated: {len(listings)}")
    print(f"Filtered listings written: {len(filtered_listings)}")
    print(f"Updated listings file: {JOB_LISTINGS_PATH}")


if __name__ == "__main__":
    main()
