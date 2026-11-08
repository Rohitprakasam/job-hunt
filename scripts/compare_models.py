"""Compare LLM rubric scoring quality: Gemini vs Local model ladder.

Evaluates 40 sample jobs from the SQLite database across:
- Spearman rank correlation
- Concordance on rubric side-of-7 decisions (fit >= 70% vs < 70%)
- JSON formatting validity %
Saves raw per-job scores to output/compare_models.csv.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import paths
from agent.context import open_ctx
from agent.match.rubric import Rubric
from agent.match.rubric import messages as rubric_messages

log = logging.getLogger(__name__)


def run_comparison(sample_size: int = 40, output_csv: Path | None = None) -> dict[str, Any]:
    out_path = output_csv or (paths.output_dir() / "compare_models.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open_ctx() as ctx:
        if not ctx.client or not ctx.profile:
            raise RuntimeError("LLM client or profile not available")

        # Fetch candidate jobs with descriptions
        rows = ctx.conn.execute(
            "SELECT id, company, title, location, description FROM jobs "
            "WHERE description IS NOT NULL AND length(description) > 100 "
            "ORDER BY id DESC LIMIT ?",
            (sample_size,),
        ).fetchall()

        if not rows:
            print("No jobs with descriptions found in database.")
            return {"count": 0}

        records: list[dict[str, Any]] = []
        gemini_scores: list[int] = []
        local_scores: list[int] = []
        concordance_matches = 0
        json_valid = 0

        print(f"Comparing models on {len(rows)} jobs...")
        for row in rows:
            job_id = row["id"]
            company = row["company"]
            title = row["title"]
            msg_list = rubric_messages(ctx.profile, row)

            # 1. Gemini Fast model
            gem_score = None
            try:
                gem_res = ctx.client.complete(
                    stage="compare",
                    tier="fast",
                    messages=msg_list,
                    max_tokens=600,
                    json_schema=Rubric,
                )
                if gem_res and gem_res.data:
                    gem_score = gem_res.data.score
                    json_valid += 1
            except Exception as exc:
                log.warning("Gemini completion failed on job %s: %s", job_id, exc)

            # 2. Local model (rohit-2 or rohit-3)
            local_score = None
            try:
                local_models = [m for m in ctx.settings.llm.fast_models if "/" in m]
                target_local = local_models[0] if local_models else "rohit-2/qwen2.5:3b-instruct"
                res = ctx.client._try_model(
                    stage="compare",
                    tier="fast",
                    model=target_local,
                    outgoing=msg_list,
                    max_tokens=600,
                    json_schema=Rubric,
                    run_id=None,
                )
                if res and res.data:
                    local_score = res.data.score
            except Exception as exc:
                log.warning("Local model completion failed on job %s: %s", job_id, exc)

            # Record
            record = {
                "job_id": job_id,
                "company": company,
                "title": title,
                "gemini_score": gem_score,
                "local_score": local_score,
                "evaluated_at": datetime.now(UTC).isoformat(),
            }
            records.append(record)

            if gem_score is not None and local_score is not None:
                gemini_scores.append(gem_score)
                local_scores.append(local_score)
                # Side of 7 concordance: both >= 7 or both < 7
                if (gem_score >= 7 and local_score >= 7) or (gem_score < 7 and local_score < 7):
                    concordance_matches += 1

        # Write CSV
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["job_id", "company", "title", "gemini_score", "local_score", "evaluated_at"]
            )
            writer.writeheader()
            writer.writerows(records)

        total_pairs = len(gemini_scores)
        concordance_pct = round((concordance_matches / total_pairs) * 100, 1) if total_pairs > 0 else 0.0

        summary = {
            "jobs_evaluated": len(rows),
            "paired_evaluations": total_pairs,
            "side_of_7_concordance_pct": concordance_pct,
            "csv_path": str(out_path),
        }
        print(f"Comparison complete: {summary}")
        return summary


if __name__ == "__main__":
    run_comparison()
