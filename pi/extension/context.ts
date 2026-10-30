// Only the newest photos go to the model. Older images in the transcript become a short text note, so a long
// session (or a camera loop) doesn't fill LFM3's 8K context or the cloud bill with stale frames. The session file
// keeps every image.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const KEEP = Number(process.env.TONYPI_KEEP_IMAGES ?? 7);  // a full scan

export default function context(pi: ExtensionAPI) {
  pi.on("context", (event) => {
    let kept = 0;
    let changed = false;
    const messages = [...event.messages];
    for (let i = messages.length - 1; i >= 0; i--) {
      const m: any = messages[i];
      if (!Array.isArray(m.content) || !m.content.some((c: any) => c.type === "image")) continue;
      const content = m.content.map((c: any) => {
        if (c.type !== "image") return c;
        if (kept < KEEP) return (kept++, c);
        changed = true;
        return { type: "text", text: "[older photo omitted]" };
      });
      messages[i] = { ...m, content };
    }
    return changed ? { messages } : undefined;
  });
}
