#!/usr/bin/env python3
"""Read-only live body snapshot and commanded motion ranges; never moves the robot."""
import json
from datetime import datetime, timezone
from tonypi.tonyd.client import TonydClient


def main():
    client = TonydClient()
    try:
        state = client.state(fresh=True)
        joints = client.joints()
        calibration = client.call('calibration')['calibration']
        servos = client.call('servo_calibration')['servos']
        motions = client.call('motions')['motions']
        ranges = {str(i): {'min': None, 'max': None} for i in range(1, 17)}
        for motion in motions:
            data = client.call('motion_get', name=motion['name'])
            for frame in data['frames']:
                for sid, value in frame['pos'].items():
                    if sid not in ranges:
                        continue
                    r = ranges[sid]
                    if r['min'] is None or value < r['min']['value']:
                        r['min'] = {'value': value, 'motion': motion['name']}
                    if r['max'] is None or value > r['max']['value']:
                        r['max'] = {'value': value, 'motion': motion['name']}
        print(json.dumps({'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                          'note': 'Motion extrema are commands, not measured safe limits.',
                          'state': state, 'joints': joints, 'calibration': calibration,
                          'servo_calibration': servos, 'motion_count': len(motions),
                          'commanded_motion_ranges': ranges}, indent=2))
    finally:
        client.close()


if __name__ == '__main__':
    main()
