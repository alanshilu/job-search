"""Fetch jobs from public job-board APIs (Greenhouse and Lever)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import requests

TIMEOUT = 20
HEADERS = {"User-Agent": "job-agent/1.0 (personal job search tool)"}


@dataclass(frozen=True)
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str

    @property
    def key(self) -> str:
        """Stable identifier used for de-duplication across runs."""
        return f"{self.source}:{self.company}:{self.id}"


def clean_html(raw: str) -> str:
    """Convert (possibly escaped) HTML into readable plain text."""
    text = html.unescape(raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _company_name(token: str) -> str:
    return token.replace("-", " ").replace("_", " ").title()


def fetch_greenhouse(token: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    resp = requests.get(url, params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for item in resp.json().get("jobs", []):
        jobs.append(
            Job(
                id=str(item["id"]),
                title=item.get("title", ""),
                company=_company_name(token),
                location=(item.get("location") or {}).get("name", ""),
                url=item.get("absolute_url", ""),
                description=clean_html(item.get("content", "")),
                source="greenhouse",
            )
        )
    return jobs


def fetch_lever(company: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company}"
    resp = requests.get(url, params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for item in resp.json():
        parts = [item.get("descriptionPlain", "")]
        for section in item.get("lists", []) or []:
            parts.append(section.get("text", ""))
            parts.append(clean_html(section.get("content", "")))
        parts.append(item.get("additionalPlain", ""))
        jobs.append(
            Job(
                id=str(item["id"]),
                title=item.get("text", ""),
                company=_company_name(company),
                location=(item.get("categories") or {}).get("location", "") or "",
                url=item.get("hostedUrl", ""),
                description="\n".join(p for p in parts if p).strip(),
                source="lever",
            )
        )
    return jobs


def fetch_all(boards: dict) -> tuple[list[Job], list[str]]:
    """Fetch every configured board. Returns (jobs, warnings); never raises on one bad board."""
    jobs: list[Job] = []
    warnings: list[str] = []
    fetchers = {"greenhouse": fetch_greenhouse, "lever": fetch_lever}
    for source, tokens in (boards or {}).items():
        fetch = fetchers.get(source)
        if fetch is None:
            warnings.append(f"Unknown source '{source}' skipped")
            continue
        for token in tokens or []:
            try:
                jobs.extend(fetch(token))
            except Exception as exc:  # network error, bad token, bad JSON...
                warnings.append(f"{source}/{token}: {exc}")
    return jobs, warnings
