# This Raspberry Pi is the TonyPi robot

- Project: `/home/pi/tonypi` — read `/home/pi/tonypi/CLAUDE.md` and `/home/pi/tonypi/docs/ROADMAP.md` before doing anything.
- Fresh Raspberry Pi OS Lite 64-bit (trixie), reflashed 2026-10-28 from the old Hiwonder image. One user: `pi` (passwordless sudo).
- First-boot provisioning: `tonypi-provision.service` → log `/var/log/tonypi-provision.log`, done when `/var/lib/tonypi/provisioned` exists.
  Re-run safely with `sudo /home/pi/tonypi/provision/provision.sh`.
- Models: `/home/pi/models`. llama.cpp: `/home/pi/src/llama.cpp` (binaries linked in /usr/local/bin).
- The servo controller UART `/dev/ttyAMA0` must have exactly one owner process.
