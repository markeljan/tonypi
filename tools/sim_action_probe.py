"""Offline model-only instrumentation; replay through the unchanged simulation engine."""
from __future__ import annotations

import argparse
import bisect
import copy
import json
import math
from pathlib import Path
import tempfile


CHECK_KEYS = ("simulation_success", "max_tilt_deg", "end_tilt_deg", "displacement_xy_m", "yaw_deg",
              "end_speed_m_s", "end_spin_deg_s", "min_com_height_m", "peak_speed_m_s", "physics_seconds")


def box_bottom(center, rotation, halfsize):
    """Lowest world z of an oriented box, including tilted sole corners."""
    row = rotation[6:9] if len(rotation) == 9 else rotation[2]
    return float(center[2] - sum(abs(float(r)) * float(h) for r, h in zip(row, halfsize)))


def phase_ends(frames, dt):
    if not math.isfinite(dt) or dt <= 0:
        raise ValueError("timestep must be finite and positive")
    ends, steps = [], 0
    if not isinstance(frames, list) or not frames:
        raise ValueError("raw_frames must be a nonempty list")
    for frame in frames:
        ms = frame.get("ms")
        if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms <= 0:
            raise ValueError("frame ms must be finite and positive")
        if not isinstance(frame.get("pos"), dict):
            raise ValueError("frame pos must be an object")
        steps += math.ceil(ms / 1000 / dt)
        ends.append(steps)
    return ends


def phase_for_step(step, ends):
    """Completed step at a frame boundary belongs to that frame; zero is initialization."""
    return bisect.bisect_left(ends, max(1, step))


def load_input(path):
    path = Path(path)
    directory = path if path.is_dir() else path.parent
    if path.is_file() and path.name not in ("report.json", "request.json", "physics.json"):
        raise ValueError("input must be a report directory, report.json, physics.json or request.json")
    request = json.loads((directory / "request.json").read_text())
    if not isinstance(request, dict) or not isinstance(request.get("plan"), dict):
        raise ValueError("request must contain a plan")
    phase_ends(request["plan"].get("raw_frames"), .004)
    reference = directory / "physics.json"
    if not reference.exists():
        reference = directory / "report.json"
    original = json.loads(reference.read_text()) if reference.exists() else None
    return request, original


def probe(request, original=None, interval_s=.02):
    import mujoco as mj
    from tonypi.simulation import engine

    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("trace interval must be finite and positive")
    request = copy.deepcopy(request)
    request["render"] = False
    trace, phases = [], []
    steps, last_trace = 0, -math.inf
    ids = None
    original_step = mj.mj_step

    def observe(model, data):
        nonlocal ids, last_trace
        if ids is None:
            ids = {side: mj.mj_name2id(model, mj.mjtObj.mjOBJ_GEOM, f"{side}_ankle_roll_sole")
                   for side in ("l", "r")}
            ids["floor"] = mj.mj_name2id(model, mj.mjtObj.mjOBJ_GEOM, "floor")
            ids["torso"] = mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, "torso")
            if min(ids.values()) < 0:
                raise ValueError("model lacks required soles, floor or torso")
            ends = phase_ends(request["plan"]["raw_frames"], float(model.opt.timestep))
            ids["ends"] = ends
            ids["mass_kg"] = float(model.body_mass.sum())
            ids["actuators"] = [mj.mj_id2name(model, mj.mjtObj.mjOBJ_ACTUATOR, i) for i in range(model.nu)]
            previous = 0
            for i, end in enumerate(ends + [ends[-1] + math.ceil(request["settle_s"] / model.opt.timestep)]):
                phases.append({"phase": i, "kind": "frame" if i < len(ends) else "settle",
                               "start_s": float(previous * model.opt.timestep),
                               "end_s": float(end * model.opt.timestep), "samples": 0,
                               "feet": {}, "peak_abs_actuator_force_model": [0.] * model.nu})
                previous = end
        phase = phase_for_step(steps, ids["ends"])
        record = {"t_s": round(float(data.time), 6), "phase": phase,
                  "torso_subtree_com_m": data.subtree_com[ids["torso"]].tolist(),
                  "tilt_deg": math.degrees(math.acos(max(-1., min(1., float(data.xmat[ids["torso"], 8]))))),
                  "feet": {}, "actuator_force_model": data.actuator_force.tolist()}
        stats = phases[phase]
        stats["samples"] += 1
        stats["max_tilt_deg"] = max(stats.get("max_tilt_deg", 0.), record["tilt_deg"])
        for side in ("l", "r"):
            geom = ids[side]
            center = data.geom_xpos[geom].tolist()
            bottom = box_bottom(center, data.geom_xmat[geom], model.geom_size[geom])
            contact = any({int(c.geom1), int(c.geom2)} == {geom, ids["floor"]} for c in data.contact)
            record["feet"][side] = {"center_m": center, "sole_bottom_m": bottom, "floor_contact": contact}
            foot = stats["feet"].setdefault(side, {"min_sole_bottom_m": bottom, "max_sole_bottom_m": bottom,
                                                  "start_center_m": center, "floor_contact_samples": 0})
            foot["min_sole_bottom_m"] = min(foot["min_sole_bottom_m"], bottom)
            foot["max_sole_bottom_m"] = max(foot["max_sole_bottom_m"], bottom)
            foot["end_center_m"] = center
            foot["floor_contact_samples"] += int(contact)
        stats["peak_abs_actuator_force_model"] = [max(a, abs(float(b))) for a, b in
                                                  zip(stats["peak_abs_actuator_force_model"], data.actuator_force)]
        if data.time - last_trace >= interval_s - 1e-9:
            trace.append(record)
            last_trace = float(data.time)

    def step(model, data, *args, **kwargs):
        nonlocal steps
        if not steps:
            observe(model, data)
        result = original_step(model, data, *args, **kwargs)
        steps += 1
        observe(model, data)
        return result

    try:
        mj.mj_step = step
        with tempfile.TemporaryDirectory(prefix="tonypi-model-probe-") as directory:
            physics = engine.run(request, Path(directory))
    finally:
        mj.mj_step = original_step
    comparison = None if original is None else {
        key: {"original": original.get(key), "replay": physics.get(key), "equal": key in original and key in physics and original[key] == physics[key]}
        for key in CHECK_KEYS}
    return {"model_only": True, "hardware_validated": False,
            "limitations": "Ideal floor and uncalibrated model geometry, friction, gains and mass; sole clearance is "
                           "geometric, not rug crossing evidence. Actuator forces are model estimates, not servo measurements. "
                           "Contacts follow mj_step timing, with up to one timestep offset.",
            "exact_replay_verified": None if comparison is None else all(v["equal"] for v in comparison.values()),
            "comparison": comparison, "physics": physics, "actuator_names": ids["actuators"],
            "model_mass_kg": ids["mass_kg"], "trace_interval_s": interval_s, "phases": phases, "trace": trace}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--interval-s", type=float, default=.02)
    args = parser.parse_args()
    request, original = load_input(args.input)
    result = probe(request, original, args.interval_s)
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
