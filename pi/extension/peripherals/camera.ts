// Camera + head peripheral: snapshot (one photo), look (point the head, then a photo), video (a short burst of
// frames). Images go back to the model as tool-result image content; pi forwards them
// (Responses API: input_image in the tool output; chat completions: a user message right after the tool result).

import { readFile } from "node:fs/promises";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as tonyd from "../tonyd.ts";

const HEAD_SETTLE_MS = 450; // servo move (400 ms) + a fresh frame at 15 fps
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function photo() {
  const s = await tonyd.snapshot();
  const data = (await readFile(s.path)).toString("base64");
  const caption = `photo ${s.width}x${s.height}, head pan ${s.head_deg.pan} deg (+ = left), tilt ${s.head_deg.tilt} deg`;
  return { caption, image: { type: "image" as const, data, mimeType: "image/jpeg" }, path: s.path };
}

export default function camera(pi: ExtensionAPI) {
  pi.registerTool(defineTool({
    name: "snapshot",
    label: "Snapshot",
    description: "Take a photo with Tony's head camera (what Tony sees right now).",
    parameters: Type.Object({}),
    async execute() {
      const p = await photo();
      return { content: [{ type: "text", text: p.caption }, p.image], details: { path: p.path } };
    },
  }));

  pi.registerTool(defineTool({
    name: "look",
    label: "Look",
    description: "Point the head (camera) and, by default, take a photo there. pan_deg: + left / - right (-60..60); "
      + "tilt_deg: + up / - down (-40..25); 0, 0 = straight ahead.",
    parameters: Type.Object({
      pan_deg: Type.Number({ minimum: -60, maximum: 60 }),
      tilt_deg: Type.Optional(Type.Number({ minimum: -40, maximum: 25 })),
      photo: Type.Optional(Type.Boolean({ description: "take a photo after moving (default true)" })),
    }),
    executionMode: "sequential",
    async execute(_id, { pan_deg, tilt_deg = 0, photo: take = true }) {
      const h = await tonyd.head(pan_deg, tilt_deg);
      if (!take) return { content: [{ type: "text", text: `head at pan ${h.pan}, tilt ${h.tilt}` }], details: h };
      await sleep(HEAD_SETTLE_MS);
      const p = await photo();
      return { content: [{ type: "text", text: p.caption }, p.image], details: { ...h, path: p.path } };
    },
  }));

  pi.registerTool(defineTool({
    name: "scan",
    label: "Scan",
    description: "Look around without moving the body: sweep the head from left to right and take a photo at each "
      + "angle (default 5 photos: pan 60, 30, 0, -30, -60; + = left), then look straight again. Each photo is "
      + "labelled with its pan angle, so you know which way to turn (body turn left = positive). The camera sees "
      + "about 60 degrees across.",
    parameters: Type.Object({
      tilt_deg: Type.Optional(Type.Number({ minimum: -40, maximum: 25, description: "head tilt, default 0; -20 to see the floor" })),
      photos: Type.Optional(Type.Integer({ minimum: 3, maximum: 7, description: "default 5" })),
    }),
    executionMode: "sequential",
    async execute(_id, { tilt_deg = 0, photos = 5 }, signal) {
      const content: any[] = [];
      for (let i = 0; i < photos && !signal?.aborted; i++) {
        const pan = Math.round(60 - (120 * i) / (photos - 1));
        await tonyd.head(pan, tilt_deg, 300);
        await sleep(i === 0 ? HEAD_SETTLE_MS + 200 : HEAD_SETTLE_MS);
        const p = await photo();
        content.push({ type: "text", text: `pan ${pan} deg (${pan > 0 ? "left" : pan < 0 ? "right" : "straight ahead"}), tilt ${tilt_deg}` }, p.image);
      }
      await tonyd.head(0, tilt_deg);
      return { content, details: { photos: content.length / 2, tilt_deg } };
    },
  }));

  pi.registerTool(defineTool({
    name: "video",
    label: "Video",
    description: "Short video: a burst of 2-6 photos over up to 5 s, oldest first. Use it to see motion or change.",
    parameters: Type.Object({
      frames: Type.Optional(Type.Integer({ minimum: 2, maximum: 6, description: "default 3" })),
      seconds: Type.Optional(Type.Number({ minimum: 0.3, maximum: 5, description: "total span, default 2" })),
    }),
    async execute(_id, { frames = 3, seconds = 2 }, signal) {
      const content: any[] = [];
      const t0 = Date.now();
      for (let i = 0; i < frames && !signal?.aborted; i++) {
        if (i) await sleep(Math.max(0, t0 + (i * seconds * 1000) / (frames - 1) - Date.now()));
        const p = await photo();
        content.push({ type: "text", text: `frame ${i + 1} at ${((Date.now() - t0) / 1000).toFixed(1)} s` }, p.image);
      }
      return { content, details: { frames: content.length / 2 } };
    },
  }));
}
