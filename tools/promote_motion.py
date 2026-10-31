#!/usr/bin/env python3
"""Owner-requested promotion of a verified exact frame sequence; this command never moves the robot."""

import argparse
import json
from pathlib import Path

from tonypi.motion.promotion import promote
from tonypi.simulation.catalog import calibration, root
from tonypi.tonyd.client import TonydClient


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("name")
    p.add_argument("report_id")
    p.add_argument("--description", default="")
    p.add_argument("--user-dir", type=Path, default=Path.home() / ".local/share/tonypi")
    args = p.parse_args()
    if len(args.report_id) != 32 or any(c not in "0123456789abcdef" for c in args.report_id):
        p.error("report_id must be 32 lowercase hexadecimal characters")
    try:
        result = promote(args.name, root() / "reports" / args.report_id,
                         args.user_dir / "brain/learning/ledger.jsonl", args.user_dir / "jobs.jsonl",
                         args.user_dir, calibration(TonydClient()), args.description)
    except (OSError, ValueError, KeyError) as e:
        p.exit(1, f"{e}\n")
    print(json.dumps({k: v for k, v in result.items() if k != "frames"}, indent=2))


if __name__ == "__main__":
    main()
