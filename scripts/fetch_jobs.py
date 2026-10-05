#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "sources.csv"
DATA_DIR = BASE_DIR / "data"
JOB_LISTINGS_PATH = DATA_DIR / "job_listings.csv"
FETCH_RUNS_PATH = DATA_DIR / "fetch_runs.csv"

JOB_LISTING_FIELDS = [
    "date_seen",
    "source_id",
    "company",
    "careers_url",
    "job_id",
    "job_title",
    "job_url",
    "job_location",
    "date_posted",
    "description",
]

FETCH_RUN_FIELDS = [
    "run_at",
    "source_id",
    "company",
    "careers_url",
    "status",
    "http_status",
    "jobs_found",
    "message",
]

JOB_KEYWORDS = (
    "job",
    "jobs",
    "career",
    "careers",
    "position",
    "vacancy",
    "opening",
    "apply",
    "economist",
    "analyst",
    "research",
    "consultant",
    "phd",
    "fellow",
    "scientist",
    "policy",
)


@dataclass
class Source:
    source_id: str
    company: str
    careers_url: str
    parser_type: str
    enabled: bool


@dataclass
class LinkCandidate:
    title: str
    url: str


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[LinkCandidate] = []
        self._current_href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = dict(attrs)
        self._current_href = attr_map.get("href")
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        title = _clean_text(" ".join(self._text_parts))
        href = self._current_href.strip()
        if href:
            self.links.append(LinkCandidate(title=title, url=href))
        self._current_href = None
        self._text_parts = []


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


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _read_sources(path: Path) -> list[Source]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    sources: list[Source] = []
    for row in rows:
        enabled = (row.get("enabled") or "").strip() == "1"
        sources.append(
            Source(
                source_id=(row.get("source_id") or "").strip(),
                company=(row.get("company") or "").strip(),
                careers_url=(row.get("careers_url") or "").strip(),
                parser_type=(row.get("parser_type") or "links").strip(),
                enabled=enabled,
            )
        )
    return [source for source in sources if source.enabled and source.careers_url]


def _ensure_csv(path: Path, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()


def _read_existing_job_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    return {row["job_url"] for row in rows if row.get("job_url")}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def _append_rows(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writerows(rows)


def _rewrite_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fetch_html(url: str) -> tuple[int, str]:
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
        status = getattr(response, "status", 200)
        content_type = response.headers.get_content_charset() or "utf-8"
        html_text = response.read().decode(content_type, errors="replace")
    return status, html_text


def _looks_like_job_link(title: str, url: str, careers_url: str) -> bool:
    haystack = f"{title} {url}".lower()
    if any(keyword in haystack for keyword in JOB_KEYWORDS):
        return True
    parsed = urlparse(urljoin(careers_url, url))
    return parsed.path.count("/") >= 2 and parsed.netloc == urlparse(careers_url).netloc


def _normalize_link_candidates(source: Source, candidates: list[LinkCandidate]) -> list[dict[str, str]]:
    seen_urls: set[str] = set()
    normalized: list[dict[str, str]] = []
    date_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for candidate in candidates:
        job_url = urljoin(source.careers_url, candidate.url)
        if not job_url.startswith(("http://", "https://")):
            continue
        if job_url in seen_urls:
            continue
        title = candidate.title or _title_from_url(job_url)
        if not title:
            continue
        seen_urls.add(job_url)
        normalized.append(
            {
                "date_seen": date_seen,
                "source_id": source.source_id,
                "company": source.company,
                "careers_url": source.careers_url,
                "job_id": _job_id(job_url),
                "job_title": title,
                "job_url": job_url,
                "job_location": "",
                "date_posted": "",
                "description": "",
            }
        )
    return normalized


def _extract_first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _clean_text(match.group(1))


def _strip_html_tags(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_text(text)


def _full_description(html_text: str) -> str:
    parser = DescriptionParser()
    parser.feed(html_text)
    if parser.text:
        return parser.text
    meta_description = _extract_first_match(r'<meta name="description" content="(.*?)">', html_text)
    if meta_description:
        return meta_description
    raw_description = _extract_first_match(r'<div itemprop="description">(.*)</div>', html_text)
    if not raw_description:
        return ""
    return _strip_html_tags(raw_description)


def _smartrecruiters_location(html_text: str) -> str:
    locality = _extract_first_match(r'<meta itemprop="addressLocality" content="(.*?)">', html_text)
    region = _extract_first_match(r'<meta itemprop="addressRegion" content="(.*?)">', html_text)
    country = _extract_first_match(r'<meta itemprop="addressCountry" content="(.*?)">', html_text)
    parts = [part for part in [locality, region, country] if part]
    return ", ".join(parts)


def _smartrecruiters_date_posted(html_text: str) -> str:
    raw_date = _extract_first_match(r'<meta itemprop="datePosted" content="(.*?)">', html_text)
    if not raw_date:
        return ""
    return raw_date[:10]


def _smartrecruiters_title(html_text: str) -> str:
    return _extract_first_match(r'<h1 class="job-title" itemprop="title">(.*?)</h1>', html_text)


def _extract_grade(text: str) -> str:
    match = re.search(r"\bGrade:\s*([A-Z]+\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).upper()


def _smartrecruiters_job_detail(job_url: str) -> tuple[str, str, str, str, str]:
    _, html_text = _fetch_html(job_url)
    description = _full_description(html_text)
    page_text = _strip_html_tags(html_text)
    return (
        _smartrecruiters_title(html_text),
        _smartrecruiters_location(html_text),
        _smartrecruiters_date_posted(html_text),
        _extract_grade(page_text),
        description,
    )


def _normalize_smartrecruiters_candidates(source: Source, candidates: list[LinkCandidate]) -> list[dict[str, str]]:
    seen_urls: set[str] = set()
    normalized: list[dict[str, str]] = []
    date_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for candidate in candidates:
        job_url = urljoin(source.careers_url, candidate.url)
        parsed = urlparse(job_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != "jobs.smartrecruiters.com":
            continue
        if f"/{source.company.replace(' ', '')}/" not in parsed.path and f"/{source.company}/" not in parsed.path:
            continue
        if job_url in seen_urls:
            continue
        title = _clean_text(candidate.title or _title_from_url(job_url))
        if not title:
            continue
        detail_title = ""
        job_location = ""
        date_posted = ""
        job_grade = ""
        description = ""
        try:
            detail_title, job_location, date_posted, job_grade, description = _smartrecruiters_job_detail(job_url)
        except Exception:  # noqa: BLE001
            # Keep the listing even if the detail page parse fails.
            pass
        if detail_title:
            title = detail_title
        seen_urls.add(job_url)
        normalized.append(
            {
                "date_seen": date_seen,
                "source_id": source.source_id,
                "company": source.company,
                "careers_url": source.careers_url,
                "job_id": _job_id(job_url),
                "job_title": title,
                "job_url": job_url,
                "job_location": job_location,
                "date_posted": date_posted,
                "job_grade": job_grade,
                "description": description,
            }
        )
    return normalized


def _normalize_candidates(source: Source, html_text: str) -> list[dict[str, str]]:
    parser = AnchorParser()
    parser.feed(html_text)
    if source.parser_type == "smartrecruiters":
        return _normalize_smartrecruiters_candidates(source, parser.links)
    filtered_links = [
        candidate
        for candidate in parser.links
        if _looks_like_job_link(candidate.title, candidate.url, source.careers_url)
    ]
    return _normalize_link_candidates(source, filtered_links)


def _title_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return ""
    slug = path.rsplit("/", 1)[-1]
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug.title()


def _job_id(job_url: str) -> str:
    return hashlib.sha1(job_url.encode("utf-8")).hexdigest()[:16]


def _migrate_job_listings_schema(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = reader.fieldnames or []
        if fieldnames == JOB_LISTING_FIELDS:
            return
        rows = list(reader)
    migrated_rows: list[dict[str, str]] = []
    for row in rows:
        description = row.get("description", "") or row.get("description_snippet", "") or ""
        migrated_rows.append(
            {
                "date_seen": row.get("date_seen", ""),
                "source_id": row.get("source_id", ""),
                "company": row.get("company", ""),
                "careers_url": row.get("careers_url", ""),
                "job_id": row.get("job_id", ""),
                "job_title": row.get("job_title", ""),
                "job_url": row.get("job_url", ""),
                "job_location": row.get("job_location", ""),
                "date_posted": row.get("date_posted", ""),
                "description": description,
            }
        )
    _rewrite_csv(path, JOB_LISTING_FIELDS, migrated_rows)


def _listing_row(row: dict[str, str]) -> dict[str, str]:
    return {field: row.get(field, "") for field in JOB_LISTING_FIELDS}


def _merge_listing_details(existing_row: dict[str, str], fresh_row: dict[str, str]) -> bool:
    changed = False
    refreshable_fields = [
        "job_title",
        "job_location",
        "date_posted",
        "description",
    ]
    for field in refreshable_fields:
        fresh_value = fresh_row.get(field, "").strip()
        existing_value = existing_row.get(field, "").strip()
        if fresh_value and fresh_value != existing_value:
            existing_row[field] = fresh_value
            changed = True
    return changed


def main() -> None:
    _migrate_job_listings_schema(JOB_LISTINGS_PATH)
    _ensure_csv(JOB_LISTINGS_PATH, JOB_LISTING_FIELDS)
    _ensure_csv(FETCH_RUNS_PATH, FETCH_RUN_FIELDS)

    sources = _read_sources(CONFIG_PATH)
    existing_rows = _read_csv_rows(JOB_LISTINGS_PATH)
    existing_urls = {row["job_url"] for row in existing_rows if row.get("job_url")}
    existing_rows_by_url = {row["job_url"]: row for row in existing_rows if row.get("job_url")}
    new_rows: list[dict[str, str]] = []
    fetch_logs: list[dict[str, str]] = []
    existing_rows_updated = 0
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for source in sources:
        try:
            status_code, html_text = _fetch_html(source.careers_url)
            rows = _normalize_candidates(source, html_text)
            for row in rows:
                existing_row = existing_rows_by_url.get(row["job_url"])
                if existing_row and _merge_listing_details(existing_row, row):
                    existing_rows_updated += 1
            fresh_rows = [row for row in rows if row["job_url"] not in existing_urls]
            for row in fresh_rows:
                existing_urls.add(row["job_url"])
                listing_row = _listing_row(row)
                existing_rows.append(listing_row)
                existing_rows_by_url[row["job_url"]] = listing_row
            new_rows.extend(fresh_rows)
            fetch_logs.append(
                {
                    "run_at": run_at,
                    "source_id": source.source_id,
                    "company": source.company,
                    "careers_url": source.careers_url,
                    "status": "ok",
                    "http_status": str(status_code),
                    "jobs_found": str(len(rows)),
                    "message": "",
                }
            )
        except HTTPError as error:
            fetch_logs.append(
                {
                    "run_at": run_at,
                    "source_id": source.source_id,
                    "company": source.company,
                    "careers_url": source.careers_url,
                    "status": "http_error",
                    "http_status": str(error.code),
                    "jobs_found": "0",
                    "message": str(error.reason),
                }
            )
        except URLError as error:
            fetch_logs.append(
                {
                    "run_at": run_at,
                    "source_id": source.source_id,
                    "company": source.company,
                    "careers_url": source.careers_url,
                    "status": "url_error",
                    "http_status": "",
                    "jobs_found": "0",
                    "message": str(error.reason),
                }
            )
        except Exception as error:  # noqa: BLE001
            fetch_logs.append(
                {
                    "run_at": run_at,
                    "source_id": source.source_id,
                    "company": source.company,
                    "careers_url": source.careers_url,
                    "status": "error",
                    "http_status": "",
                    "jobs_found": "0",
                    "message": str(error),
                }
            )

    if new_rows or existing_rows_updated:
        _rewrite_csv(JOB_LISTINGS_PATH, JOB_LISTING_FIELDS, existing_rows)
    _append_rows(FETCH_RUNS_PATH, FETCH_RUN_FIELDS, fetch_logs)

    print(f"Sources scanned: {len(sources)}")
    print(f"New jobs added: {len(new_rows)}")
    print(f"Existing jobs refreshed: {existing_rows_updated}")
    print(f"Job listings file: {JOB_LISTINGS_PATH}")
    print(f"Fetch log file: {FETCH_RUNS_PATH}")


if __name__ == "__main__":
    main()
