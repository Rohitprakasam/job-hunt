"""`agent` command line."""

from __future__ import annotations

import difflib
import getpass
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from agent import config, db, paths, quota
from agent import secrets as sec

app = typer.Typer(no_args_is_help=True, help="Job hunt agent.")
secrets_app = typer.Typer(no_args_is_help=True, help="Keychain secrets (values are never printed).")
db_app = typer.Typer(no_args_is_help=True, help="Local SQLite database.")
cloud_app = typer.Typer(no_args_is_help=True, help="MongoDB Atlas (job_agent db).")
llm_app = typer.Typer(no_args_is_help=True, help="Gemini usage.")
profile_app = typer.Typer(no_args_is_help=True, help="Candidate profile.")
for sub, name in (
    (secrets_app, "secrets"),
    (db_app, "db"),
    (cloud_app, "cloud"),
    (llm_app, "llm"),
    (profile_app, "profile"),
):
    app.add_typer(sub, name=name)

console = Console()
ICON = {"ok": "[green]OK[/]", "warn": "[yellow]WARN[/]", "fail": "[red]FAIL[/]", "skip": "[dim]skip[/]"}


def _open_db():
    conn = db.connect()
    db.migrate(conn)
    return conn


# ---------------------------------------------------------------- secrets


@secrets_app.command("set")
def secrets_set(name: str) -> None:
    """Store one secret (typed without echo)."""
    val_name = name.value if hasattr(name, "value") else str(name)
    value = getpass.getpass(f"{val_name}: ")
    sec.set_secret(val_name, value)
    console.print(f"stored {val_name}")


@secrets_app.command("list")
def secrets_list() -> None:
    for name, present in sec.status().items():
        console.print(f"{'[green]present[/]' if present else '[red]missing[/]'}  {name}")


@secrets_app.command("import-env")
def secrets_import_env(path: Path) -> None:
    """Import the allow-listed WhatsApp and Gemini values from another project's .env."""
    imported, skipped = sec.import_env(path)
    console.print(f"imported: {', '.join(imported) or 'nothing'}")
    console.print(f"[dim]not imported: {', '.join(skipped) or '-'}[/]")


@secrets_app.command("purge")
def secrets_purge(yes: bool = typer.Option(False, "--yes", help="Skip the prompt.")) -> None:
    if not yes and not typer.confirm("Delete every job-agent secret from the keychain?"):
        raise typer.Abort()
    console.print(f"deleted: {', '.join(sec.purge()) or 'nothing'}")


# ---------------------------------------------------------------- db


@db_app.command("migrate")
def db_migrate() -> None:
    conn = db.connect()
    result = db.migrate(conn)
    if result.from_version == result.to_version:
        console.print(f"up to date (v{result.to_version})")
    else:
        console.print(f"migrated v{result.from_version} -> v{result.to_version}")
    if result.backup:
        console.print(f"backup: {result.backup}")


@db_app.command("info")
def db_info() -> None:
    info = db.info(db.connect())
    console.print(
        f"path: {info['path']}\nsize: {info['size_bytes']} bytes\n"
        f"user_version: {info['user_version']}\njournal_mode: {info['journal_mode']}"
    )
    for table, count in info["rows"].items():
        console.print(f"  {table}: {count}")


# ---------------------------------------------------------------- cloud


def _cloud():
    from agent.cloud import Cloud

    return Cloud(sec.get(sec.Secret.MONGODB_URI), config.load_settings().mongo)


@cloud_app.command("init")
def cloud_init() -> None:
    """Create the validated job_agent collections. Safe to run again."""
    cloud = _cloud()
    try:
        console.print(f"ready: {', '.join(cloud.init_collections())}")
    finally:
        cloud.close()


# ---------------------------------------------------------------- llm


@llm_app.command("usage")
def llm_usage() -> None:
    """Requests today per model against the free-tier daily limit."""
    settings = config.load_settings()
    conn = _open_db()
    now = datetime.now(UTC)
    table = Table("model", "today", "limit/day", "429 today", "exhausted until")
    start = db.to_iso(quota.pacific_day_start(now))
    for model in [*settings.llm.fast_models, *settings.llm.strong_models]:
        rejected = conn.execute(
            "SELECT count(*) FROM llm_calls WHERE model = ? AND error_code LIKE 'rate_limited%' AND created_at >= ?",
            (model, start),
        ).fetchone()[0]
        until = quota.exhausted_until(conn, model, now)
        table.add_row(
            model,
            str(quota.used_today(conn, model, now)),
            str(settings.llm.limits[model].rpd),
            str(rejected),
            db.to_iso(until) if until else "-",
        )
    console.print(table)
    console.print(f"llm_paused_until: {db.get_flag(conn, 'llm_paused_until') or '-'}")


# ---------------------------------------------------------------- profile


@profile_app.command("from-tex")
def profile_from_tex(
    path: Path | None = typer.Option(None, help="LaTeX resume (default config/resume.tex)."),
) -> None:
    """Rebuild config/profile.draft.yaml from your LaTeX resume (old draft kept as .bak)."""
    from agent import profile_tex

    source = path or paths.config_dir() / "resume.tex"
    draft, todos, backup = profile_tex.write(source, paths.config_dir() / "profile.draft.yaml")
    console.print(
        f"wrote {draft} ({todos} TODO values to fill)"
        + (f"; previous draft: {backup.name}" if backup else "")
    )
    console.print("Review it, fill the TODOs, then save as config/profile.yaml and run `agent answers seed`.")


@profile_app.command("import")
def profile_import(
    from_file: Path | None = typer.Option(None, help="Path to src/data/portfolio.ts instead of Mongo."),
    yes: bool = typer.Option(False, "--yes", help="Write even if the source changed since the last import."),
) -> None:
    """Write config/profile.draft.yaml from the portfolio. Never touches profile.yaml."""
    from agent import profile_import as pi

    if from_file:
        data = pi.load_portfolio_ts(from_file)
    else:
        cloud = _cloud()
        try:
            data = cloud.read_portfolio()
        finally:
            cloud.close()
        if not data:
            console.print("[red]could not read the portfolio db[/]; try --from-file <portfolio.ts>")
            raise typer.Exit(1)
    target = paths.config_dir() / "profile.draft.yaml"
    result = pi.write_draft(_open_db(), data, target, confirm_changed=yes)
    if not result.written:
        new = yaml.safe_dump(pi.to_draft(data), sort_keys=False, allow_unicode=True).splitlines()
        old = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        for line in difflib.unified_diff(old, new, "current draft", "new import", lineterm="", n=1):
            console.print(line, markup=False, highlight=False)
        console.print(
            "[yellow]The portfolio data changed since the last import.[/] Re-run with --yes to write."
        )
        raise typer.Exit(1)
    console.print(f"wrote {result.path} ({result.todos} TODO values to fill)")
    console.print("Review it, then save as config/profile.yaml.")


# ---------------------------------------------------------------- doctor


@app.command()
def doctor(
    offline: bool = typer.Option(False, help="Skip network checks."),
    send_test: bool = typer.Option(False, help="Send a WhatsApp test message if the window is open."),
) -> None:
    """Check config, secrets, database, redaction, Gemini, Mongo and WhatsApp."""
    from agent import doctor as doc

    results = doc.run(offline=offline, send_test=send_test)
    table = Table("check", "status", "detail", "fix")
    for check in results:
        table.add_row(check.name, ICON[check.status], check.detail, check.hint)
    console.print(table)
    if any(c.status == "fail" for c in results):
        raise typer.Exit(1)


# ---------------------------------------------------------------- pipeline

jobs_app = typer.Typer(no_args_is_help=True, help="Discovered jobs.")
app_app = typer.Typer(no_args_is_help=True, help="Applications (including ones you sent yourself).")
answers_app = typer.Typer(no_args_is_help=True, help="Screening-question answer bank.")
track_app = typer.Typer(
    no_args_is_help=True, help="Reply tracking (email import; Gmail polling is postponed)."
)
apply_app = typer.Typer(no_args_is_help=True, help="Automatic applying (dry run first).")
boards_app = typer.Typer(no_args_is_help=True, help="Manage job board logins and live view.")
review_app = typer.Typer(no_args_is_help=True, help="Weekly review and improvement.")
service_app = typer.Typer(no_args_is_help=True, help="Run at Windows logon.")
cluster_app = typer.Typer(no_args_is_help=True, help="InstaBox machine cluster management.")
briefs_app = typer.Typer(no_args_is_help=True, help="Company research briefs.")
machine_app = typer.Typer(no_args_is_help=True, help="Machine resource usage and processes.")
for sub, name in (
    (jobs_app, "jobs"),
    (app_app, "app"),
    (answers_app, "answers"),
    (track_app, "track"),
    (apply_app, "apply"),
    (boards_app, "boards"),
    (review_app, "review"),
    (service_app, "service"),
    (cluster_app, "cluster"),
    (briefs_app, "briefs"),
    (machine_app, "machine"),
):
    app.add_typer(sub, name=name)


def _logging(profile_needed: bool = False) -> None:
    from agent.logging_setup import setup_logging
    from agent.redact import Redactor

    redactor = None
    try:
        settings = config.load_settings()
        redactor = Redactor.from_profile(config.load_profile(), settings.redaction)
    except config.ConfigError:
        if profile_needed:
            raise
    setup_logging(redactor=redactor, console=True)


@app.command()
def run() -> None:
    """Start the worker: every stage on its schedule until stopped (Ctrl+C)."""
    import logging
    import time

    from agent import scheduler
    from agent.lock import AlreadyRunning, single_instance

    _logging(profile_needed=True)
    log = logging.getLogger("agent.worker")
    try:
        with single_instance():
            while True:  # supervisor: a crash in the scheduler itself restarts it after a minute
                try:
                    scheduler.run_forever()
                    return
                except (KeyboardInterrupt, SystemExit):
                    return
                except Exception:
                    log.exception("worker crashed; restarting in 60 s")
                    time.sleep(60)
    except AlreadyRunning:
        console.print("worker already running; nothing to do")


STAGE_HELP = (
    "cycle | discover | discover-ats | discover-boards | score | urgent | digest | prepare-apply | prep-packs | "
    "heartbeat | flush | backup | weekly | briefs"
)


@app.command("run-once")
def run_once(stage: str = typer.Argument(..., help=STAGE_HELP)) -> None:
    """Run one stage now."""
    from agent import scheduler

    if stage not in scheduler.STAGES:
        console.print(f"[red]unknown stage[/] {stage}; choose from {', '.join(scheduler.STAGES)}")
        raise typer.Exit(2)
    _logging()
    outcome = scheduler.run_stage(stage)
    console.print(f"{stage}: {outcome}", markup=False)
    if outcome.startswith("error"):
        raise typer.Exit(1)


def _set_remote_control(stop: bool) -> None:
    if sec.get_optional(sec.Secret.MONGODB_URI):
        cloud = _cloud()
        try:
            cloud.set_control(stop, "cli")
        finally:
            cloud.close()


@app.command()
def stop() -> None:
    """Halt applying now: STOP file + local flag + Mongo control (if configured)."""
    paths.stop_file().write_text("stop\n", encoding="utf-8")
    db.set_flag(_open_db(), "apply_paused", "1")
    _set_remote_control(True)
    console.print("[yellow]stopped[/]: no application will be submitted until `agent start`")


@app.command()
def start() -> None:
    """Undo `agent stop`."""
    paths.stop_file().unlink(missing_ok=True)
    conn = _open_db()
    db.clear_flag(conn, "apply_paused")
    db.set_flag(conn, "control_stop_cached", "0")
    _set_remote_control(False)
    console.print("[green]started[/]")


@app.command("console")
def console_cmd() -> None:
    """Open the read-only local console on http://127.0.0.1:8765."""
    from agent.console import HOST, PORT, serve

    console.print(f"http://{HOST}:{PORT}  (Ctrl+C to stop)")
    serve()


# ---------------------------------------------------------------- jobs


@jobs_app.command("top")
def jobs_top(n: int = 20) -> None:
    conn = _open_db()
    table = Table("id", "fit", "job", "source", "posted", "link")
    rows = conn.execute(
        "SELECT j.id, s.fit, j.title, j.company, j.source, coalesce(j.posted_at, j.first_seen_at) posted, j.apply_url"
        " FROM jobs j JOIN scores s ON s.job_id = j.id WHERE j.filter_reason IS NULL ORDER BY s.priority DESC LIMIT ?",
        (n,),
    )
    for r in rows:
        table.add_row(
            str(r["id"]), str(round(r["fit"])), f"{r['title']} — {r['company']}", r["source"],
            (r["posted"] or "")[:10], r["apply_url"] or "",
        )  # fmt: skip
    console.print(table)


JOB_FIELDS = (
    "title", "company", "location", "source", "ats_type", "apply_url", "filter_reason", "fit", "semantic",
    "skills", "llm_rubric", "seniority", "freshness", "reasons_json", "gaps_json", "skills_found_json",
)  # fmt: skip


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    conn = _open_db()
    r = conn.execute(
        "SELECT j.*, s.* FROM jobs j LEFT JOIN scores s ON s.job_id = j.id WHERE j.id = ?", (job_id,)
    ).fetchone()
    if r is None:
        raise typer.Exit(1)
    for key in JOB_FIELDS:
        console.print(f"{key}: {r[key]}", markup=False, highlight=False)


@jobs_app.command("stats")
def jobs_stats() -> None:
    """How many jobs each hard filter removed."""
    conn = _open_db()
    for reason, count in conn.execute(
        "SELECT coalesce(filter_reason, CASE WHEN filtered_at IS NULL THEN 'not filtered' ELSE 'passed' END), count(*)"
        " FROM jobs GROUP BY 1 ORDER BY 2 DESC"
    ):
        console.print(f"{count:6d}  {reason}")


# ---------------------------------------------------------------- applications


@app_app.command("list")
def app_list(status: str | None = None, n: int = 30) -> None:
    conn = _open_db()
    sql = (
        "SELECT a.id, a.status, a.origin, a.submitted_at, a.skip_reason, j.title, j.company FROM applications a"
        " JOIN jobs j ON j.id = a.job_id"
    )
    args: tuple = ()
    if status:
        sql += " WHERE a.status = ?"
        args = (status,)
    table = Table("id", "status", "job", "submitted", "note")
    for r in conn.execute(sql + " ORDER BY a.id DESC LIMIT ?", (*args, n)):
        table.add_row(
            str(r["id"]), r["status"], f"{r['title']} — {r['company']}", (r["submitted_at"] or "")[:10],
            (r["skip_reason"] or "")[:60],
        )  # fmt: skip
    console.print(table)


@app_app.command("add")
def app_add(
    company: str, title: str, url: str | None = None, date: str | None = typer.Option(None, help="YYYY-MM-DD")
) -> None:
    """Record an application you sent yourself, so replies and follow-ups are tracked."""
    from agent.track.ingest import record_external

    now = datetime.now(UTC)
    when = datetime.fromisoformat(date).replace(tzinfo=UTC) if date else now
    console.print(f"application {record_external(_open_db(), company, title, url, when, now)} recorded")


MANUAL_STATUSES = ("applied", "assessment", "interview", "rejected", "offer", "no_response")


@app_app.command("status")
def app_status(app_id: int, status: str) -> None:
    """Set a status by hand: applied, assessment, interview, rejected, offer, no_response."""
    if status not in MANUAL_STATUSES:
        console.print(f"[red]status must be one of[/] {', '.join(MANUAL_STATUSES)}")
        raise typer.Exit(2)
    conn = _open_db()
    now = db.utcnow()
    conn.execute("UPDATE applications SET status = ?, last_event_at = ? WHERE id = ?", (status, now, app_id))
    conn.execute(
        "INSERT INTO events(application_id, type, source, occurred_at) VALUES (?, ?, 'manual', ?)",
        (app_id, status, now),
    )
    console.print(f"application {app_id}: {status}")


# ---------------------------------------------------------------- answers


@answers_app.command("seed")
def answers_seed() -> None:
    """Write the answers that come straight from profile.yaml (safe to repeat)."""
    from agent.prepare import answers

    console.print(f"{answers.seed(_open_db(), config.load_profile())} profile answers saved")


@answers_app.command("set")
def answers_set(question: str, answer: str) -> None:
    """Save your answer to a question (legal questions included). Reused word for word."""
    from agent.prepare import answers

    answers.upsert(_open_db(), question, answer, answers.category(question), approved=True)
    console.print(f"saved ({answers.category(question)})")


@answers_app.command("list")
def answers_list() -> None:
    table = Table("category", "question", "answer", "used", "approved")
    for r in _open_db().execute("SELECT * FROM answers_bank ORDER BY category, question_raw"):
        table.add_row(
            r["category"],
            r["question_raw"],
            r["answer"][:60],
            str(r["used_count"]),
            "yes" if r["approved"] else "no",
        )
    console.print(table)


@answers_app.command("pending")
def answers_pending() -> None:
    """Questions that made the agent skip a form. Answer each once with `agent answers set`."""
    rows = _open_db().execute("SELECT * FROM pending_questions ORDER BY seen_count DESC").fetchall()
    if not rows:
        console.print("nothing pending")
    for r in rows:
        console.print(f"{r['seen_count']:3d}x  {r['question_raw']}", markup=False, highlight=False)


# ---------------------------------------------------------------- tracking


@track_app.command("import")
def track_import(path: Path) -> None:
    """Import emails: a JSON list ({id, from, subject, date, body}), an .eml file or a folder of .eml."""
    from agent.context import open_ctx
    from agent.track.ingest import ingest, load

    with open_ctx(need_profile=True) as ctx:
        result = ingest(
            ctx.conn,
            load(path),
            ctx.client,
            notify=lambda body, kind: ctx.wa.send_text(body, kind=kind, priority=8),
        )
    console.print(vars(result))


@track_app.command("followups")
def track_followups() -> None:
    from agent.track.ingest import followups

    console.print(followups(_open_db(), datetime.now(UTC)))


# ---------------------------------------------------------------- apply


@apply_app.command("status")
def apply_status() -> None:
    from agent.apply.boards.base import applications_today, prompts_today
    from agent.apply.runner import LIVE_FLAG, clean_dry_runs

    settings = config.load_settings()
    conn = _open_db()
    paused = db.get_flag(conn, "apply_paused") == "1" or paths.stop_file().exists()
    console.print(f"mode: {settings.apply.mode}   paused: {paused}")
    for adapter in settings.apply.adapters:
        live = db.get_flag(conn, LIVE_FLAG.format(adapter=adapter)) == "1"
        done = clean_dry_runs(conn, adapter)
        console.print(
            f"  {adapter}: {done}/{settings.apply.dry_runs_before_live} clean dry runs, live {'on' if live else 'off'}"
        )
    if settings.apply.boards:
        console.print("boards:")
        now = datetime.now(UTC)
        for board, b_cfg in settings.apply.boards.items():
            live = db.get_flag(conn, LIVE_FLAG.format(adapter=board)) == "1"
            blocked = db.get_flag(conn, f"board_blocked:{board}")
            status_str = (
                f"[red]blocked ({blocked})[/]"
                if blocked
                else ("[green]live on[/]" if live else "[yellow]live off[/]")
            )
            app_cnt = applications_today(conn, board, now)
            prompt_cnt = prompts_today(conn, board, now)
            console.print(
                f"  {board}: {status_str} ({app_cnt}/{b_cfg.daily_cap} apps today, {prompt_cnt}/{settings.apply.human_prompts_per_site_per_day} prompts today)"
            )
    for outcome, count in conn.execute("SELECT outcome, count(*) FROM apply_attempts GROUP BY outcome"):
        console.print(f"  attempts {outcome}: {count}")


@apply_app.command("go-live")
def apply_go_live(adapter: str, yes: bool = typer.Option(False, "--yes")) -> None:
    """Allow real submissions for one ATS (after enough clean dry runs). Also set apply.mode: live."""
    from agent.apply.runner import go_live

    if not yes and not typer.confirm(f"Let the agent submit real applications on {adapter} without asking?"):
        raise typer.Abort()
    ok, message = go_live(_open_db(), config.load_settings(), adapter)
    console.print(message, style="green" if ok else "yellow", markup=False)
    if not ok:
        raise typer.Exit(1)


@apply_app.command("go-dry")
def apply_go_dry(adapter: str) -> None:
    """Back to dry runs for one ATS."""
    from agent.apply.runner import LIVE_FLAG

    db.clear_flag(_open_db(), LIVE_FLAG.format(adapter=adapter))
    console.print(f"{adapter}: back to dry runs")


@apply_app.command("resume")
def apply_resume() -> None:
    """Clear the pause set after repeated failures (`agent start` clears a manual stop)."""
    db.clear_flag(_open_db(), "apply_paused")
    console.print("apply pause cleared")


# ---------------------------------------------------------------- boards


@boards_app.command("status")
def boards_status() -> None:
    """Show live status and limits for job boards (LinkedIn, Naukri, Indeed)."""
    from agent.apply.boards.base import applications_today, prompts_today
    from agent.apply.runner import LIVE_FLAG

    settings = config.load_settings()
    conn = _open_db()
    now = datetime.now(UTC)
    console.print(f"Board Apply (mode: {settings.apply.mode}):")
    boards = settings.apply.boards or {}
    for site in ("linkedin", "naukri", "indeed"):
        cfg = boards.get(site)
        live = db.get_flag(conn, LIVE_FLAG.format(adapter=site)) == "1"
        blocked = db.get_flag(conn, f"board_blocked:{site}")
        status = "[red]BLOCKED[/]" if blocked else ("[green]LIVE[/]" if live else "[yellow]OFF[/]")
        apps = applications_today(conn, site, now)
        prompts = prompts_today(conn, site, now)
        cap = cfg.daily_cap if cfg else 10
        console.print(
            f"  {site:10}: {status:15} apps: {apps}/{cap} today   prompts: {prompts}/{settings.apply.human_prompts_per_site_per_day} today"
        )
        if blocked:
            console.print(f"             Reason: {blocked}")


@boards_app.command("login")
def boards_login(
    site: str = typer.Argument(..., help="Board site name: linkedin, naukri, or indeed."),
) -> None:
    """Open a job board login page in headed browser and trigger live view for manual user sign-in."""
    from agent.apply.boards.runner import default_board_browser
    from agent.context import open_ctx
    from agent.live_view import live_view, wait_for_resolution

    site = site.lower().strip()
    login_urls = {
        "linkedin": "https://www.linkedin.com/login",
        "naukri": "https://www.naukri.com/nlogin/login",
        "indeed": "https://secure.indeed.com/auth",
    }
    if site not in login_urls:
        console.print(f"[red]Unknown site '{site}'. Choose from: linkedin, naukri, indeed[/]")
        raise typer.Exit(1)

    with open_ctx(need_profile=False) as ctx:
        settings = ctx.settings

        console.print(f"Opening browser for [bold]{site.capitalize()}[/] login...")
        with default_board_browser(settings) as context:
            page = context.new_page()
            try:
                url = login_urls[site]
                console.print(f"Navigating to {url}...")
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

                console.print("Opening live view session...")
                with live_view(
                    site,
                    "initial login",
                    wa_client=ctx.wa,
                    timeout_minutes=settings.apply.human_step_minutes,
                ) as session:
                    if not session:
                        console.print(
                            "[red]Could not open live view session. Ensure WhatsApp window is open and JOB_AGENT_SECRET_LIVE_VIEW_LINK is configured.[/]"
                        )
                        raise typer.Exit(1)

                    console.print(f"[green]Live view open for {site.capitalize()}![/]")
                    console.print(
                        "WhatsApp prompt sent. Open the link from WhatsApp, enter the password, and log in."
                    )
                    console.print(
                        f"Waiting up to {settings.apply.human_step_minutes} minutes for login to complete..."
                    )

                    def check_logged_in() -> bool:
                        current_url = page.url.lower()
                        if site == "linkedin":
                            if (
                                "session_redirect" in current_url
                                or "/login" in current_url
                                or "/checkpoint" in current_url
                            ):
                                return False
                            if (
                                "linkedin.com/feed" in current_url
                                or "/in/" in current_url
                                or "/mynetwork" in current_url
                                or "/jobs" in current_url
                            ):
                                return True
                            try:
                                if (
                                    page.query_selector(
                                        ".global-nav, #global-nav, [data-view-name*='profile']"
                                    )
                                    is not None
                                ):
                                    return True
                            except Exception:
                                pass
                            return False
                        elif site == "naukri":
                            if "/nlogin" in current_url or "/login" in current_url:
                                return False
                            if "naukri.com/mnjuser" in current_url or "my.naukri.com" in current_url:
                                return True
                            try:
                                if page.query_selector(".user-info, .nI-gNb-drawer") is not None:
                                    return True
                            except Exception:
                                pass
                            return False
                        elif site == "indeed":
                            if "/auth" in current_url or "/account/login" in current_url:
                                return False
                            if "indeed.com" in current_url and (
                                "/feed" in current_url or "/home" in current_url
                            ):
                                return True
                            try:
                                if (
                                    page.query_selector(
                                        "[data-gnav-element*='profile'], button[aria-label*='account']"
                                    )
                                    is not None
                                ):
                                    return True
                            except Exception:
                                pass
                            return False
                        return False

                    resolved = wait_for_resolution(
                        session,
                        check_logged_in,
                        timeout_seconds=settings.apply.human_step_minutes * 60,
                        poll_interval_s=3.0,
                    )
                    if resolved:
                        console.print(f"[bold green]Successfully logged in to {site.capitalize()}![/]")
                        console.print("Session cookies and state saved to persistent profile.")
                    else:
                        console.print(f"[yellow]Timed out waiting for login to {site.capitalize()}.[/]")
            finally:
                page.close()


# ---------------------------------------------------------------- review


@review_app.command("weekly")
def review_weekly() -> None:
    from agent.report import weekly

    path, text = weekly.write(_open_db(), datetime.now(UTC))
    console.print(text, markup=False)
    console.print(f"\nfull report: {path}", markup=False)


@review_app.command("refit")
def review_refit() -> None:
    """Re-fit score weights from outcomes (needs 50 submitted applications with an outcome)."""
    from agent.match.score import refit_path
    from agent.report import refit

    weights, n = refit.refit(_open_db(), refit_path())
    if weights is None:
        console.print(f"not enough outcomes yet ({n}/{refit.MIN_SAMPLES})")
        raise typer.Exit(1)
    console.print(f"new weights from {n} outcomes: {weights.model_dump()}")
    console.print("Set scoring.use_refit_weights: true in settings.yaml to use them.")


@review_app.command("referrals")
def review_referrals() -> None:
    """Draft referral notes for top jobs from config/contacts.csv (name, company, relation, link)."""
    from agent.report.prep import referral_drafts

    path = referral_drafts(_open_db(), paths.config_dir() / "contacts.csv")
    console.print(
        f"written: {path}" if path else "no contacts at your top companies (or no contacts.csv)", markup=False
    )


@review_app.command("ab")
def review_ab() -> None:
    """Resume summary A/B test so far."""
    from agent.report import experiments

    arms, verdict = experiments.compare(_open_db())
    for a in arms:
        console.print(f"{a.variant}: {a.responses}/{a.n} responded ({a.low:.0%}-{a.high:.0%})")
    console.print(verdict)


# ---------------------------------------------------------------- service


@service_app.command("install")
def service_install(
    yes: bool = typer.Option(False, "--yes", help="Create the task now instead of printing it."),
) -> None:
    """Two Task Scheduler tasks, no console window: start at logon, and an hourly watchdog that starts
    the worker again if it stopped (the single-instance lock makes extra starts exit at once)."""
    import subprocess
    import sys

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    action = f'"{pythonw}" -m agent.cli run'
    commands = [
        ["schtasks", "/Create", "/TN", "job-agent", "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR", action],
        [
            "schtasks",
            "/Create",
            "/TN",
            "job-agent-watchdog",
            "/SC",
            "HOURLY",
            "/RL",
            "LIMITED",
            "/F",
            "/TR",
            action,
        ],
    ]
    if not yes:
        console.print("Run these (or re-run with --yes):", markup=False)
        for command in commands:
            console.print(subprocess.list2cmdline(command), markup=False)
        console.print(
            "Keep the PC awake while plugged in: powercfg /change standby-timeout-ac 0", markup=False
        )
        return
    for command in commands:
        subprocess.run(command, check=True)
    console.print("tasks created: job-agent (at logon), job-agent-watchdog (hourly)")


@service_app.command("uninstall")
def service_uninstall() -> None:
    import subprocess

    for name in ("job-agent", "job-agent-watchdog"):
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], check=False)


@cluster_app.command("status")
def cluster_status() -> None:
    """Show cluster node status and roles."""
    from rich.table import Table

    from agent.cluster import load_cluster_config
    from agent.cluster.client import InstaBoxClient

    cfg = load_cluster_config()
    client = InstaBoxClient()
    table = Table(title="Cluster Nodes")
    table.add_column("Name", style="bold")
    table.add_column("Role", style="cyan")
    table.add_column("Workspace")
    table.add_column("Status")
    table.add_column("Last Active")
    for node in cfg.machines:
        st = client.get_machine_state(node)
        status_color = "green" if st.get("status") in ("running", "ready") else "yellow"
        table.add_row(
            node.name,
            node.role,
            node.workspace,
            f"[{status_color}]{st.get('status')}[/]",
            str(st.get("last_active") or "unknown"),
        )
    console.print(table)


@cluster_app.command("tick")
def cluster_tick() -> None:
    """Run hourly master cluster tick: check nodes and dispatch worker."""
    from agent.cluster import tick

    result = tick()
    console.print(
        f"Cluster tick complete at {result['timestamp']}: worker dispatched = {result['worker_dispatched']}"
    )
    for node in result["cluster"]:
        console.print(f"  {node['name']} ({node['role']}): {node['state']}")


# ---------------------------------------------------------------- briefs


@briefs_app.command("import")
def briefs_import(
    source: Path = typer.Argument(..., help="Directory or markdown file containing research briefs."),
) -> None:
    """Import company research briefs (*.md) into the agent's briefs directory."""
    import shutil

    dest = paths.briefs_dir()
    imported = 0
    if source.is_file() and source.suffix.lower() == ".md":
        shutil.copy2(source, dest / source.name)
        console.print(f"imported {source.name} -> {dest / source.name}")
        imported = 1
    elif source.is_dir():
        for file in sorted(source.glob("*.md")):
            shutil.copy2(file, dest / file.name)
            console.print(f"imported {file.name}")
            imported += 1
    else:
        console.print(f"[red]error:[/] {source} is not a valid markdown file or directory")
        raise typer.Exit(1)
    console.print(f"[green]Successfully imported {imported} brief(s).[/]")


@briefs_app.command("list")
def briefs_list() -> None:
    """List all imported company research briefs."""
    dest = paths.briefs_dir()
    files = sorted(dest.glob("*.md"))
    if not files:
        console.print("No briefs imported yet.")
        return
    table = Table(title="Company Research Briefs")
    table.add_column("File / Job ID", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Modified")
    for f in files:
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        size = f"{f.stat().st_size} bytes"
        table.add_row(f.name, size, mtime)
    console.print(table)


@briefs_app.command("show")
def briefs_show(
    job_id_or_name: str = typer.Argument(..., help="Job ID (e.g. 123) or filename (e.g. 123.md)."),
) -> None:
    """Display the contents of a research brief."""
    dest = paths.briefs_dir()
    name = job_id_or_name if job_id_or_name.endswith(".md") else f"{job_id_or_name}.md"
    target = dest / name
    if not target.is_file():
        console.print(f"[red]Brief not found:[/] {target}")
        raise typer.Exit(1)
    console.print(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- machine status & usage


@machine_app.command("status")
def machine_status_cmd(
    node: str | None = typer.Argument(None, help="Optional node name to inspect (e.g. rohit-6)."),
) -> None:
    """Show machine resource usage (CPU, RAM, Disk) and running processes."""
    from rich.table import Table

    from agent.cluster.metrics import collect_local_metrics

    metrics = collect_local_metrics(node)
    table = Table(title=f"Machine Status: {metrics['name']}")
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", style="green")

    table.add_row("State", metrics["state"])
    table.add_row(
        "Uptime",
        f"{metrics['uptime_s']} s ({metrics['uptime_s'] // 3600}h {(metrics['uptime_s'] % 3600) // 60}m)",
    )
    table.add_row("Load Average (1m, 5m, 15m)", ", ".join(str(x) for x in metrics["load"]))
    table.add_row(
        "Memory Usage",
        f"{metrics['mem_used_mb']} MB / {metrics['mem_total_mb']} MB ({metrics['mem_pct']}%)",
    )
    table.add_row(
        "Disk Usage",
        f"{metrics['disk_free_gb']} GB free / {metrics['disk_total_gb']} GB",
    )
    if metrics.get("models"):
        table.add_row("Ollama Models", ", ".join(metrics["models"]))

    console.print(table)

    if metrics.get("top_procs"):
        procs_table = Table(title="Top Running Processes (by CPU)")
        procs_table.add_column("Process Info", style="yellow")
        for p in metrics["top_procs"]:
            procs_table.add_row(p)
        console.print(procs_table)


@machine_app.command("report")
def machine_report_cmd(node: str | None = typer.Option(None, "--name", help="Machine node name.")) -> None:
    """Collect local metrics and report to MongoDB Atlas (job_agent.machines)."""
    from agent.cluster.metrics import collect_local_metrics

    metrics = collect_local_metrics(node)
    cloud = _cloud()
    if cloud is None:
        console.print("[yellow]MongoDB URI not configured; skipping remote report.[/]")
        return
    ok = cloud.write_machine_metrics(metrics["name"], metrics)
    if ok:
        console.print(f"[green]Reported machine metrics for {metrics['name']} to MongoDB.[/]")
    else:
        console.print(f"[red]Failed to report machine metrics for {metrics['name']}.[/]")


trigger_app = typer.Typer(name="trigger", help="Hourly cycle trigger commands.")
app.add_typer(trigger_app, name="trigger")


@trigger_app.command("serve")
def trigger_serve(
    port: int = typer.Option(8787, "--port", help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind."),
) -> None:
    """Serve the HTTP cycle trigger endpoint on /cycle."""
    from agent.trigger import run_trigger_server

    run_trigger_server(host=host, port=port)


companies_app = typer.Typer(name="companies", help="Company ATS discovery and management.")
app.add_typer(companies_app, name="companies")


@companies_app.command("discover")
def companies_discover(
    limit: int = typer.Option(200, "--limit", help="Max distinct companies to probe."),
) -> None:
    """Probe ATS boards (Greenhouse, Lever, Ashby) for companies found on job boards."""
    from agent.discover.company_discovery import discover_companies

    conn = _open_db()
    hits = discover_companies(conn, limit=limit)
    if hits:
        console.print(f"[green]Discovered {len(hits)} new company board(s):[/]")
        for h in hits:
            console.print(f"  • {h['name']} ({h['ats_type']}: {h['ats_slug']})")
    else:
        console.print("[dim]No new matching company boards discovered.[/]")


if __name__ == "__main__":
    app()
