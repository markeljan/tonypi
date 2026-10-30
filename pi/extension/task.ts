// Task loop: Tony keeps going until a task is done.
//
// - Every run re-reads pi/SYSTEM.md (the prompt) and pi/LESSONS.md (what earlier tasks taught him) into the system
//   prompt, so edits to either apply on the next command without a restart.
// - task_done(success, summary) ends a task. When the model stops after acting on the world (moving, looking)
//   without calling it, pi's agent_before_settle sends it back in: check, try another way, or give up explicitly.
//   Bounded: MAX_NUDGES per task and the 10-minute task cap (E2E rules).
// - A fall, refused walk or failed move is a reason to try another way; the front end's fall reflex gets Tony up
//   and re-prompts "continue the task".

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PI_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const MAX_NUDGES = Number(process.env.TONYPI_MAX_NUDGES ?? 8);
const TASK_CAP_MS = Number(process.env.TONYPI_TASK_CAP_S ?? 600) * 1000;
// tools that move the body: after any of these a task has to be closed with task_done (a question answered from a
// photo needs no task_done: no nudge, no second spoken reply)
const ACTING = new Set(["run_skill", "play_frames", "set_pose", "save_skill"]);

const readOr = (path: string, fallback = "") => { try { return readFileSync(path, "utf8").trim(); } catch { return fallback; } };

// Stuck check (owner's lesson, 2026-10-29: a harness kept failing to step up onto the rug). The third identical
// body action in a task, or a second failure in a row, gets this appended to its result.
const STUCK = "[stuck check] Stop repeating this. 1) Gather data: look down at your feet (look tilt_deg -35), take a "
  + "video while trying once more, compare turned/max tilt, get_joints. 2) Say out loud what you think is wrong. "
  + "3) Try something genuinely different: another angle, a side-step or back-up, or author a new motion (e.g. a "
  + "higher step to clear an edge; see the motion-authoring skill). 4) If you don't know, ask the user out loud with "
  + "ask_user and use the answer. When something works, save it as a _pi skill and add a line to pi/LESSONS.md.";
const BODY = new Set(["run_skill", "play_frames", "set_pose"]);
const FAILED = /\b(failed|refused|stopped because|not started|tilted past|0 of \d+ steps)\b/i;

export default function task(pi: ExtensionAPI) {
  let started = 0;
  let acted = false;
  let done = false;
  let nudges = 0;
  let tries = new Map<string, number>();
  let failsInRow = 0;

  pi.on("tool_result", (event: any) => {
    const name = event.toolName;
    if (!BODY.has(name)) return;
    const sig = `${name}:${JSON.stringify(event.input ?? {})}`;
    const n = (tries.get(sig) ?? 0) + 1;
    tries.set(sig, n);
    const text = (event.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join(" ");
    failsInRow = event.isError || FAILED.test(text) ? failsInRow + 1 : 0;
    if (n >= 3 || failsInRow >= 2) {
      return { content: [...(event.content ?? []), { type: "text", text: `${STUCK} (same action ${n}x, failures in a row ${failsInRow})` }] };
    }
  });

  pi.on("before_agent_start", (event: any) => {
    const o = event.systemPromptOptions;
    if (o) {
      const prompt = readOr(join(PI_DIR, "SYSTEM.md"));
      if (prompt) o.customPrompt = prompt;  // the preamble (pi reserves sections.preamble)
      const lessons = readOr(join(PI_DIR, "LESSONS.md"));
      if (lessons && o.sections) o.sections.lessons = `# Lessons from earlier tasks (pi/LESSONS.md; add to it when you learn something)\n${lessons}`;
    }
    if (event.messageType === "user") {  // a new command (steers join the running task)
      const text = String(event.prompt ?? "");
      if (!text.startsWith("[body]")) {  // "[body] you fell, continue the task" keeps the task's budget
        started = Date.now();
        nudges = 0;
        tries = new Map();
        failsInRow = 0;
      }
      acted = false;
      done = false;
    }
  });

  pi.on("tool_execution_start", (event: any) => {
    if (ACTING.has(event.toolName)) acted = true;
  });

  pi.registerTool(defineTool({
    name: "task_done",
    label: "Task done",
    description: "Close the current task once you have checked the result (photo, gyro, state): success=true when "
      + "it is achieved; success=false only when you have truly run out of ways to try (summary then says what you "
      + "tried and what you need). summary: one or two sentences on how you did it.",
    parameters: Type.Object({ success: Type.Boolean(), summary: Type.String() }),
    async execute(_id, { success, summary }) {
      done = true;
      return { content: [{ type: "text", text: `Task closed (${success ? "success" : "gave up"}). Now tell the user briefly.` }],
        details: { success, summary, nudges, seconds: Math.round((Date.now() - started) / 1000) } };
    },
  }));

  pi.on("agent_before_settle", (event: any) => {
    if (!acted || done || event.outcome !== "completed") return;
    const elapsed = Date.now() - started;
    if (nudges >= MAX_NUDGES || elapsed > TASK_CAP_MS) return;
    nudges++;
    return {
      continue: true,
      entries: [{
        type: "custom_message", customType: "tony-persist", display: false,
        content: `[task loop, nudge ${nudges}/${MAX_NUDGES}, ${Math.round(elapsed / 1000)} s of ${TASK_CAP_MS / 1000} s] `
          + "You stopped without task_done. Check whether the task is really achieved (take a photo, read the "
          + "gyro/state). If not, try a different way than before (turn and scan, move closer, re-plan, write or fix a "
          + "motion). If it is achieved, call task_done(true, ...). If you have truly run out of options, call "
          + "task_done(false, ...) saying what you tried and what you need.",
      }],
    };
  });
}
