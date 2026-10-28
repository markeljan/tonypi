"""Read-only hardware bench. Sends no motion commands.

Measures: IMU stream rate/units, battery, per-servo read latency, positions, temps, voltages,
offsets, torque state. Usage: python tools/bench.py [--seconds 3]
"""

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tonypi.hal.board import Board  # noqa: E402
from tonypi.hal.proto import BusCmd  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=3.0)
ap.add_argument("--device", default="/dev/ttyAMA0")
args = ap.parse_args()

out = {}
with Board(args.device) as b:
    time.sleep(0.3)
    c0, t0 = b.imu_count, time.monotonic()
    samples = []
    b.imu_listeners.append(samples.append)
    time.sleep(args.seconds)
    b.imu_listeners.clear()
    rate = (b.imu_count - c0) / (time.monotonic() - t0)
    out["imu_rate_hz"] = round(rate, 1)
    if samples:
        ax = [statistics.fmean(s.accel[i] for s in samples) for i in range(3)]
        gx = [statistics.fmean(s.gyro[i] for s in samples) for i in range(3)]
        out["imu_accel_mean"] = [round(v, 4) for v in ax]
        out["imu_accel_norm"] = round(math.sqrt(sum(v * v for v in ax)), 4)
        out["imu_gyro_mean"] = [round(v, 4) for v in gx]
        out["imu_gyro_std"] = [round(statistics.pstdev(s.gyro[i] for s in samples), 4) for i in range(3)]
        dts = [b2.t - a.t for a, b2 in zip(samples, samples[1:])]
        if dts:
            out["imu_dt_ms_p50_p99"] = [round(1000 * statistics.median(dts), 2),
                                        round(1000 * sorted(dts)[int(0.99 * (len(dts) - 1))], 2)]
    out["battery_mv"] = b.battery_mv
    out["rx_frames"] = {str(k): v for k, v in b.rx_frames.items()}

    servos = {}
    lat = []
    for sid in range(1, 19):
        t = time.perf_counter()
        pos = b.read_position(sid, timeout=0.05)
        lat.append(time.perf_counter() - t)
        if pos is None:
            servos[sid] = None
            continue
        servos[sid] = {
            "pos": pos,
            "temp_c": b.bus_read(sid, BusCmd.READ_TEMP),
            "vin_mv": b.bus_read(sid, BusCmd.READ_VIN),
            "offset": b.bus_read(sid, BusCmd.READ_OFFSET),
            "torque_state": b.bus_read(sid, BusCmd.READ_TORQUE),
            "angle_limit": b.bus_read(sid, BusCmd.READ_ANGLE_LIMIT),
        }
    out["servos"] = servos
    ok = [sid for sid, v in servos.items() if v]
    t = time.perf_counter()
    n = 5
    for _ in range(n):
        b.read_positions(ok, timeout=0.05)
    full = (time.perf_counter() - t) / n
    out["full_position_sweep_ms"] = round(1000 * full, 1)
    out["full_position_sweep_hz"] = round(1 / full, 1) if full else None
    out["first_read_latency_ms_median"] = round(1000 * statistics.median(lat), 2)
    out["pwm_head"] = {ch: b.pwm_read(ch) for ch in (1, 2)}
    out["crc_errors"] = b._decoder.crc_errors

print(json.dumps(out, indent=1, default=str))
