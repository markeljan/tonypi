"""Install pinned data and run simulator-only experiments from a shell."""

import argparse
import json
from pathlib import Path
import subprocess

from . import catalog, service


def install():
    repo = catalog.repository()
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", catalog.URL + ".git", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", catalog.REVISION], check=True)
    catalog.verify(repo)
    return service.status(repo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "status", "catalog"):
        sub.add_parser(name)
    run = sub.add_parser("run")
    run.add_argument("motion", nargs="?", default="")
    run.add_argument("--frames", type=Path, help="JSON list of motion-space frames")
    run.add_argument("--count", type=int, default=1)
    run.add_argument("--start", choices=("home", "prone", "supine"), default="home")
    run.add_argument("--objective", choices=("stability", "recover"), default="stability")
    run.add_argument("--no-render", action="store_true")
    run.add_argument("--offline", action="store_true", help="identity calibration; no tonyd connection")
    args = parser.parse_args()
    try:
        if args.command == "install":
            result = install()
        elif args.command == "status":
            result = service.status()
        else:
            from tonypi.tonyd.client import TonydClient

            client = None if getattr(args, "offline", False) else TonydClient()
            if args.command == "catalog":
                result = catalog.catalog(catalog.repository(), client)
            else:
                frames = json.loads(args.frames.read_text()) if args.frames else None
                result = service.simulate(client, args.motion, frames, args.count, start=args.start,
                                          objective=args.objective, render=not args.no_render)
        print(json.dumps(result, indent=1))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        parser.exit(1, f"{e}\n")
