"""Fetch jobs from public job-board APIs (Greenhouse and Lever)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import requests

TIMEOUT = 20
HEADERS = {"User-Agent": "job-agent/1.0 (personal job search tool)"}


class MissingKey(RuntimeError):
    """Raised when a source needs an API key that isn't configured; the whole source is skipped."""


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


def fetch_ashby(name: str) -> list[Job]:
    """Ashby job boards (jobs.ashbyhq.com/<name>)."""
    resp = requests.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{name}",
        params={"includeCompensation": "false"}, headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for item in resp.json().get("jobs", []):
        jobs.append(
            Job(
                id=str(item.get("id", item.get("jobUrl", ""))),
                title=item.get("title", ""),
                company=_company_name(name),
                location=item.get("location", "") or "",
                url=item.get("jobUrl", ""),
                description=item.get("descriptionPlain") or clean_html(item.get("descriptionHtml", "")),
                source="ashby",
            )
        )
    return jobs


def fetch_mycareersfuture(query: str) -> list[Job]:
    """Singapore government job portal, keyword search. Token = the search phrase."""
    last_exc = None
    for _ in range(2):  # the portal is slow; retry once with a longer timeout
        try:
            resp = requests.get(
                "https://api.mycareersfuture.gov.sg/v2/jobs",
                params={"search": query, "limit": 50, "page": 0, "sortBy": "new_posting_date"},
                headers=HEADERS, timeout=60,
            )
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
    else:
        raise last_exc
    jobs = []
    for item in resp.json().get("results", []):
        meta = item.get("metadata") or {}
        jobs.append(
            Job(
                id=str(item.get("uuid", "")),
                title=item.get("title", ""),
                company=(item.get("postedCompany") or {}).get("name", "Unknown"),
                location="Singapore",
                url=meta.get("jobDetailsUrl", ""),
                description=clean_html(item.get("description", "")),
                source="mycareersfuture",
            )
        )
    return jobs


def fetch_adzuna(token: str) -> list[Job]:
    """Adzuna aggregator. Token = '<country code>:<search phrase>', e.g. 'gb:graduate software engineer'.
    Needs free ADZUNA_APP_ID and ADZUNA_APP_KEY (developer.adzuna.com)."""
    import os

    country, _, query = token.partition(":")
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise MissingKey("ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
    resp = requests.get(
        f"https://api.adzuna.com/v1/api/jobs/{country.strip()}/search/1",
        params={"app_id": app_id, "app_key": app_key, "results_per_page": 50,
                "what": query.strip(), "sort_by": "date", "content-type": "application/json"},
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for item in resp.json().get("results", []):
        jobs.append(
            Job(
                id=str(item.get("id", "")),
                title=item.get("title", ""),
                company=(item.get("company") or {}).get("display_name", "Unknown"),
                location=(item.get("location") or {}).get("display_name", ""),
                url=item.get("redirect_url", ""),
                description=clean_html(item.get("description", "")),
                source="adzuna",
            )
        )
    return jobs


def fetch_remotive(query: str) -> list[Job]:
    """Remotive remote-jobs API (remotive.com). Token = search phrase."""
    resp = requests.get("https://remotive.com/api/remote-jobs",
                        params={"search": query, "limit": 100}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for item in resp.json().get("jobs", []):
        loc = item.get("candidate_required_location") or "Worldwide"
        jobs.append(Job(
            id=str(item.get("id", "")), title=item.get("title", ""),
            company=item.get("company_name", "Unknown"), location=f"Remote ({loc})",
            url=item.get("url", ""), description=clean_html(item.get("description", "")),
            source="remotive"))
    return jobs


def fetch_remoteok(tag: str) -> list[Job]:
    """RemoteOK public API (remoteok.com). Token = a tag such as 'python' or 'junior'."""
    resp = requests.get("https://remoteok.com/api", params={"tag": tag}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for item in resp.json():
        if not isinstance(item, dict) or "position" not in item:  # first element is a legal notice
            continue
        loc = item.get("location") or "Worldwide"
        jobs.append(Job(
            id=str(item.get("id", "")), title=item.get("position", ""),
            company=item.get("company", "Unknown"), location=f"Remote ({loc})",
            url=item.get("url", ""), description=clean_html(item.get("description", "")),
            source="remoteok"))
    return jobs


def fetch_all(boards: dict) -> tuple[list[Job], list[str]]:
    """Fetch every configured board. Returns (jobs, warnings); never raises on one bad board."""
    jobs: list[Job] = []
    warnings: list[str] = []
    fetchers = {
        "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
        "mycareersfuture": fetch_mycareersfuture, "adzuna": fetch_adzuna,
        "remotive": fetch_remotive, "remoteok": fetch_remoteok,
    }
    for source, tokens in (boards or {}).items():
        fetch = fetchers.get(source)
        if fetch is None:
            warnings.append(f"Unknown source '{source}' skipped")
            continue
        for token in tokens or []:
            try:
                jobs.extend(fetch(token))
            except MissingKey as exc:  # no key: skip the rest of this source with one note
                warnings.append(f"{source} skipped: {exc}")
                break
            except Exception as exc:  # network error, bad token, bad JSON...
                warnings.append(f"{source}/{token}: {type(exc).__name__}: {str(exc)[:160]}")
    return jobs, warnings
