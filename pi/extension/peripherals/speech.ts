// Speech peripheral: say. Under the front end (TONYPI_FRONTEND=1, src/tonypi/pi_front) the front end owns the
// speaker and speaks every `say` call as it starts (it sees the tool events on pi's RPC stream), so the tool only
// records the text. Without it (pi in a terminal), the text goes to tonypi-agent's socket like tonypi-mcp's say.

import { createConnection } from "node:net";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const AGENT_SOCK = process.env.TONYPI_SOCK ?? "/run/tonypi/agent.sock";

function agentSay(text: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const s = createConnection(AGENT_SOCK);
    const timer = setTimeout(() => (s.destroy(), reject(new Error("speech socket timeout"))), 5000);
    s.on("connect", () => s.write(JSON.stringify({ op: "say", text }) + "\n"));
    s.on("data", () => (clearTimeout(timer), s.destroy(), resolve()));
    s.on("error", (e) => (clearTimeout(timer), reject(new Error(`speech not reachable at ${AGENT_SOCK}: ${e.message}`))));
  });
}

export default function speech(pi: ExtensionAPI) {
  pi.registerTool(defineTool({
    name: "say",
    label: "Say",
    description: "Say something out loud now, while you keep working (e.g. 'On my way.'). Your final reply is "
      + "spoken anyway, so don't repeat it with say.",
    parameters: Type.Object({ text: Type.String() }),
    async execute(_id, { text }) {
      if (!process.env.TONYPI_FRONTEND) await agentSay(text);
      return { content: [{ type: "text", text: "said" }], details: { text } };
    },
  }));
}
