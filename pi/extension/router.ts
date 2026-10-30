// The brain as one pi model, "tony/brain", routed per request (config: pi/brain.json):
//
//   simple    "wave", "what do you see?"            gpt-6.1-terra, no reasoning   (fast)
//   multistep "look left and tell me if ...", "then" gpt-6.1-terra, more reasoning
//   advanced  "find the door", failures, escalate   gpt-6.1-sol                   (open-ended planning, hard perception)
//
// - each user message (prompt or steer) picks a tier from its words; the model can call `escalate` to move the
//   rest of the task to the advanced tier, and a failed/refused tool result does the same;
// - all tiers use the owner's ChatGPT subscription (provider openai-codex). If a cloud request fails, pi retries
//   and the retry goes to the onboard LFM3 (pi/models.json "local"); the cloud is skipped for cloud_cooldown_s.
// - env overrides for benchmarks: TONYPI_BRAIN_MODEL / TONYPI_BRAIN_THINKING force one model / level for every
//   tier, TONYPI_BRAIN_TIER forces one tier, TONYPI_BRAIN_CONFIG points at another config file.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type, type Api, type Model } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";

type Tier = "simple" | "multistep" | "advanced";
interface TierConfig { model: string; thinking: string }
interface Config {
  tiers: Record<Tier, TierConfig>;
  multistep_words: string;
  advanced_words: string;
  local: string;
  cloud_cooldown_s: number;
}
interface State { cloudDownUntil?: number; tier?: Tier }

const CONFIG_PATH = process.env.TONYPI_BRAIN_CONFIG ?? join(dirname(fileURLToPath(import.meta.url)), "../brain.json");

function loadConfig(): Config {
  const c: Config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  for (const t of Object.values(c.tiers)) {
    if (process.env.TONYPI_BRAIN_MODEL) t.model = process.env.TONYPI_BRAIN_MODEL;
    if (process.env.TONYPI_BRAIN_THINKING) t.thinking = process.env.TONYPI_BRAIN_THINKING;
  }
  return c;
}

const split = (s: string) => [s.slice(0, s.indexOf("/")), s.slice(s.indexOf("/") + 1)] as const;
const textOf = (m: any) => typeof m?.content === "string" ? m.content
  : (m?.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join(" ");

export default function router(pi: ExtensionAPI) {
  const cfg = loadConfig();
  const multistep = new RegExp(cfg.multistep_words, "i");
  const advanced = new RegExp(cfg.advanced_words, "i");
  const forced = process.env.TONYPI_BRAIN_TIER as Tier | undefined;
  let registry: ExtensionContext["modelRegistry"] | undefined;
  let escalateNext = false;
  pi.on("session_start", (_e, ctx) => { registry = ctx.modelRegistry; });

  const classify = (text: string): Tier =>
    forced ?? (advanced.test(text) ? "advanced" : multistep.test(text) ? "multistep" : "simple");

  pi.registerTool(defineTool({
    name: "escalate",
    label: "Escalate",
    description: "Switch the rest of this task to the stronger, slower model: use it for open-ended planning, hard "
      + "perception questions, or when something keeps failing.",
    parameters: Type.Object({ reason: Type.String() }),
    async execute(_id, { reason }) {
      escalateNext = true;
      return { content: [{ type: "text", text: "Escalated: the next steps use the advanced model." }], details: { reason } };
    },
  }));

  pi.registerVirtualModel<State>({
    provider: "tony",
    id: "brain",
    name: "Tony brain (ChatGPT terra/sol by task, else local LFM3)",
    thinkingLevels: ["low"],
    input: ["text", "image"],
    route(req, ctx?: { modelRegistry?: ExtensionContext["modelRegistry"] }) {
      const reg = ctx?.modelRegistry ?? registry;
      const find = (spec: string) => reg?.find(...split(spec));
      const local = find(cfg.local);
      const state: State = req.state ?? {};
      const route = (model: Model<Api>, thinking: string, st: State) => ({ model, thinkingLevel: thinking as any, state: st });
      const toLocal = (st: State) => {
        if (!local) throw new Error(`no cloud model available and ${cfg.local} missing (link pi/models.json)`);
        return route(local, "off", st);
      };

      if (req.reason === "retry" && req.failed && req.failed.model.provider !== local?.provider) {
        console.error(`[tony] cloud failed (${req.failed.message.errorMessage}); falling back to ${cfg.local}`);
        return toLocal({ ...state, cloudDownUntil: Date.now() + cfg.cloud_cooldown_s * 1000 });
      }
      let tier: Tier = state.tier ?? "simple";
      if (req.reason === "user") {
        const users = req.messages.filter((m: any) => m.role === "user");
        tier = classify(textOf(users[users.length - 1]));
      } else if (escalateNext) {
        tier = "advanced";
      } else if (req.reason === "continuation") {
        const last: any = req.messages[req.messages.length - 1];
        if (last?.role === "toolResult" && (last.isError || /\b(failed|refused)\b/.test(textOf(last)))) tier = "advanced";
      }
      escalateNext = false;
      const st = tier === state.tier ? req.state : { ...state, tier };
      if (state.cloudDownUntil && Date.now() < state.cloudDownUntil) return toLocal(st ?? state);
      const t = cfg.tiers[tier];
      const m = find(t.model);
      if (m && reg!.hasConfiguredAuth(m)) return route(m, t.thinking, st as State);
      return toLocal(st ?? state);
    },
  });
}
