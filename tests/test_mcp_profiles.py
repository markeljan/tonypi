"""The tool registration boundary is process scoped, with no everyday tool to enable learning."""
import asyncio
import json
import os
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tonypi import mcp_server as server


def profile_tools(profile):
    code = 'import asyncio,json; from tonypi.mcp_server import mcp; print(json.dumps([x.name for x in asyncio.run(mcp.list_tools())]))'
    p = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'TONYPI_MCP_PROFILE': profile},
                       capture_output=True, text=True, check=True)
    return set(json.loads(p.stdout))


def test_everyday_has_only_ten_tools_and_learning_is_separate():
    everyday = profile_tools('everyday')
    assert everyday == server.EVERYDAY_TOOLS and len(everyday) == 10
    learning = profile_tools('learning')
    assert everyday < learning
    assert {'set_pose', 'play_frames', 'set_torque', 'get_joints', 'calibration', 'servo_calibrate',
            'body_sweep', 'trial', 'compare', 'simulate_motion', 'simulation_execute'} <= learning


def test_hidden_tool_cannot_be_called_through_everyday_mcp():
    with pytest.raises(Exception, match='Unknown tool|unknown tool'):
        asyncio.run(server.mcp.call_tool('set_pose', {'positions': {'1': 500}}))


@pytest.mark.parametrize('call', [lambda: server.move('jump'), lambda: server.move('forward', 0),
    lambda: server.move('forward', True), lambda: server.posture('somersault'), lambda: server.gesture('random'),
    lambda: server.look(float('nan'), 0), lambda: server.look(61, 0), lambda: server.perceive('upload'),
    lambda: server.say(''), lambda: server.run_skill('wave', wait='false')])
def test_validation_refuses_before_any_daemon_call(monkeypatch, call):
    class NoRobot:
        def call(self, *args, **kwargs):
            pytest.fail('invalid argument reached the robot')
    monkeypatch.setattr(server, 'tonyd', NoRobot())
    with pytest.raises(ToolError):
        call()


def test_everyday_routes_to_curated_daemon_not_raw_run(monkeypatch):
    calls = []
    class Robot:
        def call(self, op, **kwargs):
            calls.append((op, kwargs))
            return {'job': {'state': 'queued'}}
    monkeypatch.setattr(server, 'tonyd', Robot())
    server.move('backward', 2, False)
    server.run_skill('wave')
    assert [c[0] for c in calls] == ['execute', 'execute']
    assert calls[0][1]['skill'] == 'walk_backward' and calls[0][1]['count'] == 2
