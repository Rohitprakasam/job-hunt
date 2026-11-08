"""Read-only local console on http://127.0.0.1:8765: what was found, prepared and sent, with proof.

Never bound to anything but 127.0.0.1. No forms, no actions: changes go through the `agent` CLI.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from agent import db, paths

HOST, PORT = "127.0.0.1", 8765
STYLE = """
<style>
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e6;--ok:#137333;--bad:#b3261e;--accent:#0b57d0}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--muted:#9a9aa2;--line:#2c2c31;--ok:#6dd58c;--bad:#f2b8b5;--accent:#a8c7fa}}
body{font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:1100px;padding:16px}
a{color:var(--accent)} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600} .ok{color:var(--ok)} .bad{color:var(--bad)} nav a{margin-right:14px} .muted{color:var(--muted)}
img{max-width:100%;border:1px solid var(--line)}
</style>"""


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _href(url: object) -> str:
    """Scraped links are untrusted: only http(s) becomes a link."""
    text = str(url or "")
    return _e(text) if text.startswith(("https://", "http://")) else "#"


def _page(title: str, body: str) -> HTMLResponse:
    nav = '<nav><a href="/">Applications</a><a href="/jobs">Top jobs</a><a href="/questions">Questions</a></nav>'
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{_e(title)}</title>{STYLE}{nav}<h1>{_e(title)}</h1>{body}"
    )


def create_app(db_path: Path | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def conn():
        return db.connect(db_path)

    @app.get("/", response_class=HTMLResponse)
    def applications() -> HTMLResponse:
        c = conn()
        rows = c.execute(
            "SELECT a.id, a.status, a.submitted_at, a.skip_reason, j.title, j.company, s.fit FROM applications a"
            " JOIN jobs j ON j.id = a.job_id LEFT JOIN scores s ON s.job_id = j.id ORDER BY a.id DESC LIMIT 200"
        ).fetchall()
        c.close()
        body = (
            "<table><tr><th>#</th><th>job</th><th>fit</th><th>status</th><th>submitted</th><th>note</th></tr>"
        )
        for r in rows:
            body += (
                f"<tr><td><a href='/applications/{r['id']}'>{r['id']}</a></td><td>{_e(r['title'])} — {_e(r['company'])}</td>"
                f"<td>{_e(round(r['fit']) if r['fit'] is not None else '')}</td><td>{_e(r['status'])}</td>"
                f"<td>{_e(r['submitted_at'] or '')}</td><td class=muted>{_e(r['skip_reason'] or '')}</td></tr>"
            )
        return _page("Applications", body + "</table>")

    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application(app_id: int) -> HTMLResponse:
        c = conn()
        a = c.execute(
            "SELECT a.*, j.title, j.company, j.apply_url, j.url FROM applications a JOIN jobs j ON j.id = a.job_id"
            " WHERE a.id = ?", (app_id,),
        ).fetchone()  # fmt: skip
        attempts = c.execute(
            "SELECT * FROM apply_attempts WHERE application_id = ? ORDER BY id", (app_id,)
        ).fetchall()
        c.close()
        if a is None:
            raise HTTPException(404)
        gates = json.loads(a["gate_results_json"] or "[]")
        body = f"<p>{_e(a['title'])} — {_e(a['company'])} · <a href='{_href(a['apply_url'] or a['url'])}'>posting</a> · status <b>{_e(a['status'])}</b></p>"
        body += (
            "<h2>Gates</h2><table>"
            + "".join(
                f"<tr><td>{_e(g['gate'])}</td><td class={'ok' if g['ok'] else 'bad'}>{'pass' if g['ok'] else 'fail'}</td>"
                f"<td>{_e(g.get('detail'))}</td></tr>"
                for g in gates
            )
            + "</table>"
        )
        body += (
            "<h2>Attempts</h2><table>"
            + "".join(
                f"<tr><td>{_e(t['mode'])}</td><td>{_e(t['outcome'])}</td><td>{_e(t['started_at'])}</td><td>{_e(t['error'] or '')}</td></tr>"
                for t in attempts
            )
            + "</table><h2>Files</h2><ul>"
        )
        folder = paths.output_dir() / "applications" / f"{app_id:05d}"
        files = sorted(folder.glob("*")) if folder.exists() else []
        body += "".join(
            f"<li><a href='/files/{app_id}/{_e(f.name)}'>{_e(f.name)}</a></li>" for f in files if f.is_file()
        )
        body += "</ul>"
        for f in files:
            if f.suffix == ".png":
                body += f"<h3>{_e(f.name)}</h3><img src='/files/{app_id}/{_e(f.name)}' alt='{_e(f.name)}'>"
        return _page(f"Application {app_id}", body)

    @app.get("/files/{app_id}/{name}")
    def files(app_id: int, name: str) -> FileResponse:
        folder = (paths.output_dir() / "applications" / f"{app_id:05d}").resolve()
        target = (folder / name).resolve()
        if target.parent != folder or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs() -> HTMLResponse:
        c = conn()
        rows = c.execute(
            "SELECT j.id, j.title, j.company, j.location, j.source, j.apply_url, s.fit, s.reasons_json, s.gaps_json"
            " FROM jobs j JOIN scores s ON s.job_id = j.id WHERE j.filter_reason IS NULL ORDER BY s.priority DESC LIMIT 100"
        ).fetchall()
        c.close()
        body = (
            "<table><tr><th>fit</th><th>job</th><th>source</th><th>brief</th><th>why</th><th>gaps</th></tr>"
        )
        briefs_folder = paths.briefs_dir()
        for r in rows:
            has_brief = (briefs_folder / f"{r['id']}.md").is_file()
            brief_html = (
                f"<a href='/briefs/{r['id']}'>brief</a>" if has_brief else "<span class=muted>-</span>"
            )
            body += (
                f"<tr><td>{round(r['fit'])}</td><td><a href='{_href(r['apply_url'])}'>{_e(r['title'])}</a> — {_e(r['company'])}"
                f"<div class=muted>{_e(r['location'] or '')}</div></td><td>{_e(r['source'])}</td>"
                f"<td>{brief_html}</td>"
                f"<td>{_e('; '.join(json.loads(r['reasons_json'] or '[]')))}</td>"
                f"<td class=muted>{_e(', '.join(json.loads(r['gaps_json'] or '[]')))}</td></tr>"
            )
        return _page("Top jobs", body + "</table>")

    @app.get("/briefs/{job_id}", response_class=HTMLResponse)
    def brief_view(job_id: int) -> HTMLResponse:
        brief_file = paths.briefs_dir() / f"{job_id}.md"
        if not brief_file.is_file():
            raise HTTPException(404, "Research brief not found")
        content = brief_file.read_text(encoding="utf-8")
        job = None
        try:
            c = conn()
            job = c.execute(
                "SELECT title, company, apply_url, url FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            c.close()
        except Exception:
            pass
        title_str = f"Brief: {job['title']} at {job['company']}" if job else f"Brief for Job #{job_id}"
        body = (
            f"<p><a href='/jobs'>&larr; Back to jobs</a></p>"
            f"<div style='white-space: pre-wrap; font-family: system-ui, sans-serif; background: var(--line); padding: 16px; border-radius: 8px;'>{_e(content)}</div>"
        )
        return _page(title_str, body)

    @app.get("/questions", response_class=HTMLResponse)
    def questions() -> HTMLResponse:
        c = conn()
        rows = c.execute("SELECT * FROM pending_questions ORDER BY seen_count DESC").fetchall()
        c.close()
        body = '<p class=muted>Answer once with: agent answers set "&lt;question&gt;" "&lt;answer&gt;"</p><table>'
        body += "".join(f"<tr><td>{_e(r['question_raw'])}</td><td>{r['seen_count']}×</td></tr>" for r in rows)
        return _page("Unanswered questions", body + "</table>")

    return app


def serve() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")
