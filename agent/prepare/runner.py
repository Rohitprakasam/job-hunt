"""Prepare stage: for the best new jobs on a supported ATS, tailor the resume, write a cover note,
render the PDF and run the fit and truth gates. The result is an application row ('prepared' or
'skipped' with the reason). Forms are filled later by the apply stage."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agent import paths
from agent.config import Profile, Settings
from agent.gates import GateResult, as_json, fit_gate, truth_gate
from agent.llm import LLMBusy, LLMPaused, QuotaExhausted
from agent.prepare import latex, render, tailor
from agent.report.experiments import assign_variant
from agent.runs import stage_run

log = logging.getLogger(__name__)


class Cover(BaseModel):
    text: str = Field(max_length=1500)


COVER_SYSTEM = (
    "Write a short cover note (90-150 words, plain text, no greeting line, no sign-off, no placeholders) for "
    "this job using ONLY facts from the candidate summary and bullets given. No new skills, numbers, "
    "employers or claims. Mirror the job's own wording where it is true for the candidate. JSON only."
)


def cover_note(client, profile: Profile, tailored: tailor.Tailored, job) -> str:
    facts = (
        tailored.summary
        + "\n"
        + "\n".join(b.text for s in tailored.experience + tailored.projects for b in s.bullets)
    )
    if client is not None:
        try:
            result = client.complete(
                "cover", "strong",
                [{"role": "system", "content": COVER_SYSTEM},
                 {"role": "user", "content": f"CANDIDATE\n{facts}\n\nJOB: {job['title']} at {job['company']}\n"
                                             f"{(job['description'] or '')[:4000]}"}],
                max_tokens=4000, json_schema=Cover,
            )  # fmt: skip
            if result.data is not None:
                return result.data.text.strip()  # type: ignore[union-attr]
        except (LLMPaused, QuotaExhausted, LLMBusy):
            raise
        except Exception as exc:
            log.warning("cover note via LLM failed: %s", type(exc).__name__)
    first = (
        tailored.experience[0].bullets[0].text
        if tailored.experience and tailored.experience[0].bullets
        else ""
    )
    return (
        f"I am applying for the {job['title']} role at {job['company']}. {tailored.summary} {first}".strip()
    )


@dataclass
class PrepareResult:
    prepared: int = 0
    skipped: int = 0
    stopped: str | None = None


def candidates(conn: sqlite3.Connection, settings: Settings, limit: int) -> list[sqlite3.Row]:
    adapters = settings.apply.adapters
    marks = ",".join("?" * len(adapters))
    return conn.execute(
        f"SELECT j.*, s.fit FROM jobs j JOIN scores s ON s.job_id = j.id"
        f" LEFT JOIN applications a ON a.job_id = j.id"
        f" WHERE a.id IS NULL AND j.filter_reason IS NULL AND s.fit >= ? AND j.ats_type IN ({marks})"
        f" AND j.apply_url IS NOT NULL ORDER BY s.priority DESC LIMIT ?",
        (settings.apply.min_fit, *adapters, limit),
    ).fetchall()


def prepare_one(conn, settings: Settings, profile: Profile, client, job, run_id=None) -> tuple[int, bool]:
    """Returns (application_id, prepared?)."""
    gates = [fit_gate(job["fit"], job["filter_reason"], settings.apply)]
    job_text = f"{job['title']}\n{job['description'] or ''}"
    tailored = None
    if client is not None:
        try:
            tailored = tailor.with_llm(client, profile, job, run_id)
        except (LLMPaused, QuotaExhausted, LLMBusy):
            raise
        except Exception as exc:
            log.warning("tailor via LLM failed for job %s: %s", job["id"], type(exc).__name__)
    if tailored is not None:
        llm_check = truth_gate(profile, tailored, job_text)
        if not llm_check.ok:
            # The LLM added something untrue: keep the record, fall back to the always-true version.
            log.warning("truth gate rejected LLM tailoring for job %s: %s", job["id"], llm_check.detail)
            gates.append(GateResult("truth_llm", False, llm_check.detail))
            tailored = None
    if tailored is None:
        tailored = tailor.deterministic(profile, job)
    variant = assign_variant(conn, "summary")
    if variant == "B":
        tailored = tailored.model_copy(update={"summary": profile.summary})
    cover = cover_note(client, profile, tailored, job)
    gates.append(truth_gate(profile, tailored, job_text, cover, (job["title"], job["company"])))
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin, resume_variant) VALUES (?, 'prepared', 'agent', ?)",
        (job["id"], f"{tailored.method}:{variant}"),
    )
    app_id = cur.lastrowid
    conn.execute(
        "INSERT INTO experiments(name, variant, application_id) VALUES ('summary', ?, ?)", (variant, app_id)
    )
    failed = [g for g in gates if not g.ok and g.gate != "truth_llm"]
    out = paths.application_dir(app_id)
    (out / "cover.txt").write_text(cover, encoding="utf-8")
    (out / "tailored.json").write_text(tailored.model_dump_json(indent=2), encoding="utf-8")
    resume_path = None
    if not failed and settings.resume.engine == "latex":
        try:
            pdf, edits = latex.build(
                profile, job, settings.resume, paths.config_dir() / settings.resume.latex_template, out,
                client if settings.resume.llm_summary else None, run_id,
            )  # fmt: skip
            resume_path = str(pdf)
            if edits.problems:  # Gemini's rewording was discarded; the resume itself stayed true
                gates.append(
                    GateResult(
                        "truth_llm", False, "summary rewording discarded: " + "; ".join(edits.problems)
                    )
                )
            gates.append(
                GateResult("ats", True, f"matched {len(edits.matched)}, missing {len(edits.missing)}")
            )
        except latex.LatexError as exc:
            failed.append(GateResult("render", False, str(exc)[:200]))
    elif not failed:
        try:
            resume_path = str(render.render_pdf(profile, tailored, out))
        except render.RenderError as exc:
            failed.append(GateResult("render", False, str(exc)[:200]))
    conn.execute(
        "UPDATE applications SET status = ?, resume_path = ?, cover_path = ?, gate_results_json = ?, skip_reason = ?"
        " WHERE id = ?",
        (
            "skipped" if failed else "prepared",
            resume_path,
            str(out / "cover.txt"),
            json.dumps(as_json(gates)),
            failed[0].gate + ": " + failed[0].detail if failed else None,
            app_id,
        ),
    )
    return app_id, not failed


def run_prepare(
    conn, settings: Settings, profile: Profile, client=None, limit: int | None = None
) -> PrepareResult:
    result = PrepareResult()
    if settings.resume.engine == "latex" and latex.find_engine(settings.resume.latex_command) is None:
        # Pause instead of marking good jobs "skipped": they are prepared once an engine is installed.
        result.stopped = "no LaTeX engine"
        return result
    with stage_run(conn, "prepare") as run:
        for job in candidates(conn, settings, settings.apply.prepare_per_run if limit is None else limit):
            try:
                _, ok = prepare_one(conn, settings, profile, client, job, run.id)
            except (LLMPaused, QuotaExhausted, LLMBusy) as exc:
                result.stopped = type(exc).__name__
                break
            result.prepared += int(ok)
            result.skipped += int(not ok)
        run.items = result.prepared
    log.info("prepare: %s prepared, %s skipped", result.prepared, result.skipped)
    return result
