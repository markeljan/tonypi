// iPhone peripheral: not built yet.
//
// TODO(iphone): a phone app as an extra body part. Plan:
//  - the app connects to the front end (src/tonypi/pi_front) over the LAN (WebSocket, paired with a token from
//    /home/pi/.config/tonypi/secrets.env) and streams what it offers: camera frames, mic audio, GPS/compass, its
//    own speaker/screen.
//  - tools registered here, each only while the phone is connected (pi.setActiveTools): phone_photo (image
//    content, like camera.ts), phone_location, phone_show (text/image on the screen).
//  - mic audio from the phone joins the same Listener path as the USB mic; typed text from the app is a front-end
//    command like tonypi-chat.
// Keep it a separate module like the others: one file per peripheral, registered from index.ts.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function iphone(_pi: ExtensionAPI) {}
