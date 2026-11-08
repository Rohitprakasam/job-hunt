from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agent import db, scheduler
from agent.context import Ctx
from agent.lock import AlreadyRunning


@pytest.fixture
def fake_ctx(settings, profile, conn):
    ctx = Ctx(
        conn=conn,
        settings=settings,
        search=None,
        companies=None,
        profile=profile,
        client=MagicMock(),
        cloud=None,
        wa=MagicMock(),
    )
    return ctx


def test_cycle_due_logic(fake_ctx, monkeypatch):
    executed = []

    def fake_run_stage(name: str) -> str:
        executed.append(name)
        return f"{name}-ok"

    monkeypatch.setattr(scheduler, "run_stage", fake_run_stage)

    # 1. First run: morning 07:30 (before 08:00 digest, Tuesday)
    t1 = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)  # 07:30 IST (+5:30)
    res1 = scheduler.cycle(fake_ctx, now=t1)
    assert "discover-ats" in executed
    assert "discover-boards" in executed
    assert "score" in executed
    assert "urgent" in executed
    assert "digest" not in executed  # before 08:00
    assert "prepare-apply" in executed
    assert "prep-packs" in executed
    assert "heartbeat" in executed
    assert "flush" in executed
    assert "backup" in executed
    assert "weekly" not in executed  # Tuesday
    assert "discover-ats: discover-ats-ok" in res1

    # Verify flags set
    assert db.get_flag(fake_ctx.conn, "cycle:last_run:discover-boards") is not None
    assert db.get_flag(fake_ctx.conn, "cycle:last_run:backup") == "2026-11-10"
    assert db.get_flag(fake_ctx.conn, "cycle:last_run:digest") is None

    # 2. Second run 1 hour later: 08:30 IST (digest is due, boards NOT due < 3h)
    executed.clear()
    t2 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)  # 08:30 IST
    scheduler.cycle(fake_ctx, now=t2)
    assert "discover-ats" in executed
    assert "discover-boards" not in executed  # only 1h passed
    assert "digest" in executed  # 08:30 > 08:00
    assert "backup" not in executed  # already done today
    assert db.get_flag(fake_ctx.conn, "cycle:last_run:digest") == "2026-11-10"

    # 3. Third run on Sunday evening: 19:30 IST
    executed.clear()
    t3 = datetime(2026, 11, 15, 14, 0, tzinfo=UTC)  # 19:30 IST (Sunday)
    scheduler.cycle(fake_ctx, now=t3)
    assert "weekly" in executed
    assert db.get_flag(fake_ctx.conn, "cycle:last_run:weekly") == "2026-11-15"


def test_cycle_concurrent_lock_refusal(fake_ctx, tmp_path, monkeypatch):
    from agent.lock import single_instance

    monkeypatch.setattr("agent.paths.agent_home", lambda: tmp_path)
    lock_file = tmp_path / "data" / "worker.lock"

    with single_instance(lock_file):
        with pytest.raises(AlreadyRunning):
            scheduler.cycle(fake_ctx)
