#!/usr/bin/env python3
"""Experimental real openpilot/comma UI clip renderer for review_queue.py outputs.

This stays local-only. It reads a review queue, creates a temporary Route-compatible
symlink view over copied local segment dirs, and invokes tools/clip/run.py to render
openpilot's real onroad UI (AugmentedRoadView) using qcamera + local logs.

Notes:
- Requires the repo's UI runtime deps (pyray/msgq/etc.) to work on the host.
- On macOS worktrees with Linux-built msgq artifacts, this may print dependency
  errors; use --dry-run first to see exact commands.
- Does not upload logs or clips.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])).expanduser()
ROOT = TOOLS_ROOT
OPENPILOT_REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
DATA_ROOT = Path(os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB")).expanduser()
DEFAULT_RAW_DIR = Path(os.environ.get("BRICKPILOT_LOGDRIVE_RAW_ROOT", DATA_ROOT / "imports/raw/from_comma"))
FAKE_DONGLE_ID = "69da3021e107975f"


def relpath(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT.resolve()))
    except Exception:
        return str(p)


def find_raw_route_dir(route_id: str, raw_dir: Path) -> Path | None:
    direct = raw_dir / route_id
    if direct.exists():
        return direct
    matches = sorted(raw_dir.glob(f"*_{route_id}"))
    return matches[-1] if matches else None


def route_segments_dir(raw_route: Path) -> Path:
    segs = raw_route / "segments"
    return segs if segs.exists() else raw_route


def event_segments(ev: dict[str, Any]) -> list[int]:
    sync = ev.get("video_sync") or {}
    start = int(sync.get("clip_start_segment_index", ev.get("segment_index", 0)) or 0)
    end = int(sync.get("clip_end_segment_index", start) or start)
    return list(range(start, end + 1))


def ensure_route_view(ev: dict[str, Any], queue_dir: Path, raw_dir: Path) -> tuple[str, Path]:
    route_id = ev.get("route_id") or ""
    if not route_id:
        raise RuntimeError("event has no route_id")
    raw_route = find_raw_route_dir(route_id, raw_dir)
    if not raw_route:
        raise RuntimeError(f"no local raw route dir for {route_id} under {raw_dir}")
    seg_root = route_segments_dir(raw_route)
    cache = queue_dir / ".openpilot_ui_route_cache" / route_id
    cache.mkdir(parents=True, exist_ok=True)
    canonical = f"{FAKE_DONGLE_ID}|{route_id}"
    for seg in event_segments(ev):
        src = seg_root / f"{route_id}--{seg}"
        if not src.exists():
            prefixed = list(seg_root.glob(f"*_{route_id}--{seg}"))
            src = prefixed[-1] if prefixed else src
        if not src.exists():
            raise RuntimeError(f"missing segment dir for {route_id} segment {seg}: {src}")
        dst = cache / f"{canonical}--{seg}"
        if dst.exists() or dst.is_symlink():
            continue
        os.symlink(src.resolve(), dst, target_is_directory=True)
    return canonical, cache


def output_path(queue_dir: Path, ev: dict[str, Any]) -> Path:
    event_id = ev.get("event_id") or f"event_{ev.get('queue_rank', 'x')}"
    return queue_dir / "openpilot_ui_clips" / f"{event_id}.mp4"


def build_command(ev: dict[str, Any], queue_dir: Path, raw_dir: Path, big: bool, qcam: bool) -> tuple[list[str], Path]:
    canonical, cache = ensure_route_view(ev, queue_dir, raw_dir)
    sync = ev.get("video_sync") or {}
    start = int(math.floor(float(sync.get("clip_start_route_time_sec", ev.get("start_route_time_sec", 0)) or 0)))
    end = int(math.ceil(float(sync.get("clip_end_route_time_sec", ev.get("end_route_time_sec", start + 10)) or start + 10)))
    if end <= start:
        end = start + 1
    out = output_path(queue_dir, ev)
    out.parent.mkdir(parents=True, exist_ok=True)
    runner = [shutil.which("uv") or sys.executable]
    if Path(runner[0]).name == "uv":
        runner += ["run", "python"]
    cmd = [
        *runner,
        str(OPENPILOT_REPO_ROOT / "tools/clip/run.py"),
        canonical,
        "-s", str(start),
        "-e", str(end),
        "-o", str(out),
        "-d", str(cache),
        "--no-metadata",
        "--no-time-overlay",
    ]
    if big:
        cmd.append("--big")
    if qcam:
        cmd.append("--qcam")
    return cmd, out


def select_events(events: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = events
    if args.event_id:
        selected = [e for e in selected if e.get("event_id") == args.event_id]
    if args.rank is not None:
        selected = [e for e in selected if int(e.get("queue_rank", -1)) == args.rank]
    selected = [e for e in selected if e.get("video_clip_path")]
    return selected[: args.limit]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queue_json", type=Path, help="path to review queue.json")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--event-id")
    ap.add_argument("--rank", type=int)
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--execute", action="store_true", help="actually run tools/clip/run.py; otherwise print commands only")
    ap.add_argument("--no-big", action="store_true", help="use mici/smaller UI instead of big comma-style UI")
    ap.add_argument("--fcam", action="store_true", help="use fcamera instead of qcamera")
    ap.add_argument("--update-queue", action="store_true", help="write openpilot_ui_clip_path back into queue.json after successful render")
    args = ap.parse_args()

    queue_path = args.queue_json.resolve()
    queue_dir = queue_path.parent
    data = json.loads(queue_path.read_text(encoding="utf-8"))
    events = data.get("events") or []
    selected = select_events(events, args)
    if not selected:
        raise SystemExit("No matching events with video clips found")

    changed = False
    for ev in selected:
        cmd, out = build_command(ev, queue_dir, args.raw_dir.resolve(), big=not args.no_big, qcam=not args.fcam)
        print("\n#", ev.get("event_id"), "rank", ev.get("queue_rank"))
        print("PYTHONPATH=" + shlex_quote(str(ROOT)) + " " + " ".join(shlex_quote(x) for x in cmd))
        if not args.execute:
            continue
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(TOOLS_ROOT), str(OPENPILOT_REPO_ROOT), env["PYTHONPATH"]]) if env.get("PYTHONPATH") else os.pathsep.join([str(TOOLS_ROOT), str(OPENPILOT_REPO_ROOT)])
        res = subprocess.run(cmd, cwd=OPENPILOT_REPO_ROOT, env=env)
        if res.returncode != 0:
            raise SystemExit(res.returncode)
        if out.exists() and out.stat().st_size > 0:
            ev["openpilot_ui_clip_path"] = relpath(out)
            changed = True
            print("rendered", relpath(out))
    if args.execute and args.update_queue and changed:
        queue_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("updated", relpath(queue_path))


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(str(s))


if __name__ == "__main__":
    main()
