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

export default function task(pi: ExtensionAPI) {
  let started = 0;
  let acted = false;
  let done = false;
  let nudges = 0;

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
