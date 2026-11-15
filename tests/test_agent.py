import json
from types import SimpleNamespace

import pytest

import main
from src import emailer, filters, scorer, sources
from src.sources import Job


def make_job(i="1", title="Software Engineer, New Grad", location="Singapore", desc="python react fastapi"):
    return Job(id=i, title=title, company="Acme", location=location,
               url=f"https://x.test/{i}", description=desc, source="greenhouse")


SEARCH = {
    "title_include": ["software engineer", "intern"],
    "title_exclude": ["senior", "staff"],
    "locations": ["singapore", "remote"],
}


# ---- filters --------------------------------------------------------------------------

def test_title_include_and_exclude():
    assert filters.title_ok("Software Engineer Intern", SEARCH["title_include"], SEARCH["title_exclude"])
    assert not filters.title_ok("Senior Software Engineer", SEARCH["title_include"], SEARCH["title_exclude"])
    assert not filters.title_ok("Account Executive", SEARCH["title_include"], SEARCH["title_exclude"])


def test_location_filter_is_case_insensitive():
    assert filters.location_ok("Singapore, SG", SEARCH["locations"])
    assert filters.location_ok("REMOTE - APAC", SEARCH["locations"])
    assert not filters.location_ok("Berlin", SEARCH["locations"])
    assert not filters.location_ok("", SEARCH["locations"])


def test_filter_jobs_combines_title_and_location():
    jobs = [make_job("1"), make_job("2", title="Senior Software Engineer"), make_job("3", location="Berlin")]
    assert [j.id for j in filters.filter_jobs(jobs, SEARCH)] == ["1"]


def test_state_roundtrip_and_dedupe(tmp_path):
    path = tmp_path / "seen.json"
    jobs = [make_job("1"), make_job("2")]
    assert filters.only_new(jobs, filters.load_state(path)) == jobs  # empty state: all new
    filters.save_state(path, {jobs[0].key: "2999-01-01"})
    assert [j.id for j in filters.only_new(jobs, filters.load_state(path))] == ["2"]


def test_state_prunes_old_entries(tmp_path):
    path = tmp_path / "seen.json"
    filters.save_state(path, {"old": "2000-01-01", "new": "2999-01-01"})
    assert list(json.loads(path.read_text())) == ["new"]


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert filters.load_state(path) == {}


def test_prescore_ranks_relevant_job_higher():
    resume = "Python FastAPI React TypeScript Docker PostgreSQL Apache Flink"
    good = make_job("1", desc="We use Python, FastAPI, React and PostgreSQL daily")
    bad = make_job("2", desc="Cobol mainframe assembler fortran punchcards")
    ranked = filters.rank_by_prescore(resume, [bad, good], keep=2)
    assert ranked[0][0].id == "1"
    assert ranked[0][1] > ranked[1][1]


# ---- sources --------------------------------------------------------------------------

def test_clean_html_strips_tags_and_entities():
    raw = "&lt;p&gt;Build &amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    text = sources.clean_html(raw)
    assert "<" not in text and "Build & ship" in text and "Python" in text


def test_fetch_greenhouse_parses_payload(monkeypatch):
    payload = {"jobs": [{"id": 42, "title": "SWE", "location": {"name": "Singapore"},
                         "absolute_url": "https://g.test/42", "content": "&lt;p&gt;Hi&lt;/p&gt;"}]}
    monkeypatch.setattr(sources.requests, "get",
                        lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload))
    job = sources.fetch_greenhouse("mw-tech-grad")[0]
    assert (job.id, job.company, job.location, job.description) == ("42", "Mw Tech Grad", "Singapore", "Hi")


def test_fetch_lever_parses_payload(monkeypatch):
    payload = [{"id": "abc", "text": "Intern", "categories": {"location": "Singapore"},
                "hostedUrl": "https://l.test/abc", "descriptionPlain": "Build things"}]
    monkeypatch.setattr(sources.requests, "get",
                        lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload))
    job = sources.fetch_lever("palantir")[0]
    assert job.title == "Intern" and job.description == "Build things" and job.source == "lever"


def test_fetch_all_survives_a_failing_board(monkeypatch):
    def boom(token):
        raise RuntimeError("403")
    monkeypatch.setattr(sources, "fetch_greenhouse", boom)
    monkeypatch.setattr(sources, "fetch_lever", lambda c: [make_job("9")])
    jobs, warnings = sources.fetch_all({"greenhouse": ["bad"], "lever": ["ok"], "nope": ["x"]})
    assert [j.id for j in jobs] == ["9"]
    assert any("greenhouse/bad" in w for w in warnings) and any("Unknown source" in w for w in warnings)


# ---- scorer ---------------------------------------------------------------------------

GOOD_REPLY = '''Sure! {"score": 82.4, "verdict": "Strong fit", "matched": ["Python"],
"missing": ["Go"], "flags": [], "resume_tweaks": ["a","b","c","d"]}'''


def test_parse_result_normalises():
    res = scorer.parse_result(GOOD_REPLY)
    assert res["score"] == 82 and res["matched"] == ["Python"] and len(res["resume_tweaks"]) == 3


def test_parse_result_clamps_score():
    assert scorer.parse_result('{"score": 250}')["score"] == 100
    assert scorer.parse_result('{"score": -5}')["score"] == 0


def test_parse_result_rejects_garbage():
    with pytest.raises(ValueError):
        scorer.parse_result("no json here")


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])


def test_score_job_retries_once_then_succeeds():
    client = FakeClient([RuntimeError("rate limit"), GOOD_REPLY])
    res = scorer.score_job(client, "m", "resume", make_job(), "ctx")
    assert res["score"] == 82 and client.calls == 2


def test_score_job_returns_error_after_two_failures():
    client = FakeClient(["garbage", "still garbage"])
    res = scorer.score_job(client, "m", "resume", make_job(), "ctx")
    assert res["score"] is None and "error" in res


def test_prompt_truncates_long_descriptions():
    job = make_job(desc="x" * 50000)
    assert len(scorer.build_user_prompt("r", job, "c")) < scorer.MAX_JD_CHARS + 1000


# ---- emailer --------------------------------------------------------------------------

def test_email_html_escapes_untrusted_text():
    job = make_job(title="<script>alert(1)</script>")
    res = {"score": 90, "verdict": "<b>x</b>", "matched": ["<i>m</i>"], "missing": [], "flags": [], "resume_tweaks": []}
    html_body = emailer.build_html([(job, res)])
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body


def test_subject_handles_empty_and_non_empty():
    assert "no new matches" in emailer.build_subject([])
    res = {"score": 91}
    assert "91" in emailer.build_subject([(make_job(), res)])


# ---- end to end -----------------------------------------------------------------------

CFG = {
    "model": "m", "min_score_to_email": 60, "max_jobs_to_score": 5, "prefilter_keep_top": 10,
    "candidate_context": "ctx", "search": SEARCH, "boards": {"greenhouse": ["x"]},
}


def patch_fetch(monkeypatch, jobs):
    monkeypatch.setattr(main.sources, "fetch_all", lambda boards: (jobs, ["note"]))


def test_dry_run_sends_nothing_and_keeps_state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    patch_fetch(monkeypatch, [make_job("1")])
    sent = []
    monkeypatch.setattr(main.emailer, "send_email", lambda *a, **k: sent.append(a))
    results = main.run(CFG, "python react", dry_run=True, use_llm=True, client=FakeClient([GOOD_REPLY]))
    assert len(results) == 1 and not sent and not (tmp_path / "seen.json").exists()


def test_real_run_emails_once_and_never_repeats_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    patch_fetch(monkeypatch, [make_job("1"), make_job("2")])
    sent = []
    monkeypatch.setattr(main.emailer, "send_email", lambda *a, **k: sent.append(a))
    client = FakeClient([GOOD_REPLY, GOOD_REPLY])
    main.run(CFG, "python react", dry_run=False, use_llm=True, client=client)
    assert len(sent) == 1 and client.calls == 2
    # second run: same jobs are now "seen", so nothing is scored again
    main.run(CFG, "python react", dry_run=False, use_llm=True, client=client)
    assert client.calls == 2


def test_low_scores_are_not_emailed(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    patch_fetch(monkeypatch, [make_job("1")])
    low = '{"score": 30, "verdict": "weak", "matched": [], "missing": [], "flags": [], "resume_tweaks": []}'
    results = main.run(CFG, "python", dry_run=True, use_llm=True, client=FakeClient([low]))
    assert results == []


def test_scoring_failure_leaves_job_unseen_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    patch_fetch(monkeypatch, [make_job("1")])
    monkeypatch.setattr(main.emailer, "send_email", lambda *a, **k: None)
    main.run(CFG, "python", dry_run=False, use_llm=True, client=FakeClient(["bad", "bad"]))
    assert filters.load_state(tmp_path / "seen.json") == {}


def test_no_email_when_nothing_to_report(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(main.sources, "fetch_all", lambda boards: ([], []))
    sent = []
    monkeypatch.setattr(main.emailer, "send_email", lambda *a, **k: sent.append(a))
    main.run(CFG, "python", dry_run=False, use_llm=False)
    assert sent == []


def test_email_sent_when_there_are_warnings_even_without_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setattr(main.sources, "fetch_all", lambda boards: ([], ["greenhouse/x: 404"]))
    sent = []
    monkeypatch.setattr(main.emailer, "send_email", lambda *a, **k: sent.append(a))
    main.run(CFG, "python", dry_run=False, use_llm=False)
    assert len(sent) == 1


def test_no_llm_mode_needs_no_client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    patch_fetch(monkeypatch, [make_job("1", desc="python react fastapi")])
    cfg = dict(CFG, min_score_to_email=0)
    results = main.run(cfg, "python react fastapi", dry_run=True, use_llm=False)
    assert len(results) == 1
