// Self-update: Tony changes his own prompt, lessons, skills, brain config, extension and front-end code ("learn X",
// "improve how you do Y", or by himself after a failure), checks the change, commits it on this branch, and reloads.
//
// - pi's built-in read/edit/write tools are on, but edit/write only reach EDITABLE (everything else is blocked here).
// - self_commit runs a load check (the extension must load in a fresh pi; the front end must compile), commits only
//   the editable paths, and returns reload: the front end then restarts pi on the same session (new extension code
//   and skills take effect, the conversation continues); a front-end change restarts the front end too.
// - SYSTEM.md and LESSONS.md need no reload: task.ts re-reads them on every command.

import { execFile } from "node:child_process";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const run = promisify(execFile);
const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EDITABLE = ["pi/", "src/tonypi/pi_front/"];
const PY = process.env.TONYPI_PYTHON ?? "/home/pi/tonypi/.venv/bin/python";

export const editable = (path: string) => {
  const rel = relative(REPO, resolve(REPO, path));
  return !rel.startsWith("..") && EDITABLE.some((p) => rel.startsWith(p)) && !rel.includes("node_modules");
};

async function changed(): Promise<string[]> {
  const { stdout } = await run("git", ["status", "--porcelain", "--", ...EDITABLE], { cwd: REPO });
  return stdout.split("\n").filter(Boolean).map((l) => l.slice(3));
}

/** The extension must still load in a fresh pi, and the front end must still compile. */
async function check(files: string[]): Promise<string | null> {
  try {
    // a broken extension doesn't fail pi (exit 0, nothing on stderr): it just loads without it. tony/brain is
    // registered last (index.ts), so seeing it means every module loaded.
    const { stdout, stderr } = await run("pi", ["--offline", "--list-models", "tony", "-nbt", "-nc", "-ns", "-ne", "--no-mcp",
      "-e", join(REPO, "pi/extension")], { cwd: REPO, timeout: 60_000, env: { ...process.env, TONYPI_FRONTEND: "1" } });
    if (!/^tony\s+brain\b/m.test(stdout) || /error/i.test(stderr)) {
      return `the extension no longer loads (tony/brain missing). ${stderr.slice(0, 1200)}`.trim();
    }
  } catch (e: any) {
    return `extension load failed: ${(e.stderr || e.message || "").slice(0, 1500)}`;
  }
  const py = files.filter((f) => f.endsWith(".py"));
  if (py.length) {
    try {
      await run(PY, ["-m", "py_compile", ...py], { cwd: REPO, timeout: 30_000 });
    } catch (e: any) {
      return `python: ${(e.stderr || e.message || "").slice(0, 1500)}`;
    }
  }
  return null;
}

export default function self(pi: ExtensionAPI) {
  pi.on("tool_call", (event: any) => {
    const path = String(event.input?.path ?? "");
    if ((event.toolName === "edit" || event.toolName === "write") && !editable(path)) {
      return { block: true, reason: `you may only change files under ${EDITABLE.join(", ")} of ${REPO}` };
    }
    // read: the repo only (prompt, lessons, skills, own code, motion data). Voice requests can be room chatter
    // (2026-10-30: a delegated "remove fish as the default shell" made it read ~/.zshrc).
    if (event.toolName === "read" && relative(REPO, resolve(REPO, path)).startsWith("..")) {
      return { block: true, reason: `you may only read files inside ${REPO}` };
    }
  });

  pi.registerTool(defineTool({
    name: "self_commit",
    label: "Commit self-update",
    description: "After changing your own files (pi/SYSTEM.md, pi/LESSONS.md, pi/skills/*, pi/brain.json, "
      + "pi/extension/*, src/tonypi/pi_front/*) with edit/write: check that they still load, commit them on your "
      + "branch, and reload so the change takes effect. If the check fails, fix the files and call it again.",
    parameters: Type.Object({ message: Type.String({ description: "what you changed and why, one line" }) }),
    async execute(_id, { message }) {
      const files = await changed();
      if (!files.length) return { content: [{ type: "text", text: "Nothing changed under pi/ or the front end." }], details: {} };
      const problem = await check(files);
      if (problem) {
        return { content: [{ type: "text", text: `Not committed: the change breaks loading. Fix it first.\n${problem}` }],
          details: { files, problem }, isError: true };
      }
      await run("git", ["add", "--", ...EDITABLE], { cwd: REPO });
      await run("git", ["commit", "-q", "-m", `Tony self-update: ${message}`, "-m",
        "Committed by the robot's own brain (pi harness, self_commit)."], { cwd: REPO });
      const { stdout: sha } = await run("git", ["rev-parse", "--short", "HEAD"], { cwd: REPO });
      const code = files.some((f) => f.startsWith("pi/extension/") || f.startsWith("src/tonypi/pi_front/") || f === "pi/brain.json");
      return {
        content: [{ type: "text", text: `Committed ${sha.trim()}: ${files.join(", ")}.`
          + (code ? " Reloading after this reply (the conversation continues)." : " Prompt/lessons/skill changes apply from the next command.") }],
        details: { sha: sha.trim(), files, reload: code, frontend: files.some((f) => f.startsWith("src/tonypi/pi_front/")) },
      };
    },
  }));
}
