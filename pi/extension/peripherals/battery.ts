// Estimated 3S LiPo charge from fresh tonyd battery voltage.
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as tonyd from "../tonyd.ts";

export default function battery(pi: ExtensionAPI) {
  pi.registerTool(defineTool({
    name: "battery_percent",
    label: "Battery percentage",
    description: "Read fresh battery voltage from tonyd and estimate 3S LiPo charge linearly: 10.5 V = 0%, 12.6 V = 100%, clamped to 0–100. Voltage-based estimate, not a measured state of charge.",
    parameters: Type.Object({}),
    async execute() {
      const st = await tonyd.state(true);
      const mv = st?.battery_mv;
      if (typeof mv !== "number" || !Number.isFinite(mv) || mv <= 0) {
        return {
          content: [{ type: "text", text: "Battery voltage unavailable; cannot estimate percentage." }],
          details: { error: "battery_voltage_unavailable" },
          isError: true,
        };
      }
      const voltage = mv / 1000;
      const percent = Math.round(Math.max(0, Math.min(100, (mv - 10500) / 2100 * 100)));
      return {
        content: [{ type: "text", text: `Battery approximately ${percent}% (${voltage.toFixed(2)} V; linear 3S LiPo estimate).` }],
        details: { voltage, percent, estimated: true },
      };
    },
  }));
}
