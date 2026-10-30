// Body peripheral: run_skill, list_skills, stop. Skills are tonyd's: the built-in ones, the default motion
// sequences, and any the brain saved itself (studio.ts).
//
// Balance is ours, not tonyd's (since main ef99364 tonyd only stops, guards temperature, and goes limp 1.5 s after
// the last job in whatever pose it ended). Without the model:
// - every job carries a tilt guard (GUARD_TILT_DEG, tighter if the model asks; not for getting up / lying down);
// - after a body move, if no other body job starts within SETTLE_AFTER_MS, Tony settles into a neutral stand before
//   tonyd's 1.5 s limp (the move holds its end pose HOLD_S meanwhile). So he never goes limp mid-step, also after a
//   "stop", and servos aren't kept loaded while the model thinks (a stand is safe limp);
// - a body move while lying gets up first.
// The front end's fall reflex (src/tonypi/pi_front) gets him up after an unexpected fall.
//
// Steering: pi delivers a steering message only after the running tool returns, so a motion tool must not keep
// the body busy while the user has said something new. Stepped skills (walk, turn, side step: any sequence with a
// "*step") run one step per tonyd job and stop between steps (a step always ends balanced); other jobs are
// cancelled at once. The tool then returns "stopped because the user sent a new message" and the model reads it.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import * as tonyd from "../tonyd.ts";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "../../..");

/** Fallback when tonyd is down at startup: the built-in list in skills.py. */
export function loadSkills(): Record<string, string> {
  const src = readFileSync(join(REPO, "src/tonypi/tonyd/skills.py"), "utf8");
  const block = src.slice(src.indexOf("SKILLS"), src.indexOf("}", src.indexOf("SKILLS")));
  return Object.fromEntries([...block.matchAll(/"(\w+)":\s*"([^"]*)"/g)].map((m) => [m[1], m[2]]));
}

const STEPPED = new Set(["walk_forward", "walk_backward", "turn_left", "turn_right", "step_left", "step_right",
  "step_in_place"]);
const MAX_STEPS = 10;
const POLL_MS = 100;
// head-only skills, and skills that end in a stable pose: no hold, no settle after them, no get-up before them
const NO_SETTLE = /^(look_|nod$|shake_head$|settle$|stand$|lie_down$|sleep$|get_up$)/;
const FLOOR = /^(get_up|stand|lie_down|sleep|settle)$/;  // these pass through lying: no tilt guard
const HEAD = /^(look_|nod$|shake_head$)/;
export const GUARD_TILT_DEG = Number(process.env.TONYPI_GUARD_TILT_DEG ?? 70);  // dances lean past 50 on purpose
export const HOLD_S = Number(process.env.TONYPI_HOLD_S ?? 2);
// benchmark limit (T9: 40 turn steps): skills with "turn" in the name stop once this many steps ran (unset = none)
const TURN_BUDGET = process.env.TONYPI_TURN_BUDGET ? Number(process.env.TONYPI_TURN_BUDGET) : Infinity;
let turnSteps = 0;
const SETTLE_AFTER_MS = Number(process.env.TONYPI_SETTLE_AFTER_MS ?? 1000);

// ---- settle when idle after a body move (reflex, no model) -------------------------------------------------------
let settleTimer: ReturnType<typeof setTimeout> | undefined;
let jobsRunning = 0;
/** A body job is about to start: no settle in between. */
export function bodyBusy() {
  clearTimeout(settleTimer);
  jobsRunning++;
}
/** A body job ended; moved = it left Tony in a pose that isn't a stand. */
export function bodyIdle(moved: boolean) {
  jobsRunning = Math.max(0, jobsRunning - 1);
  clearTimeout(settleTimer);
  if (!moved) return;
  settleTimer = setTimeout(async () => {
    if (jobsRunning) return;
    const st = await tonyd.state().catch(() => null);
    if (!st || st.lying || st.busy) return;  // lying: the fall reflex's business; busy: someone else moves him
    await tonyd.run("settle", 1).catch(() => {});
  }, SETTLE_AFTER_MS);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Why the body should stop now, if it should. */
function interruption(ctx: ExtensionContext, signal?: AbortSignal): string | null {
  if (signal?.aborted) return "aborted";
  if (ctx.hasPendingMessages()) return "the user sent a new message";
  return null;
}

/** One tonyd job; cancelled when the turn is aborted, or (unless finishStep) when a new message arrives. */
export async function job(start: () => Promise<tonyd.Job>, ctx: ExtensionContext, signal: AbortSignal | undefined,
                          finishStep = false, moves = true): Promise<{ job: tonyd.Job; why: string | null }> {
  bodyBusy();
  const p = start();
  p.finally(() => bodyIdle(moves)).catch(() => {});
  let settled = false;
  let why: string | null = null;
  p.finally(() => (settled = true)).catch(() => {});
  while (!settled) {
    await Promise.race([p.catch(() => {}), sleep(POLL_MS)]);
    const w = settled ? null : interruption(ctx, signal);
    if (w && !why) {
      why = w;
      if (w === "aborted" || !finishStep) await tonyd.stop().catch(() => {});  // else: let this step finish
    }
  }
  return { job: await p, why };
}

/** Short job summary for the model: state, time, how far Tony leaned, what the body said. */
export function describe(label: string, j: tonyd.Job, why: string | null, secs: string): string {
  let msg = `${label}: ${j.state} (${secs} s${j.max_tilt_deg != null ? `, max tilt ${j.max_tilt_deg.toFixed(1)} deg` : ""}`
    + `${j.yaw_deg != null ? `, turned ${j.yaw_deg.toFixed(1)} deg` : ""})`;
  if (why && j.state !== "done") msg += `; stopped because ${why}`;
  else if (j.state === "failed") msg += `; the body refused or aborted it${j.error ? ` (${j.error})` : ""}`;
  if (j.said.length) msg += `. Body said: ${j.said.join(" ")}`;
  return msg + ".";
}

export default async function body(pi: ExtensionAPI) {
  let skills: Record<string, string>;
  try {
    skills = await Promise.race([tonyd.skills(), sleep(1500).then(() => { throw new Error("timeout"); })]);
    if (!skills || typeof skills !== "object") throw new Error("no skills list");
  } catch {
    skills = loadSkills();
  }

  pi.registerTool(defineTool({
    name: "run_skill",
    label: "Run skill",
    description: "Move Tony's body with one skill; returns when it ends, with the max tilt (how far Tony leaned). "
      + "For walking, turning and side steps count = steps (1-10; a walk step is a few cm, a turn step roughly "
      + "20-30 degrees; open loop, it drifts). Skills you saved yourself work too (list_skills). Skills: "
      + Object.entries(skills).map(([n, d]) => `${n} (${d})`).join("; "),
    parameters: Type.Object({
      skill: Type.String({ description: "skill name, e.g. wave, walk_forward, or one you saved" }),
      count: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_STEPS, description: "steps or repeats" })),
      guard_tilt_deg: Type.Optional(Type.Number({ minimum: 5, maximum: 120,
        description: `stop if Tony tilts past this (default ${GUARD_TILT_DEG}; use ~25 when testing a new gait)` })),
    }),
    executionMode: "sequential",
    async execute(_id, { skill, count = 1, guard_tilt_deg }, signal, _onUpdate, ctx) {
      const pre = interruption(ctx, signal);
      if (pre) return text(`${skill} not started: ${pre}.`, { skill, done: 0, of: count, why: pre });
      const notes: string[] = [];
      if (!FLOOR.test(skill) && !HEAD.test(skill)) {
        const st = await tonyd.state().catch(() => null);
        if (st?.lying) {  // tonyd no longer gets up by itself before moving
          const up = await job(() => tonyd.run("get_up"), ctx, signal, false, false);
          notes.push(`I was lying down, so I got up first (${up.job.state})`);
          if (up.job.state !== "done") return text(`${notes[0]}; ${skill} not started.`, { skill, done: 0, of: count, state: "failed" });
        }
      }
      const o: tonyd.JobOpts = {
        guard_tilt_deg: FLOOR.test(skill) ? null : guard_tilt_deg ?? GUARD_TILT_DEG,
        hold_s: NO_SETTLE.test(skill) ? 0 : HOLD_S,
      };
      const info = ((await tonyd.skillDetails().catch(() => undefined)) ?? {})[skill];
      const stepped = STEPPED.has(skill) || !!info?.motions?.some((m: string) => m.startsWith("*"));
      const t0 = Date.now();
      const said: string[] = [];
      let done = 0;
      let why: string | null = null;
      let state = "done";
      let tilt: number | null = null;
      let yaw: number | null = null;
      const yaws: number[] = [];
      for (let i = 0; i < (stepped ? count : 1); i++) {
        if (skill.includes("turn") && turnSteps >= TURN_BUDGET) {
          why = `the benchmark's budget of ${TURN_BUDGET} turn steps is used up`;
          state = "failed";
          break;
        }
        if (skill.includes("turn")) turnSteps += stepped ? 1 : count;
        let r: Awaited<ReturnType<typeof job>>;
        try {
          r = await job(() => tonyd.run(skill, stepped ? 1 : Math.min(count, 5), o), ctx, signal, stepped,
                        !NO_SETTLE.test(skill));
        } catch (e) {  // refused mid-way (e.g. the charger tether's step budget): report what got done
          if (!done) throw e;
          why = `the body refused: ${(e as Error).message}`;
          state = "failed";
          break;
        }
        said.push(...r.job.said);
        state = r.job.state;
        if (r.job.max_tilt_deg != null) tilt = Math.max(tilt ?? 0, r.job.max_tilt_deg);
        if (r.job.yaw_deg != null) { yaw = (yaw ?? 0) + r.job.yaw_deg; yaws.push(r.job.yaw_deg); }
        if (r.job.state === "done") done += stepped ? 1 : count;
        why = r.why ?? (r.job.state === "done" ? interruption(ctx, signal) : null);
        if (r.job.state !== "done" || why) break;
      }
      const secs = ((Date.now() - t0) / 1000).toFixed(1);
      const tiltS = (tilt != null ? `, max tilt ${tilt.toFixed(1)} deg` : "")
        + (yaw != null ? `, turned ${yaw.toFixed(1)} deg (+ = left${yaws.length > 1 ? `; per step ${yaws.map((y) => y.toFixed(1)).join(", ")}` : ""})` : "");
      let msg = stepped ? `${skill}: ${done} of ${count} steps done (${secs} s${tiltS})` : `${skill}: ${state} (${secs} s${tiltS})`;
      if (why && (done < count || state !== "done")) msg += `; stopped because ${why}`;
      else if (state === "failed") msg += "; the body refused or aborted it";
      if (said.length) msg += `. Body said: ${said.join(" ")}`;
      if (notes.length) msg = `${notes.join("; ")}. ${msg}`;
      return text(msg + ".", { skill, done, of: count, state, why, said, max_tilt_deg: tilt, yaw_deg: yaw });
    },
  }));

  pi.registerTool(defineTool({
    name: "list_skills",
    label: "List skills",
    description: "Every skill the body can run now, including saved user skills and overrides: source "
      + "(builtin/default/user) and the motions a sequence skill plays.",
    parameters: Type.Object({}),
    async execute() {
      const d = await tonyd.skillDetails();
      const lines = Object.entries(d).map(([n, v]: [string, any]) =>
        `${n} [${v.source}]${v.motions ? ` = ${v.motions.join(" ")}` : ""}${v.description ? ` - ${v.description}` : ""}`);
      return text(lines.join("\n"), d);
    },
  }));

  pi.registerTool(defineTool({
    name: "stop",
    label: "Stop",
    description: "Stop all body motion immediately.",
    parameters: Type.Object({}),
    async execute() {
      await tonyd.stop();
      return text("Stopped.", undefined);
    },
  }));
}

export function text(s: string, details: any) {
  return { content: [{ type: "text" as const, text: s }], details };
}
