"""Camera snapshot + on-board VLM description (LFM2.5-VL-1.6B via llama-mtmd-cli, ~25 s on the Pi 5)."""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

MODELS = Path("/home/pi/models")
VLM = MODELS / "LFM2.5-VL-1.6B-Q4_K_M.gguf"
MMPROJ = MODELS / "mmproj-LFM2.5-VL-1.6b-Q8_0.gguf"
PROMPT = ("This photo was taken by a small robot's camera, close to the floor. Describe what is in it in one or two "
          "short sentences, mentioning any people. Speak in the first person as the robot, e.g. 'I see ...'.")


def snapshot(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "v4l2", "-input_format", "yuyv422",
                    "-video_size", "640x480", "-i", "/dev/video0", "-vf", r"select=gte(n\,10)", "-frames:v", "1",
                    str(path)], check=True, timeout=20)
    return path


def describe(image: Path, cancelled: threading.Event) -> str | None:
    proc = subprocess.Popen([str(p) for p in ["llama-mtmd-cli", "-m", VLM, "--mmproj", MMPROJ, "--image", image,
                                              "-p", PROMPT, "-n", "70", "-t", "4", "--temp", "0.2"]],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    while proc.poll() is None:
        if cancelled.wait(0.2):
            proc.kill()
            return None
    return re.sub(r"[*_#`]", "", proc.stdout.read()).strip() or None
