// Client for tonyd's Unix socket (JSON lines, see src/tonypi/tonyd/server.py). One connection per request: tonyd
// serves each connection on its own thread, so a long `run` with wait never blocks a `stop` or a `state`.

import { createConnection } from "node:net";

export const SOCK = process.env.TONYD_SOCK ?? "/run/tonypi/tonyd.sock";

export class TonydError extends Error {}

let rids = 1;

export function call(op: string, args: Record<string, unknown> = {}, timeoutMs = 10_000): Promise<any> {
  return new Promise((resolve, reject) => {
    const rid = rids++;
    const sock = createConnection(SOCK);
    let buf = "";
    const done = (err: Error | null, value?: any) => {
      clearTimeout(timer);
      sock.destroy();
      err ? reject(err) : resolve(value);
    };
    const timer = setTimeout(() => done(new TonydError(`tonyd did not answer ${op} within ${timeoutMs / 1000} s`)), timeoutMs);
    sock.on("connect", () => sock.write(JSON.stringify({ op, rid, ...args }) + "\n"));
    sock.on("error", (e) => done(new TonydError(`tonyd not reachable at ${SOCK} (${e.message})`)));
    sock.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        const msg = JSON.parse(line);
        if (msg.rid !== rid) continue;
        if (!msg.ok) return done(new TonydError(msg.error ?? "error"));
        delete msg.ok;
        delete msg.rid;
        return done(null, msg);
      }
    });
  });
}

export interface Job {
  id: number;
  skill: string;
  count: number;
  state: "queued" | "running" | "done" | "failed" | "cancelled";
  said: string[];
  error: string | null;
  seconds: number | null;
}

export const run = async (skill: string, count = 1, waitS = 180): Promise<Job> =>
  (await call("run", { skill, count, wait: true, wait_s: waitS }, (waitS + 5) * 1000)).job;
export const stop = () => call("stop");
export const state = async (fresh = false) => (await call("state", { fresh }, 5_000)).state;
export const head = async (pan_deg: number, tilt_deg: number, ms = 400) =>
  (await call("head", { pan_deg, tilt_deg, ms })).head_deg as { pan: number; tilt: number };
export const snapshot = () =>
  call("snapshot") as Promise<{ path: string; frame_age_s: number; width: number; height: number; head_deg: any }>;
export const skills = async () => (await call("skills")).skills as Record<string, string>;
