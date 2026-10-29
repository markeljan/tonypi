// Body peripheral: run_skill and stop. Skills are tonyd's (src/tonypi/tonyd/skills.py is the only list); the model
// never sends joint targets.
//
// Steering: pi delivers a steering message only after the running tool returns, so a motion tool must not keep
// the body busy while the user has said something new. Walking and turning run one step per tonyd job and stop
// between steps (a step always ends balanced, <= 1.75 s); other skills are cancelled at once. The tool then returns
// "interrupted" and the model reads the new message.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { StringEnum, Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import * as tonyd from "../tonyd.ts";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "../../..");

export function loadSkills(): Record<string, string> {
  const src = readFileSync(join(REPO, "src/tonypi/tonyd/skills.py"), "utf8");
  const block = src.slice(src.indexOf("SKILLS"), src.indexOf("}", src.indexOf("SKILLS")));
  return Object.fromEntries([...block.matchAll(/"(\w+)":\s*"([^"]*)"/g)].map((m) => [m[1], m[2]]));
}

const STEPPED = new Set(["walk_forward", "walk_backward", "turn_left", "turn_right", "step_left", "step_right",
  "step_in_place"]);
const MAX_STEPS = 10;
const POLL_MS = 100;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Why the body should stop now, if it should. */
function interruption(ctx: ExtensionContext, signal?: AbortSignal): string | null {
  if (signal?.aborted) return "aborted";
  if (ctx.hasPendingMessages()) return "the user sent a new message";
  return null;
}

/** One tonyd job; cancelled when the turn is aborted, or (unless finishStep) when a new message arrives. */
async function job(skill: string, count: number, ctx: ExtensionContext, signal: AbortSignal | undefined,
                   finishStep: boolean): Promise<{ job: tonyd.Job; why: string | null }> {
  const p = tonyd.run(skill, count);
  let settled = false;
  let why: string | null = null;
  p.finally(() => (settled = true)).catch(() => {});
  while (!settled) {
    await Promise.race([p.catch(() => {}), sleep(POLL_MS)]);
    const w = settled ? null : interruption(ctx, signal);
    if (w && !why && (w === "aborted" || !finishStep)) {
      why = w;
      await tonyd.stop().catch(() => {});
    } else if (w && !why) {
      why = w; // let this step finish, then stop
    }
  }
  return { job: await p, why };
}

export default function body(pi: ExtensionAPI) {
  const skills = loadSkills();
  const names = Object.keys(skills);

  pi.registerTool(defineTool({
    name: "run_skill",
    label: "Run skill",
    description: "Move Tony's body with one skill; returns when it ends. For walking, turning and side steps count = "
      + "steps (1-10; a walk step is a few cm, a turn step roughly 20-30 degrees; open loop, it drifts). Skills: "
      + names.map((n) => `${n} (${skills[n]})`).join("; "),
    parameters: Type.Object({
      skill: StringEnum(names as [string, ...string[]]),
      count: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_STEPS, description: "steps or repeats" })),
    }),
    executionMode: "sequential",
    async execute(_id, { skill, count = 1 }, signal, _onUpdate, ctx) {
      const pre = interruption(ctx, signal);
      if (pre) return text(`${skill} not started: ${pre}.`, { skill, done: 0, of: count, why: pre });
      const t0 = Date.now();
      const said: string[] = [];
      let done = 0;
      let why: string | null = null;
      let state = "done";
      const steps = STEPPED.has(skill) ? count : 1;
      for (let i = 0; i < steps; i++) {
        const r = await job(skill, STEPPED.has(skill) ? 1 : Math.min(count, 5), ctx, signal, STEPPED.has(skill));
        said.push(...r.job.said);
        state = r.job.state;
        if (r.job.state === "done") done += STEPPED.has(skill) ? 1 : count;
        why = r.why ?? (r.job.state === "done" ? interruption(ctx, signal) : null);
        if (r.job.state !== "done" || why) break;
      }
      const secs = ((Date.now() - t0) / 1000).toFixed(1);
      let msg = STEPPED.has(skill) ? `${skill}: ${done} of ${count} steps done (${secs} s)` : `${skill}: ${state} (${secs} s)`;
      if (why && (done < count || state !== "done")) msg += `; stopped because ${why}`;
      else if (state === "failed") msg += "; the body refused or aborted it";
      if (said.length) msg += `. Body said: ${said.join(" ")}`;
      return text(msg + ".", { skill, done, of: count, state, why, said });
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
