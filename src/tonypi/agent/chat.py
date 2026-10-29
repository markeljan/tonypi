"""tonypi-chat: talk to the running tonypi-agent from a terminal.

  tonypi-chat            interactive; also shows what Tony hears by voice
  tonypi-chat "dance"    one command, waits until it's done
  /status                battery, tilt, temperatures;  /quit to leave
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

SOCK = os.environ.get("TONYPI_SOCK", "/run/tonypi/agent.sock")
DIM, BOLD, CYAN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


def fmt(ev: dict) -> str | None:
    t = ev.get("type")
    if t == "heard":
        return f"{DIM}[{ev['source']}]{RESET} you: {ev['text'] or '(nothing)'}"
    if t == "wake":
        return f"{DIM}[voice] hey Tony… listening{RESET}"
    if t == "thinking":
        return f"{DIM}  thinking…{RESET}"
    if t == "plan":
        acts = ", ".join(n if c == 1 else f"{n}×{c}" for n, c in ev["actions"]) or "—"
        return f"{DIM}  plan ({ev['seconds']} s, {ev['source']}): {acts}{RESET}"
    if t == "say":
        return f"{BOLD}{CYAN}tony:{RESET} {ev['text']}"
    if t == "action":
        return f"{YELLOW}  ▶ {ev['name']}{'' if ev['count'] == 1 else ' ×' + str(ev['count'])}{RESET}"
    if t == "info":
        return f"{DIM}  · {ev['text']}{RESET}"
    if t == "status":
        temps = " ".join(f"{k}:{v}" for k, v in ev["temps"].items())
        bat = f"{ev['battery_mv'] / 1000:.2f} V" if ev.get("battery_mv") else "?"
        return (f"  battery {bat} · tilt {ev['tilt_deg']}° {'(lying)' if ev['lying'] else ''} · "
                f"busy {ev['busy']}\n  temps °C {temps}")
    return None


def main() -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except OSError as e:
        sys.exit(f"can't reach tonypi-agent at {SOCK} ({e}). Is it running?  systemctl status tonypi-agent")
    events = s.makefile("r", encoding="utf-8")

    if len(sys.argv) > 1:  # one-shot
        text = " ".join(sys.argv[1:])
        s.sendall((json.dumps({"text": text}) + "\n").encode())
        rid = None
        for line in events:
            ev = json.loads(line)
            if ev["type"] == "heard" and ev["source"] == "terminal" and ev["text"] == text.strip() and rid is None:
                rid = ev["id"]
            if (out := fmt(ev)) and (ev.get("id") in (None, rid) or ev["type"] == "status"):
                print(out, flush=True)
            if text.strip() == "/status" and ev["type"] == "status":
                return
            if ev["type"] == "done" and ev["id"] == rid:
                return
        return

    def reader() -> None:
        for line in events:
            if out := fmt(json.loads(line)):
                print(f"\r{out}\nyou> ", end="", flush=True)
        print("\nagent disconnected")
        os._exit(1)

    threading.Thread(target=reader, daemon=True).start()
    print(f"{BOLD}Talking to Tony.{RESET} Type a command (or say \"hey Tony …\"). "
          f"'stop' stops at once, /status, /quit.")
    try:
        while True:
            line = input("you> ").strip()
            if line in ("/quit", "/exit"):
                break
            if line:
                s.sendall((json.dumps({"text": line}) + "\n").encode())
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
