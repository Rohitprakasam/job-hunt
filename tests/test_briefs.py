from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent import cli, db, paths
from agent.console import create_app

runner = CliRunner()


def test_briefs_import_and_list_and_show(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("JOB_AGENT_HOME", str(home))

    src_dir = tmp_path / "incoming_briefs"
    src_dir.mkdir()
    (src_dir / "101.md").write_text(
        "# Stripe Brief\nStripe is an online payments provider.", encoding="utf-8"
    )
    (src_dir / "102.md").write_text(
        "# Datadog Brief\nDatadog is an observability platform.", encoding="utf-8"
    )
    (src_dir / "ignored.txt").write_text("not markdown", encoding="utf-8")

    # Import
    res = runner.invoke(cli.app, ["briefs", "import", str(src_dir)])
    assert res.exit_code == 0
    assert "imported 101.md" in res.output
    assert "imported 102.md" in res.output
    assert "Successfully imported 2 brief(s)" in res.output

    # List
    res_list = runner.invoke(cli.app, ["briefs", "list"])
    assert res_list.exit_code == 0
    assert "101.md" in res_list.output
    assert "102.md" in res_list.output

    # Show
    res_show = runner.invoke(cli.app, ["briefs", "show", "101"])
    assert res_show.exit_code == 0
    assert "Stripe is an online payments provider" in res_show.output

    # Initialize test DB schema
    conn = db.connect(paths.db_path())
    db.migrate(conn)
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, company, title, dedupe_key, first_seen_at) "
        "VALUES (101, 'test', 'ext101', 'Stripe', 'Software Engineer', 'dedupe101', '2026-11-07T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Console view
    client = TestClient(create_app(db_path=paths.db_path()))
    view_res = client.get("/briefs/101")
    assert view_res.status_code == 200
    assert "Stripe is an online payments provider" in view_res.text
    assert "Stripe" in view_res.text

    # Missing brief returns 404
    missing_res = client.get("/briefs/999999")
    assert missing_res.status_code == 404
