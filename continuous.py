#!/usr/bin/env python3
"""Continuous, time-bounded sampling inside a GitHub-hosted runner.
Each sample gets its own immutable directory, including failed attempts.
The runner starts immediately if late and never fabricates missing samples.
"""
from __future__ import annotations
import argparse
import json
import os
import time
import traceback
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import monitor

ROOT = Path(os.getenv("PROBE_OUTPUT", "continuous-output"))
# Offset hours from configured daily opening. Max segment length: 4.5 h.
CUTS = (0, 4.5, 9, 13)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def sample(segment_dir, slot, checked):
    folder = segment_dir / "samples" / checked.strftime("%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, exist_ok=False)
    monitor.OUT = folder
    # The single-sample reporter must not flood the Actions job summary.
    previous = os.environ.pop("GITHUB_STEP_SUMMARY", None)
    try:
        rc = monitor.run()
        path = folder / "report.json"
        if not path.exists():
            raise RuntimeError("No report generated for this sample")
        report = json.loads(path.read_text(encoding="utf-8"))
        report["collector_mode"] = "continuous"
        report["scheduled_slot_utc"] = monitor.fmt(slot)
        report["sample_start_utc"] = monitor.fmt(checked)
        report["sample_lateness_seconds"] = round((checked-slot).total_seconds(), 3)
        report["exit_code"] = rc
    except Exception as exc:
        report = {
            "checked_at_utc": monitor.fmt(checked),
            "sample_start_utc": monitor.fmt(checked),
            "scheduled_slot_utc": monitor.fmt(slot),
            "collector_mode": "continuous",
            "api_success": False,
            "sources": {},
            "comparison": None,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    finally:
        if previous is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = previous
    write_json(folder / "report.json", report)
    stats = report.get("comparison") or {}
    entry = {
        "slot_utc": monitor.fmt(slot),
        "checked_at_utc": report["checked_at_utc"],
        "api_success": report.get("api_success", False),
        "themeparks_ok": report.get("sources", {}).get("themeparks", {}).get("ok", False),
        "queue_times_ok": report.get("sources", {}).get("queue_times", {}).get("ok", False),
        "comparable": stats.get("comparable_open_wait_count", 0),
        "exact": stats.get("exact_match_count", 0),
        "within_five": stats.get("within_five_minutes_count", 0),
        "matched": stats.get("matched_count", 0),
        "status_mismatch": stats.get("status_mismatch_count", 0),
        "report_path": str(folder / "report.json"),
    }
    print("SAMPLE " + json.dumps(entry, ensure_ascii=False), flush=True)
    with (segment_dir / "samples.jsonl").open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def run(segment):
    cfg, ranges = monitor.load_config()
    zone = ZoneInfo(cfg["timezone"])
    current = monitor.now()
    # Use the actual local date of this job, not a stale scheduled event timestamp.
    today = current.astimezone(zone).date()
    selected = next(((a,b) for a,b in ranges if a.astimezone(zone).date() == today), None)
    if not selected:
        print("No configured operating day; no API requests.", flush=True)
        return 0
    opening, closing = selected
    lo = min(opening + timedelta(hours=CUTS[segment-1]), closing)
    hi = min(opening + timedelta(hours=CUTS[segment]), closing)
    # Do not occupy a runner for more than 45 minutes before its assigned segment.
    if current >= hi or current < lo-timedelta(minutes=45):
        print(f"SEGMENT_SKIP {today} segment={segment} now={monitor.fmt(current)} window={monitor.fmt(lo)}..{monitor.fmt(hi)}", flush=True)
        return 0

    step = timedelta(minutes=cfg["interval_target_minutes"])
    if step.total_seconds() < 60:
        raise ValueError("Unexpected sampling interval")
    segment_dir = ROOT / str(today) / f"segment-{segment}"
    segment_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    first_start = monitor.now()
    expected_slots = int((hi-lo).total_seconds() // step.total_seconds())
    print(f"SEGMENT_START {today} segment={segment} window={monitor.fmt(lo)}..{monitor.fmt(hi)} expected={expected_slots}", flush=True)

    def save_progress():
        report = {
            "local_date": str(today), "segment": segment,
            "segment_start_utc": monitor.fmt(lo), "segment_end_utc": monitor.fmt(hi),
            "runner_started_utc": monitor.fmt(first_start),
            "updated_utc": monitor.fmt(monitor.now()),
            "expected_slots": expected_slots,
            "actual_samples": len(entries),
            "both_success": sum(x["api_success"] for x in entries),
            "themeparks_success": sum(x["themeparks_ok"] for x in entries),
            "queue_times_success": sum(x["queue_times_ok"] for x in entries),
            "comparable_pairs": sum(x["comparable"] for x in entries),
            "exact_matches": sum(x["exact"] for x in entries),
            "within_five_matches": sum(x["within_five"] for x in entries),
            "matched_status_pairs": sum(x["matched"] for x in entries),
            "status_mismatches": sum(x["status_mismatch"] for x in entries),
            "official_app_comparison": "NOT_PERFORMED",
        }
        write_json(segment_dir / "segment_summary.json", report)
        return report

    save_progress()
    # Anchor to five-minute wall-clock slots; start immediately in the current slot,
    # then wait until the next boundary. Never backfill gaps with fictional data.
    last_slot = None
    while True:
        checked = monitor.now()
        if checked >= hi:
            break
        if checked < lo:
            time.sleep(min(30, (lo-checked).total_seconds()))
            continue
        n = int((checked-lo).total_seconds() // step.total_seconds())
        slot = lo + n*step
        if slot != last_slot:
            entries.append(sample(segment_dir, slot, checked))
            last_slot = slot
            save_progress()
        next_slot = min(hi, slot+step)
        remaining = (next_slot-monitor.now()).total_seconds()
        if remaining > 0:
            time.sleep(min(remaining, 30))
    result = save_progress()
    print("SEGMENT_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fp:
            fp.write(f"## {today} / segment {segment}\n\n")
            fp.write(f"- Actual / planned slots: {len(entries)} / {expected_slots}\n")
            fp.write(f"- Both APIs successful: {result['both_success']}\n")
            fp.write(f"- Exact wait matches: {result['exact_matches']} / {result['comparable_pairs']}\n")
            fp.write(f"- Status mismatches: {result['status_mismatches']}\n")
            fp.write("- Official App comparison not performed.\n")
    # API errors are recorded per sample. Continue subsequent segments regardless.
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment", type=int, choices=(1,2,3), required=True)
    raise SystemExit(run(parser.parse_args().segment))
