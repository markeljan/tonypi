// TonyPi extension for pi: every robot capability as a pi tool, one module per peripheral, plus the brain router.
// Load with `pi -e /home/pi/tonypi/pi/extension` (the front end does). Body, studio, camera and sensors go to tonyd's
// socket; speech to the front end (or tonypi-agent). Stop and fall recovery never depend on this: tonyd and the
// front end handle them without the model.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import body from "./peripherals/body.ts";
import camera from "./peripherals/camera.ts";
import context from "./context.ts";
import iphone from "./peripherals/iphone.ts";
import sensors from "./peripherals/sensors.ts";
import speech from "./peripherals/speech.ts";
import studio from "./peripherals/studio.ts";
import router from "./router.ts";

export const PERIPHERALS = { body, studio, camera, sensors, speech, iphone };

export default async function tonypi(pi: ExtensionAPI) {
  const only = process.env.TONYPI_PERIPHERALS?.split(",");
  for (const [name, register] of Object.entries(PERIPHERALS)) {
    if (!only || only.includes(name)) await register(pi);
  }
  router(pi);
  context(pi);
}
