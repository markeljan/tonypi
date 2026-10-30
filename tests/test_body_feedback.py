import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('body_feedback', Path(__file__).parents[1] / 'tools/body_feedback.py')
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


class FakeClient:
    def __init__(self):
        self.calls = []

    def state(self, fresh=False):
        self.calls.append(('state', fresh))
        return {'heading_deg': 2, 'imu_age_s': 0.1, 'temps': {'6': 35}}

    def joints(self):
        self.calls.append(('joints',))
        return {'pos': {6: 917, 7: None}, 'raw': {6: 917, 7: None}}

    def call(self, op):
        self.calls.append((op,))
        return {'calibration': {}} if op == 'calibration' else {'servos': {}}


def test_capture_only_reads_and_compares_missing_positions():
    client = FakeClient()
    result = feedback.capture(client, {'6': 930, '7': 800}, 'arms')
    assert client.calls == [('state', True), ('joints',), ('calibration',), ('servo_calibration',)]
    assert result['comparison']['6'] == {'target': 930, 'actual': 917, 'error': -13}
    assert result['comparison']['7']['error'] is None
    assert result['state']['imu_age_s'] == 0.1


@pytest.mark.parametrize('value', ['[]', '{"17": 1}', '{"06": 1}', '{"6": true}', '{"6": 1001}', '{"6": 1.5}'])
def test_invalid_targets(value):
    with pytest.raises(ValueError):
        feedback.targets_from_json(value)
