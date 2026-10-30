// Camera + head peripheral: snapshot (one photo), look (point the head, then a photo), scan (head sweep, tonyd's
// scan op: one settled photo per angle + gyro heading), video (tonyd's burst op). Images go back to the model as tool-result image content; pi forwards them
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
    description: "Look around without moving the body: one photo per head angle (default pan 60, 30, 0, -30, -60; "
      + "+ = left), each after the head settled, then the head goes back. Photos are labelled with their pan angle and "
      + "you get the gyro heading, so you know which way to turn (body turn left = positive). The camera sees about "
      + "60 degrees across.",
    parameters: Type.Object({
      pans: Type.Optional(Type.Array(Type.Number({ minimum: -60, maximum: 60 }), { minItems: 1, maxItems: 7,
        description: "head angles, default [60, 30, 0, -30, -60]; [60, 0, -60] is quicker" })),
      tilt_deg: Type.Optional(Type.Number({ minimum: -40, maximum: 25, description: "head tilt; -20 to see the floor" })),
    }),
    executionMode: "sequential",
    async execute(_id, { pans = [60, 30, 0, -30, -60], tilt_deg }) {
      const r = await tonyd.call("scan", { pans, ...(tilt_deg != null ? { tilt_deg } : {}) }, 30_000);
      const content: any[] = [{ type: "text", text: `scan at body heading ${r.heading_deg} deg (gyro):` }];
      for (const v of r.views) {
        const side = v.pan_deg > 0 ? "left" : v.pan_deg < 0 ? "right" : "straight ahead";
        content.push({ type: "text", text: `pan ${v.pan_deg} deg (${side}), tilt ${v.tilt_deg}` },
          { type: "image", data: (await readFile(v.path)).toString("base64"), mimeType: "image/jpeg" });
      }
      return { content, details: { heading_deg: r.heading_deg, pans: r.views.map((v: any) => v.pan_deg) } };
    },
  }));

  pi.registerTool(defineTool({
    name: "video",
    label: "Video",
    description: "Short video: a burst of 2-12 photos over a few seconds, oldest first. Use it to see motion or change.",
    parameters: Type.Object({
      frames: Type.Optional(Type.Integer({ minimum: 2, maximum: 12, description: "default 4" })),
      seconds: Type.Optional(Type.Number({ minimum: 0.2, maximum: 10, description: "total span, default 2" })),
    }),
    async execute(_id, { frames = 4, seconds = 2 }) {
      const r = await tonyd.call("burst", { frames, seconds }, (seconds + 10) * 1000);
      const content: any[] = [];
      for (const f of r.frames) {
        content.push({ type: "text", text: `frame at ${f.t_s} s` },
          { type: "image", data: (await readFile(f.path)).toString("base64"), mimeType: "image/jpeg" });
      }
      return { content, details: { frames: r.frames.length } };
    },
  }));
}
