"""Job agent entry point: fetch -> filter -> de-dupe -> pre-rank -> score with Claude -> email."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import yaml

from src import emailer, filters, scorer, sources

STATE_PATH = Path("state/seen_jobs.json")


def load_resume(cfg: dict) -> str:
    env_text = os.environ.get("RESUME_TEXT", "").strip()
    if env_text:
        return env_text
    path = Path(cfg.get("resume_path", "resume.txt"))
    if not path.exists():
        sys.exit(f"Resume not found at {path}. Add it or set the RESUME_TEXT environment variable.")
    return path.read_text()


def run(cfg: dict, resume: str, *, dry_run: bool, use_llm: bool, client=None) -> list:
    all_jobs, warnings = sources.fetch_all(cfg.get("boards", {}))
    print(f"Fetched {len(all_jobs)} jobs from {len(cfg.get('boards', {}))} source type(s).")
    for w in warnings:
        print(f"  warning: {w}")

    matching = filters.dedupe(filters.filter_jobs(all_jobs, cfg.get("search", {})))
    state = filters.load_state(STATE_PATH)
    fresh = filters.only_new(matching, state)
    print(f"{len(matching)} match your filters; {len(fresh)} are new since the last run.")

    ranked = filters.rank_by_prescore(resume, fresh, cfg.get("prefilter_keep_top", 40))
    to_score = ranked[: cfg.get("max_jobs_to_score", 25)]

    results: list[tuple[sources.Job, dict]] = []
    scored_jobs: list[sources.Job] = []
    for job, pre in to_score:
        if use_llm:
            res = scorer.score_job(
                client, cfg["model"], resume, job, cfg.get("candidate_context", "")
            )
            if res.get("score") is None:
                warnings.append(f"Could not score {job.company} - {job.title}: {res.get('error')}")
                continue
        else:  # keyword-only mode (no API key needed)
            res = {
                "score": int(pre * 100),
                "verdict": "Keyword-overlap score only (LLM scoring is off).",
                "matched": [], "missing": [], "flags": [], "resume_tweaks": [],
            }
        scored_jobs.append(job)
        if res["score"] >= cfg.get("min_score_to_email", 60):
            results.append((job, res))

    results.sort(key=lambda pair: pair[1]["score"], reverse=True)
    print(f"Scored {len(scored_jobs)} jobs; {len(results)} are at or above the email threshold.")

    html_body = emailer.build_html(results, warnings)
    subject = emailer.build_subject(results)
    if dry_run:
        for job, res in results:
            print(f"  {res['score']:>3}  {job.company} - {job.title} ({job.location})")
        print(f"[dry run] would send: {subject!r}; state not updated.")
    else:
        if results or warnings:  # skip the daily "nothing today" email, but never hide errors
            user = os.environ["GMAIL_ADDRESS"]
            emailer.send_email(
                user, os.environ["GMAIL_APP_PASSWORD"], os.environ.get("EMAIL_TO", user),
                subject, html_body,
            )
            print("Email sent.")
        else:
            print("Nothing to report; no email sent.")
        today = date.today().isoformat()
        for job in scored_jobs:
            state[job.key] = today
        filters.save_state(STATE_PATH, state)
        print("State saved.")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily job alert agent")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print results, no email, no state change")
    parser.add_argument("--no-llm", action="store_true", help="keyword scoring only (no API key needed)")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    resume = load_resume(cfg)

    client = None
    if not args.no_llm:
        import anthropic

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    run(cfg, resume, dry_run=args.dry_run, use_llm=not args.no_llm, client=client)


if __name__ == "__main__":
    main()
