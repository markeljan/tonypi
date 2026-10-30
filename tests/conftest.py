import pytest


@pytest.fixture(autouse=True)
def _private_robot_lock(tmp_path, monkeypatch):
    """Tests never look at the real /run/lock/tonypi-robot.lock (a live harness test may hold it)."""
    from tonypi.agent import daemon

    monkeypatch.setattr(daemon, "ROBOT_LOCK", str(tmp_path / "robot.lock"))
