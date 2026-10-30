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

const FRONT_SOCK = process.env.TONYPI_PI_SOCK ?? "/run/tonypi/pi.sock";

/** Ask the person out loud through the front end (voice service `ask`, or speak + next typed/spoken words). */
function frontAsk(text: string, timeout_s: number): Promise<string | null> {
  return new Promise((resolve, reject) => {
    const s = createConnection(FRONT_SOCK);
    let buf = "";
    const timer = setTimeout(() => (s.destroy(), resolve(null)), (timeout_s + 10) * 1000);
    s.on("connect", () => s.write(JSON.stringify({ op: "ask", text, timeout_s }) + "\n"));
    s.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      for (const line of buf.split("\n").slice(0, -1)) {
        const msg = JSON.parse(line);
        if (msg.type === "answer") {
          clearTimeout(timer);
          s.destroy();
          resolve(msg.answer ?? null);
        }
      }
      buf = buf.slice(buf.lastIndexOf("\n") + 1);
    });
    s.on("error", (e) => (clearTimeout(timer), reject(new Error(`front end not reachable at ${FRONT_SOCK}: ${e.message}`))));
  });
}

export default function speech(pi: ExtensionAPI) {
  pi.registerTool(defineTool({
    name: "ask_user",
    label: "Ask the user",
    description: "Ask the person a question out loud and wait for the answer (e.g. \"I'm stuck at the rug edge, what "
      + "should I do?\" or \"What's going on?\"). Use it when stuck or unsure instead of looping or giving up. "
      + "Returns their answer, or that nobody answered.",
    parameters: Type.Object({ question: Type.String(), timeout_s: Type.Optional(Type.Number({ minimum: 5, maximum: 120 })) }),
    executionMode: "sequential",
    async execute(_id, { question, timeout_s = 30 }) {
      const answer = await frontAsk(question, timeout_s);
      return { content: [{ type: "text", text: answer ? `The user answered: ${answer}` : `No answer within ${timeout_s} s.` }],
        details: { question, answer } };
    },
  }));

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
