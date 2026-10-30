import pytest


@pytest.fixture(autouse=True)
def _private_robot_lock(tmp_path, monkeypatch):
    """Tests never look at the real /run/lock/tonypi-robot.lock (a live harness test may hold it), nor write the real
    autonomy preempt mark (tonypi.brain.preempt_file reads TONYPI_LIVE_DIR)."""
    from tonypi.agent import daemon
    from tonypi.learn import runner
    from tonypi.voice import autonomy

    lock = str(tmp_path / "robot.lock")
    monkeypatch.setattr(daemon, "ROBOT_LOCK", lock)
    monkeypatch.setattr(runner, "ROBOT_LOCK", lock)
    monkeypatch.setattr(autonomy, "ROBOT_LOCK", lock)
    monkeypatch.setenv("TONYPI_LIVE_DIR", str(tmp_path / "live"))
