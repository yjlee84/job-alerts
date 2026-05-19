#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
ASSETS_DIR = BASE_DIR / "assets"
CONFIG_DIR = BASE_DIR / "config"
OUTPUTS_DIR = BASE_DIR / "outputs"

JOB_LISTINGS_PATH = DATA_DIR / "job_listings.csv"
JOB_TRACKER_PATH = REPORTS_DIR / "job_tracker.csv"
RESUME_TEX_PATH = ASSETS_DIR / "cv.tex"
RESUME_BIB_PATH = ASSETS_DIR / "citations.bib"
COVER_LETTER_TEX_PATH = ASSETS_DIR / "letter.tex"
OPENAI_API_URL = "https://api.openai.com/v1/responses"
DOTENV_PATH = BASE_DIR / ".env"

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
    "Description",
]

GPT_BLOCK_PATTERN = re.compile(
    r"(?ms)^% BEGIN GPT:(?P<name>[A-Z0-9_]+)\n(?P<content>.*?)^% END GPT:(?P=name)\s*$"
)
MAX_DOC_PAGES = 1
DEFAULT_PRIORITY_KEYWORD_COUNT = 8
RESUME_EDITABLE_BLOCKS = {
    "SUMMARY",
    "CORE_COMPETENCIES",
    "TECHNICAL_ACUMEN",
}
COVER_LETTER_STYLE_SENSITIVE_BLOCKS = {
    "CLOSING_PARAGRAPHS",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def _migrate_tracker_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{field: row.get(field, "") for field in TRACKER_FIELDS} for row in rows]


def _slugify(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value)
    compact = "-".join(part for part in cleaned.split("-") if part)
    return compact or "job"


def _job_output_dir(job_id: str, position: str) -> Path:
    job_token = job_id.strip() or _slugify(position)[:40]
    return OUTPUTS_DIR / job_token


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _find_tracker_row(
    tracker_rows: list[dict[str, str]],
    *,
    job_id: str,
    job_url: str,
) -> dict[str, str]:
    for row in tracker_rows:
        if job_id and row.get("Job ID", "").strip() == job_id:
            return row
        if job_url and row.get("Website", "").strip() == job_url:
            return row
    raise ValueError("Job was not found in reports/job_tracker.csv")


def _find_listing(
    listings: list[dict[str, str]],
    *,
    job_id: str,
    job_url: str,
) -> dict[str, str]:
    for row in listings:
        if job_id and row.get("job_id", "").strip() == job_id:
            return row
        if job_url and row.get("job_url", "").strip() == job_url:
            return row
    raise ValueError("Matching job listing was not found in data/job_listings.csv")


def _listing_from_tracker_row(tracker_row: dict[str, str]) -> dict[str, str]:
    return {
        "company": tracker_row.get("Company", "").strip(),
        "job_title": tracker_row.get("Position", "").strip(),
        "job_url": tracker_row.get("Website", "").strip(),
        "job_id": tracker_row.get("Job ID", "").strip(),
        "date_posted": "",
        "description": tracker_row.get("Description", "").strip(),
    }


def _extract_text_response(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    parts: list[str] = []
    for output_item in payload.get("output", []):
        if output_item.get("type") != "message":
            continue
        for content in output_item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _parse_json_response(text: str) -> dict[str, Any]:
    clean_text = _strip_markdown_fence(text)
    try:
        payload = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model output was not valid JSON:\n{clean_text}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Model output must be a JSON object")
    return payload


def _responses_api_call(api_key: str, model: str, instructions: str, payload: dict[str, Any]) -> str:
    request_body = {
        "model": model,
        "instructions": instructions,
        "input": json.dumps(payload, ensure_ascii=False),
    }
    request = Request(
        OPENAI_API_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with status {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

    text = _extract_text_response(response_payload)
    if not text:
        raise RuntimeError("OpenAI API response did not contain text output")
    return text


def _analysis_instructions() -> str:
    return (
        "You are preparing ATS-safe job application materials. "
        "Return only valid JSON. "
        "Use only facts in the supplied resume source. "
        "Do not invent employers, dates, tools, metrics, or achievements. "
        "Analyze the job description and map it to the supplied resume source. "
        f"Select exactly the top {DEFAULT_PRIORITY_KEYWORD_COUNT} high-priority ATS keywords for the resume. "
        "Prioritize core role terms, repeated skills/methods, important tools, and central responsibility phrases. "
        "Exclude low-value admin jargon, one-off internal phrases, and overly specific terms unless they are central to the role. "
        "Separate the selected keywords into supported keywords that can be defended from the supplied resume "
        "versus unsupported keywords that should not be added. "
        "The JSON object must contain these keys: "
        "target_role, seniority, must_have_skills, preferred_skills, ats_keywords, "
        "priority_keywords, supported_ats_keywords, unsupported_ats_keywords, key_responsibilities, fit_strengths, "
        "fit_gaps, resume_focus, cover_letter_focus, summary."
    )


def _tailored_cover_letter_analysis_instructions() -> str:
    return (
        "Draft a concise tailored cover letter in Markdown. "
        "Use only facts present in the supplied resume source. "
        "Do not invent experience or claims. "
        "The letter should explain fit for the role and mirror relevant job-description language naturally."
    )


def _pdf_css() -> str:
    return """
body {
  font-family: "Aptos", "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: #111111;
  margin: 0;
  line-height: 1.35;
  font-size: 11pt;
}
h1 {
  font-size: 22pt;
  letter-spacing: 0.08em;
  margin: 0 0 8pt 0;
  text-transform: uppercase;
}
h2 {
  font-size: 12pt;
  margin: 16pt 0 6pt 0;
  text-transform: uppercase;
  border-bottom: 1px solid #222222;
  padding-bottom: 2pt;
}
h3 {
  font-size: 11pt;
  margin: 10pt 0 2pt 0;
}
p {
  margin: 0 0 7pt 0;
}
ul {
  margin: 0 0 8pt 18pt;
  padding: 0;
}
li {
  margin: 0 0 4pt 0;
}
hr {
  border: none;
  border-top: 1px solid #cccccc;
  margin: 12pt 0;
}
blockquote {
  margin: 0;
  padding: 0;
}
code {
  font-family: inherit;
}
"""


def _copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.write_bytes(source.read_bytes())


def _extract_gpt_blocks(tex_source: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in GPT_BLOCK_PATTERN.finditer(tex_source):
        blocks[match.group("name")] = match.group("content").strip("\n")
    if not blocks:
        raise RuntimeError("No GPT-editable blocks were found in the LaTeX source")
    return blocks


def _select_editable_blocks(document_type: str, blocks: dict[str, str]) -> dict[str, str]:
    if document_type == "resume":
        selected = {name: content for name, content in blocks.items() if name in RESUME_EDITABLE_BLOCKS}
        if not selected:
            raise RuntimeError("No ATS-safe editable resume blocks were found in the LaTeX source")
        return selected
    return blocks


def _replace_gpt_blocks(tex_source: str, replacements: dict[str, str]) -> str:
    seen: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        seen.add(name)
        original_content = match.group("content")
        new_content = replacements.get(name, original_content.strip("\n"))
        trailing_newlines = ""
        suffix = match.group(0).split(f"% END GPT:{name}", 1)[1]
        if suffix:
            trailing_newlines = suffix
        return f"% BEGIN GPT:{name}\n{new_content.rstrip()}\n% END GPT:{name}{trailing_newlines}"

    updated = GPT_BLOCK_PATTERN.sub(repl, tex_source)
    missing = set(replacements) - seen
    if missing:
        raise RuntimeError(f"Replacement blocks not found in template: {sorted(missing)}")
    return updated


def _count_latex_items(text: str) -> int:
    return len(re.findall(r"(?m)^\s*\\item\b", text))


def _count_paragraph_breaks(text: str) -> int:
    return len(re.findall(r"\n\s*\n", text.strip()))


def _count_vspace_commands(text: str) -> int:
    return len(re.findall(r"\\vspace\{[^}]+\}", text))


def _validate_gpt_replacements(original_blocks: dict[str, str], replacements: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    expected = set(original_blocks)
    got = set(replacements)
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        raise RuntimeError(f"GPT block set mismatch. Missing={missing} Extra={extra}")

    for name, original in original_blocks.items():
        replacement = replacements[name]
        if not isinstance(replacement, str):
            raise RuntimeError(f"Replacement for block {name} must be a string")
        if "% BEGIN GPT:" in replacement or "% END GPT:" in replacement:
            raise RuntimeError(f"Replacement for block {name} illegally contains GPT markers")
        if "\\documentclass" in replacement or "\\begin{document}" in replacement:
            raise RuntimeError(f"Replacement for block {name} illegally contains document-level LaTeX")
        if _count_latex_items(original) != _count_latex_items(replacement):
            raise RuntimeError(
                f"Replacement for block {name} changed the number of \\item entries "
                f"from {_count_latex_items(original)} to {_count_latex_items(replacement)}"
            )
        if name in COVER_LETTER_STYLE_SENSITIVE_BLOCKS:
            if _count_paragraph_breaks(original) != _count_paragraph_breaks(replacement):
                raise RuntimeError(
                    f"Replacement for block {name} changed paragraph breaks "
                    f"from {_count_paragraph_breaks(original)} to {_count_paragraph_breaks(replacement)}"
                )
            if _count_vspace_commands(original) != _count_vspace_commands(replacement):
                raise RuntimeError(
                    f"Replacement for block {name} changed \\vspace commands "
                    f"from {_count_vspace_commands(original)} to {_count_vspace_commands(replacement)}"
                )
        normalized[name] = replacement.strip()
    return normalized


def _pdf_page_count(pdf_path: Path) -> int:
    if shutil.which("pdfinfo"):
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":", 1)[1].strip())
    if shutil.which("mdls"):
        result = subprocess.run(
            ["mdls", "-name", "kMDItemNumberOfPages", "-raw", str(pdf_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(result.stdout.strip())
    raise RuntimeError("No PDF page-count tool found. Install pdfinfo or mdls.")


def _compile_tex_content(
    tex_content: str,
    pdf_path: Path,
    *,
    output_stem: str,
    tex_output_path: Path | None = None,
    max_pages: int = MAX_DOC_PAGES,
) -> int:
    with tempfile.TemporaryDirectory() as tmpdir_name:
        tmpdir = Path(tmpdir_name)
        tex_filename = f"{output_stem}.tex"
        tex_path = tmpdir / tex_filename
        bib_path = tmpdir / "citations.bib"
        tex_path.write_text(tex_content, encoding="utf-8")
        _copy_if_exists(RESUME_BIB_PATH, bib_path)
        result = subprocess.run(
            [
                "latexmk",
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                tex_filename,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LaTeX compile failed for {tex_filename}:\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
            )
        built_pdf = tmpdir / f"{output_stem}.pdf"
        if not built_pdf.exists():
            raise RuntimeError(f"latexmk completed without producing {output_stem}.pdf")
        page_count = _pdf_page_count(built_pdf)
        if page_count > max_pages:
            raise RuntimeError(
                f"{output_stem}.pdf exceeded the {max_pages}-page limit with {page_count} pages"
            )
        if tex_output_path is not None:
            tex_output_path.write_text(tex_content, encoding="utf-8")
        pdf_path.write_bytes(built_pdf.read_bytes())
        return page_count


def _tex_tailoring_instructions(*, document_type: str, max_pages: int) -> str:
    base = (
        f"You are tailoring a {document_type} written in LaTeX. "
        "Return only valid JSON. "
        "You will receive the full LaTeX file plus the current contents of the GPT-editable blocks. "
        "Only edit the provided GPT blocks. Do not rewrite any other part of the LaTeX file. "
        "Do not include GPT markers, code fences, commentary, or extra keys. "
        "Use only facts already present in the supplied LaTeX source. "
        "Do not invent employers, titles, dates, metrics, tools, publications, locations, or achievements. "
        f"Keep the final document to at most {max_pages} page(s). "
        "Tighten wording instead of adding volume. Preserve the number of bullet items in any block that already contains \\item lines. "
        "Preserve valid LaTeX syntax. "
        "Return a JSON object with exactly these keys: the GPT block names you were given."
    )
    if document_type == "resume":
        return (
            base
            + " This is an ATS keyword-alignment task, not a resume rewrite. "
            "Do not change employers, titles, dates, education, or experience bullets. "
            "Prefer minimal edits in the summary, core competencies, and technical acumen blocks only. "
            f"Try to include at least 6 of the {DEFAULT_PRIORITY_KEYWORD_COUNT} selected priority keywords when they are supported and fit naturally. "
            "If a supported keyword cannot be added cleanly in those blocks, leave it out rather than forcing awkward phrasing. "
            "Do not change formatting style, paragraph structure, or list style. "
            "Keep each editable block in the same presentation style it already uses. "
            "For example: keep SUMMARY as a plain paragraph, CORE_COMPETENCIES as a labeled competency line, "
            "and TECHNICAL_ACUMEN as a labeled tools line. "
            "Use job-description language naturally and only when supported by the supplied resume. "
            "Do not keyword-stuff. Preserve the candidate's existing voice and factual scope."
        )
    if document_type == "cover letter":
        return (
            base
            + " Preserve the existing cover-letter layout and paragraph structure exactly. "
            "Do not merge separate paragraphs into one paragraph. "
            "If a block already contains blank lines or \\vspace commands, keep them in the same places. "
            "Keep the tone natural and professional rather than optimized for ATS."
        )
    return base


def _run_tex_block_tailor(
    *,
    api_key: str,
    model: str,
    document_type: str,
    template_source: str,
    analysis: dict[str, Any],
    listing: dict[str, str],
    tracker_row: dict[str, str],
    max_pages: int,
    feedback: str = "",
) -> dict[str, str]:
    current_blocks = _select_editable_blocks(document_type, _extract_gpt_blocks(template_source))
    prompt_payload: dict[str, Any] = {
        "job": listing,
        "tracker_row": tracker_row,
        "analysis": analysis,
        "document_type": document_type,
        "max_pages": max_pages,
        "full_latex_source": template_source,
        "editable_blocks": current_blocks,
    }
    if feedback:
        prompt_payload["revision_feedback"] = feedback
    response_text = _responses_api_call(
        api_key,
        model,
        _tex_tailoring_instructions(
            document_type=document_type,
            max_pages=max_pages,
        ),
        prompt_payload,
    )
    replacements = _parse_json_response(response_text)
    return _validate_gpt_replacements(current_blocks, replacements)


def _build_tailored_tex_document(
    *,
    api_key: str,
    model: str,
    document_type: str,
    template_source: str,
    analysis: dict[str, Any],
    listing: dict[str, str],
    tracker_row: dict[str, str],
    output_stem: str,
    tex_output_path: Path,
    pdf_output_path: Path,
    max_pages: int = MAX_DOC_PAGES,
) -> int:
    feedback = ""
    last_error = "Unknown tailoring failure"
    for attempt in range(1, 4):
        try:
            replacements = _run_tex_block_tailor(
                api_key=api_key,
                model=model,
                document_type=document_type,
                template_source=template_source,
                analysis=analysis,
                listing=listing,
                tracker_row=tracker_row,
                max_pages=max_pages,
                feedback=feedback,
            )
            tailored_tex = _replace_gpt_blocks(template_source, replacements)
            return _compile_tex_content(
                tailored_tex,
                pdf_output_path,
                output_stem=output_stem,
                tex_output_path=tex_output_path,
                max_pages=max_pages,
            )
        except RuntimeError as exc:
            last_error = str(exc)
            feedback = (
                f"Attempt {attempt} failed. {last_error} "
                "Revise only the editable blocks, keep the document more concise, and preserve valid LaTeX."
            )
    raise RuntimeError(f"Unable to build tailored {document_type}: {last_error}")


def _run_analysis(
    *,
    api_key: str,
    model: str,
    listing: dict[str, str],
    tracker_row: dict[str, str],
    resume_source: str,
) -> dict[str, Any]:
    text = _responses_api_call(
        api_key,
        model,
        _analysis_instructions(),
        {
            "job": {
                "company": listing.get("company", ""),
                "title": listing.get("job_title", ""),
                "url": listing.get("job_url", ""),
                "date_posted": listing.get("date_posted", ""),
                "description": listing.get("description", ""),
            },
            "tracker_row": tracker_row,
            "resume_source": resume_source,
        },
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Analysis output was not valid JSON:\n{text}") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate tailored application materials for a tracked job.")
    parser.add_argument("--job-id", default="", help="Job ID from reports/job_tracker.csv")
    parser.add_argument("--url", default="", help="Job URL from reports/job_tracker.csv")
    parser.add_argument(
        "--all-review",
        action="store_true",
        help="Generate materials for every tracker row whose Status is Review.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        help="OpenAI model to use. Defaults to OPENAI_MODEL or gpt-5.4-mini.",
    )
    args = parser.parse_args()
    if args.all_review and (args.job_id or args.url):
        parser.error("--all-review cannot be combined with --job-id or --url")
    if not args.all_review and not args.job_id and not args.url:
        parser.error("Pass either --job-id, --url, or --all-review")
    return args


def prepare_application_materials(
    *,
    tracker_row: dict[str, str],
    listing: dict[str, str],
    api_key: str,
    model: str,
) -> dict[str, Path]:
    if not RESUME_TEX_PATH.exists():
        raise SystemExit(f"Missing base resume source: {RESUME_TEX_PATH}")
    if not COVER_LETTER_TEX_PATH.exists():
        raise SystemExit(f"Missing base cover letter source: {COVER_LETTER_TEX_PATH}")

    resume_source = RESUME_TEX_PATH.read_text(encoding="utf-8")
    cover_letter_source = COVER_LETTER_TEX_PATH.read_text(encoding="utf-8")

    analysis = _run_analysis(
        api_key=api_key,
        model=model,
        listing=listing,
        tracker_row=tracker_row,
        resume_source=resume_source,
    )

    output_dir = _job_output_dir(tracker_row.get("Job ID", "").strip(), tracker_row.get("Position", "").strip())
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    resume_tex_path = source_dir / "resume.tex"
    resume_pdf_path = output_dir / "resume.pdf"
    cover_letter_tex_path = source_dir / "cover_letter.tex"
    cover_letter_pdf_path = output_dir / "cover_letter.pdf"
    cover_letter_warning_path = output_dir / "cover_letter_warning.txt"

    _build_tailored_tex_document(
        api_key=api_key,
        model=model,
        document_type="resume",
        template_source=resume_source,
        analysis=analysis,
        listing=listing,
        tracker_row=tracker_row,
        output_stem="resume",
        tex_output_path=resume_tex_path,
        pdf_output_path=resume_pdf_path,
    )
    if cover_letter_warning_path.exists():
        cover_letter_warning_path.unlink()
    try:
        _build_tailored_tex_document(
            api_key=api_key,
            model=model,
            document_type="cover letter",
            template_source=cover_letter_source,
            analysis=analysis,
            listing=listing,
            tracker_row=tracker_row,
            output_stem="cover_letter",
            tex_output_path=cover_letter_tex_path,
            pdf_output_path=cover_letter_pdf_path,
        )
    except RuntimeError as exc:
        cover_letter_warning_path.write_text(
            f"Cover letter generation failed after retries.\n{exc}\n",
            encoding="utf-8",
        )
    return {
        "output_dir": output_dir,
        "resume_tex": resume_tex_path,
        "resume_pdf": resume_pdf_path,
        "cover_letter_tex": cover_letter_tex_path,
        "cover_letter_pdf": cover_letter_pdf_path,
        "cover_letter_warning": cover_letter_warning_path,
    }


def build_review_materials(*, api_key: str, model: str) -> None:
    tracker_rows = _migrate_tracker_rows(_read_csv(JOB_TRACKER_PATH))
    listings = _read_csv(JOB_LISTINGS_PATH)
    review_rows = [row for row in tracker_rows if row.get("Status", "").strip() == "Review"]
    review_output_dirs = {
        _job_output_dir(row.get("Job ID", "").strip(), row.get("Position", "").strip()).resolve()
        for row in review_rows
    }

    removed_output_dirs = 0
    if OUTPUTS_DIR.exists():
        for child in OUTPUTS_DIR.iterdir():
            if not child.is_dir():
                continue
            if child.name == "applications":
                continue
            if child.resolve() in review_output_dirs:
                continue
            shutil.rmtree(child)
            removed_output_dirs += 1

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
            tracker_description = row.get("Description", "").strip()
            if not tracker_description:
                skipped_rows += 1
                print(
                    "Skipping review row without matching listing or stored description: "
                    f"job_id={job_id or '<missing>'} url={job_url or '<missing>'}"
                )
                continue
            listing = _listing_from_tracker_row(row)
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
    print(f"Non-review output folders removed: {removed_output_dirs}")
    print(f"Review resumes built: {built_resumes}")
    print(f"Review cover letters built: {built_cover_letters}")


def main() -> None:
    args = _parse_args()
    _load_dotenv(DOTENV_PATH)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required. Set it in the environment or in .env")
    if args.all_review:
        build_review_materials(api_key=api_key, model=args.model)
        return
    tracker_rows = _migrate_tracker_rows(_read_csv(JOB_TRACKER_PATH))
    listings = _read_csv(JOB_LISTINGS_PATH)
    tracker_row = _find_tracker_row(tracker_rows, job_id=args.job_id.strip(), job_url=args.url.strip())
    try:
        listing = _find_listing(
            listings,
            job_id=tracker_row.get("Job ID", "").strip() or args.job_id.strip(),
            job_url=tracker_row.get("Website", "").strip() or args.url.strip(),
        )
    except ValueError:
        tracker_description = tracker_row.get("Description", "").strip()
        if not tracker_description:
            raise
        listing = _listing_from_tracker_row(tracker_row)

    outputs = prepare_application_materials(
        tracker_row=tracker_row,
        listing=listing,
        api_key=api_key,
        model=args.model,
    )
    output_dir = outputs["output_dir"]
    print(f"Application materials written to: {output_dir}")
    print(f"Resume TeX: {outputs['resume_tex'].relative_to(BASE_DIR)}")
    print(f"Resume PDF: {outputs['resume_pdf'].relative_to(BASE_DIR)}")
    print(f"Cover Letter TeX: {outputs['cover_letter_tex'].relative_to(BASE_DIR)}")
    print(f"Cover Letter PDF: {outputs['cover_letter_pdf'].relative_to(BASE_DIR)}")
    cover_letter_warning = outputs["cover_letter_warning"]
    if cover_letter_warning.exists():
        print(f"Cover Letter Warning: {cover_letter_warning.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
