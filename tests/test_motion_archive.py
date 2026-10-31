import json
from pathlib import Path

import pytest

import importlib.util

spec = importlib.util.spec_from_file_location("motion_archiver", Path(__file__).parents[1] / "tools/archive_motion_candidates.py")
archiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archiver)


def test_archive_preserves_user_content_and_hashes(tmp_path, monkeypatch):
    class Client:
        def state(self, fresh=False):
            return {'busy': False}
    monkeypatch.setattr(archiver, 'TonydClient', Client)
    (tmp_path / 'motions').mkdir()
    original = b'{"frames":[],"description":"owner experiment"}\n'
    (tmp_path / 'motions/custom.json').write_bytes(original)
    (tmp_path / 'skills.json').write_text('{"custom":{"motions":["custom"]}}')
    preview = archiver.archive(tmp_path)
    assert not preview['apply'] and (tmp_path / 'motions/custom.json').read_bytes() == original
    record = archiver.archive(tmp_path, True)
    destination = Path(record['archive'])
    assert (destination / 'motions/custom.json').read_bytes() == original
    assert not (tmp_path / 'motions').exists() and not (tmp_path / 'skills.json').exists()
    inventory = json.loads((destination / 'inventory.json').read_text())
    assert inventory['files'] == preview['files']


def test_busy_archive_leaves_user_data_alone(tmp_path, monkeypatch):
    class Client:
        def state(self, fresh=False):
            return {'busy': True}
    monkeypatch.setattr(archiver, 'TonydClient', Client)
    (tmp_path / 'skills.json').write_text('{}')
    with pytest.raises(ValueError, match='busy'):
        archiver.archive(tmp_path, True)
    assert (tmp_path / 'skills.json').read_text() == '{}'
    assert not (tmp_path / 'archive').exists()
