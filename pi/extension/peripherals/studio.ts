// Studio peripheral: the brain writes its own movement (tonyd's studio, see src/tonypi/tonyd/studio.py). Read
// joints, play ad-hoc keyframes, hold a pose, go limp to be posed by hand, read/save/delete motions and skills
// (a saved skill can override walk_forward, turn_left, ...), and set calibration (trim, soft limits).
// tonyd still guards everything played: stop, temperature, tilt > 70 deg, limits; it settles and unloads when idle.
// The gait-tuning skill (pi/skills/gait-tuning) teaches the workflow.
//
// Servo map (positions 0..1000, 500 = centre, ~0.24 deg/count), L/R: 1/9 ankle roll, 2/10 ankle pitch, 3/11 knee,
// 4/12 hip pitch, 5/13 hip roll, 6/14 elbow, 7/15 shoulder roll, 8/16 shoulder pitch.

import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as tonyd from "../tonyd.ts";
import { describe, GUARD_TILT_DEG, HOLD_S, job, text } from "./body.ts";

const SERVOS = "L/R: 1/9 ankle roll, 2/10 ankle pitch, 3/11 knee, 4/12 hip pitch, 5/13 hip roll, 6/14 elbow, "
  + "7/15 shoulder roll, 8/16 shoulder pitch; positions 0..1000, 500 = centre, ~0.24 deg per count";
const Frame = Type.Object({
  ms: Type.Integer({ minimum: 20, maximum: 10000, description: "time to reach this keyframe" }),
  pos: Type.Record(Type.String(), Type.Integer({ minimum: 0, maximum: 1000 }), { description: "servo id -> position" }),
});
const json = (o: unknown) => JSON.stringify(o);
// bake-off rule: the user layer (~/.local/share/tonypi) is shared by every harness, so this one saves only names
// with its suffix (turn_left_pi, turn_left_pi_v2) and never overrides a default (turn_left stays the factory turn until a winner is promoted).
// Set TONYPI_STUDIO_SUFFIX="" to lift it.
export const SUFFIX = process.env.TONYPI_STUDIO_SUFFIX ?? "_pi";
/** One of ours (or no suffix rule). Other brains' user motions/skills (e.g. _own, _hermes) are hidden and refused:
 *  in the bake-off each harness authors its own moves. Defaults and built-ins are for everyone. */
export const mine = (name: string) => !SUFFIX || name.endsWith(SUFFIX) || name.includes(SUFFIX + "_");
function own(name: string) {
  if (!mine(name))
    throw new Error(`save it as "${name}${SUFFIX}": names saved here must contain "${SUFFIX}" (e.g. turn_left${SUFFIX}_v2; the library is shared, defaults stay as they are)`);
}

export default function studio(pi: ExtensionAPI) {
  const op = (name: string, label: string, description: string, parameters: any, run: (p: any) => Promise<any>,
              format: (r: any) => string = json) =>
    pi.registerTool(defineTool({
      name, label, description, parameters,
      async execute(_id, params) {
        const r = await run(params);
        return text(format(r), r);
      },
    }));

  op("get_joints", "Joints", `Current servo positions in motion space (what motions use; raw minus trim). ${SERVOS}.`,
    Type.Object({}), () => tonyd.joints(), (r) => json(r.pos));

  pi.registerTool(defineTool({
    name: "play_frames",
    label: "Play frames",
    description: "Play your own keyframes without saving them, e.g. [{\"ms\": 400, \"pos\": {\"4\": 620, \"12\": 380}}]. "
      + "Servos left out keep their position. It stops if Tony tilts past guard_tilt_deg. Afterwards Tony settles into "
      + "a stand by himself, unless you hold the last pose longer with hold_s (then he goes limp in it). Returns state "
      + `and max tilt. ${SERVOS}.`,
    parameters: Type.Object({ frames: Type.Array(Frame, { minItems: 1, maxItems: 400 }),
      hold_s: Type.Optional(Type.Number({ minimum: 0, maximum: 120, description: `default ${HOLD_S}; longer = hold the pose (no settle)` })),
      guard_tilt_deg: Type.Optional(Type.Number({ minimum: 5, maximum: 120, description: `default ${GUARD_TILT_DEG}` })) }),
    executionMode: "sequential",
    async execute(_id, { frames, hold_s = HOLD_S, guard_tilt_deg = GUARD_TILT_DEG }, signal, _u, ctx) {
      const t0 = Date.now();
      // settles into a stand ~1 s after, unless the model asked to hold the pose longer
      const r = await job(() => tonyd.play(frames, { hold_s, guard_tilt_deg }), ctx, signal, false, hold_s <= HOLD_S);
      return text(describe(`play ${frames.length} frames`, r.job, r.why, ((Date.now() - t0) / 1000).toFixed(1)), r.job);
    },
  }));

  pi.registerTool(defineTool({
    name: "set_pose",
    label: "Set pose",
    description: `Move some servos to positions over ms and hold (torque on) for hold_s (max 120). For trying a pose `
      + `or calibrating. ${SERVOS}.`,
    parameters: Type.Object({ positions: Type.Record(Type.String(), Type.Integer({ minimum: 0, maximum: 1000 })),
      ms: Type.Optional(Type.Integer({ minimum: 20, maximum: 10000 })), hold_s: Type.Optional(Type.Number({ minimum: 0, maximum: 120 })),
      guard_tilt_deg: Type.Optional(Type.Number({ minimum: 5, maximum: 120 })) }),
    executionMode: "sequential",
    async execute(_id, { positions, ms = 500, hold_s = 10, guard_tilt_deg = GUARD_TILT_DEG }, signal, _u, ctx) {
      const r = await job(() => tonyd.pose(positions, ms, { hold_s, guard_tilt_deg }), ctx, signal, false, false);
      return text(describe("pose", r.job, r.why, ((r.job.seconds ?? 0)).toFixed(1)), r.job);
    },
  }));

  op("set_torque", "Torque", "Torque off (limp: someone poses Tony by hand, then get_joints records it) or on for "
    + "the given servos (default all 16); on is held for hold_s, then the idle rule unloads again. Going limp while "
    + "standing makes Tony collapse: only with someone holding him.",
    Type.Object({ on: Type.Boolean(), ids: Type.Optional(Type.Array(Type.Integer({ minimum: 1, maximum: 16 }))),
      hold_s: Type.Optional(Type.Number({ minimum: 0, maximum: 120 })) }),
    ({ on, ids, hold_s = 0 }) => tonyd.torque(ids, on, hold_s));

  op("list_motions", "Motions", "Every motion (keyframe file): factory defaults and saved user motions (a user "
    + "motion overrides the default of the same name). Name, source, frames, duration.",
    Type.Object({}), async () => {
      const r = await tonyd.call("motions");
      return { motions: r.motions.filter((m: any) => m.source !== "user" || mine(m.name)) };
    },
    (r) => r.motions.map((m: any) => `${m.name}${m.source === "user" ? " [user]" : ""} ${m.frames}f ${m.ms}ms`).join(", "));

  op("get_motion", "Get motion", "A motion's keyframes, to study or copy before writing your own version.",
    Type.Object({ name: Type.String() }), async ({ name }) => {
      const m = await tonyd.call("motion_get", { name });
      if (m.source === "user" && !mine(name)) throw new Error(`${name} belongs to another brain; start from a default motion or one of yours`);
      return m;
    });

  op("save_motion", "Save motion", "Save keyframes as a named motion (lowercase, digits, _). The name "
    + `must contain "${SUFFIX}" (e.g. turn_left${SUFFIX}_v2); defaults can't be overridden from here.`,
    Type.Object({ name: Type.String(), frames: Type.Array(Frame, { minItems: 1, maxItems: 400 }),
      description: Type.Optional(Type.String()) }),
    ({ name, frames, description = "" }) => (own(name), tonyd.call("motion_save", { name, frames, description })));

  op("delete_motion", "Delete motion", "Delete a user motion (restores the default of that name, if any).",
    Type.Object({ name: Type.String() }), ({ name }) => (own(name), tonyd.call("motion_delete", { name })));

  op("save_skill", "Save skill", "Define a skill as a motion sequence; \"*name\" repeats with count. E.g. a new "
    + `turn: save_skill("turn_left${SUFFIX}_v2", ["*turn_left${SUFFIX}_v2"]). Names must contain "${SUFFIX}". `
    + "run_skill gets up first if Tony is lying and settles after the task.",
    Type.Object({ name: Type.String(), motions: Type.Array(Type.String(), { minItems: 1 }),
      description: Type.Optional(Type.String()) }),
    ({ name, motions, description = "" }) => (own(name), tonyd.call("skill_save", { name, motions, description })));

  op("delete_skill", "Delete skill", "Delete a user skill (restores the default skill of that name, if any).",
    Type.Object({ name: Type.String() }), ({ name }) => (own(name), tonyd.call("skill_delete", { name })));

  op("calibration", "Calibration", "Read (no arguments) or change calibration: trim = counts added to every target "
    + "of a servo (-200..200, 0 removes), limits = soft [lo, hi] every target is clamped to (null removes). "
    + "Applies to every motion immediately.",
    Type.Object({ trim: Type.Optional(Type.Record(Type.String(), Type.Integer({ minimum: -200, maximum: 200 }))),
      limits: Type.Optional(Type.Record(Type.String(), Type.Union([Type.Array(Type.Integer(), { minItems: 2, maxItems: 2 }), Type.Null()]))) }),
    async ({ trim, limits }) => (await tonyd.call("calibration", { trim, limits })).calibration);

  op("servo_calibration", "Servo calibration", "What the servos themselves store (EEPROM): offset (-125..125) and "
    + "angle limits per servo. Read-only. Most fixes belong in `calibration` (trim, soft limits), not here.",
    Type.Object({ ids: Type.Optional(Type.Array(Type.Integer({ minimum: 1, maximum: 16 }))) }),
    ({ ids }) => tonyd.call("servo_calibration", { ids }), (r) => json(r.servos));

  op("servo_calibrate", "Servo calibrate", "Change a servo's own EEPROM offset or angle limits. Only to fix a bad "
    + "calibration you have evidence for (e.g. a joint off-centre in the stand pose); never to tune a gait. reason "
    + "is required and logged; save=true makes the offset permanent. Tell the person before you do it.",
    Type.Object({ id: Type.Integer({ minimum: 1, maximum: 16 }), reason: Type.String({ minLength: 10 }),
      offset: Type.Optional(Type.Integer({ minimum: -125, maximum: 125 })),
      limits: Type.Optional(Type.Array(Type.Integer({ minimum: 0, maximum: 1000 }), { minItems: 2, maxItems: 2 })),
      save: Type.Optional(Type.Boolean()) }),
    ({ id, reason, offset, limits, save = false }) => tonyd.call("servo_calibrate", { id, reason, offset, limits, save }));
}
