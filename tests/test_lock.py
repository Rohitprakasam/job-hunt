import subprocess
import sys

import pytest

from agent.lock import AlreadyRunning, single_instance

HOLD = """
import sys, time
from pathlib import Path
from agent.lock import single_instance
with single_instance(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(30)
"""


def test_second_worker_is_refused(tmp_path):
    path = tmp_path / "worker.lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning), single_instance(path):
            pass
    finally:
        holder.kill()
        holder.wait()
    import time

    reacquired = False
    for _ in range(20):
        try:
            with single_instance(path):  # released when the holder died
                assert path.read_text().strip().isdigit()
                reacquired = True
                break
        except AlreadyRunning:
            time.sleep(0.05)
    assert reacquired, "Lock was never re-acquired after holder process died"


def test_is_locked(tmp_path):
    from agent.lock import is_locked

    path = tmp_path / "test.lock"
    assert not is_locked(path)
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert is_locked(path)
    finally:
        holder.kill()
        holder.wait()
    import time

    time.sleep(0.1)
    assert not is_locked(path)
