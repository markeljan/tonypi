// A scripted "model" for tests/test_pi_steering.py: provider fake/brain. It reads the transcript like a model would:
// "walk forward 3 steps, then turn left" -> walk_forward x3, then turn_left x2; a newer user message saying
// "turn right" -> turn_right x2; afterwards a short text reply.

import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const text = (m: any) =>
  typeof m.content === "string" ? m.content : (m.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join(" ");

function decide(context: any) {
  const msgs = context.messages.filter((m: any) => m.role !== "system");
  const last = msgs[msgs.length - 1];
  const users = msgs.filter((m: any) => m.role === "user");
  const newest = text(users[users.length - 1] ?? { content: "" });
  const calls = msgs.filter((m: any) => m.role === "assistant").flatMap((m: any) => m.content.filter((c: any) => c.type === "toolCall"));
  const did = (skill: string) => calls.some((c: any) => c.arguments?.skill === skill);
  const run = (skill: string, count: number) => fauxAssistantMessage(fauxToolCall("run_skill", { skill, count }), { stopReason: "toolUse" });
  if (/try the new turn/i.test(newest)) {
    if (!calls.some((c: any) => c.name === "play_frames"))
      return fauxAssistantMessage(fauxToolCall("play_frames", { frames: [{ ms: 300, pos: { "5": 520, "13": 480 } }] }), { stopReason: "toolUse" });
    if (!did("my_turn")) return run("my_turn", 2);
    return fauxAssistantMessage(fauxText("Tried it."));
  }
  if (/turn right/i.test(newest)) return did("turn_right") ? fauxAssistantMessage(fauxText("Turned right.")) : run("turn_right", 2);
  if (/walk forward 3/i.test(newest)) {
    if (!did("walk_forward")) return run("walk_forward", 3);
    if (!did("turn_left")) return run("turn_left", 2);
  }
  return fauxAssistantMessage(fauxText(last?.role === "toolResult" ? "Done." : "OK."));
}

export default function (pi: ExtensionAPI) {
  const faux = fauxProvider({ provider: "fake", models: [{ id: "brain", input: ["text", "image"] }] });
  faux.setResponses(Array.from({ length: 200 }, () => decide));
  pi.registerProvider(faux.provider);
}
