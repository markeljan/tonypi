// Sensors peripheral: posture, battery, servo temperatures, camera, current skill. Read from tonyd's state.

import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as tonyd from "../tonyd.ts";

const LIMIT_C = 85; // servos self-protect here; tonyd refuses motions from 81 C

export function summary(st: any): string {
  const parts: string[] = [];
  if (st.tilt_deg != null) parts.push(st.lying ? `lying down (tilt ${st.tilt_deg} deg)` : `upright (tilt ${st.tilt_deg} deg)`);
  if (st.heading_deg != null) parts.push(`heading ${st.heading_deg} deg (gyro, + = left, drifts slowly)`);
  if (st.asleep) parts.push("asleep (motors off)");
  if (st.battery_mv) parts.push(`battery ${(st.battery_mv / 1000).toFixed(2)} V (3S LiPo: 12.6 full, 10.5 low)`);
  if (st.hottest) parts.push(`hottest servo ${st.hottest[1]} at ${st.hottest[2]} C (limit ${LIMIT_C} C)`);
  const temps = Object.values(st.temps ?? {}).filter((t) => t != null) as number[];
  if (temps.length) parts.push(`servo temps ${Math.min(...temps)}-${Math.max(...temps)} C`);
  if (st.torque_on != null) parts.push(`torque on ${st.torque_on}/16 servos`);
  parts.push(st.busy ? `busy with ${st.job?.skill}` : "idle");
  parts.push(`camera ${st.camera?.ok ? "ok" : "not working"}`);
  parts.push(`head pan ${st.head_deg?.pan}, tilt ${st.head_deg?.tilt}`);
  if (st.tether?.on) parts.push(`tethered to the charger: ${st.tether.walked} of ${st.tether.max_steps} walking steps used`);
  return parts.join("; ");
}

export default function sensors(pi: ExtensionAPI) {
  pi.registerTool(defineTool({
    name: "tether",
    label: "Tether",
    description: "Charging-cable tether. While on, walking (walk_*, side steps, any skill with walk in its name) may "
      + "use at most max_steps steps in total so the cable isn't dragged; turns don't count. No arguments = read it. "
      + "reset=true after walking back to the charger; on=false only when someone says Tony is unplugged.",
    parameters: Type.Object({ on: Type.Optional(Type.Boolean()), max_steps: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
      reset: Type.Optional(Type.Boolean()) }),
    async execute(_id, { on, max_steps, reset = false }) {
      const t = (await tonyd.call("tether", { on, max_steps, reset })).tether;
      return { content: [{ type: "text", text: JSON.stringify(t) }], details: t };
    },
  }));

  pi.registerTool(defineTool({
    name: "get_state",
    label: "State",
    description: "Tony's body state: posture/tilt, battery voltage, servo temperatures, torque, current skill, camera.",
    parameters: Type.Object({}),
    async execute() {
      const st = await tonyd.state(true);
      return { content: [{ type: "text", text: summary(st) }], details: st };
    },
  }));
}
