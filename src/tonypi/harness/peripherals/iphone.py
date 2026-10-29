"""iPhone app peripheral: placeholder, no tools yet.

TODO(iphone): the planned app connects to the harness (WebSocket on the LAN, paired once with a token) and is both
  - a peripheral: tools such as `phone_photo` (the phone's camera, as an image Result), `phone_location`,
    `notify(text)` (push to the owner), each a Tool here whose handler forwards the call over the socket and waits
    for the reply;
  - an input: typed or dictated messages go to `Harness.submit(text, source="iphone")`, so they steer a running
    task exactly like voice does, and harness events (say/action/done) stream back to the app.
Nothing else in the harness needs to change: enable it with TONYPI_PERIPHERALS=body,camera,voice,iphone.
"""

from __future__ import annotations

from tonypi.harness.peripherals import Peripheral, Tool


class IphonePeripheral(Peripheral):
    name = "iphone"
    prompt = ""  # nothing to say until the app exists

    def tools(self) -> list[Tool]:
        return []
