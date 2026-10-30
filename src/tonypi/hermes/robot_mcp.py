"""tonypi-mcp for Hermes (stdio): the shared robot MCP server's tools (tonypi.mcp_server, unchanged) with the bake-off's
fairness rule on top: Hermes sees and uses only the factory defaults and its own learned moves (names with SUFFIX),
never another harness's (_own, _pi). It also saves and deletes only names with SUFFIX, so it can't touch a default
or someone else's move.
"""

from __future__ import annotations

import os

from mcp.server.mcpserver.exceptions import ToolError

from tonypi import mcp_server as shared

SUFFIX = os.environ.get("TONYPI_HARNESS_SUFFIX", "_hermes")
mcp = shared.mcp


def mine(name: str) -> bool:
    return SUFFIX in name


def _user_names() -> tuple[set[str], set[str]]:
    """(user skills, user motions) currently in tonyd: the learned layer shared by all harnesses."""
    skills = {k for k, v in shared.tonyd.call("skills")["details"].items() if v.get("source") == "user"}
    motions = {m["name"] for m in shared.tonyd.call("motions")["motions"] if m.get("source") == "user"}
    return skills, motions


def _check_visible(kind: str, name: str) -> None:
    skills, motions = _user_names()
    if name in (skills if kind == "skill" else motions) and not mine(name):
        raise ToolError(f"{name} is another harness's {kind}; use a default or your own (*{SUFFIX})")


def _check_mine(kind: str, name: str) -> None:
    if not mine(name):
        raise ToolError(f"save and delete only your own {kind}s: name it <something>{SUFFIX}, e.g. "
                        f"high_step{SUFFIX} (never a default or another harness's name)")


def _replace(fn):
    """Swap the shared tool of the same name for `fn` (same name; fn's docstring is the description)."""
    mcp.remove_tool(fn.__name__)
    mcp.add_tool(fn, name=fn.__name__, description=fn.__doc__)
    return fn


@_replace
def list_skills() -> dict:
    """Every skill the body can run (factory defaults, built-ins and your own *_hermes): description, source and,
    for sequence skills, the motions it plays. count repeats steps for walking/turning skills."""
    with shared._robot():
        details = shared.tonyd.call("skills")["details"]
    return {k: v for k, v in details.items() if v.get("source") != "user" or mine(k)}


@_replace
def list_motions() -> list[dict]:
    """Every motion (keyframe file): factory defaults and your own saved *_hermes motions."""
    with shared._robot():
        motions = shared.tonyd.call("motions")["motions"]
    return [m for m in motions if m.get("source") != "user" or mine(m["name"])]


@_replace
def get_motion(name: str) -> dict:
    """A motion's keyframes (a default or one of yours), to study or copy before writing your own version."""
    with shared._robot():
        _check_visible("motion", name)
        return shared.tonyd.call("motion_get", name=name)


@_replace
def run_skill(skill: str, count: int = 1, wait: bool = True, guard_tilt_deg: float | None = None) -> dict:
    """Run one body skill (see list_skills). count = steps/repeats (1-5). With wait=True, returns when it ends:
    state is done, failed (refused: too hot, ...; see `said`) or cancelled (preempted or stopped). max_tilt_deg says
    how far Tony leaned, yaw_deg how far he turned (+ = left). guard_tilt_deg (opt-in) stops the skill if he tilts
    past that angle."""
    with shared._robot():
        _check_visible("skill", skill)
    try:
        return shared.run_skill(skill, count, wait, guard_tilt_deg)
    except ToolError as e:  # tonyd's "unknown skill ...; known: <all>" would name other harnesses' skills
        if "unknown skill" not in str(e):
            raise
        raise ToolError(f"unknown skill {skill!r}; known: {', '.join(list_skills())}") from None


@_replace
def save_motion(name: str, frames: list[dict], description: str = "") -> dict:
    """Save keyframes as your own motion: the name must end in _hermes (e.g. high_step_hermes)."""
    _check_mine("motion", name)
    return shared.save_motion(name, frames, description)


@_replace
def save_skill(name: str, motions: list[str], description: str = "") -> dict:
    """Define your own skill (name ends in _hermes) as a motion sequence; "*name" repeats with count. Motions must be
    defaults or yours, e.g. save_skill("walk_hermes", ["go_forward_start", "*my_step_hermes", "go_forward_end"])."""
    _check_mine("skill", name)
    with shared._robot():
        for m in motions:
            _check_visible("motion", m.lstrip("*"))
    return shared.save_skill(name, motions, description)


@_replace
def delete_motion(name: str) -> dict:
    """Delete one of your own (*_hermes) motions."""
    _check_mine("motion", name)
    return shared.delete_motion(name)


@_replace
def delete_skill(name: str) -> dict:
    """Delete one of your own (*_hermes) skills."""
    _check_mine("skill", name)
    return shared.delete_skill(name)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
