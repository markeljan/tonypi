import json

import pytest

from tonypi.learn.gait import digest
from tonypi.motion import provisional as p
from tonypi.simulation.catalog import digest as sim_digest
from tonypi.simulation.service import adapter_digest


def write(path, value):
    path.write_text(json.dumps(value))


def rows(path, value):
    path.write_text('\n'.join(json.dumps(row) for row in value))


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'manifest', lambda: {'skills': {'stand': {}}})
    monkeypatch.setattr(p, 'DATA_DIR', tmp_path / 'factory')
    report_dir = tmp_path / ('a' * 32)
    report_dir.mkdir()
    frames = [{'ms': 100, 'pos': {'1': 500}}]
    cal = {'trim': {}, 'limits': {}}
    request = {'adapter': adapter_digest(), 'start': 'home', 'objective': 'stability', 'revision': 'source-rev',
               'plan': {'count': 1, 'calibration_source': 'tonyd', 'calibration': cal, 'hardware_frames': frames,
                        'source': 'frames', 'motion': '', 'soft_limit_clips': []}}
    report = {'request_digest': sim_digest(request), 'simulation_success': True, 'hardware_candidate': True,
              'model_joint_clips': [], 'soft_limit_clips': []}
    write(report_dir / 'request.json', request)
    write(report_dir / 'report.json', report)
    trial = {'kind': 'trial', 'cand': digest(frames), 'count': 1, 'verdict': 'ok', 'guard_tilt_deg': 30,
             'label': 'simulation-aaaaaaaaaa', 'exp': 'experiment', 'metrics': {'fell': False}, 'before': {'lying': False, 'tilt_deg': 2}, 'job': 1}
    job = {'id': 1, 'label': 'experiment', 'state': 'done', 'lying_after': False, 'end_tilt_deg': 4, 'guard_tilt_deg': 30,
           'params': {'hash': digest(frames), 'calibration_digest': sim_digest(cal),
                      'start_lying': False, 'start_tilt_deg': 2}}
    rows(tmp_path / 'ledger.jsonl', [trial])
    rows(tmp_path / 'jobs.jsonl', [job])
    return tmp_path, report_dir, cal, trial, job


def args(evidence):
    d, _, cal, _, _ = evidence
    return d / 'ledger.jsonl', d / 'jobs.jsonl', d, cal


def promote(evidence, name='screened'):
    return p.promote(name, evidence[1], *args(evidence))


def test_single_pass_available_separate_from_reliability(evidence):
    value = promote(evidence)
    assert value['approval_level'] == 'provisional'
    assert value['hardware_verified'] is value['reliability_verified'] is False
    assert value['physical_trial_count'] == 1 and value['physical_jobs'] == [1]
    assert value['source'] == 'frames' and value['simulation_revision'] == 'source-rev'
    assert value['count_limit'] == 1 and value['precondition'] == 'standing' and value['locomotion_unknown']
    assert 'frames' not in value
    assert p.library(*args(evidence))['screened']['available']
    assert 'frames' not in p.library(*args(evidence))['screened']
    assert not (evidence[0] / 'promoted_skills.json').exists()


@pytest.mark.parametrize('failure', ['zero', 'fall', 'ledger_guard', 'job_guard', 'job_failed', 'lying', 'nan',
                                    'end', 'hash', 'calibration', 'digest', 'adapter', 'model', 'count', 'limits'])
def test_refuses_inadequate_or_contradictory_evidence(evidence, failure):
    d, report_dir, cal, trial, job = evidence
    if failure == 'zero':
        rows(d / 'ledger.jsonl', [])
    elif failure in ('digest', 'adapter', 'model', 'count', 'limits'):
        request = json.loads((report_dir / 'request.json').read_text())
        report = json.loads((report_dir / 'report.json').read_text())
        if failure == 'digest':
            request['start'] = 'supine'
        elif failure == 'adapter':
            request['adapter'] = 'old'
        elif failure == 'model':
            report['model_joint_clips'] = [1]
        elif failure == 'count':
            request['plan']['count'] = 2
        else:
            cal['limits'] = {'1': [0, 400]}
            request['plan']['calibration'] = cal
        if failure != 'digest':
            report['request_digest'] = sim_digest(request)
        write(report_dir / 'request.json', request)
        write(report_dir / 'report.json', report)
    elif failure == 'calibration':
        cal['trim']['1'] = 2
    else:
        if failure == 'fall':
            trial['metrics']['fell'] = True
        elif failure == 'ledger_guard':
            trial.pop('guard_tilt_deg')
        elif failure == 'job_guard':
            job['guard_tilt_deg'] = 40
        elif failure == 'job_failed':
            job['state'] = 'failed'
        elif failure == 'lying':
            job['params']['start_lying'] = True
        elif failure == 'nan':
            trial['before']['tilt_deg'] = float('nan')
        elif failure == 'end':
            job['end_tilt_deg'] = 31
        else:
            job['params']['hash'] = 'bad'
        rows(d / 'ledger.jsonl', [trial])
        rows(d / 'jobs.jsonl', [job])
    with pytest.raises(ValueError):
        promote(evidence)


def test_duplicate_rows_count_once_and_conflicting_rows_refused(evidence):
    d, _, _, trial, job = evidence
    rows(d / 'ledger.jsonl', [trial, trial])
    assert promote(evidence)['physical_trial_count'] == 1
    failed = {**trial, 'verdict': 'failed'}
    rows(d / 'ledger.jsonl', [trial, failed])
    with pytest.raises(ValueError, match='contradictory'):
        promote(evidence)
    rows(d / 'jobs.jsonl', [job, {**job, 'state': 'failed'}])
    with pytest.raises(ValueError, match='duplicate'):
        promote(evidence)


@pytest.mark.parametrize('failure', ['report', 'frames', 'approval', 'calibration', 'job'])
def test_stale_approval_unavailable_in_discovery_and_execution(evidence, failure):
    d, report_dir, cal, trial, job = evidence
    promote(evidence)
    if failure == 'report':
        report = json.loads((report_dir / 'report.json').read_text())
        report['extra'] = 'changed'
        write(report_dir / 'report.json', report)
    elif failure in ('frames', 'approval'):
        registry = json.loads((d / 'provisional_skills.json').read_text())
        if failure == 'frames':
            registry['screened']['frames'][0]['pos']['1'] = 600
        else:
            registry['screened']['hardware_verified'] = True
        write(d / 'provisional_skills.json', registry)
    elif failure == 'calibration':
        cal['trim']['1'] = 2
    else:
        rows(d / 'jobs.jsonl', [])
    assert p.library(*args(evidence))['screened']['available'] is False
    with pytest.raises(ValueError):
        p.execute(object(), 'screened', *args(evidence))


@pytest.mark.parametrize('collision', ['curated', 'promoted', 'default'])
def test_names_cannot_override(evidence, collision):
    d = evidence[0]
    if collision == 'promoted':
        write(d / 'promoted_skills.json', {'stand': {}})
    elif collision == 'default':
        (d / 'factory').mkdir()
        write(d / 'factory/stand.json', {})
    with pytest.raises(ValueError, match='collides'):
        promote(evidence, 'stand')


def test_execution_uses_existing_bridge_once_and_preserves_refusal(evidence, monkeypatch):
    import tonypi.simulation.service as service

    promote(evidence)
    calls = []
    client = object()
    def execute(got_client, report_id):
        calls.append((got_client, report_id))
        return {'hardware_validated': False, 'physical_trial': {'trials': [{'verdict': 'fell'}]}}
    monkeypatch.setattr(service, 'execute', execute)
    result = p.execute(client, 'screened', *args(evidence))
    assert calls == [(client, 'a' * 32)]
    assert result['hardware_validated'] is False and result['reliability_verified'] is False
    def refuse(*unused):
        raise ValueError('current raw joints differ')
    monkeypatch.setattr(service, 'execute', refuse)
    with pytest.raises(ValueError, match='raw joints differ'):
        p.execute(client, 'screened', *args(evidence))


@pytest.mark.parametrize('target', ['request', 'report', 'plan', 'ledger', 'job', 'registry'])
def test_missing_or_malformed_evidence_is_a_clear_value_error(evidence, target):
    d, report_dir, _, _, _ = evidence
    if target in ('request', 'report'):
        (report_dir / f'{target}.json').unlink()
    elif target == 'plan':
        write(report_dir / 'request.json', {'plan': []})
    elif target == 'ledger':
        (d / 'ledger.jsonl').write_text('invalid json')
    elif target == 'job':
        rows(d / 'jobs.jsonl', [{}])
    else:
        write(d / 'provisional_skills.json', [])
    with pytest.raises(ValueError):
        promote(evidence)


@pytest.mark.parametrize('field', ['label', 'exp', 'job_label'])
def test_success_must_belong_to_exact_simulation_bridge(evidence, field):
    d, _, _, trial, job = evidence
    if field == 'job_label':
        job['label'] = 'unrelated'
    else:
        trial[field] = 'unrelated'
    rows(d / 'ledger.jsonl', [trial])
    rows(d / 'jobs.jsonl', [job])
    with pytest.raises(ValueError):
        promote(evidence)


def test_user_skill_name_collision(evidence):
    write(evidence[0] / 'skills.json', {'screened': {'sequence': []}})
    with pytest.raises(ValueError, match='collides'):
        promote(evidence)


@pytest.fixture
def everyday(evidence, monkeypatch):
    from tonypi import mcp_server as server
    import tonypi.tonyd.studio as studio
    import tonypi.simulation.service as service

    d, _, cal, _, _ = evidence
    promote(evidence)
    (d / 'brain/learning').mkdir(parents=True)
    (d / 'brain/learning/ledger.jsonl').write_text((d / 'ledger.jsonl').read_text())
    monkeypatch.setattr(studio, 'USER_DIR', d)
    calls = []
    class Robot:
        def state(self, **kwargs):
            return {'lying': False}
        def job(self):
            return None
        def call(self, op, **kwargs):
            calls.append(op)
            if op == 'catalog':
                return {'skills': {'stand': {'available': True}}}
            if op == 'calibration':
                return {'calibration': cal}
            pytest.fail(f'unexpected body call: {op}')
    robot = Robot()
    monkeypatch.setattr(server, 'tonyd', robot)
    bridge_calls = []
    def bridge(client, report_id):
        bridge_calls.append((client, report_id))
        return {'hardware_validated': True}
    monkeypatch.setattr(service, 'execute', bridge)
    return server, calls, bridge_calls, robot


def test_everyday_status_and_run_skill_use_provisional_evidence(everyday):
    server, calls, bridge_calls, robot = everyday
    entry = server.status()['skills']['screened']
    assert entry['available'] and entry['approval_level'] == 'provisional'
    assert entry['hardware_verified'] is entry['reliability_verified'] is False
    assert entry['physical_trial_count'] == 1 and 'frames' not in entry
    result = server.run_skill('screened')
    assert result['approval_level'] == 'provisional'
    assert bridge_calls == [(robot, 'a' * 32)]
    assert 'execute' not in calls and 'run' not in calls


@pytest.mark.parametrize('count,wait', [(2, True), (1, False)])
def test_everyday_provisional_count_and_wait_refused_without_execution(everyday, count, wait):
    from mcp.server.mcpserver.exceptions import ToolError
    server, _, bridge_calls, _ = everyday
    with pytest.raises(ToolError, match='count=1 and wait=True'):
        server.run_skill('screened', count, wait)
    assert not bridge_calls


def test_everyday_stale_evidence_and_bridge_refusals_propagate(everyday, evidence, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    import tonypi.simulation.service as service
    server, _, _, _ = everyday
    def refuse(*unused):
        raise ValueError('current raw joints differ')
    monkeypatch.setattr(service, 'execute', refuse)
    with pytest.raises(ToolError, match='raw joints differ'):
        server.run_skill('screened')
    (evidence[1] / 'report.json').unlink()
    assert server.status()['skills']['screened']['available'] is False
    with pytest.raises(ToolError, match='missing or malformed'):
        server.run_skill('screened')
