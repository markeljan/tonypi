"""tonypi-self: allow-listed self edits and the after-task commit of skills Tony refined himself."""

import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tonypi.hermes import self_mcp


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo, profile = tmp_path / "repo", tmp_path / "profile"
    (repo / "hermes/skills/robot/walk").mkdir(parents=True)
    (repo / "hermes/skills/robot/walk/SKILL.md").write_text("v1\n")
    (repo / "hermes/SOUL.md").write_text("soul\n")
    (profile / "skills/robot/walk").mkdir(parents=True)
    (profile / "skills/robot/walk/SKILL.md").write_text("v1\n")
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"], ["add", "-A"],
                ["commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    monkeypatch.setattr(self_mcp, "REPO", repo)
    monkeypatch.setattr(self_mcp, "PROFILE", profile)
    monkeypatch.setattr(self_mcp, "STATE", tmp_path / "state")
    monkeypatch.setattr(self_mcp, "CHANGES", tmp_path / "state/changes.jsonl")
    return repo, profile


def test_learned_skill_edits_are_committed(repo):
    repo_dir, profile = repo
    assert self_mcp.commit_learned("nothing yet") is None
    (profile / "skills/robot/walk/SKILL.md").write_text("v2: check the floor pattern\n")
    (profile / "skills/robot/jump").mkdir()
    (profile / "skills/robot/jump/SKILL.md").write_text("new\n")
    head = self_mcp.commit_learned("walk to the door")
    assert head and "Tony learned: jump, walk" in head
    assert (repo_dir / "hermes/skills/robot/walk/SKILL.md").read_text().startswith("v2")
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True,
                          text=True).stdout == ""


def test_self_write_is_allow_listed_and_applies_now(repo):
    repo_dir, profile = repo
    with pytest.raises(ToolError):
        self_mcp.self_write("src/tonypi/tonyd/server.py", "x", "nope")
    with pytest.raises(ToolError):
        self_mcp.self_write("hermes/../../etc/passwd", "x", "nope")
    r = self_mcp.self_write("hermes/skills/robot/walk/SKILL.md", "v3\n", "better")
    assert r["applied_now"] == ["skills/robot/walk/SKILL.md"]
    assert (profile / "skills/robot/walk/SKILL.md").read_text() == "v3\n"
