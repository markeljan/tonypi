#!/usr/bin/env python3
"""Archive the unpromoted legacy user layer without deleting any authored data. Never drives the robot."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from tonypi.tonyd.client import TonydClient


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(user_dir, apply=False):
    state = TonydClient().state(fresh=True)
    if state['busy']:
        raise ValueError('robot is busy; wait for the current job to finish')
    paths = []
    motions = user_dir / 'motions'
    if motions.exists():
        paths.extend(sorted(p for p in motions.rglob('*') if p.is_file()))
    skills = user_dir / 'skills.json'
    if skills.exists():
        paths.append(skills)
    inventory = {str(p.relative_to(user_dir)): {'sha256': digest(p), 'bytes': p.stat().st_size} for p in paths}
    if not apply or not paths:
        return {'apply': False, 'files': inventory}
    # Refuse symlinks and an existing destination; rename preserves contents and permissions.
    if motions.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError('symlinked user data requires separate review')
    destination = user_dir / 'archive' / ('motion-layer-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    destination.mkdir(parents=True, exist_ok=False)
    if motions.exists():
        motions.rename(destination / 'motions')
    if skills.exists():
        skills.rename(destination / 'skills.json')
    for relative, row in inventory.items():
        if digest(destination / relative) != row['sha256']:
            raise ValueError('archive verification failed: ' + relative)
    record = {'reason': 'Legacy experiments lack exact model/current-calibration promotion evidence; preserved for learning.',
              'source': str(user_dir), 'archive': str(destination), 'files': inventory,
              'restore': 'While idle, move archived motions/ and skills.json back if the active paths are absent. Archive is never deleted.'}
    (destination / 'inventory.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-dir', type=Path, default=Path.home() / '.local/share/tonypi')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(archive(args.user_dir, args.apply), indent=2))


if __name__ == '__main__':
    main()
