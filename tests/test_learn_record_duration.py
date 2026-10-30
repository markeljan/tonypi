"""Recording must span long skill jobs as well as frame-based motions."""

from test_learn import FakePhone, FakeTonyd
from tonypi.learn.runner import Lab


class DurationPhone(FakePhone):
    def record_start(self, label, max_s=60):
        self.max_s = max_s
        return super().record_start(label, max_s)


def test_skill_recording_covers_job_timeout_and_tail(tmp_path):
    phone = DurationPhone()
    lab = Lab(tonyd=FakeTonyd(), phone=phone, root=tmp_path / 'learning',
              lock_path=str(tmp_path / 'lock'), sleep=lambda _: None)
    lab.trial({'skill': 'wave', 'guard_tilt_deg': 30, 'trials': 1})
    assert phone.max_s >= 122
