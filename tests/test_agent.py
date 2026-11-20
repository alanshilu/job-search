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


def test_score_job_reads_forced_tool_output():
    block = SimpleNamespace(type="tool_use", input={"score": 77, "verdict": "ok", "matched": [], "missing": [],
                                                     "flags": [], "resume_tweaks": []})
    class C:
        def __init__(self): self.messages = self; self.kwargs = None
        def create(self, **kw):
            self.kwargs = kw
            return SimpleNamespace(content=[block], stop_reason="tool_use")
    c = C()
    res = scorer.score_job(c, "m", "r", make_job(), "ctx")
    assert res["score"] == 77 and c.kwargs["tool_choice"]["name"] == "record_score"


def test_parse_failure_error_includes_diagnostics():
    client = FakeClient(["", ""])
    res = scorer.score_job(client, "m", "r", make_job(), "ctx")
    assert "stop_reason" in res["error"]


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
    patch_fetch(monkeypatch, [make_job("1"), make_job("2", title="Software Engineer Intern")])
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


# ---- extra sources (payload shapes are from public docs/memory; live use is untested) ----

def _fake_get(payload):
    return lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def test_fetch_ashby_parses_payload(monkeypatch):
    payload = {"jobs": [{"id": "a1", "title": "SWE Intern", "location": "Singapore",
                         "jobUrl": "https://a.test/a1", "descriptionPlain": "Build"}]}
    monkeypatch.setattr(sources.requests, "get", _fake_get(payload))
    job = sources.fetch_ashby("ramp")[0]
    assert (job.source, job.title, job.description) == ("ashby", "SWE Intern", "Build")


def test_fetch_mycareersfuture_parses_payload(monkeypatch):
    payload = {"results": [{"uuid": "u1", "title": "Software Engineer", "postedCompany": {"name": "Acme"},
                            "metadata": {"jobDetailsUrl": "https://m.test/u1"}, "description": "&lt;p&gt;Hi&lt;/p&gt;"}]}
    monkeypatch.setattr(sources.requests, "get", _fake_get(payload))
    job = sources.fetch_mycareersfuture("software engineer")[0]
    assert (job.company, job.location, job.description) == ("Acme", "Singapore", "Hi")


def test_fetch_adzuna_needs_keys_and_parses(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    with pytest.raises(sources.MissingKey):
        sources.fetch_adzuna("gb:graduate")
    monkeypatch.setenv("ADZUNA_APP_ID", "i")
    monkeypatch.setenv("ADZUNA_APP_KEY", "k")
    payload = {"results": [{"id": 7, "title": "Grad SWE", "company": {"display_name": "Acme"},
                            "location": {"display_name": "London"}, "redirect_url": "https://z.test/7",
                            "description": "python"}]}
    monkeypatch.setattr(sources.requests, "get", _fake_get(payload))
    job = sources.fetch_adzuna("gb:graduate")[0]
    assert (job.company, job.location, job.source) == ("Acme", "London", "adzuna")


def test_location_exclude_drops_us_only_remote():
    search = dict(SEARCH, location_exclude=["united states", "us only"])
    jobs = [make_job("1", location="Remote (Worldwide)"), make_job("2", location="Remote (United States)")]
    assert [j.id for j in filters.filter_jobs(jobs, search)] == ["1"]


def test_graduate_programme_titles_pass():
    inc = ["graduate", "trainee"]
    assert filters.title_ok("Technology Graduate Programme 2027", inc, ["senior"])
    assert filters.title_ok("IT Trainee", inc, ["senior"])


def test_fetch_remotive_parses_payload(monkeypatch):
    payload = {"jobs": [{"id": 5, "title": "Junior Dev", "company_name": "Acme", "url": "https://r.test/5",
                         "candidate_required_location": "Worldwide", "description": "<p>Python</p>"}]}
    monkeypatch.setattr(sources.requests, "get", _fake_get(payload))
    job = sources.fetch_remotive("junior")[0]
    assert (job.location, job.description, job.source) == ("Remote (Worldwide)", "Python", "remotive")


def test_fetch_remoteok_skips_legal_notice(monkeypatch):
    payload = [{"legal": "notice"}, {"id": "9", "position": "Python Engineer", "company": "Acme",
                                     "location": "", "url": "https://ro.test/9", "description": "x"}]
    monkeypatch.setattr(sources.requests, "get", _fake_get(payload))
    jobs = sources.fetch_remoteok("python")
    assert len(jobs) == 1 and jobs[0].location == "Remote (Worldwide)"


def test_tech_word_requirement_drops_non_tech_junior_roles():
    search = dict(SEARCH, title_include=["junior", "graduate"], title_must_have_tech_word=["engineer", "data", "ai"])
    jobs = [make_job("1", title="Junior Data Engineer"), make_job("2", title="Junior Contact Center Agent"),
            make_job("3", title="Junior Retail Associate"), make_job("4", title="Graduate AI Developer")]
    assert [j.id for j in filters.filter_jobs(jobs, search)] == ["1", "4"]


def test_dedupe_same_company_and_title_across_sources():
    a = make_job("1", title="Full-Stack Developer")
    b = Job(id="x", title="Full Stack Developer", company="ACME", location="Remote", url="u", description="d", source="remoteok")
    assert len(filters.dedupe([a, b])) == 1


def test_missing_key_skips_whole_source_with_one_warning(monkeypatch):
    def nokey(token):
        raise sources.MissingKey("no key")
    monkeypatch.setattr(sources, "fetch_adzuna", nokey)
    _, warnings = sources.fetch_all({"adzuna": ["a", "b", "c"]})
    assert warnings == ["adzuna skipped: no key"]
