"""Build and send the HTML digest email through Gmail SMTP."""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from .sources import Job


def _chips(items: list[str], color: str) -> str:
    return " ".join(
        f'<span style="background:{color};border-radius:10px;padding:2px 8px;'
        f'font-size:12px;margin-right:4px;display:inline-block">{escape(i)}</span>'
        for i in items
    )


def build_subject(rows: list[tuple[Job, dict]]) -> str:
    if not rows:
        return "Job alerts: no new matches today"
    top = rows[0]
    return f"Job alerts: {len(rows)} new matches (top: {top[1]['score']} - {top[0].company})"


def build_html(rows: list[tuple[Job, dict]], warnings: list[str] | None = None) -> str:
    cards = []
    for job, res in rows:
        score = res["score"]
        color = "#1a7f37" if score >= 80 else "#9a6700" if score >= 65 else "#57606a"
        flags = _chips(res.get("flags", []), "#ffebe9")
        matched = _chips(res.get("matched", []), "#dafbe1")
        missing = _chips(res.get("missing", []), "#fff8c5")
        tweaks = "".join(f"<li>{escape(t)}</li>" for t in res.get("resume_tweaks", []))
        cards.append(
            f"""
<div style="border:1px solid #d0d7de;border-radius:8px;padding:14px;margin:12px 0">
  <div style="font-size:22px;font-weight:700;color:{color}">{score}/100</div>
  <div style="font-size:16px;font-weight:600">
    <a href="{escape(job.url)}" style="color:#0969da;text-decoration:none">{escape(job.title)}</a>
  </div>
  <div style="color:#57606a;margin-bottom:6px">{escape(job.company)} &middot; {escape(job.location)}</div>
  <div style="margin-bottom:6px">{escape(res.get('verdict', ''))}</div>
  {f'<div style="margin:4px 0"><b>Risks:</b> {flags}</div>' if flags else ''}
  {f'<div style="margin:4px 0"><b>Matches:</b> {matched}</div>' if matched else ''}
  {f'<div style="margin:4px 0"><b>Missing:</b> {missing}</div>' if missing else ''}
  {f'<div style="margin:4px 0"><b>Resume tweaks:</b><ul style="margin:4px 0">{tweaks}</ul></div>' if tweaks else ''}
</div>"""
        )
    warn = ""
    if warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in warnings)
        warn = f'<p style="color:#9a6700"><b>Heads up:</b></p><ul>{items}</ul>'
    body = "".join(cards) or "<p>No new jobs matched your filters today.</p>"
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:auto">'
        f"<h2>Your daily job matches</h2>{body}{warn}"
        '<p style="color:#8c959f;font-size:12px">Scores are AI estimates based on your resume text. '
        "Always read the full posting before applying.</p></div>"
    )


def send_email(user: str, app_password: str, to: str, subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText("Open this email in an HTML-capable client to view your job matches.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, app_password)
        server.sendmail(user, [to], msg.as_string())
