"""Non-submitting browser policy and a small, persistent API spending ledger."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

READ_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_HOSTS = {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}
SUBMISSION_WORDS = re.compile(r"\b(submit|send application|apply now|complete application|finish|confirm application)\b", re.I)


def validate_job_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Only HTTPS SmartRecruiters job URLs are supported in this trial.")


def request_allowed(method: str, url: str) -> bool:
    """Fail closed: this stage does not upload files or send applicant data remotely.

    Inputs are filled in the DOM; local review includes attachment paths. The
    real upload/submission APIs are intentionally not allowed by this policy.
    """
    if method.upper() not in READ_METHODS:
        return False
    parsed = urlparse(url)
    # Do not allow applications/create/submit even if a site uses a GET action.
    return not bool(re.search(r"/(?:submit|apply|applications?/(?:create|submit))(?:/|$)", parsed.path, re.I))


class Budget:
    """Charge failed/unknown requests conservatively; do not retry automatically.

    Rates are for standard gpt-5.4-mini only. A UTF-8 byte bound plus a generous
    schema overhead bounds text input; max_output_tokens includes reasoning.
    Nothing else in the document-generation pipeline uses this ledger.
    """
    MODEL = "gpt-5.4-mini"
    INPUT_RATE = 0.75 / 1_000_000
    OUTPUT_RATE = 4.50 / 1_000_000

    def __init__(self, path: Path, limit: float = 0.03):
        if not 0 < limit <= 0.03:
            raise ValueError("Application worker budget must be between $0 and $0.03.")
        self.path = path
        self.limit = limit
        self.entries = json.loads(path.read_text()) if path.exists() else []

    @property
    def spent(self):
        return sum(item["charged_usd"] for item in self.entries)

    def reserve(self, body: dict) -> int:
        if body.get("model") != self.MODEL:
            raise ValueError("Only gpt-5.4-mini has verified rates in this worker.")
        input_bound = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 8192
        bound = input_bound * self.INPUT_RATE + body["max_output_tokens"] * self.OUTPUT_RATE
        if self.spent + bound > self.limit:
            raise RuntimeError(f"GPT request skipped: its conservative cost bound exceeds the ${self.limit:.2f} application budget.")
        self.entries.append({"status": "reserved", "charged_usd": bound, "upper_bound_usd": bound})
        self.save()
        return len(self.entries) - 1

    def settle(self, index: int, usage: dict) -> None:
        if "input_tokens" not in usage or "output_tokens" not in usage:
            return  # Retain the conservative reservation when usage is unknown.
        entry = self.entries[index]
        entry.update(status="completed", input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                     charged_usd=usage["input_tokens"] * self.INPUT_RATE + usage["output_tokens"] * self.OUTPUT_RATE)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2) + "\n")
        from cloud_state import checkpoint
        checkpoint()
