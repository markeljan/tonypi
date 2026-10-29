// T7 without pulling the network cable: a "cloud" provider with credentials whose endpoint refuses connections.
// Use with TONYPI_CLOUD=deadcloud/gpt and TONYPI_PI_EXTRA_EXT=tests/pi_dead_cloud.ts.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerProvider("deadcloud", {
    name: "Unreachable cloud (test)",
    baseUrl: "http://127.0.0.1:9/v1",
    api: "openai-completions",
    apiKey: "test",
    models: [{ id: "gpt", name: "dead", input: ["text", "image"], reasoning: false, contextWindow: 128000,
               maxTokens: 1000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
  } as any);
}
