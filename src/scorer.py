"""Score a resume against a job description using Claude, returning structured JSON."""
from __future__ import annotations

import json
import re

from .sources import Job

MAX_JD_CHARS = 6000

SYSTEM_PROMPT = """You are a strict, honest recruiter assistant. You compare ONE candidate resume \
with ONE job description and return a fit score.

Rules:
- Base every judgement ONLY on the resume text provided. Never invent or assume experience.
- Penalise hard requirements the resume does not show (required years of experience, required \
languages/frameworks, required degrees, citizenship/PR/work-authorisation restrictions).
- Use the candidate context for eligibility (e.g. citizen/PR-only roles are a poor fit).
- Score 0-100: 85+ apply immediately; 70-84 strong fit; 55-69 stretch worth a tailored resume; \
below 55 weak fit.
- resume_tweaks may only suggest re-ordering, re-wording or emphasising experience that is \
already in the resume. Never suggest adding skills the candidate does not have.
- Reply with ONLY a JSON object, no prose, with exactly these keys:
  "score" (integer 0-100),
  "verdict" (one short sentence),
  "matched" (list of up to 6 short strings: resume strengths matching the JD),
  "missing" (list of up to 6 short strings: JD requirements not shown in the resume),
  "flags" (list of short strings for deal-breakers or risks, e.g. "Requires 4+ years", \
"Citizen/PR only", "Requires Go"; empty list if none),
  "resume_tweaks" (list of up to 3 short strings)."""


def build_user_prompt(resume: str, job: Job, candidate_context: str) -> str:
    return (
        f"CANDIDATE CONTEXT:\n{candidate_context.strip()}\n\n"
        f"RESUME:\n{resume.strip()}\n\n"
        f"JOB: {job.title} at {job.company} ({job.location})\n"
        f"JOB DESCRIPTION:\n{job.description[:MAX_JD_CHARS]}"
    )


def parse_result(text: str) -> dict:
    """Extract and normalise the JSON object from a model reply. Raises ValueError if unusable."""
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model reply")
    data = json.loads(match.group(0))
    score = int(round(float(data["score"])))

    def as_list(key: str) -> list[str]:
        value = data.get(key) or []
        return [str(v) for v in value][:6] if isinstance(value, list) else [str(value)]

    return {
        "score": max(0, min(100, score)),
        "verdict": str(data.get("verdict", "")),
        "matched": as_list("matched"),
        "missing": as_list("missing"),
        "flags": as_list("flags"),
        "resume_tweaks": as_list("resume_tweaks")[:3],
    }


SCORE_TOOL = {
    "name": "record_score",
    "description": "Record the fit assessment for this resume and job.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "verdict": {"type": "string"},
            "matched": {"type": "array", "items": {"type": "string"}},
            "missing": {"type": "array", "items": {"type": "string"}},
            "flags": {"type": "array", "items": {"type": "string"}},
            "resume_tweaks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["score", "verdict", "matched", "missing", "flags", "resume_tweaks"],
    },
}


def extract_result(reply) -> dict:
    """Read the score from a tool_use block if present, otherwise from JSON in the text."""
    for block in reply.content:
        if getattr(block, "type", "") == "tool_use":
            return parse_result(json.dumps(block.input))
    text = "".join(getattr(b, "text", "") for b in reply.content if getattr(b, "type", "") == "text")
    try:
        return parse_result(text)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        stop = getattr(reply, "stop_reason", "?")
        snippet = text.strip().replace("\n", " ")[:120] or "<no text>"
        raise ValueError(f"{exc} (stop_reason={stop}; reply starts: {snippet})") from exc


def score_job(client, model: str, resume: str, job: Job, candidate_context: str) -> dict:
    """Call Claude for one job. Forces structured output via a tool; falls back to plain JSON text.
    On failure returns a dict with score None and an error message."""
    base = dict(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(resume, job, candidate_context)}],
    )
    attempts = [
        dict(base, tools=[SCORE_TOOL], tool_choice={"type": "tool", "name": "record_score"}),
        base,  # fallback if the tool call is rejected or malformed
    ]
    last_error = ""
    for kwargs in attempts:
        try:
            return extract_result(client.messages.create(**kwargs))
        except Exception as exc:
            last_error = str(exc)
    return {"score": None, "error": last_error}
