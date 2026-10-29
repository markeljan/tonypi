// The brain as one pi model, "tony/brain": the owner's ChatGPT subscription when logged in and reachable, else the
// onboard LFM3-Omni-2B (llama-server :8080, provider "local" in pi/models.json).
//
// - user turns go to the first cloud model in TONYPI_CLOUD that has credentials (pi /login), unless the cloud
//   failed in the last COOLDOWN_MS (any pi provider works here, e.g. anthropic/..., but the owner chose ChatGPT);
// - a failed cloud request is retried by pi; the retry is routed to LFM3 and the cloud is skipped for a while;
// - tool follow-ups stay on the model that answered the turn (keeps prompt caches valid).

import type { Api, Model } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const CLOUD = (process.env.TONYPI_CLOUD ?? "openai-codex/gpt-6.1-sol,openai/gpt-6.1-sol")
  .split(",").map((s) => s.trim()).filter(Boolean);
const LOCAL = process.env.TONYPI_LOCAL ?? "local/lfm3-omni-2b";
const COOLDOWN_MS = Number(process.env.TONYPI_CLOUD_COOLDOWN_S ?? 120) * 1000;
const CLOUD_THINKING = (process.env.TONYPI_CLOUD_THINKING ?? "low") as any;

interface State { cloudDownUntil?: number }

const split = (s: string) => [s.slice(0, s.indexOf("/")), s.slice(s.indexOf("/") + 1)] as const;

export default function router(pi: ExtensionAPI) {
  let registry: ExtensionContext["modelRegistry"] | undefined;
  pi.on("session_start", (_e, ctx) => { registry = ctx.modelRegistry; });

  const find = (spec: string, reg = registry) => reg?.find(...split(spec));
  const cloud = (reg = registry): Model<Api> | undefined => {
    for (const spec of CLOUD) {
      const m = find(spec, reg);
      if (m && reg!.hasConfiguredAuth(m)) return m;
    }
  };

  pi.registerVirtualModel<State>({
    provider: "tony",
    id: "brain",
    name: "Tony brain (cloud, else local LFM3)",
    thinkingLevels: ["low"],
    input: ["text", "image"],
    route(req, ctx?: { modelRegistry?: ExtensionContext["modelRegistry"] }) {
      const reg = ctx?.modelRegistry ?? registry;
      const local = find(LOCAL, reg);
      if (!local) throw new Error(`local model ${LOCAL} missing: link pi/models.json to ~/.pi/agent/models.json`);
      const state = req.state ?? {};
      const pick = (m: Model<Api>, st: State | undefined = req.state) =>
        ({ model: m, thinkingLevel: m.provider === local.provider ? "off" : CLOUD_THINKING, state: st }) as any;
      if (req.reason === "retry" && req.failed) {
        if (req.failed.model.provider !== local.provider) {
          console.error(`[tony] cloud failed (${req.failed.message.errorMessage}); falling back to ${LOCAL}`);
          return pick(local, { cloudDownUntil: Date.now() + COOLDOWN_MS });
        }
        return pick(local);
      }
      if (req.reason === "continuation" && req.previous) return pick(req.previous.model as Model<Api>);
      const c = cloud(reg);
      if (c && !(state.cloudDownUntil && Date.now() < state.cloudDownUntil)) return pick(c);
      return pick(local);
    },
  });
}
