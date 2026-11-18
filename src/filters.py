"""Cheap filtering, de-duplication state, and keyword pre-ranking (no LLM involved)."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from .sources import Job

STOPWORDS = set(
    """a an and are as at be by for from has have in is it its of on or our that the their this to
    with you your we will can able etc using use work working experience years year team teams
    role job strong good must should including such other more also than into across within
    ability skills knowledge understanding required preferred plus""".split()
)


def title_ok(title: str, include: list[str], exclude: list[str]) -> bool:
    t = title.lower()
    if any(word.lower() in t for word in exclude):
        return False
    return any(word.lower() in t for word in include)


def location_ok(location: str, wanted: list[str], excluded: list[str] | None = None) -> bool:
    loc = (location or "").lower()
    if any(x.lower() in loc for x in (excluded or [])):
        return False
    return any(w.lower() in loc for w in wanted)


def filter_jobs(jobs: list[Job], search: dict) -> list[Job]:
    include = search.get("title_include", [])
    exclude = search.get("title_exclude", [])
    locations = search.get("locations", [])
    loc_exclude = search.get("location_exclude", [])
    return [
        j
        for j in jobs
        if title_ok(j.title, include, exclude) and location_ok(j.location, locations, loc_exclude)
    ]


# ---- seen-jobs state ----------------------------------------------------------------------

def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, str], keep_days: int = 120) -> None:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pruned, indent=2, sort_keys=True))


def only_new(jobs: list[Job], state: dict[str, str]) -> list[Job]:
    return [j for j in jobs if j.key not in state]


# ---- keyword pre-ranking ------------------------------------------------------------------

def terms(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9+#.\-]{1,}", text.lower())
    return {w.strip(".-") for w in words if w not in STOPWORDS and len(w) > 2}


def prescore(resume: str, job: Job) -> float:
    """Share of the job's distinctive terms that also appear in the resume (0..1)."""
    jd_terms = terms(job.title + " " + job.description)
    if not jd_terms:
        return 0.0
    return len(jd_terms & terms(resume)) / len(jd_terms)


def rank_by_prescore(resume: str, jobs: list[Job], keep: int) -> list[tuple[Job, float]]:
    scored = [(j, prescore(resume, j)) for j in jobs]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:keep]
