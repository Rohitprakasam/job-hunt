"""The long-running worker: every stage on its schedule, one at a time, each failure isolated.

Started by `agent run` (Windows Task Scheduler at logon: `agent service install`). Missed runs while
the PC slept are coalesced into one catch-up run on wake.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from agent import db, killswitch
from agent.context import Ctx, open_ctx

log = logging.getLogger(__name__)

STARTED = time.monotonic()


def uptime() -> int:
    return int(time.monotonic() - STARTED)


# ---------------------------------------------------------------- stages (each opens its own context)


def discover(kinds: tuple[str, ...]) -> Callable[[Ctx], str]:
    def stage(ctx: Ctx) -> str:
        from agent.discover.runner import run_discovery
        from agent.notify.digest import send_alert

        result = run_discovery(ctx.conn, ctx.settings, ctx.search, ctx.companies, kinds=kinds)
        for source in result.tripped:
            send_alert(ctx.wa, f"⚠️ Job source {source} failed 3 runs in a row; paused for "
                       f"{ctx.settings.discovery.breaker_hours} h.")  # fmt: skip
        return f"{result.new} new"

    return stage


def score(ctx: Ctx) -> str:
    from agent.match.score import run_scoring

    if ctx.client:
        ctx.client.prewarm(stage="score")
    result = run_scoring(ctx.conn, ctx.settings, ctx.search, ctx.profile, ctx.client)
    return f"{result.scored} scored, {result.rubric_done} rubric ({result.semantic_method})"


def urgent(ctx: Ctx) -> str:
    from agent.notify.digest import send_urgent

    return f"{send_urgent(ctx.conn, ctx.wa, ctx.settings, datetime.now(UTC))} urgent"


def digest(ctx: Ctx) -> str:
    from agent.notify.digest import send_digest
    from agent.track.ingest import followups

    now = datetime.now(UTC)
    due = followups(ctx.conn, now)
    status = send_digest(ctx.conn, ctx.wa, ctx.settings, now)
    if due["nudge"]:
        ctx.wa.send_text(
            "🔁 *Follow up today*\n" + "\n".join(f"- {x}" for x in due["nudge"][:10]), kind="followup"
        )
    return f"digest {status}; {len(due['nudge'])} nudges, {len(due['no_response'])} no-response"


def prepare_and_apply(ctx: Ctx) -> str:
    from agent.apply.runner import run_apply
    from agent.prepare.runner import run_prepare

    if killswitch.is_stopped(ctx.conn, ctx.cloud):
        return "stopped (kill switch)"
    if ctx.client:
        ctx.client.prewarm(stage="prepare")
    prepared = run_prepare(ctx.conn, ctx.settings, ctx.profile, ctx.client)
    applied = run_apply(ctx.conn, ctx.settings, ctx.profile, ctx.client, ctx.cloud, wa=ctx.wa)
    if applied.submitted:
        ctx.wa.send_text(
            "✅ *Applied*\n" + "\n".join(f"- {x}" for x in applied.submitted), kind="applied", priority=2
        )
    if applied.stopped and "failed in a row" in applied.stopped:
        from agent.notify.digest import send_alert

        send_alert(
            ctx.wa, "⛔ Applying paused: 3 submissions failed in a row. `agent apply resume` after a look."
        )
    paused = f" (prepare paused: {prepared.stopped})" if prepared.stopped else ""
    return f"prepared {prepared.prepared} skipped {prepared.skipped}{paused}; apply {applied.outcomes or applied.stopped}"


def prep_packs(ctx: Ctx) -> str:
    from agent.report.prep import pending_packs, write_pack

    made = [write_pack(ctx.conn, ctx.client, ctx.profile, app_id) for app_id in pending_packs(ctx.conn)]
    return f"{len(made)} prep packs"


def heartbeat(ctx: Ctx) -> str:
    from agent.cluster.metrics import collect_local_metrics
    from agent.report.sanitize import build
    from agent.runs import last_ok

    if ctx.cloud is None:
        return "no mongo"
    paused = killswitch.is_stopped(ctx.conn, ctx.cloud)
    ok = ctx.cloud.write_heartbeat(uptime(), last_ok(ctx.conn))
    snap = ctx.cloud.write_snapshot(build(ctx.conn, datetime.now(UTC), uptime(), paused))
    try:
        metrics = collect_local_metrics()
        ctx.cloud.write_machine_metrics(metrics["name"], metrics)
    except Exception as exc:
        log.warning("Failed to collect or write machine metrics: %s", exc)
    return f"heartbeat {ok} snapshot {snap}"


def flush(ctx: Ctx) -> str:
    return str(ctx.wa.flush())


def backup(ctx: Ctx) -> str:
    from agent.backup import nightly

    db_copy, archive = nightly(ctx.conn)
    return f"{db_copy.name}, {archive.name}"


def weekly(ctx: Ctx) -> str:
    from agent.report import weekly as weekly_report

    try:
        from agent.discover.company_discovery import discover_companies

        discover_companies(ctx.conn)
    except Exception as exc:
        log.warning("weekly company discovery failed: %s", exc)

    path, text = weekly_report.write(ctx.conn, datetime.now(UTC))
    ctx.wa.send_text(text, kind="weekly", priority=1)
    return str(path)


def inbox(ctx: Ctx) -> str:
    if not ctx.cloud:
        return "skipped (no mongo)"
    items = ctx.cloud.read_inbox()
    if not items:
        return "0 items"
    row = ctx.conn.execute("SELECT sent_at FROM digest_items ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return "0 items (no digest_items)"
    latest_sent = row[0]
    mapping = dict(
        ctx.conn.execute("SELECT n, job_id FROM digest_items WHERE sent_at = ?", (latest_sent,)).fetchall()
    )
    processed = 0
    now_iso = db.to_iso(datetime.now(UTC))
    for item in items:
        action = item.get("action")
        numbers = item.get("numbers") or []
        for n in numbers:
            job_id = mapping.get(n)
            if not job_id:
                continue
            if action == "applied":
                existing = ctx.conn.execute(
                    "SELECT id FROM applications WHERE job_id = ?", (job_id,)
                ).fetchone()
                if existing:
                    ctx.conn.execute(
                        "UPDATE applications SET status = 'applied', origin = 'manual', submitted_at = ? WHERE id = ?",
                        (now_iso, existing[0]),
                    )
                else:
                    ctx.conn.execute(
                        "INSERT INTO applications(job_id, status, origin, submitted_at) VALUES (?, 'applied', 'manual', ?)",
                        (job_id, now_iso),
                    )
                processed += 1
            elif action == "skip":
                existing = ctx.conn.execute(
                    "SELECT id FROM applications WHERE job_id = ?", (job_id,)
                ).fetchone()
                if existing:
                    ctx.conn.execute(
                        "UPDATE applications SET status = 'skipped', skip_reason = 'manual_skip' WHERE id = ?",
                        (existing[0],),
                    )
                else:
                    ctx.conn.execute("UPDATE jobs SET filter_reason = 'manual_skip' WHERE id = ?", (job_id,))
                processed += 1
        ctx.cloud.delete_inbox_item(item["_id"])
    ctx.conn.commit()
    return f"{processed} processed"


def briefs(ctx: Ctx) -> str:
    """Takes top 5 jobs without a brief (fit >= min_fit) and generates a brief via strong-tier LLM."""
    import re

    import httpx

    from agent import paths
    from agent.runs import stage_run

    min_fit = ctx.settings.apply.min_fit if ctx.settings else 70
    b_dir = paths.briefs_dir()

    rows = ctx.conn.execute(
        "SELECT j.*, s.fit, s.priority FROM jobs j JOIN scores s ON s.job_id = j.id"
        " WHERE s.fit >= ? AND j.filter_reason IS NULL ORDER BY s.priority DESC",
        (min_fit,),
    ).fetchall()

    candidates = []
    for r in rows:
        target = b_dir / f"{r['id']}.md"
        if not target.exists():
            candidates.append(r)
        if len(candidates) >= 5:
            break

    if not candidates:
        return "0 briefs (none pending)"

    done = 0
    with stage_run(ctx.conn, "briefs") as run:
        for job in candidates:
            target = b_dir / f"{job['id']}.md"
            company_domain = None
            if ctx.companies and ctx.companies.companies:
                for c in ctx.companies.companies:
                    if c.name.lower() == job["company"].lower() and c.domain:
                        company_domain = c.domain
                        break
            homepage_url = f"https://{company_domain}" if company_domain else None
            homepage_text = ""
            if homepage_url:
                try:
                    resp = httpx.get(
                        homepage_url,
                        timeout=10,
                        follow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if resp.status_code == 200:
                        raw = re.sub(r"<[^>]+>", " ", resp.text)
                        homepage_text = " ".join(raw.split())[:8192]
                except Exception as exc:
                    log.warning("Could not fetch homepage for %s: %s", job["company"], exc)

            sources_list = [u for u in [job["url"], job["apply_url"], homepage_url] if u]
            prompt = (
                "You are a research analyst preparing a concise company & job brief for an interview candidate.\n"
                "IMPORTANT INSTRUCTIONS:\n"
                "- Use ONLY the text given below. Do not invent any facts, numbers, or external info.\n"
                f"- List ONLY these provided URLs as sources: {', '.join(sources_list)}. Never hallucinate any other URL.\n\n"
                f"Job: {job['title']} at {job['company']}\n"
                f"Allowed Sources: {', '.join(sources_list)}\n\n"
                f"Job Description:\n{(job['description'] or '')[:5000]}\n\n"
            )
            if homepage_text:
                prompt += f"Company Homepage Text ({homepage_url}):\n{homepage_text}\n\n"
            prompt += (
                "Structure your response with:\n"
                "1. Overview & Mission\n"
                "2. Product & Engineering Culture\n"
                "3. Tech Stack Highlights\n"
                "4. 3 Smart Questions to Ask\n"
                "5. Sources (list only allowed URLs)\n"
            )

            if ctx.client:
                try:
                    res = ctx.client.complete(
                        "brief",
                        "strong",
                        [{"role": "user", "content": prompt}],
                        max_tokens=800,
                        run_id=run.id,
                    )
                    if res and res.text:
                        target.write_text(res.text.strip(), encoding="utf-8")
                        done += 1
                except Exception as exc:
                    log.warning("Brief generation failed for job %s: %s", job["id"], exc)
        run.items = done
    return f"{done} briefs generated"


def cycle(ctx: Ctx, now: datetime | None = None) -> str:
    """The master hourly cycle: executes due pipeline stages in order under a single instance lock."""
    from zoneinfo import ZoneInfo

    from agent import config
    from agent.lock import single_instance

    now = now or datetime.now(UTC)
    settings = ctx.settings or config.load_settings()
    outcomes: list[str] = []

    with single_instance():
        # 1. discover-ats
        outcomes.append(f"discover-ats: {run_stage('discover-ats')}")

        # 2. discover-boards (every 3 h)
        boards_due = False
        last_boards = db.get_flag(ctx.conn, "cycle:last_run:discover-boards")
        if not last_boards:
            boards_due = True
        else:
            try:
                boards_due = (
                    now - db.parse_iso(last_boards)
                ).total_seconds() >= settings.discovery.jobspy_interval_minutes * 60
            except Exception:
                boards_due = True
        if boards_due:
            res = run_stage("discover-boards")
            outcomes.append(f"discover-boards: {res}")
            if not res.startswith("error"):
                db.set_flag(ctx.conn, "cycle:last_run:discover-boards", db.to_iso(now))

        # 3. score
        outcomes.append(f"score: {run_stage('score')}")

        # 4. urgent
        outcomes.append(f"urgent: {run_stage('urgent')}")

        # 5. digest (once a day after digest_time in notify.timezone)
        local_dt = now.astimezone(ZoneInfo(settings.notify.timezone))
        dh, dm = map(int, settings.notify.digest_time.split(":"))
        today_str = local_dt.strftime("%Y-%m-%d")
        digest_due = (local_dt.hour, local_dt.minute) >= (dh, dm) and db.get_flag(
            ctx.conn, "cycle:last_run:digest"
        ) != today_str
        if digest_due:
            res = run_stage("digest")
            outcomes.append(f"digest: {res}")
            if not res.startswith("error"):
                db.set_flag(ctx.conn, "cycle:last_run:digest", today_str)

        # 6. inbox
        outcomes.append(f"inbox: {run_stage('inbox')}")

        # 7. prepare-apply
        outcomes.append(f"prepare-apply: {run_stage('prepare-apply')}")

        # 7. prep-packs
        outcomes.append(f"prep-packs: {run_stage('prep-packs')}")

        # 8. heartbeat
        outcomes.append(f"heartbeat: {run_stage('heartbeat')}")

        # 9. flush
        outcomes.append(f"flush: {run_stage('flush')}")

        # 10. backup (daily)
        backup_due = db.get_flag(ctx.conn, "cycle:last_run:backup") != today_str
        if backup_due:
            res = run_stage("backup")
            outcomes.append(f"backup: {res}")
            if not res.startswith("error"):
                db.set_flag(ctx.conn, "cycle:last_run:backup", today_str)

        # 11. weekly (on notify.weekly_day after weekly_time)
        wh, wm = map(int, settings.notify.weekly_time.split(":"))
        is_weekly_day = local_dt.strftime("%a").lower()[:3] == settings.notify.weekly_day.lower()[:3]
        weekly_due = (
            is_weekly_day
            and (local_dt.hour, local_dt.minute) >= (wh, wm)
            and db.get_flag(ctx.conn, "cycle:last_run:weekly") != today_str
        )
        if weekly_due:
            res = run_stage("weekly")
            outcomes.append(f"weekly: {res}")
            if not res.startswith("error"):
                db.set_flag(ctx.conn, "cycle:last_run:weekly", today_str)

        # 12. daily briefs (top 5 jobs without a brief, fit >= min_fit)
        briefs_due = False
        last_briefs = db.get_flag(ctx.conn, "cycle:last_run:briefs")
        if not last_briefs or (now - db.parse_iso(last_briefs)).total_seconds() >= 86400:
            briefs_due = True
        if briefs_due:
            res = run_stage("briefs")
            outcomes.append(f"briefs: {res}")
            if not res.startswith("error"):
                db.set_flag(ctx.conn, "cycle:last_run:briefs", db.to_iso(now))

    return " | ".join(outcomes)


STAGES: dict[str, tuple[Callable[[Ctx], str], bool]] = {
    # name: (function, needs profile)
    "cycle": (cycle, True),
    "discover": (discover(("ats", "jobspy")), False),
    "discover-ats": (discover(("ats",)), False),
    "discover-boards": (discover(("jobspy",)), False),
    "score": (score, True),
    "urgent": (urgent, False),
    "digest": (digest, False),
    "inbox": (inbox, False),
    "prepare-apply": (prepare_and_apply, True),
    "prep-packs": (prep_packs, True),
    "heartbeat": (heartbeat, False),
    "flush": (flush, False),
    "backup": (backup, False),
    "weekly": (weekly, False),
    "briefs": (briefs, True),
}


def run_stage(name: str) -> str:
    fn, need_profile = STAGES[name]
    try:
        with open_ctx(need_profile=need_profile) as ctx:
            outcome = fn(ctx)
    except Exception as exc:
        log.exception("stage %s failed", name)
        return f"error: {type(exc).__name__}"
    log.info("stage %s: %s", name, outcome)
    return outcome


def pipeline_after_discovery(kind: str) -> Callable[[], None]:
    def job() -> None:
        run_stage(kind)
        run_stage("score")
        run_stage("urgent")

    return job


def run_forever() -> None:  # pragma: no cover - wiring only; each stage is tested on its own
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.blocking import BlockingScheduler

    from agent import config

    settings = config.load_settings()
    n = settings.notify
    sched = BlockingScheduler(
        timezone=n.timezone,
        executors={"default": ThreadPoolExecutor(max_workers=1)},  # one stage at a time
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    d = settings.discovery
    digest_h, digest_m = map(int, n.digest_time.split(":"))
    week_h, week_m = map(int, n.weekly_time.split(":"))
    sched.add_job(pipeline_after_discovery("discover-ats"), "interval", minutes=d.ats_interval_minutes, id="ats",
                  next_run_time=datetime.now(UTC))  # fmt: skip
    sched.add_job(
        pipeline_after_discovery("discover-boards"),
        "interval",
        minutes=d.jobspy_interval_minutes,
        id="boards",
    )
    sched.add_job(lambda: run_stage("prepare-apply"), "interval", minutes=30, id="apply")
    sched.add_job(lambda: run_stage("digest"), "cron", hour=digest_h, minute=digest_m, id="digest")
    sched.add_job(lambda: run_stage("prep-packs"), "interval", hours=2, id="prep")
    sched.add_job(
        lambda: run_stage("heartbeat"),
        "interval",
        minutes=15,
        id="heartbeat",
        next_run_time=datetime.now(UTC),
    )
    sched.add_job(lambda: run_stage("flush"), "interval", minutes=10, id="flush")
    sched.add_job(lambda: run_stage("backup"), "cron", hour=2, minute=30, id="backup")
    sched.add_job(
        lambda: run_stage("weekly"), "cron", day_of_week=n.weekly_day, hour=week_h, minute=week_m, id="weekly"
    )
    log.info("worker started; jobs: %s", [j.id for j in sched.get_jobs()])
    conn = db.connect()
    db.migrate(conn)
    conn.close()
    sched.start()
