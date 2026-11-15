# Job Agent

A small agent that checks company job boards every morning, scores each new job against your
resume with Claude, and emails you a ranked digest. It runs free on GitHub Actions.

```
Greenhouse / Lever APIs --> filter (title, location) --> drop already-seen jobs
   --> keyword pre-rank --> Claude scores resume vs JD (JSON) --> HTML email (best first)
```

## Why it is built this way
- **Public job-board APIs, not scraping.** Stable, allowed, and returns full job descriptions.
- **Two-stage scoring.** A free keyword pass ranks everything, then Claude scores only the top
  `max_jobs_to_score` jobs, so the API cost stays small and predictable.
- **Honest scoring.** The prompt forbids inventing experience, penalises unmet hard requirements
  (years, languages, citizen/PR-only), and only suggests re-wording what is already on the resume.
- **Never repeats a job.** `state/seen_jobs.json` is committed back to the repo after each run.
  Jobs that fail to score are *not* marked seen, so they are retried next run.
- **Tested.** `pytest` covers filters, state, parsers, scoring retries, email escaping and the
  full pipeline using fake network and fake Claude clients.

## Setup (about 15 minutes)

1. **Create a GitHub repo** and push this folder. (`resume.txt` is git-ignored on purpose.)
2. **Anthropic API key:** create one at console.anthropic.com.
3. **Gmail app password:** turn on 2-step verification, then create an App Password at
   myaccount.google.com/apppasswords. Use that, not your normal password.
4. In the repo go to **Settings > Secrets and variables > Actions** and add:

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your Anthropic key |
   | `GMAIL_ADDRESS` | the Gmail address that sends (and receives) the email |
   | `GMAIL_APP_PASSWORD` | the 16-character app password |
   | `RESUME_TEXT` | paste the full text of `resume.txt` (no phone number needed) |

5. Open the **Actions** tab, choose *Daily job alerts*, and click **Run workflow** to test.
   After that it runs every day at 8:00 am Singapore time.

## Run locally

```bash
pip install -r requirements.txt
python main.py --dry-run --no-llm        # keyword scoring only, no keys needed, nothing is sent
python main.py --dry-run                 # real Claude scoring (needs ANTHROPIC_API_KEY), nothing is sent
python main.py                           # full run: also needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD
pytest -q
```

## Customise (edit `config.yaml`, no code changes)
- `search.title_include / title_exclude / locations`: what counts as a relevant job.
- `boards`: add company tokens. Open `https://boards-api.greenhouse.io/v1/boards/<token>/jobs`
  (or `https://api.lever.co/v0/postings/<company>?mode=json`) in a browser to confirm a token works.
- `candidate_context`: what Claude should know about you (keep it true; it drives eligibility flags).
- `min_score_to_email`, `max_jobs_to_score`: quality bar and cost cap.

## Limits to know about
- Only companies that publish on Greenhouse or Lever are covered. Workday, SuccessFactors and
  most bank career sites are not (add a new fetcher in `src/sources.py` for those).
- Scores are AI estimates, not ATS results. Read the posting before applying.
- Gmail may put the first email in spam; mark it "not spam" once.
