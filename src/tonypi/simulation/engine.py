"""Headless physics worker. Deliberately has no tonyd, serial, phone or upstream controller imports."""

from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import sys
import zlib

from .catalog import mapping

NAMES = ("ankle_roll", "ankle_pitch", "knee", "hip_pitch", "hip_roll", "elbow", "shoulder_roll", "shoulder_pitch")


def png(path, rgb):
    """Write an RGB PNG without another optional image dependency."""
    h, w, _ = rgb.shape

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    raw = b"".join(b"\0" + row.tobytes() for row in rgb)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def run(request: dict, directory: Path) -> dict:
    import mujoco as mj
    import numpy as np

    repo = Path(request["repo"])
    zero, sign = mapping(repo)
    model = mj.MjModel.from_xml_path(str(repo / "model/scene.xml"))
    data = mj.MjData(model)
    start = request["start"]
    key = mj.mj_name2id(model, mj.mjtObj.mjOBJ_KEY, start)
    if key < 0:
        raise ValueError("unknown start keyframe")
    mj.mj_resetDataKeyframe(model, data, key)
    joint_names = [f"{side}_{n}" for side in ("l", "r") for n in NAMES]
    joints = [mj.mj_name2id(model, mj.mjtObj.mjOBJ_JOINT, n) for n in joint_names]
    acts = [mj.mj_name2id(model, mj.mjtObj.mjOBJ_ACTUATOR, n) for n in joint_names]
    if min(joints + acts) < 0:
        raise ValueError("model has an unsupported joint/actuator layout")
    qadr = model.jnt_qposadr[joints]
    ranges = model.actuator_ctrlrange[acts]
    scale = 1000 / math.radians(240)

    def angle(sid, pulse):
        return sign[sid] * (pulse - zero[sid]) / scale

    if request.get("initial_raw") is not None:
        for sid, pulse in request["initial_raw"].items():
            data.qpos[qadr[int(sid) - 1]] = angle(int(sid), pulse)
    targets = data.qpos[qadr].copy()
    data.ctrl[acts] = targets
    mj.mj_forward(model, data)
    feet = {mj.mj_name2id(model, mj.mjtObj.mjOBJ_GEOM, f"{s}_ankle_roll_sole") for s in ("l", "r")}
    floor = mj.mj_name2id(model, mj.mjtObj.mjOBJ_GEOM, "floor")
    torso = mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, "torso")
    # For an explicit upright starting joint pose, translate the free base so the lowest sole touches the floor.
    # This is an initialization assumption, not a measured real robot root height.
    if request.get("initial_raw") is not None:
        bottom = min(data.geom_xpos[g, 2] - abs(data.geom_xmat[g].reshape(3, 3)[2]) @ model.geom_size[g]
                     for g in feet)
        data.qpos[2] -= bottom
        mj.mj_forward(model, data)
    initial_raw = {sid: int(round(zero[sid] + sign[sid] * data.qpos[qadr[sid - 1]] * scale))
                   for sid in range(1, 17)}
    initial_xy = data.subtree_com[torso, :2].copy()
    initial_rot = data.xmat[torso].reshape(3, 3).copy()
    clips, nonfoot, max_tilt, min_com, peak_speed = [], set(), 0.0, float("inf"), 0.0
    finite, last_contacts, trace = True, [], []
    dt = model.opt.timestep
    renderer, render_error, pictures = None, None, []
    if request["render"]:
        try:
            renderer = mj.Renderer(model, height=360, width=480)
        except Exception as e:
            render_error = f"{type(e).__name__}: {e}"

    def picture():
        nonlocal render_error, renderer
        if renderer is None:
            return
        try:
            camera = mj.MjvCamera()
            camera.lookat[:] = data.subtree_com[torso]
            camera.distance, camera.azimuth, camera.elevation = 0.85, 130, -15
            renderer.update_scene(data, camera=camera)
            pictures.append(renderer.render().copy())
        except Exception as e:
            render_error = f"{type(e).__name__}: {e}"
            renderer.close()
            renderer = None

    def sample():
        nonlocal max_tilt, min_com, finite, peak_speed, last_contacts
        finite = bool(finite and np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all()
                      and not any(data.warning[w].number for w in
                                  (mj.mjtWarning.mjWARN_BADQACC, mj.mjtWarning.mjWARN_BADQPOS,
                                   mj.mjtWarning.mjWARN_BADQVEL)))
        tilt = math.degrees(math.acos(float(np.clip(data.xmat[torso, 8], -1, 1))))
        max_tilt = max(max_tilt, tilt)
        min_com = min(min_com, float(data.subtree_com[torso, 2]))
        peak_speed = max(peak_speed, float(np.linalg.norm(data.qvel[:3])))
        last_contacts = []
        for contact in data.contact:
            if floor in (contact.geom1, contact.geom2):
                g = contact.geom2 if contact.geom1 == floor else contact.geom1
                name = mj.mj_id2name(model, mj.mjtObj.mjOBJ_GEOM, g) or str(g)
                last_contacts.append(name)
                if g not in feet:
                    nonfoot.add(name)
        if len(trace) == 0 or data.time - trace[-1]["t_s"] >= 0.1:
            trace.append({"t_s": round(float(data.time), 4), "tilt_deg": round(tilt, 2),
                          "com_m": data.subtree_com[torso].round(5).tolist()})
        return tilt

    sample()
    picture()
    frames = request["plan"]["raw_frames"]
    for n, frame in enumerate(frames):
        current = data.qpos[qadr].copy()
        chosen = []
        for sid_string, pulse in frame["pos"].items():
            sid = int(sid_string)
            requested = angle(sid, pulse)
            actual = float(np.clip(requested, *ranges[sid - 1]))
            if abs(actual - requested) > 1e-9:
                clips.append({"frame": n, "servo": sid, "requested_rad": requested, "actual_rad": actual})
            targets[sid - 1] = actual
            chosen.append(sid - 1)
        steps = math.ceil(frame["ms"] / 1000 / dt)
        for step in range(steps):
            control = targets.copy()
            control[chosen] = current[chosen] + (targets[chosen] - current[chosen]) * min(1, (step + 1) * dt / (frame["ms"] / 1000))
            data.ctrl[acts] = control
            mj.mj_step(model, data)
            sample()
            if not finite or data.warning[mj.mjtWarning.mjWARN_BADQACC].number:
                finite = False
                break
        if n == len(frames) // 2:
            picture()
        if not finite:
            break
    for _ in range(math.ceil(request["settle_s"] / dt)):
        if not finite:
            break
        data.ctrl[acts] = targets
        mj.mj_step(model, data)
        sample()
    mj.mj_forward(model, data)
    end_tilt = sample()
    picture()
    image_path = None
    if pictures:
        image_path = str(directory / "simulation.png")
        png(image_path, np.concatenate(pictures, axis=1))
    if renderer is not None:
        renderer.close()
    end_rot = data.xmat[torso].reshape(3, 3)
    yaw = math.degrees(math.atan2(end_rot[1, 0], end_rot[0, 0]) - math.atan2(initial_rot[1, 0], initial_rot[0, 0]))
    yaw = (yaw + 180) % 360 - 180
    speed, spin = float(np.linalg.norm(data.qvel[:3])), math.degrees(float(np.linalg.norm(data.qvel[3:6])))
    foot_end = any(name.endswith("ankle_roll_sole") for name in last_contacts)
    objective = request["objective"]
    success = (finite and end_tilt < 30 and speed < 0.08 and spin < 20 and foot_end
               and (objective == "recover" or max_tilt < 45))
    return {"simulation_success": bool(success), "success_definition":
            "finite physics, final torso tilt <30 deg, base speed <0.08 m/s, spin <20 deg/s, foot contact; "
            "stability also requires peak tilt <45 deg; clipping is reported separately",
            "objective": objective, "finite": finite, "max_tilt_deg": round(max_tilt, 2),
            "end_tilt_deg": round(end_tilt, 2), "fell_threshold_60_deg": max_tilt >= 60,
            "displacement_xy_m": (data.subtree_com[torso, :2] - initial_xy).round(5).tolist(),
            "yaw_deg": round(yaw, 2), "min_com_height_m": round(min_com, 5),
            "end_speed_m_s": round(speed, 5), "end_spin_deg_s": round(spin, 2),
            "peak_speed_m_s": round(peak_speed, 5), "end_ground_contacts": sorted(set(last_contacts)),
            "nonfoot_ground_contacts_seen": sorted(nonfoot), "model_joint_clips": clips,
            "initial_raw": initial_raw, "start_keyframe": start, "physics_seconds": round(float(data.time), 4),
            "requested_motion_seconds": sum(f["ms"] for f in frames) / 1000,
            "timestep_s": dt, "mujoco_version": mj.__version__, "trace": trace,
            "image": image_path, "render_error": render_error,
            "hardware_validated": False, "model_only": True}


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    try:
        result = run(json.loads((directory / "request.json").read_text()), directory)
        (directory / "physics.json").write_text(json.dumps(result, allow_nan=False))
    except Exception as e:
        (directory / "physics.json").write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        raise
