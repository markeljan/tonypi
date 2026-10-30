"""Offline, conditional evidence for balance recipes; never a controller or a guarantee.

``summarize(rows, manifest)`` consumes trial ledger dictionaries. The optional manifest maps experiment IDs to
metadata: surface, start, starting_conditions, segment, frame_hash, commanded_hold_s and contact_review. Context is
never filled from another trial, even when labels match. Missing context remains unknown. Candidate hashes identify
the runner's recipe, not necessarily its current frames: callers must independently bind saved-skill frame hashes.

A contact_review is explicit human/model-reviewed phone evidence, with ``reviewed: true``, ``source: "phone"``,
the matching ``episode``, ``support: "left" | "right" | "both"``, and positive ``observed_hold_s``. It is retained
per trial, never inferred from IMU, total job duration, or another trial's review. Commanded hold is not observed
hold. No result establishes COM, load fractions, force capacity, or a continuously safe region between recipes.

CLI: python -m tonypi.learn.balance LEDGER [--manifest MANIFEST] [--label-prefix PREFIX]
All inputs are JSON/JSONL; this module imports no robot runtime and performs no robot operations.
"""

from __future__ import annotations

import argparse
import json
from math import isfinite
from pathlib import Path
from statistics import mean, stdev

CONTEXT_FIELDS = ("surface", "start", "setup", "starting_conditions", "segment", "frame_hash", "commanded_hold_s")
EVIDENCE_LIMIT = "Observed recipe-specific evidence only; falls remain possible and untested conditions are unknown."


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _finite_json(value):
    """Reject nonfinite numbers even in optional context; JSON's default NaN extension is unsuitable evidence."""
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("nonfinite value")
    if isinstance(value, dict):
        for item in value.values():
            _finite_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite_json(item)
    json.dumps(value, allow_nan=False)


def _stats(values):
    return {"n": len(values), "mean": mean(values) if values else None,
            "std": stdev(values) if len(values) > 1 else (0.0 if values else None),
            "min": min(values) if values else None, "max": max(values) if values else None}


def zero_failure_upper95(n):
    """One-sided exact binomial 95% upper bound, conditional on n independent trials with zero failures."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("n must be a nonnegative integer")
    return 1 - 0.05 ** (1 / n) if n else None


def _context(row, metadata):
    context = {key: row[key] if key in row else metadata.get(key) for key in CONTEXT_FIELDS}
    # The runner stores setup's job-specific record; only the setup skill is a starting condition.
    if isinstance(context["setup"], dict):
        context["setup"] = context["setup"].get("skill")
    return context


def _contact(row, metadata, completed):
    review = metadata.get("contact_review")
    if not isinstance(review, dict) or not completed:
        return None
    if (review.get("reviewed") is not True or review.get("source") != "phone"
            or not row.get("episode") or review.get("episode") != row["episode"]
            or review.get("support") not in ("left", "right", "both")
            or not _number(review.get("observed_hold_s")) or review["observed_hold_s"] <= 0):
        return None
    return {"exp": row.get("exp"), "episode": row["episode"], "support": review["support"],
            "observed_hold_s": review["observed_hold_s"]}


def summarize(rows, manifest=None, label_prefix=None):
    """Group exact recipes/conditions; exclude malformed trials explicitly and preserve unsuccessful attempts.

    ``n`` counts valid attempts, ``completed`` counts done/ok/no-fall attempts with measured peak and end tilt.
    ``failures`` includes every other valid attempt (including cancellation and missing outcome measurements).
    Bounds describe these observed recipe failures, not just falls; independence is an assumption, not verified.
    Non-trial rows and rows outside label_prefix are ignored; malformed selected trials are listed in rejected.
    """
    manifest = {} if manifest is None else manifest
    if not isinstance(manifest, dict):
        raise ValueError("manifest must map experiment IDs to metadata dictionaries")
    groups, rejected = {}, []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            rejected.append({"index": index, "exp": None, "reason": "row must be a dictionary"})
            continue
        if row.get("kind") != "trial":
            continue
        if label_prefix is not None and not str(row.get("label") or "").startswith(label_prefix):
            continue
        try:
            metadata = manifest.get(str(row.get("exp")), {})
            if not isinstance(metadata, dict):
                raise ValueError("trial metadata must be a dictionary")
            _finite_json(row)
            _finite_json(metadata)
            cand = row.get("cand")
            if not isinstance(cand, str) or not cand.strip():
                raise ValueError("missing candidate hash")
            count = row.get("count")
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
                raise ValueError("count must be a positive integer or unknown")
            guard = row.get("guard_tilt_deg")
            if guard is not None and (not _number(guard) or guard <= 0):
                raise ValueError("guard must be positive finite degrees or unknown/disabled")
            metrics = row.get("metrics")
            if not isinstance(metrics, dict):
                raise ValueError("missing metrics dictionary")
            for key in ("max_tilt_deg", "end_tilt_deg", "seconds"):
                value = metrics.get(key)
                if value is not None and (not _number(value) or value < 0):
                    raise ValueError(f"{key} must be a nonnegative finite number or unknown")
            context = _context(row, metadata)
            frame_hash = context["frame_hash"]
            if frame_hash is not None and (not isinstance(frame_hash, str) or not frame_hash.strip()):
                raise ValueError("frame_hash must be a nonempty string or unknown")
            hold = context["commanded_hold_s"]
            if hold is not None and (not _number(hold) or hold < 0):
                raise ValueError("commanded_hold_s must be nonnegative finite seconds or unknown")
            identity = {"cand": cand, "count": count, "guard_tilt_deg": guard, **context}
            key = json.dumps(identity, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            rejected.append({"index": index, "exp": row.get("exp"), "reason": str(exc)})
            continue
        group = groups.setdefault(key, {**identity, "labels": set(), "experiments": [], "n": 0, "completed": 0,
                                        "falls": 0, "guard_failures": 0, "_tilts": [], "_completed_tilts": [],
                                        "contact_reviews": []})
        group["n"] += 1
        if isinstance(row.get("label"), str):
            group["labels"].add(row["label"])
        group["experiments"].append(row.get("exp"))
        end_tilt = metrics.get("end_tilt_deg")
        fell = (metrics.get("fell") is True or row.get("verdict") == "fell"
                or (_number(end_tilt) and end_tilt > 45))  # Same observable terminal-tilt criterion as metrics.fell.
        completed = (row.get("verdict") == "ok" and metrics.get("state") == "done"
                     and metrics.get("fell") is False and not fell
                     and _number(metrics.get("max_tilt_deg")) and _number(metrics.get("end_tilt_deg")))
        group["completed"] += int(completed)
        group["falls"] += int(fell)
        failure_text = " ".join(str(v) for v in (row.get("verdict"), metrics.get("state"), row.get("error")))
        group["guard_failures"] += int(not completed and "guard" in failure_text.lower())
        if _number(metrics.get("max_tilt_deg")):
            group["_tilts"].append(metrics["max_tilt_deg"])
            if completed:
                group["_completed_tilts"].append(metrics["max_tilt_deg"])
        contact = _contact(row, metadata, completed)
        if contact:
            group["contact_reviews"].append(contact)
    result = []
    for key in sorted(groups):
        group = groups[key]
        group["labels"] = sorted(group["labels"])
        group["failures"] = group["n"] - group["completed"]
        group["peak_tilt_deg"] = _stats(group.pop("_tilts"))
        group["completed_peak_tilt_deg"] = _stats(group.pop("_completed_tilts"))
        group["zero_failure_upper95"] = zero_failure_upper95(group["n"]) if not group["failures"] else None
        group["contact_unverified_trials"] = group["n"] - len(group["contact_reviews"])
        group["frame_binding"] = "explicit" if group["frame_hash"] else "runner candidate only; saved skill may change"
        group["evidence_limit"] = EVIDENCE_LIMIT
        result.append(group)
    return {"version": 1, "groups": result, "rejected": rejected, "evidence_limit": EVIDENCE_LIMIT,
            "bound_assumption": "Exact zero-failure binomial bound assumes independent trials under these conditions."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--label-prefix")
    args = parser.parse_args(argv)
    try:
        rows = [json.loads(line) for line in args.ledger.read_text().splitlines() if line.strip()]
        manifest = json.loads(args.manifest.read_text()) if args.manifest else None
        result = summarize(rows, manifest, args.label_prefix)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
