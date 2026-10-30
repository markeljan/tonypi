#!/usr/bin/env python3
"""Read-only tonyd snapshots and target comparison; no body commands."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tonypi.tonyd.client import TonydClient, TonydError

LIMITATIONS = [
    'Heading is relative integrated gyro and drifts; compare nearby readings only.',
    'No absolute location, contact, clearance, balance or mechanical limits are measured.',
    'Head angles are commanded values, not independently measured; raw IMU vectors are not exposed by state.',
    'State and joint reads are sequential, not simultaneous; fresh state may be cached while busy.',
    'Target error is observed motion-space position minus recorded command, not proof of a safe or completed move.',
]


def targets_from_json(value):
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError('targets must be a JSON object mapping servo IDs to counts')
    result = {}
    for key, count in data.items():
        if not isinstance(key, str) or not key.isdigit() or str(int(key)) != key or not 1 <= int(key) <= 16:
            raise ValueError(f'invalid servo ID: {key}')
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
            raise ValueError(f'invalid target for servo {key}')
        result[key] = count
    return result


def compare(targets, positions):
    result = {}
    for sid, target in targets.items():
        actual = positions.get(sid, positions.get(int(sid)))
        result[sid] = {'target': target, 'actual': actual,
                       'error': None if actual is None else actual - target}
    return result


def capture(client, targets, label):
    started = datetime.now(timezone.utc).isoformat()
    state = client.state(fresh=True)
    joints = client.joints()
    calibration = client.call('calibration')['calibration']
    servo_calibration = client.call('servo_calibration')['servos']
    return {'started_utc': started, 'completed_utc': datetime.now(timezone.utc).isoformat(),
            'label': label, 'state': state, 'joints': joints, 'calibration': calibration,
            'servo_calibration': servo_calibration, 'comparison': compare(targets, joints['pos']),
            'limitations': LIMITATIONS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets', default='{}', help='recorded motion-space target JSON, e.g. {"6":930}')
    parser.add_argument('--label', default='', help='experiment or sampling phase')
    parser.add_argument('--log', type=Path, help='append complete snapshot as one JSON line')
    args = parser.parse_args(argv)
    try:
        targets = targets_from_json(args.targets)
        client = TonydClient()
        try:
            result = capture(client, targets, args.label)
        finally:
            client.close()
        if args.log:
            with args.log.open('a', encoding='utf-8') as output:
                output.write(json.dumps(result, allow_nan=False) + '\n')
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, KeyError, OSError, TonydError) as exc:
        print(f'body feedback: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
