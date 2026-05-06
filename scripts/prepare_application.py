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
OUTPUTS_DIR = BASE_DIR / "outputs" / "applications"

JOB_LISTINGS_PATH = DATA_DIR / "job_listings.csv"
JOB_TRACKER_PATH = REPORTS_DIR / "job_tracker.csv"
RESUME_PROFILE_PATH = CONFIG_DIR / "resume_profile.json"
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
]

GPT_BLOCK_PATTERN = re.compile(
    r"(?ms)^% BEGIN GPT:(?P<name>[A-Z0-9_]+)\n(?P<content>.*?)^% END GPT:(?P=name)\s*$"
)
MAX_DOC_PAGES = 1


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


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as json_file:
        return json.load(json_file)


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
        "You are preparing job application materials. "
        "Return only valid JSON. "
        "Use only facts in the candidate profile and supplied resume source. "
        "Do not invent employers, dates, tools, metrics, or achievements. "
        "Analyze the job description and map it to the candidate profile. "
        "The JSON object must contain these keys: "
        "target_role, seniority, must_have_skills, preferred_skills, ats_keywords, "
        "key_responsibilities, fit_strengths, fit_gaps, resume_focus, cover_letter_focus, summary."
    )


def _tailored_cover_letter_analysis_instructions() -> str:
    return (
        "Draft a concise tailored cover letter in Markdown. "
        "Use only facts present in the supplied resume source and candidate profile. "
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


def _write_pdf_from_markdown(markdown_text: str, pdf_path: Path) -> None:
    clean_markdown = _strip_markdown_fence(markdown_text).rstrip() + "\n"
    with tempfile.TemporaryDirectory() as tmpdir_name:
        tmpdir = Path(tmpdir_name)
        markdown_path = tmpdir / "document.md"
        html_path = tmpdir / "document.html"
        css_path = tmpdir / "style.css"
        markdown_path.write_text(clean_markdown, encoding="utf-8")
        css_path.write_text(_pdf_css(), encoding="utf-8")

        subprocess.run(
            [
                "pandoc",
                str(markdown_path),
                "--standalone",
                "--css",
                str(css_path),
                "--metadata",
                "title=document",
                "-o",
                str(html_path),
            ],
            check=True,
        )
        subprocess.run(
            [
                "wkhtmltopdf",
                "--quiet",
                "--enable-local-file-access",
                str(html_path),
                str(pdf_path),
            ],
            check=True,
        )


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


def _replace_gpt_blocks(tex_source: str, replacements: dict[str, str]) -> str:
    seen: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        seen.add(name)
        original_content = match.group("content")
        new_content = replacements.get(name, original_content.strip("\n"))
        return f"% BEGIN GPT:{name}\n{new_content.rstrip()}\n% END GPT:{name}"

    updated = GPT_BLOCK_PATTERN.sub(repl, tex_source)
    missing = set(replacements) - seen
    if missing:
        raise RuntimeError(f"Replacement blocks not found in template: {sorted(missing)}")
    return updated


def _count_latex_items(text: str) -> int:
    return len(re.findall(r"(?m)^\s*\\item\b", text))


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


def _build_tex_pdf(source_tex_path: Path, pdf_path: Path, *, output_stem: str) -> None:
    if not source_tex_path.exists():
        raise SystemExit(f"Missing LaTeX source: {source_tex_path}")
    _compile_tex_content(source_tex_path.read_text(encoding="utf-8"), pdf_path, output_stem=output_stem)


def _build_base_resume_pdf(pdf_path: Path) -> None:
    _build_tex_pdf(RESUME_TEX_PATH, pdf_path, output_stem="cv")


def _build_base_cover_letter_pdf(pdf_path: Path) -> None:
    _build_tex_pdf(COVER_LETTER_TEX_PATH, pdf_path, output_stem="letter")


def _tex_tailoring_instructions(*, document_type: str, max_pages: int) -> str:
    return (
        f"You are tailoring a {document_type} written in LaTeX. "
        "Return only valid JSON. "
        "You will receive the full LaTeX file plus the current contents of the GPT-editable blocks. "
        "Only edit the provided GPT blocks. Do not rewrite any other part of the LaTeX file. "
        "Do not include GPT markers, code fences, commentary, or extra keys. "
        "Use only facts already present in the supplied LaTeX source and candidate profile. "
        "Do not invent employers, titles, dates, metrics, tools, publications, locations, or achievements. "
        f"Keep the final document to at most {max_pages} page(s). "
        "Tighten wording instead of adding volume. Preserve the number of bullet items in any block that already contains \\item lines. "
        "Preserve valid LaTeX syntax. "
        "Return a JSON object with exactly these keys: the GPT block names you were given."
    )


def _run_tex_block_tailor(
    *,
    api_key: str,
    model: str,
    document_type: str,
    template_source: str,
    analysis: dict[str, Any],
    listing: dict[str, str],
    tracker_row: dict[str, str],
    resume_profile: dict[str, Any],
    max_pages: int,
    feedback: str = "",
) -> dict[str, str]:
    current_blocks = _extract_gpt_blocks(template_source)
    prompt_payload: dict[str, Any] = {
        "job": listing,
        "tracker_row": tracker_row,
        "analysis": analysis,
        "resume_profile": resume_profile,
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
    resume_profile: dict[str, Any],
    output_stem: str,
    tex_output_path: Path,
    pdf_output_path: Path,
    max_pages: int = MAX_DOC_PAGES,
) -> int:
    feedback = ""
    last_error = "Unknown tailoring failure"
    for attempt in range(1, 4):
        replacements = _run_tex_block_tailor(
            api_key=api_key,
            model=model,
            document_type=document_type,
            template_source=template_source,
            analysis=analysis,
            listing=listing,
            tracker_row=tracker_row,
            resume_profile=resume_profile,
            max_pages=max_pages,
            feedback=feedback,
        )
        tailored_tex = _replace_gpt_blocks(template_source, replacements)
        try:
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
    resume_profile: dict[str, Any],
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
            "resume_profile": resume_profile,
        },
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Analysis output was not valid JSON:\n{text}") from exc


def _run_cover_letter_draft(
    *,
    api_key: str,
    model: str,
    listing: dict[str, str],
    analysis: dict[str, Any],
    resume_source: str,
    resume_profile: dict[str, Any],
) -> str:
    return _responses_api_call(
        api_key,
        model,
        _tailored_cover_letter_analysis_instructions(),
        {
            "job": listing,
            "analysis": analysis,
            "resume_source": resume_source,
            "resume_profile": resume_profile,
            "output_requirements": {
                "format": "markdown",
                "goal": "Write a concise role-specific cover letter draft",
            },
        },
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate tailored application materials for a tracked job.")
    parser.add_argument("--job-id", default="", help="Job ID from reports/job_tracker.csv")
    parser.add_argument("--url", default="", help="Job URL from reports/job_tracker.csv")
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        help="OpenAI model to use. Defaults to OPENAI_MODEL or gpt-5.4-mini.",
    )
    args = parser.parse_args()
    if not args.job_id and not args.url:
        parser.error("Pass either --job-id or --url")
    return args


def prepare_application_materials(
    *,
    tracker_row: dict[str, str],
    listing: dict[str, str],
    api_key: str,
    model: str,
) -> dict[str, Path]:
    if not RESUME_PROFILE_PATH.exists():
        raise SystemExit(f"Missing resume profile: {RESUME_PROFILE_PATH}")
    if not RESUME_TEX_PATH.exists():
        raise SystemExit(f"Missing base resume source: {RESUME_TEX_PATH}")
    if not COVER_LETTER_TEX_PATH.exists():
        raise SystemExit(f"Missing base cover letter source: {COVER_LETTER_TEX_PATH}")

    resume_source = RESUME_TEX_PATH.read_text(encoding="utf-8")
    cover_letter_source = COVER_LETTER_TEX_PATH.read_text(encoding="utf-8")
    resume_profile = _load_json(RESUME_PROFILE_PATH)

    analysis = _run_analysis(
        api_key=api_key,
        model=model,
        listing=listing,
        tracker_row=tracker_row,
        resume_source=resume_source,
        resume_profile=resume_profile,
    )

    output_dir = _job_output_dir(tracker_row.get("Job ID", "").strip(), tracker_row.get("Position", "").strip())
    output_dir.mkdir(parents=True, exist_ok=True)

    resume_tex_path = output_dir / "resume.tex"
    resume_pdf_path = output_dir / "resume.pdf"
    cover_letter_tex_path = output_dir / "cover_letter.tex"
    cover_letter_pdf_path = output_dir / "cover_letter.pdf"

    _build_tailored_tex_document(
        api_key=api_key,
        model=model,
        document_type="resume",
        template_source=resume_source,
        analysis=analysis,
        listing=listing,
        tracker_row=tracker_row,
        resume_profile=resume_profile,
        output_stem="resume",
        tex_output_path=resume_tex_path,
        pdf_output_path=resume_pdf_path,
    )
    _build_tailored_tex_document(
        api_key=api_key,
        model=model,
        document_type="cover letter",
        template_source=cover_letter_source,
        analysis=analysis,
        listing=listing,
        tracker_row=tracker_row,
        resume_profile=resume_profile,
        output_stem="cover_letter",
        tex_output_path=cover_letter_tex_path,
        pdf_output_path=cover_letter_pdf_path,
    )
    return {
        "output_dir": output_dir,
        "resume_tex": resume_tex_path,
        "resume_pdf": resume_pdf_path,
        "cover_letter_tex": cover_letter_tex_path,
        "cover_letter_pdf": cover_letter_pdf_path,
    }


def main() -> None:
    args = _parse_args()
    _load_dotenv(DOTENV_PATH)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required. Set it in the environment or in .env")
    tracker_rows = _migrate_tracker_rows(_read_csv(JOB_TRACKER_PATH))
    listings = _read_csv(JOB_LISTINGS_PATH)
    tracker_row = _find_tracker_row(tracker_rows, job_id=args.job_id.strip(), job_url=args.url.strip())
    listing = _find_listing(
        listings,
        job_id=tracker_row.get("Job ID", "").strip() or args.job_id.strip(),
        job_url=tracker_row.get("Website", "").strip() or args.url.strip(),
    )

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


if __name__ == "__main__":
    main()
