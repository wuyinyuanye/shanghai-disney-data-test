#!/usr/bin/env python3
"""Two-day, park-hours Shanghai Disneyland API consistency monitor, stdlib only.

IMPORTANT: compares two third-party published queue values, NOT Disney's official app.
Saves independent raw responses, retrieval times, queue state, and same-sample matches.
One invocation = one sample; GitHub Actions manages the cadence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib import error, request

CONFIG_FILE = Path(__file__).with_name("config.json")
OUT = Path(os.environ.get("PROBE_OUTPUT", "probe-output"))
CORE_RIDES = (
    "TRON Lightcycle Power Run",
    "Zootopia: Hot Pursuit",
    "Seven Dwarfs Mine Train",
    "Soaring Over the Horizon",
    "Pirates of the Caribbean Battle for the Sunken Treasure",
)


def now():
    return datetime.now(timezone.utc)


def fmt(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def load_config():
    value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    zone = ZoneInfo(value["timezone"])
    ranges = []
    for item in value["test_days"]:
        day = item["date"]
        start = datetime.fromisoformat(f"{day}T{item['open']}:00").replace(tzinfo=zone).astimezone(timezone.utc)
        end = datetime.fromisoformat(f"{day}T{item['close']}:00").replace(tzinfo=zone).astimezone(timezone.utc)
        if end <= start:
            raise ValueError(f"Invalid opening and closing: {item}")
        ranges.append((start, end))
    if len(ranges) != 2 or ranges != sorted(ranges):
        raise ValueError("Exactly two chronological operating days required")
    if ranges[0][1] >= ranges[1][0]:
        raise ValueError("Overlapping operating windows")
    return value, ranges


def get_window_state(moment=None):
    config, ranges = load_config()
    moment = moment or now()
    active = any(start <= moment < end for start, end in ranges)
    expired = moment >= ranges[-1][1]
    return config, ranges, active, expired


def gate():
    config, ranges, active, expired = get_window_state()
    moment = now()
    outputs = {
        "active": str(active).lower(),
        "expired": str(expired).lower(),
        "now_utc": fmt(moment),
    }
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fp:
            for key, val in outputs.items():
                fp.write(f"{key}={val}\n")
    print(json.dumps({
        "park_time_zone": config["timezone"],
        "daytime_sessions_utc": [{"start": fmt(a), "end": fmt(b)} for a, b in ranges],
        **outputs,
    }, ensure_ascii=False))
    return 0


def retrieve(url):
    for attempt in range(1, 4):
        start = time.monotonic()
        try:
            headers = {
                "Accept": "application/json",
                "User-Agent": "ShanghaiDisneyQueueTest/1.0 (research only)"
            }
            with request.urlopen(request.Request(url, headers=headers), timeout=18) as response:
                body = response.read(3_000_000)
                code = response.status
            result = json.loads(body)
            return result, {"ok": True, "http_code": code, "attempts": attempt,
                            "received_at_utc": fmt(now()),
                            "elapsed_seconds": round(time.monotonic() - start, 3),
                            "response_bytes": len(body)}
        except error.HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f"HTTP {exc.code} at {url}") from exc
            time.sleep(min(2 * attempt, 8))
        except (error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
            if attempt == 3:
                raise RuntimeError(f"{type(exc).__name__} at {url}: {exc}") from exc
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError("Unexpected retry exhaustion")


def normalize(name):
    return " ".join(re.sub("[^a-z0-9]+", " ", str(name or "").lower()).split())


def themeparks_rows(obj):
    return [
        {"name": x.get("name"), "id": x.get("id"),
         "status": x.get("status"), "open": x.get("status") == "OPERATING",
         "wait_minutes": ((x.get("queue") or {}).get("STANDBY") or {}).get("waitTime"),
         "last_updated": x.get("lastUpdated")}
        for x in obj.get("liveData", []) if isinstance(x, dict) and x.get("entityType") == "ATTRACTION"
    ]


def queue_times_rows(obj):
    rides = [x for x in obj.get("rides", []) if isinstance(x, dict)]
    for land in obj.get("lands", []):
        rides.extend(x for x in land.get("rides", []) if isinstance(x, dict))
    return [
        {"name": x.get("name"), "id": x.get("id"), "open": x.get("is_open"),
         "wait_minutes": x.get("wait_time"), "last_updated": x.get("last_updated")}
        for x in rides
    ]


def compare(tp, qt):
    by_name = {normalize(x["name"]): x for x in qt if x["name"]}
    pairs = []
    for ride in tp:
        other = by_name.get(normalize(ride["name"]))
        if not other:
            continue
        comparable = (ride["open"] and other["open"]
                      and type(ride["wait_minutes"]) is int
                      and type(other["wait_minutes"]) is int)
        pair = {
            "name": ride["name"],
            "themeparks_wait": ride["wait_minutes"],
            "queue_times_wait": other["wait_minutes"],
            "themeparks_open": ride["open"],
            "queue_times_open": other["open"],
            "themeparks_updated": ride["last_updated"],
            "queue_times_updated": other["last_updated"],
            "status_match": ride["open"] == other["open"],
            "comparable": comparable,
            "difference_minutes": abs(ride["wait_minutes"] - other["wait_minutes"])
            if comparable else None,
        }
        pairs.append(pair)
    measured = [x for x in pairs if x["comparable"]]
    core = [x for x in pairs if any(s.lower() in x["name"].lower() for s in CORE_RIDES)]
    return {
        "themeparks_attraction_count": len(tp),
        "queue_times_ride_count": len(qt),
        "matched_count": len(pairs),
        "comparable_open_wait_count": len(measured),
        "exact_match_count": sum(x["difference_minutes"] == 0 for x in measured),
        "within_five_minutes_count": sum(x["difference_minutes"] <= 5 for x in measured),
        "different_over_15_minutes_count": sum(x["difference_minutes"] > 15 for x in measured),
        "status_mismatch_count": sum(not x["status_match"] for x in pairs),
        "unmatched_themeparks_names": [x["name"] for x in tp if normalize(x["name"]) not in by_name],
        "core_rides": core,
        "pairs": pairs,
    }


def summary(report):
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if not target:
        return
    stats = report.get("comparison") or {}
    lines = [
        "## Shanghai Disneyland two-day daytime test",
        f"- Time (UTC): {report['checked_at_utc']}",
        f"- ThemeParks.wiki: {report['sources'].get('themeparks', {}).get('ok', False)}",
        f"- Queue-Times: {report['sources'].get('queue_times', {}).get('ok', False)}",
        f"- Matched attractions: {stats.get('matched_count', 0)}",
        f"- Comparable operating waits: {stats.get('comparable_open_wait_count', 0)}",
        f"- Exact matches: {stats.get('exact_match_count', 0)}",
        f"- Status mismatches: {stats.get('status_mismatch_count', 0)}",
        "- **These are two third-party feeds, not a comparison against the Disney official app.**",
    ]
    with open(target, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg, ranges, active, expired = get_window_state()
    checked = now()
    if not active:
        print(json.dumps({
            "skipped": "outside Shanghai Disneyland operating hours",
            "checked_at_utc": fmt(checked),
            "expired": expired,
        }, ensure_ascii=False))
        return 0
    report = {
        "checked_at_utc": fmt(checked),
        "window_start_utc": fmt(ranges[0][0]),
        "window_end_utc": fmt(ranges[-1][1]),
        "park_name": cfg["park_name"],
        "sources": {}, "comparison": None,
        "official_app_comparison": "NOT_PERFORMED",
    }
    urls = {
        "themeparks": f"https://api.themeparks.wiki/v1/entity/{cfg['park_id']}/live",
        "queue_times": f"https://queue-times.com/parks/{cfg['queue_times_park_id']}/queue_times.json",
    }
    raw = {}
    for name, url in urls.items():
        try:
            payload, metadata = retrieve(url)
            if name == "themeparks" and not isinstance(payload.get("liveData"), list):
                raise ValueError("ThemeParks.wiki liveData array missing")
            if name == "queue_times" and not isinstance(payload.get("lands"), list):
                raise ValueError("Queue-Times lands array missing")
            raw[name] = payload
            report["sources"][name] = {"url": url, **metadata}
            (OUT / f"{name}_raw.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            report["sources"][name] = {"url": url, "ok": False,
                                       "error": f"{type(exc).__name__}: {exc}",
                                       "failed_at_utc": fmt(now())}

    if len(raw) == 2:
        tp, qt = themeparks_rows(raw["themeparks"]), queue_times_rows(raw["queue_times"])
        report["comparison"] = compare(tp, qt)
        report["comparison"]["themeparks_closed_with_stored_wait"] = sum(
            not row["open"] and type(row["wait_minutes"]) is int for row in tp
        )
    report["completed_at_utc"] = fmt(now())
    report["api_success"] = len(raw) == 2
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "checked_at_utc": report["checked_at_utc"],
        "api_success": report["api_success"],
        "sources": report["sources"],
        "comparison": {k: v for k, v in (report["comparison"] or {}).items()
                       if k != "pairs"},
    }, ensure_ascii=False, indent=2))
    summary(report)
    return 0 if report["api_success"] else 1


def main():
    parser = argparse.ArgumentParser(description="Compare two Shanghai Disneyland queue feeds")
    parser.add_argument("--gate", action="store_true", help="GitHub Actions start/end window")
    args = parser.parse_args()
    return gate() if args.gate else run()


if __name__ == "__main__":
    sys.exit(main())
