"""Install pinned data, launch MuJoCo 4 Studio, and run simulator-only experiments from a shell."""

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


def launch_studio(
    *,
    gfx: str = "web",
    port: int = 8080,
    open_browser: bool = False,
    repo: Path | None = None,
) -> None:
    """Launch MuJoCo 4.0.1 Studio directly on the pinned PAB47/TonyPi scene.xml."""
    if gfx not in ("web", "opengl"):
        raise ValueError("gfx must be 'web' or 'opengl'")
    repo = repo or catalog.repository()
    catalog.verify(repo)
    scene = repo / "model/scene.xml"
    if not scene.is_file():
        raise FileNotFoundError(f"TonyPi MuJoCo scene not found: {scene} (run `tonypi-sim install`)")

    from mujoco.studio import app as studio_app
    from mujoco.studio import launch_passive, launch_web, sim_history, step_control, viewer_protocol

    config = viewer_protocol.ViewerConfig(
        title="TonyPi — MuJoCo 4 Studio",
        gfx=gfx,
        http_port=port,
        open_browser=open_browser,
    )
    plugins = [
        studio_app.App(),
        step_control.StepControl(),
        sim_history.SimHistory(),
    ]
    if gfx == "web":
        launch_web.run(config, model_path=str(scene), plugins=plugins)
    else:
        launch_passive.run(config, model_path=str(scene), plugins=plugins)


def main(argv: list[str] | None = None):
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

    studio = sub.add_parser("studio", help="Launch MuJoCo 4.0.1 Studio on pinned TonyPi scene.xml")
    studio.add_argument("--gfx", choices=("web", "opengl"), default="web")
    studio.add_argument("--port", type=int, default=8080)
    studio.add_argument("--open-browser", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            result = install()
        elif args.command == "status":
            result = service.status()
        elif args.command == "studio":
            launch_studio(gfx=args.gfx, port=args.port, open_browser=args.open_browser)
            return
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
