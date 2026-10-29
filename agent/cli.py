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
def secrets_set(name: sec.Secret) -> None:
    """Store one secret (typed without echo)."""
    value = getpass.getpass(f"{name.value}: ")
    sec.set_secret(name, value)
    console.print(f"stored {name.value}")


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


if __name__ == "__main__":
    app()
