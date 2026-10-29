"""`agent doctor`: prove every Phase 0 dependency works. Prints names and states, never secret values."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import httpx
from pymongo.errors import OperationFailure, WriteError

from agent import config, db, paths, quota
from agent import secrets as sec
from agent.logging_setup import mask_tokens
from agent.redact import PLACEHOLDER_RE, Redactor

Status = Literal["ok", "warn", "fail", "skip"]

PII_PATH_RE = re.compile(
    r"(^|/)config/(profile|profile\.draft|search|companies|settings)\.yaml$|\.db$|client_secret|token.*\.json$|(^|/)\.env"
)


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    hint: str = ""


@dataclass
class Context:
    offline: bool = False
    send_test: bool = False
    settings: config.Settings | None = None
    profile: config.Profile | None = None
    conn: object = None
    results: list[Check] = field(default_factory=list)


def _run(ctx: Context, name: str, fn: Callable[[Context], Check | list[Check]]) -> None:
    try:
        out = fn(ctx)
        ctx.results.extend(out if isinstance(out, list) else [out])
    except Exception as exc:  # a broken check must not hide the others
        ctx.results.append(Check(name, "fail", mask_tokens(f"{type(exc).__name__}: {exc}")[:300]))


# ---------------------------------------------------------------- offline checks


def check_python(_: Context) -> Check:
    ok = sys.version_info >= (3, 12)
    return Check(
        "python", "ok" if ok else "fail", sys.version.split()[0], "" if ok else "install Python 3.12+"
    )


def check_paths(_: Context) -> Check:
    home = paths.agent_home()
    probe = home / "data" / ".write-test"
    probe.write_text("ok")
    probe.unlink()
    synced = [p for p in (paths.REPO_ROOT, home) if paths.is_cloud_synced(p)]
    if synced:
        return Check(
            "paths", "warn", f"cloud-synced: {', '.join(map(str, synced))}", "move out of the synced folder"
        )
    return Check("paths", "ok", str(home))


def check_config(ctx: Context) -> list[Check]:
    out = []
    loaders = {
        "settings": config.load_settings,
        "profile": config.load_profile,
        "search": config.load_search,
        "companies": config.load_companies,
    }
    for name, loader in loaders.items():
        try:
            value = loader()
        except config.ConfigError as exc:
            hint = "run: agent profile import" if name == "profile" else f"copy config/{name}.example.yaml"
            out.append(Check(f"config:{name}", "fail", str(exc).splitlines()[0], hint))
            continue
        if name == "settings":
            ctx.settings = value
        if name == "profile":
            ctx.profile = value
        out.append(Check(f"config:{name}", "ok"))
    return out


def check_secrets(_: Context) -> Check:
    missing = [name for name, present in sec.status().items() if not present]
    if missing:
        return Check(
            "secrets",
            "fail",
            f"missing: {', '.join(missing)}",
            "agent secrets import-env / agent secrets set",
        )
    return Check("secrets", "ok", f"{len(sec.REQUIRED)} present")


def check_sqlite(ctx: Context) -> Check:
    conn = db.connect()
    ctx.conn = conn
    version, latest = db.current_version(conn), db.latest_version()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    problems = []
    if version != latest:
        problems.append(f"user_version {version} != {latest}")
    if mode.lower() != "wal":
        problems.append(f"journal_mode={mode}")
    if fk != 1:
        problems.append("foreign_keys off")
    if integrity != "ok":
        problems.append(f"integrity: {integrity}")
    if problems:
        return Check("sqlite", "fail", "; ".join(problems), "run: agent db migrate")
    return Check("sqlite", "ok", f"v{version}, WAL")


def check_redaction(ctx: Context) -> Check:
    if ctx.profile is None:
        return Check("redaction", "skip", "needs profile.yaml")
    settings = ctx.settings.redaction if ctx.settings else None
    redactor = Redactor.from_profile(ctx.profile, settings)
    text = ctx.profile.as_text()
    redacted = redactor.redact(text)
    leaks = redactor.find_leaks(redacted)
    if leaks:
        return Check(
            "redaction", "fail", f"leaked kinds: {', '.join(leaks)}", "add name_variants / link handles"
        )
    restored = redactor.unredact(redacted)
    left = [p for p in PLACEHOLDER_RE.findall(restored) if p.startswith("[[CANDIDATE")]
    if left:
        return Check("redaction", "fail", f"unrestored placeholders: {sorted(set(left))}")
    return Check("redaction", "ok", f"{len(redactor.literals)} values covered")


def check_killswitch(ctx: Context) -> Check:
    if paths.stop_file().exists():
        return Check(
            "killswitch", "warn", "STOP file present: applying is halted", f"delete {paths.stop_file()}"
        )
    return Check("killswitch", "ok", "not stopped (local)")


def check_git(_: Context) -> Check:
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=paths.REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return Check("git", "skip", "not a git repo")
    bad = [p for p in tracked if PII_PATH_RE.search(p)]
    if bad:
        return Check("git", "fail", f"personal files tracked: {', '.join(bad)}", "git rm --cached <file>")
    return Check("git", "ok", f"{len(tracked)} tracked files, no personal data")


# ---------------------------------------------------------------- online checks


def check_gemini_models(ctx: Context) -> Check:
    response = httpx.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": sec.get(sec.Secret.GEMINI_API_KEY)},
        params={"pageSize": 1000},
        timeout=20,
    )
    if response.status_code != 200:
        return Check("gemini:models", "fail", f"HTTP {response.status_code}", "check the API key")
    available = {m["name"].removeprefix("models/") for m in response.json().get("models", [])}
    llm = ctx.settings.llm
    configured = [*llm.fast_models, *llm.strong_models, llm.embedding_model]
    missing = [m for m in configured if m not in available]
    if missing:
        return Check(
            "gemini:models",
            "warn",
            f"not available to this key: {', '.join(missing)}",
            "remove them from settings.yaml",
        )
    return Check("gemini:models", "ok", f"{len(configured)} configured models available")


def check_gemini_calls(ctx: Context) -> list[Check]:
    from agent.llm import LLMClient, LLMError  # imported lazily: litellm is slow to import

    if ctx.profile is None:
        return [Check("gemini:call", "skip", "needs profile.yaml")]
    client = LLMClient(ctx.settings, ctx.profile, ctx.conn)
    out = []
    for tier in ("fast", "strong"):
        try:
            result = client.complete(
                "doctor", tier, [{"role": "user", "content": "Reply with the word OK."}], 256
            )
        except LLMError as exc:
            out.append(Check(f"gemini:{tier}", "fail", mask_tokens(str(exc))[:200]))
            continue
        limit = ctx.settings.llm.limits[result.model].rpd
        used = quota.used_today(ctx.conn, result.model, datetime.now(UTC))
        out.append(
            Check(
                f"gemini:{tier}", "ok", f"{result.model}: {result.text.strip()[:20]!r}, {used}/{limit} today"
            )
        )
    return out


def check_mongo(ctx: Context) -> list[Check]:
    from agent.cloud import Cloud

    cloud = Cloud(sec.get(sec.Secret.MONGODB_URI), ctx.settings.mongo)
    try:
        if not cloud.ping():
            return [Check("mongo", "fail", "cannot connect", "check Atlas Network Access and the URI")]
        out = [Check("mongo:connect", "ok")]
        agent_db = cloud.agent_db
        options = {c["name"]: c.get("options", {}) for c in agent_db.list_collections()}
        missing = [n for n in ("heartbeat", "control") if "validator" not in options.get(n, {})]
        if missing:
            out.append(
                Check("mongo:collections", "fail", f"missing/unvalidated: {missing}", "run: agent cloud init")
            )
        else:
            try:
                agent_db["heartbeat"].insert_one({"_id": "worker-probe", "foo": 1})
                agent_db["heartbeat"].delete_one({"_id": "worker-probe"})
                out.append(
                    Check("mongo:validator", "fail", "unknown field was accepted", "run: agent cloud init")
                )
            except WriteError:
                out.append(Check("mongo:validator", "ok", "unknown fields rejected"))
        bot = cloud.client[ctx.settings.mongo.bot_db]["_job_agent_probe"]
        try:
            bot.insert_one({"probe": True})
            bot.drop()
            out.append(
                Check(
                    "mongo:scope",
                    "fail",
                    f"URI can write to {ctx.settings.mongo.bot_db}",
                    "use the scoped job-agent Atlas user",
                )
            )
        except OperationFailure:
            out.append(Check("mongo:scope", "ok", f"no write access to {ctx.settings.mongo.bot_db}"))
        last = cloud.last_inbound_at()
        if last is None:
            out.append(
                Check(
                    "mongo:inbound",
                    "warn",
                    "no inbound WhatsApp messages found or not readable",
                    "grant read@rohit_portfolio; message the bot once",
                )
            )
        else:
            age_h = (datetime.now(UTC) - last).total_seconds() / 3600
            out.append(Check("mongo:inbound", "ok", f"last inbound {age_h:.1f} h ago"))
        return out
    finally:
        cloud.close()


def check_whatsapp(ctx: Context) -> list[Check]:
    from agent.cloud import Cloud
    from agent.notify.whatsapp import ERROR_HINTS, WhatsApp, error_code

    cloud = Cloud(sec.get(sec.Secret.MONGODB_URI), ctx.settings.mongo)
    wa = WhatsApp(ctx.settings.whatsapp, ctx.conn, cloud)
    out = []
    try:
        try:
            info = wa.phone_number_info()
            out.append(
                Check(
                    "whatsapp:number",
                    "ok",
                    f"{info.get('display_phone_number')} quality={info.get('quality_rating', '?')}",
                )
            )
        except httpx.HTTPStatusError as exc:
            code = error_code(exc.response)
            return [Check("whatsapp:number", "fail", f"error {code}", ERROR_HINTS.get(code or 0, ""))]
        try:
            token = wa.token_info()
            expires = token.get("expires_at", None)
            if not token.get("is_valid"):
                out.append(Check("whatsapp:token", "fail", "token invalid", ERROR_HINTS[190]))
            elif expires not in (0, None):
                when = datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%d %H:%M UTC")
                out.append(Check("whatsapp:token", "warn", f"expires {when}", ERROR_HINTS[190]))
            else:
                out.append(Check("whatsapp:token", "ok", "no expiry"))
        except httpx.HTTPStatusError as exc:
            out.append(Check("whatsapp:token", "warn", f"debug_token error {error_code(exc.response)}"))
        is_open = wa.window_open()
        out.append(
            Check(
                "whatsapp:window", "ok", "open (plain text is free)" if is_open else "closed: messages queue"
            )
        )
        if ctx.send_test:
            if not is_open:
                out.append(
                    Check(
                        "whatsapp:send",
                        "warn",
                        "window closed, nothing sent",
                        "send any plain message to the RohitOS bot, then retry",
                    )
                )
            else:
                status = wa.send_text("job-agent doctor test", kind="doctor")
                out.append(Check("whatsapp:send", "ok" if status == "sent" else "fail", status))
        return out
    finally:
        cloud.close()


def check_remote_stop(ctx: Context) -> Check:
    from agent.cloud import Cloud

    cloud = Cloud(sec.get(sec.Secret.MONGODB_URI), ctx.settings.mongo)
    try:
        stop = cloud.read_control()
    finally:
        cloud.close()
    if stop is None:
        return Check("killswitch:remote", "warn", "control doc not readable", "run: agent cloud init")
    return Check(
        "killswitch:remote", "warn" if stop else "ok", "STOP set in Mongo" if stop else "not stopped"
    )


# ---------------------------------------------------------------- runner


def run(offline: bool = False, send_test: bool = False) -> list[Check]:
    ctx = Context(offline=offline, send_test=send_test)
    for name, fn in (
        ("python", check_python),
        ("paths", check_paths),
        ("config", check_config),
        ("secrets", check_secrets),
        ("sqlite", check_sqlite),
        ("redaction", check_redaction),
        ("killswitch", check_killswitch),
        ("git", check_git),
    ):
        _run(ctx, name, fn)
    online = (
        ("gemini:models", check_gemini_models),
        ("gemini:call", check_gemini_calls),
        ("mongo", check_mongo),
        ("whatsapp", check_whatsapp),
        ("killswitch:remote", check_remote_stop),
    )
    if offline:
        ctx.results.extend(Check(name, "skip", "offline") for name, _ in online)
    elif ctx.settings is None or ctx.conn is None:
        ctx.results.extend(Check(name, "skip", "needs settings.yaml and the database") for name, _ in online)
    else:
        for name, fn in online:
            _run(ctx, name, fn)
    if ctx.conn is not None:
        ctx.conn.close()
    return ctx.results
