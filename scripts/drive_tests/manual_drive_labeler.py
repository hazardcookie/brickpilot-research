#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a local full-drive manual labeler for Brickpilot drive-test routes.

This is intentionally additive and local-only. It does not alter review_queue
outputs. It can optionally sync camera artifacts plus qlog/rlog from an off-road
comma over SSH, generate a full-drive video, and emit a tiny localhost UI that
appends manual labels to JSONL files.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2]))
OPENPILOT_REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", Path(__file__).resolve().parents[2].parent / "brickpilot")).expanduser()
sys.path.insert(0, str(ROOT))
if str(OPENPILOT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(OPENPILOT_REPO_ROOT))

from scripts.drive_tests import review_queue as rq  # noqa: E402

DEFAULT_DATA_ROOT = Path(os.environ.get("BRICKPILOT_DRIVE_DATA_ROOT", os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB"))).expanduser()
DEFAULT_OUT_BASE = Path(os.environ.get("BRICKPILOT_LABELER_OUT_BASE", DEFAULT_DATA_ROOT / "labeler_outputs"))
DEFAULT_RAW_DIR = Path(os.environ.get("BRICKPILOT_LOGDRIVE_RAW_ROOT", DEFAULT_DATA_ROOT / "imports/raw/from_comma"))
MANUAL_LABELER_VERSION = "0.3.25.0"
MANUAL_LABELER_SCHEMA_VERSION = 2
SEGMENT_LEN_SEC = rq.SEGMENT_LEN_SEC
RIDE_TYPES = ("test drive", "label validation", "normal drive")
RIDE_METADATA_VALUES = RIDE_TYPES  # backwards-compatible alias


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def source_metadata() -> dict[str, Any]:
    def git_value(*args: str) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=OPENPILOT_REPO_ROOT, text=True, capture_output=True, timeout=5)
            if proc.returncode == 0:
                return proc.stdout.strip()
        except Exception:
            pass
        return ""

    return {
        "tool": "manual_drive_labeler.py",
        "tool_version": MANUAL_LABELER_VERSION,
        "schema_version": MANUAL_LABELER_SCHEMA_VERSION,
        "source_commit": git_value("rev-parse", "HEAD"),
        "source_branch": git_value("branch", "--show-current"),
        "source_dirty": bool(git_value("status", "--porcelain", "--untracked-files=no")),
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def list_routes() -> list[dict[str, Any]]:
    routes = []
    for p in sorted(rq.DEFAULT_LOGDRIVE_DIR.glob("*")):
        if p.is_dir() and (p / "catalog.yaml").exists():
            r = rq.route_from_logdrive_dir(p)
            segs = sorted(segment_indices_with_any_artifact(r))
            r["local_segment_count"] = len(segs)
            r["local_segments"] = segs
            r["has_results_sqlite"] = (p / "results.sqlite").exists()
            routes.append(r)
    return routes


def pick_route(label_or_id: str | None) -> dict[str, Any]:
    routes = list_routes()
    if not routes:
        raise SystemExit("No logdrive routes found; set BRICKPILOT_LOGDRIVE_RAW_ROOT or import routes into BrickpilotDriveDB")
    if not label_or_id or label_or_id == "latest":
        return routes[-1]
    needle = label_or_id.lower()
    matches = [r for r in routes if needle in str(r.get("route_label", "")).lower() or needle in str(r.get("analysis_name", "")).lower() or needle in str(r.get("route_id", "")).lower()]
    if len(matches) != 1:
        print("Matching routes:" if matches else "No unique route match. Available routes:")
        for i, r in enumerate(matches or routes, 1):
            print(f"{i:2d}. {r['route_label']}  route_id={r.get('route_id','')}  sqlite={r.get('has_results_sqlite')}  local_segments={r.get('local_segment_count')}")
        raise SystemExit("Pass --route with a unique label/name/route_id substring")
    return matches[0]


def segment_indices_with_any_artifact(route: dict[str, Any]) -> set[int]:
    route_id = str(route.get("route_id") or "")
    out: set[int] = set()
    roots: list[Path] = []
    if route.get("local_segments_dir"):
        roots.append(Path(str(route["local_segments_dir"])))
    if route_id:
        roots.append(DEFAULT_RAW_DIR / route_id / "segments")
        roots += [p.parent for p in DEFAULT_RAW_DIR.glob(f"*/segments/{route_id}--0")]
    for root in roots:
        if not root.exists():
            continue
        for p in root.glob(f"{route_id}--*") if route_id else root.glob("*"):
            try:
                out.add(int(p.name.rsplit("--", 1)[-1]))
            except ValueError:
                pass
    return out


def route_duration(route: dict[str, Any]) -> float:
    sqlite_path = Path(route["analysis_dir"]) / "results.sqlite"
    best = 0.0
    if sqlite_path.exists():
        try:
            con = sqlite3.connect(sqlite_path)
            for table in ("longitudinal_timeseries", "lateral_timeseries"):
                try:
                    row = con.execute(f"SELECT MAX(route_time_sec) FROM {table}").fetchone()
                    if row and row[0] is not None:
                        best = max(best, float(row[0]))
                except sqlite3.Error:
                    pass
            con.close()
        except Exception:
            pass
    segs = segment_indices_with_any_artifact(route)
    if segs:
        best = max(best, (max(segs) + 1) * SEGMENT_LEN_SEC)
    return best


def run_sync_from_comma(route: dict[str, Any], ssh: str, remote_root: str, dry_run: bool = False) -> None:
    """Pull qcamera/qlog/rlog artifacts for this route from comma realdata."""
    route_id = str(route.get("route_id") or "")
    if not route_id:
        raise SystemExit("Cannot sync: selected route has no route_id in catalog.yaml")
    if not re.fullmatch(r"[0-9a-fA-F]{8}--[A-Za-z0-9]{10}", route_id):
        raise SystemExit(f"Refusing comma sync for unexpected route_id format: {route_id!r}")
    dest = DEFAULT_RAW_DIR / route_id / "segments"
    dest.mkdir(parents=True, exist_ok=True)
    # Keep this simple and inspectable: ask remote shell for matching segment dirs,
    # then rsync only camera artifacts plus qlog/rlog into the local ignored raw tree.
    find_cmd = f"find {shlex.quote(remote_root)} -maxdepth 1 -type d -name {shlex.quote('*' + route_id + '--*')} -print | sort"
    proc = subprocess.run(["ssh", ssh, find_cmd], text=True, capture_output=True, timeout=40)
    if proc.returncode != 0:
        raise SystemExit(f"ssh find failed: {proc.stderr.strip() or proc.stdout.strip()}")
    remote_dirs = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
    if not remote_dirs:
        raise SystemExit(f"No comma realdata segment dirs found for route_id {route_id} under {remote_root}")
    for rdir in remote_dirs:
        seg_name = Path(rdir).name
        local_seg = dest / seg_name
        local_seg.mkdir(parents=True, exist_ok=True)
        include = ["--include=*/", "--include=qcamera.*", "--include=fcamera.*", "--include=dcamera.*", "--include=ecamera.*", "--include=qlog.zst", "--include=rlog.zst", "--exclude=*"]
        cmd = ["rsync", "-azP", *include, f"{ssh}:{rdir}/", f"{local_seg}/"]
        print(" ".join(cmd))
        if not dry_run:
            subprocess.run(cmd, check=True, timeout=180)


def camera_segments(route: dict[str, Any]) -> list[dict[str, Any]]:
    segs = []
    for seg in sorted(segment_indices_with_any_artifact(route)):
        cam = rq.find_camera_file(route, seg)
        if cam:
            segs.append({"segment_index": seg, "path": cam, "relpath": rq.relpath(cam)})
    return segs



def route_wall_clock(route: dict[str, Any], duration_sec: float | None = None) -> dict[str, Any]:
    """Best-effort local wall-clock route range for matching external voice/video files."""
    files = []
    for seg in sorted(segment_indices_with_any_artifact(route)):
        cam = rq.find_camera_file(route, seg)
        if cam and cam.exists():
            files.append(cam)
    if not files:
        return {}
    start_ts = min(f.stat().st_mtime for f in files)
    if duration_sec and math.isfinite(duration_sec) and duration_sec > 0:
        end_ts = start_ts + float(duration_sec)
    else:
        end_ts = max(f.stat().st_mtime for f in files) + SEGMENT_LEN_SEC
    start = dt.datetime.fromtimestamp(start_ts).astimezone()
    end = dt.datetime.fromtimestamp(end_ts).astimezone()
    return {
        "route_start_wall_time": start.isoformat(),
        "route_end_wall_time": end.isoformat(),
        "route_wall_time_source": "local camera file mtimes",
    }

def make_full_video(route: dict[str, Any], out_dir: Path, force: bool = False) -> tuple[str, str]:
    cams = camera_segments(route)
    if not cams:
        return "", "no local camera files found; run with --sync-from-comma while comma is reachable/off-road"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "", "ffmpeg not found"
    video = out_dir / "full_drive_qcamera.mp4"
    if video.exists() and video.stat().st_size > 0 and not force:
        return rq.relpath(video), "already exists"
    with tempfile.NamedTemporaryFile("w", suffix=".ffconcat", delete=False) as f:
        concat = Path(f.name)
        for c in cams:
            f.write(f"file '{c['path'].resolve()}'\n")
    try:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-an", str(video)]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=600)
        if proc.returncode != 0 or not video.exists() or video.stat().st_size == 0:
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-an", str(video)]
            subprocess.run(cmd, check=True, timeout=1200)
    finally:
        concat.unlink(missing_ok=True)
    return rq.relpath(video), f"created from {len(cams)} camera segment(s)"


def video_sync_segments(route: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    video_t = 0.0
    for cam in camera_segments(route):
        dur = rq._video_duration_sec(Path(cam["path"]))
        if not math.isfinite(dur) or dur <= 0:
            dur = SEGMENT_LEN_SEC
        seg = int(cam["segment_index"])
        rows.append({
            "segment_index": seg,
            "route_start_sec": round(seg * SEGMENT_LEN_SEC, 3),
            "video_start_sec": round(video_t, 3),
            "duration_sec": round(dur, 3),
            "source": cam["relpath"],
        })
        video_t += dur
    return rows


def sample_rows(rows: list[dict[str, Any]], max_rows: int) -> list[dict[str, Any]]:
    if len(rows) <= max_rows:
        return rows
    stride = math.ceil(len(rows) / max_rows)
    return rows[::stride]


def telemetry(route: dict[str, Any], max_rows: int = 1400) -> dict[str, Any]:
    db = Path(route["analysis_dir"]) / "results.sqlite"
    if not db.exists():
        samples = telemetry_from_local_logs(route, max_rows)
        return {"status": "local log fallback" if samples else "results.sqlite missing and no local carState samples", "source": "local copied rlog/qlog", "samples": samples, "sample_count": len(samples)}
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        long_cols = rq._table_columns(con, "longitudinal_timeseries") if "longitudinal_timeseries" in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")] else set()
        lat_cols = rq._table_columns(con, "lateral_timeseries") if "lateral_timeseries" in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")] else set()
        keep_long = [c for c in ["route_time_sec", "speed_mph", "a_ego_mps2", "accel_cmd", "set_speed_mps", "speed_deficit_mph", "lead_status", "lead_d_rel_m", "lead_v_rel_mps", "gas_pressed", "brake_pressed", "long_active"] if c in long_cols]
        keep_lat = [c for c in ["route_time_sec", "lateral_error", "torque_output", "carcontrol_torque", "steering_angle_deg", "steering_pressed", "pinned"] if c in lat_cols]
        long_rows = [dict(r) for r in con.execute(f"SELECT {', '.join(keep_long)} FROM longitudinal_timeseries ORDER BY route_time_sec")] if keep_long else []
        lat_rows = [dict(r) for r in con.execute(f"SELECT {', '.join(keep_lat)} FROM lateral_timeseries ORDER BY route_time_sec")] if keep_lat else []
        con.close()
    except Exception as e:
        return {"status": f"sqlite read failed: {e}", "samples": []}
    base = long_rows or lat_rows
    samples = []
    for r in sample_rows(base, max_rows):
        rt = rq.fnum(r.get("route_time_sec"))
        if not math.isfinite(rt):
            continue
        lat = rq._nearest_by_time(lat_rows, rt, 0.35) if long_rows else r
        s = {"t": round(rt, 3)}
        for k, v in r.items():
            if k != "route_time_sec":
                s[k] = round(v, 4) if isinstance(v, float) else v
        if "set_speed_mps" in s and s["set_speed_mps"] not in (None, ""):
            s["set_speed_mph"] = round(float(s["set_speed_mps"]) * rq.MPH_PER_MPS, 1)
        if lat:
            for k, v in lat.items():
                if k != "route_time_sec":
                    s[k] = round(v, 4) if isinstance(v, float) else v
        samples.append(s)
    if samples:
        return {"status": "ok", "source": rq.relpath(db), "samples": samples, "sample_count": len(samples)}
    fallback = telemetry_from_local_logs(route, max_rows)
    return {"status": "local log fallback" if fallback else "no timeseries samples", "source": "local copied rlog/qlog", "samples": fallback, "sample_count": len(fallback)}


def telemetry_from_local_logs(route: dict[str, Any], max_rows: int = 1400) -> list[dict[str, Any]]:
    """Compact timeline telemetry fallback for fresh local-only imports.

    The analysis DB builder still prefers comma route API identifiers, so newly
    copied local routes can have full rlog/qlog files but no results.sqlite. For
    manual labeling, the timeline only needs coarse speed/activity/lead samples;
    extract those directly from local rlogs/qlogs via review_queue's cached raw
    segment reader.
    """
    route_id = str(route.get("route_id") or "")
    rows: list[dict[str, Any]] = []
    for seg in sorted(segment_indices_with_any_artifact(route)):
        paths = rq._segment_log_candidates(route_id, seg, ("rlog.zst", "qlog.zst"))
        if not paths:
            continue
        raw = rq._read_radar_segment(sorted(paths, key=lambda p: 0 if p.name == "rlog.zst" else 1)[0], seg)
        car_rows = raw.get("car_rows") or []
        radar_rows = raw.get("radar_rows") or []
        plan_rows = raw.get("plan_rows") or []
        for car in car_rows:
            rt = rq.fnum(car.get("route_time_sec"))
            if not math.isfinite(rt):
                continue
            radar = rq._nearest_raw(radar_rows, rt, 0.35) or {}
            lead = radar.get("leadOne") or {}
            plan = rq._nearest_raw(plan_rows, rt, 0.35) or {}
            rows.append({
                "t": round(rt, 3),
                "speed_mph": car.get("vEgo_mph"),
                "a_ego_mps2": car.get("aEgo_mps2"),
                "set_speed_mph": car.get("vCruise_mph"),
                "accel_cmd": plan.get("accel0_mps2"),
                "lead_status": bool(lead.get("status")),
                "lead_d_rel_m": lead.get("dRel_m"),
                "lead_v_rel_mps": lead.get("vRel_mps"),
                "gas_pressed": bool(car.get("gasPressed")),
                "brake_pressed": bool(car.get("brakePressed")),
                "steering_pressed": bool(car.get("steeringPressed")),
            })
    return sample_rows(sorted(rows, key=lambda r: rq.fnum(r.get("t"), 0)), max_rows)


def local_log_bookmarks(route: dict[str, Any]) -> list[dict[str, Any]]:
    """Read user/bookmark button markers directly from copied local logs.

    Fresh local imports may not have bookmark CSV/tag sidecars yet. The rlogs do
    contain `userBookmark`/`bookmarkButton`; surface them so the timeline can
    show the same purple pins even before full analysis artifacts exist.
    """
    route_id = str(route.get("route_id") or "")
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    try:
        from openpilot.tools.lib.logreader import LogReader  # type: ignore
    except Exception:
        return out
    for seg in sorted(segment_indices_with_any_artifact(route)):
        paths = rq._segment_log_candidates(route_id, seg, ("rlog.zst", "qlog.zst"))
        if not paths:
            continue
        path = sorted(paths, key=lambda p: 0 if p.name == "rlog.zst" else 1)[0]
        raw = rq._read_radar_segment(path, seg)
        car_rows = raw.get("car_rows") or []
        radar_rows = raw.get("radar_rows") or []
        try:
            first_mono: int | None = None
            for msg in LogReader(str(path), sort_by_time=True, only_union_types=True):
                if first_mono is None:
                    first_mono = int(msg.logMonoTime)
                try:
                    typ = msg.which()
                except Exception:
                    continue
                if typ not in {"userBookmark", "bookmarkButton"}:
                    continue
                rt = rq._route_time_from_log_mono(int(msg.logMonoTime), first_mono, seg)
                # userBookmark and bookmarkButton usually arrive as a pair; keep
                # one clean pin per human action.
                key = (round(rt * 2), route_id)
                if key in seen:
                    continue
                seen.add(key)
                car = rq._nearest_raw(car_rows, rt, 2.0) or {}
                radar = rq._nearest_raw(radar_rows, rt, 2.0) or {}
                label = "bookmark"
                accel = rq.fnum(car.get("aEgo_mps2"), 0)
                if car.get("brakePressed") or accel < -0.45:
                    label = "brake"
                elif car.get("gasPressed") or accel > 0.45:
                    label = "accel"
                elif car.get("steeringPressed"):
                    label = "steer"
                elif (radar.get("leadOne") or {}).get("status"):
                    label = "lead"
                out.append({
                    "id": f"local_log_bookmark:{route_id}:{seg}:{round(rt, 3)}",
                    "t": round(rt, 3),
                    "reason": label,
                    "label": label,
                    "tags": [label] if label != "bookmark" else [],
                    "source": typ,
                    "segment": seg,
                    "editable": True,
                })
        except Exception:
            continue
    return sorted(out, key=lambda b: rq.fnum(b.get("t"), 0))


def events_and_bookmarks(route: dict[str, Any]) -> dict[str, Any]:
    events = []
    for category, fname in rq.EVENT_FILES.items():
        for i, row in enumerate(read_csv(Path(route["analysis_dir"]) / fname), 1):
            t = rq.event_time(row)
            if not math.isfinite(t):
                continue
            events.append({
                "id": f"{category}:{i}", "type": category, "t": round(t, 3),
                "end_t": round(rq.event_end_time(row), 3),
                "segment": rq.segment_index(row),
                "summary": row.get("notes") or row.get("reason") or row.get("intervention_type") or row.get("event_type") or category,
                "metrics": {k: v for k, v in row.items() if v not in (None, "") and k in {"max_speed_deficit_mph", "final_speed_deficit_mph", "duration_sec", "lead_status", "lead_limited", "driver_gas_or_brake", "avg_mph", "max_abs_lateral_error", "intervention_type"}},
            })
    bookmarks = []
    seen_bookmark_times: set[tuple[float, str]] = set()
    for i, x in enumerate(rq.load_route_labels(str(route.get("route_id") or "")), 1):
        t = rq.fnum(x.get("route_time_sec"))
        if not math.isfinite(t):
            continue
        label = x.get("reason") or ",".join(x.get("tags") or []) or x.get("source") or "bookmark"
        key = (round(t, 2), str(label))
        seen_bookmark_times.add(key)
        bookmarks.append({
            "id": "route_label:" + str(round(t, 3)) + ":" + str(x.get("source", "")) + ":" + str(x.get("reason", "")) + ":" + str(i),
            "t": round(t, 3),
            "reason": x.get("reason", ""),
            "label": label,
            "tags": x.get("tags", []),
            "source": x.get("source", ""),
            "segment": x.get("segment_index"),
            "editable": True,
        })
    for i, row in enumerate(read_csv(Path(route["analysis_dir"]) / "bookmarks.csv"), 1):
        t = rq.fnum(row.get("route_time_sec"))
        if not math.isfinite(t):
            continue
        label = row.get("source") or row.get("reason") or "in-car bookmark"
        key = (round(t, 2), str(label))
        if key in seen_bookmark_times:
            continue
        bookmarks.append({
            "id": "bookmark_csv:" + str(round(t, 3)) + ":" + str(row.get("source", "")) + ":" + str(row.get("segment_index", "")) + ":" + str(i),
            "t": round(t, 3),
            "reason": row.get("reason", ""),
            "label": label,
            "tags": [],
            "source": row.get("source", "bookmarks.csv"),
            "segment": rq.segment_index(row),
            "editable": True,
        })
    if not bookmarks:
        for b in local_log_bookmarks(route):
            key = (round(rq.fnum(b.get("t"), 0), 2), str(b.get("label") or b.get("reason") or "bookmark"))
            if key in seen_bookmark_times:
                continue
            seen_bookmark_times.add(key)
            bookmarks.append(b)
    collapsed: list[dict[str, Any]] = []
    for b in sorted(bookmarks, key=lambda x: x["t"]):
        if collapsed and abs(rq.fnum(b.get("t"), 0) - rq.fnum(collapsed[-1].get("t"), 0)) < 0.15:
            # The car often records bookmarkButton and userBookmark a few ms apart; show one clean marker.
            prev = collapsed[-1]
            if not prev.get("reason") and b.get("reason"):
                prev["reason"] = b.get("reason")
            if prev.get("label") in {"bookmarkButton", "bookmark"} and b.get("label"):
                prev["label"] = b.get("label")
            prev["source"] = "+".join(x for x in [str(prev.get("source") or ""), str(b.get("source") or "")] if x)
            continue
        collapsed.append(b)
    return {"events": sorted(events, key=lambda x: x["t"]), "bookmarks": collapsed}


def gps_track(route: dict[str, Any], max_rows: int = 1000) -> dict[str, Any]:
    rows = []
    for seg in sorted(segment_indices_with_any_artifact(route)):
        qlog = None
        for p in rq._segment_log_candidates(str(route.get("route_id") or ""), seg, ("qlog.zst", "rlog.zst")):
            qlog = p; break
        if not qlog:
            continue
        data = rq._read_gps_segment(qlog)
        rows.extend(data.get("samples") or [])
    samples = [{"t": s.get("route_time_sec"), "lat": s.get("lat"), "lon": s.get("lon"), "speed_mps": s.get("speed_mps"), "bearing_deg": s.get("bearing_deg")} for s in sample_rows(sorted(rows, key=lambda x: rq.fnum(x.get("route_time_sec"), 0)), max_rows)]
    return {"status": "ok" if samples else "no local GPS samples", "samples": samples, "sample_count": len(samples), "privacy_note": "Local logs are not uploaded; OSM tiles are fetched by the browser when the map is viewed."}


def radar_track(route: dict[str, Any], max_rows: int = 900) -> dict[str, Any]:
    rows = []
    route_id = str(route.get("route_id") or "")
    for seg in sorted(segment_indices_with_any_artifact(route)):
        paths = rq._segment_log_candidates(route_id, seg, ("rlog.zst", "qlog.zst"))
        if not paths:
            continue
        data = rq._read_radar_segment(sorted(paths, key=lambda p: 0 if p.name == "rlog.zst" else 1)[0], seg)
        radar_rows = data.get("radar_rows") or []
        live_rows = data.get("live_rows") or []
        base = radar_rows or live_rows
        for r in base:
            rt = rq.fnum(r.get("route_time_sec"))
            live = r if "points" in r else rq._nearest_raw(live_rows, rt, 0.35)
            lead = (r if "leadOne" in r else rq._nearest_raw(radar_rows, rt, 0.25)) or {}
            pts = []
            for p in (live or {}).get("points", [])[:24]:
                if rq.fnum(p.get("dRel_m"), -1) >= 0:
                    pts.append({"d": p.get("dRel_m"), "y": p.get("yRel_m") if p.get("yRel_m") is not None else 0, "v": p.get("vRel_mps")})
            rows.append({"t": round(rt, 3), "leadOne": lead.get("leadOne"), "leadTwo": lead.get("leadTwo"), "points": pts})
    samples = sample_rows(sorted(rows, key=lambda x: rq.fnum(x.get("t"), 0)), max_rows)
    return {"status": "ok" if samples else "no local radar/liveTracks samples", "samples": samples, "sample_count": len(samples)}


KNOWN_CAN_HINTS: dict[tuple[int, int], str] = {
    # src/address hints are intentionally conservative. Unknown frames stay as
    # red 0x names in the UI until labels/CAN mining give stronger evidence.
    (0, 0x04A): "IMU / inertial family",
    (0, 0x065): "brake / longitudinal family",
    (0, 0x0A0): "wheel-speed family",
    (0, 0x0BA): "vehicle dynamics family",
    (0, 0x0E0): "steering / chassis family",
    (0, 0x0FA): "powertrain periodic family",
    (0, 0x130): "gear shifter / drivetrain family",
    (0, 0x170): "brake / regen family",
    (0, 0x260): "vehicle speed / wheel-speed family",
    (0, 0x371): "brake / regen family",
    (0, 0x386): "pedal / longitudinal state family",
    (0, 0x420): "SCC / lead-control family",
    (1, 0x1cf): "radar/SCC target family",
}


def can_telemetry(route: dict[str, Any], max_rows: int = 9000) -> dict[str, Any]:
    """Compact raw CAN stream for synchronized manual review overlay.

    This keeps local-only raw addresses/data visible without claiming semantics.
    To avoid multi-hundred-MB HTML payloads, keep one representative CAN batch
    about every 100ms and cap each batch to the first 48 frames.
    """
    route_id = str(route.get("route_id") or "")
    rows: list[dict[str, Any]] = []
    try:
        from openpilot.tools.lib.logreader import LogReader  # type: ignore
    except Exception as exc:
        return {"status": f"LogReader unavailable: {exc}", "samples": [], "sample_count": 0}

    for seg in sorted(segment_indices_with_any_artifact(route)):
        paths = rq._segment_log_candidates(route_id, seg, ("rlog.zst", "qlog.zst"))
        if not paths:
            continue
        path = sorted(paths, key=lambda p: 0 if p.name == "rlog.zst" else 1)[0]
        first_mono: int | None = None
        last_emit_t = -1e9
        try:
            for msg in LogReader(str(path), sort_by_time=True, only_union_types=True):
                if first_mono is None:
                    first_mono = int(msg.logMonoTime)
                try:
                    typ = msg.which()
                except Exception:
                    continue
                if typ != "can":
                    continue
                rt = rq._route_time_from_log_mono(int(msg.logMonoTime), first_mono, seg)
                if rt - last_emit_t < 0.10:
                    continue
                frames = []
                for i, f in enumerate(msg.can):
                    if i >= 48:
                        break
                    try:
                        addr = int(f.address)
                        src = int(f.src)
                        dat = bytes(f.dat).hex().upper()
                    except Exception:
                        continue
                    hint = KNOWN_CAN_HINTS.get((src, addr)) or KNOWN_CAN_HINTS.get((0, addr)) or ""
                    frames.append({
                        "name": f"0x{addr:X}",
                        "addr": addr,
                        "src": src,
                        "data": dat,
                        "hint": hint,
                        "unknown": not bool(hint),
                    })
                if frames:
                    rows.append({"t": round(rt, 3), "segment": seg, "frames": frames})
                    last_emit_t = rt
        except Exception:
            continue
    rows = sample_rows(sorted(rows, key=lambda r: rq.fnum(r.get("t"), 0)), max_rows)
    return {
        "status": "ok" if rows else "no local CAN samples",
        "source": "local copied rlog/qlog raw CAN; addresses are unverified unless hinted",
        "sample_period_sec": 0.10,
        "samples": rows,
        "sample_count": len(rows),
        "legend": {"raw_color": "neutral observed raw frame", "unknown_color": "red only for future suspicious/unclassified candidates", "known_hint": "conservative local hint only; not road-facing evidence"},
    }


def slugify(value: str, fallback: str = "route") -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in (value or fallback))[:80]
    return slug.strip("_") or fallback


def route_order_key(route: dict[str, Any]) -> tuple[int, str]:
    route_id = str(route.get("route_id") or "")
    head = route_id.split("--", 1)[0]
    try:
        return (int(head, 16), route_id)
    except ValueError:
        return (-1, route_id)


def prior_label_validation_jobs() -> list[tuple[Path, dict[str, Any]]]:
    """Previously generated/marked label-validation manual review jobs."""
    jobs: list[tuple[Path, dict[str, Any]]] = []
    for jobs_path in DEFAULT_OUT_BASE.glob("manual_drive_labeler*/drive_jobs.json"):
        try:
            obj = json.loads(jobs_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for job in obj.get("jobs") or []:
            ride_type = str(job.get("ride_type") or job.get("ride_metadata") or "")
            route_id = str(job.get("route_id") or "")
            if ride_type == "label validation" and route_id:
                jobs.append((jobs_path.parent, job))
    return jobs


def label_validation_route_ids() -> set[str]:
    """Route IDs from previously generated/marked label-validation jobs."""
    return {str(job.get("route_id")) for _, job in prior_label_validation_jobs() if job.get("route_id")}


def route_review_payload(route: dict[str, Any], job_dir: Path, args: argparse.Namespace, *, sync: bool = False, ride_type: str = "label validation") -> dict[str, Any]:
    if ride_type not in RIDE_TYPES:
        raise SystemExit(f"ride_type must be one of: {', '.join(RIDE_TYPES)}")
    if sync and args.sync_from_comma:
        run_sync_from_comma(route, args.comma_ssh, args.comma_remote_root, args.dry_run_sync)
    video_path, video_status = ("", "video disabled") if args.no_video else make_full_video(route, job_dir, args.force_video)
    eb = events_and_bookmarks(route)
    video_sync = video_sync_segments(route)
    telemetry_data = telemetry(route, args.max_telemetry_samples)
    map_data = gps_track(route, args.max_map_samples)
    radar_data = radar_track(route, args.max_radar_samples)
    can_data = can_telemetry(route, args.max_can_samples)
    duration_candidates: list[float] = []
    duration_candidates += [rq.fnum(s.get("t"), 0) for s in telemetry_data.get("samples", [])]
    duration_candidates += [rq.fnum(e.get("t"), 0) for e in eb["events"]]
    duration_candidates += [rq.fnum(b.get("t"), 0) for b in eb["bookmarks"]]
    if video_sync:
        duration_candidates.append(max(rq.fnum(s.get("route_start_sec"), 0) + rq.fnum(s.get("duration_sec"), 0) for s in video_sync))
    actual_duration = max([x for x in duration_candidates if math.isfinite(x) and x > 0] or [route_duration(route)])
    wall_clock = route_wall_clock(route, actual_duration)
    return {
        "schema_version": MANUAL_LABELER_SCHEMA_VERSION,
        "tool_version": MANUAL_LABELER_VERSION,
        "source": source_metadata(),
        "route": {k: (rq.relpath(v) if isinstance(v, Path) else v) for k, v in route.items() if k != "local_segments"},
        "duration_sec": round(actual_duration, 3),
        "ride_type": ride_type,
        "ride_metadata": ride_type,  # legacy/UI alias for label-validation inboxes
        "video_path": video_path,
        "video_status": video_status,
        "video_sync": video_sync,
        "telemetry": telemetry_data,
        "events": eb["events"],
        "bookmarks": eb["bookmarks"],
        "map": map_data,
        "radar": radar_data,
        "can": can_data,
        "labels": {"drive_jsonl": "drive_labels.jsonl", "phev_jsonl": "phev_labels.jsonl"},
        "privacy": {"local_only": True, "raw_logs_not_uploaded": True, "generated_dir_gitignored": True},
        **wall_clock,
    }


def write_ui(out_dir: Path) -> None:
    (out_dir / "manual_label_server.py").write_text(SERVER_PY, encoding="utf-8")
    (out_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (out_dir / "README.md").write_text(README_MD.format(out=rq.relpath(out_dir)), encoding="utf-8")


def build(args: argparse.Namespace) -> Path:
    routes = list_routes()
    if not routes:
        raise SystemExit("No logdrive routes found; set BRICKPILOT_LOGDRIVE_RAW_ROOT or import routes into BrickpilotDriveDB")
    selected = pick_route(args.route)
    selected_route_id = str(selected.get("route_id") or selected.get("analysis_name") or selected.get("route_label") or "")
    label_validation_ids = label_validation_route_ids()
    inbox_routes = []
    seen_route_ids: set[str] = set()
    sorted_routes = sorted(routes, key=route_order_key)
    for candidate in sorted_routes:
        rid = str(candidate.get("route_id") or candidate.get("analysis_name") or candidate.get("route_label") or "")
        if rid in seen_route_ids:
            continue
        if label_validation_ids and str(candidate.get("route_id") or "") not in label_validation_ids:
            continue
        seen_route_ids.add(rid)
        inbox_routes.append(candidate)
    if not inbox_routes:
        for candidate in reversed(sorted_routes):
            rid = str(candidate.get("route_id") or candidate.get("analysis_name") or candidate.get("route_label") or "")
            if rid in seen_route_ids:
                continue
            seen_route_ids.add(rid)
            inbox_routes.append(candidate)
            if len(inbox_routes) >= 3:
                break
        inbox_routes = list(reversed(inbox_routes))
    if selected not in inbox_routes:
        inbox_routes = [*inbox_routes, selected]

    slug = slugify(str(selected.get("analysis_name") or selected.get("route_label") or "route"))
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_BASE / f"manual_drive_labeler_{slug}"
    jobs_dir = out_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[dict[str, Any]] = []
    for route in inbox_routes:
        job_slug = slugify(str(route.get("analysis_name") or route.get("route_label") or route.get("route_id") or "route"))
        job_dir = jobs_dir / job_slug
        job_dir.mkdir(parents=True, exist_ok=True)
        payload = route_review_payload(route, job_dir, args, sync=(route is selected))
        (job_dir / "drive_data.json").write_text(jdump(payload) + "\n", encoding="utf-8")
        for name in ("drive_labels.jsonl", "phev_labels.jsonl", "bookmark_edits.jsonl"):
            (job_dir / name).touch(exist_ok=True)
        if payload.get("video_path"):
            video_abs = ROOT / str(payload["video_path"])
            payload["video_url"] = os.path.relpath(video_abs, out_dir)
            (job_dir / "drive_data.json").write_text(jdump(payload) + "\n", encoding="utf-8")
        rel_data = os.path.relpath(job_dir / "drive_data.json", out_dir)
        rel_dir = os.path.relpath(job_dir, out_dir)
        jobs.append({
            "job_id": job_slug,
            "route_label": route.get("route_label"),
            "analysis_name": route.get("analysis_name"),
            "route_id": route.get("route_id"),
            "ride_type": payload.get("ride_type", "label validation"),
            "ride_metadata": payload.get("ride_type", "label validation"),  # legacy UI alias
            "data_path": rel_data,
            "job_dir": rel_dir,
            "duration_sec": payload.get("duration_sec"),
            "route_start_wall_time": payload.get("route_start_wall_time"),
            "route_end_wall_time": payload.get("route_end_wall_time"),
            "route_wall_time_source": payload.get("route_wall_time_source"),
            "video_status": payload.get("video_status"),
            "event_count": len(payload.get("events") or []),
            "bookmark_count": len(payload.get("bookmarks") or []),
            "selected": str(route.get("route_id") or route.get("analysis_name") or route.get("route_label") or "") == selected_route_id,
        })
    # If some prior label-validation drives are not present in this repo's current
    # logdrive catalog (common when older data was generated on another host),
    # carry their generated job directories forward so the inbox still shows all
    # validation drives instead of silently dropping back to the newest three.
    existing_route_ids = {str(j.get("route_id") or "") for j in jobs}
    existing_job_ids = {str(j.get("job_id") or "") for j in jobs}
    for prior_root, prior_job in prior_label_validation_jobs():
        route_id = str(prior_job.get("route_id") or "")
        if not route_id or route_id in existing_route_ids:
            continue
        source_dir = (prior_root / str(prior_job.get("job_dir") or "")).resolve()
        if not (source_dir == prior_root.resolve() or prior_root.resolve() in source_dir.parents) or not source_dir.is_dir():
            continue
        job_slug = slugify(str(prior_job.get("job_id") or prior_job.get("route_label") or route_id))
        base_slug = job_slug
        n = 2
        while job_slug in existing_job_ids:
            job_slug = f"{base_slug}_{n}"
            n += 1
        job_dir = jobs_dir / job_slug
        shutil.copytree(source_dir, job_dir, dirs_exist_ok=True)
        rel_data = os.path.relpath(job_dir / "drive_data.json", out_dir)
        rel_dir = os.path.relpath(job_dir, out_dir)
        carried = {**prior_job, "job_id": job_slug, "data_path": rel_data, "job_dir": rel_dir, "selected": False}
        jobs.append(carried)
        existing_route_ids.add(route_id)
        existing_job_ids.add(job_slug)

    jobs.sort(key=lambda x: (not x.get("selected"), str(x.get("route_label") or "")))
    (out_dir / "drive_jobs.json").write_text(jdump({"schema_version": MANUAL_LABELER_SCHEMA_VERSION, "source": source_metadata(), "jobs": jobs, "default_job_id": next((j["job_id"] for j in jobs if j.get("selected")), jobs[0]["job_id"] if jobs else ""), "ride_type_allowed": list(RIDE_TYPES), "ride_metadata_allowed": list(RIDE_TYPES)}) + "\n", encoding="utf-8")

    # Back-compat: keep the selected job's data/JSONL files at the output root too.
    selected_job = next((j for j in jobs if j.get("selected")), jobs[0])
    selected_data = json.loads((out_dir / selected_job["data_path"]).read_text(encoding="utf-8"))
    (out_dir / "drive_data.json").write_text(jdump(selected_data) + "\n", encoding="utf-8")
    for name in ("drive_labels.jsonl", "phev_labels.jsonl", "bookmark_edits.jsonl"):
        (out_dir / name).touch(exist_ok=True)
    write_ui(out_dir)
    (out_dir / "build_manifest.json").write_text(jdump({"schema_version": MANUAL_LABELER_SCHEMA_VERSION, "source": source_metadata(), "privacy": {"local_only": True, "raw_logs_not_uploaded": True, "generated_outputs_gitignored": True}, "out_dir": rq.relpath(out_dir), "job_count": len(jobs), "default_job_id": selected_job["job_id"], "jobs": jobs}) + "\n", encoding="utf-8")
    return out_dir

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-routes", action="store_true", help="List local ingested logdrive routes and exit")
    ap.add_argument("--route", default="latest", help="Route label/name/id substring, or latest")
    ap.add_argument("--out-dir", default="", help="Output dir (default $BRICKPILOT_LABELER_OUT_BASE/manual_drive_labeler_<route>, outside repo)")
    ap.add_argument("--sync-from-comma", action="store_true", help="SSH/rsync camera+log artifacts from comma realdata before building")
    ap.add_argument("--dry-run-sync", action="store_true", help="Print rsync commands but do not copy")
    ap.add_argument("--comma-ssh", default="comma@192.168.1.138")
    ap.add_argument("--comma-remote-root", default="/data/media/0/realdata")
    ap.add_argument("--no-video", action="store_true", help="Do not concatenate a full-drive video")
    ap.add_argument("--force-video", action="store_true", help="Regenerate full_drive_qcamera.mp4 if present")
    ap.add_argument("--ride-type", choices=RIDE_TYPES, default="label validation", help="Ride purpose metadata stored in drive_data.json")
    ap.add_argument("--max-telemetry-samples", type=int, default=1400)
    ap.add_argument("--max-map-samples", type=int, default=1000)
    ap.add_argument("--max-radar-samples", type=int, default=900)
    ap.add_argument("--max-can-samples", type=int, default=9000)
    args = ap.parse_args()
    if args.list_routes:
        for i, r in enumerate(list_routes(), 1):
            print(f"{i:2d}. {r['route_label']}  name={r['analysis_name']}  route_id={r.get('route_id','')}  sqlite={r.get('has_results_sqlite')}  local_segments={r.get('local_segment_count')}")
        return
    out = build(args)
    print(f"Built manual drive labeler: {rq.relpath(out)}")
    print(f"Run: cd {out} && python3 manual_label_server.py --port 8770")
    print("Open: http://127.0.0.1:8770/")


SERVER_PY = '#!/usr/bin/env python3\nfrom __future__ import annotations\nimport argparse, datetime as dt, json, os, subprocess, sys, urllib.error, urllib.parse, urllib.request\nfrom http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler\nfrom pathlib import Path\nROOT=Path(__file__).resolve().parent\nDATASET_DIR=ROOT.parent/\'validated_manual_labels\'; DATASET_JSON=DATASET_DIR/\'validated_label_dataset.json\'; DATASET_HISTORY=DATASET_DIR/\'validated_label_dataset_history.jsonl\'\nDB_API_URL=\'\'; DB_API_TOKEN=\'\'; DB_ENABLED=False\n\ndef read_json(p,d):\n  try:\n    return json.loads(p.read_text()) if p.exists() and p.stat().st_size else d\n  except Exception: return d\n\ndef iter_jsonl(p):\n  out=[]\n  if p.exists():\n    for line in p.read_text(errors=\'replace\').splitlines():\n      try: out.append(json.loads(line))\n      except Exception: pass\n  return out\n\ndef safe_job_dir(job_id):\n  for j in read_json(ROOT/\'drive_jobs.json\',{\'jobs\':[]}).get(\'jobs\',[]):\n    if j.get(\'job_id\')==job_id:\n      d=(ROOT/str(j.get(\'job_dir\',\'\'))).resolve()\n      if d==ROOT.resolve() or ROOT.resolve() in d.parents: return d\n  raise ValueError(\'unknown job_id\')\n\ndef headers(extra=None):\n  h={\'accept\':\'application/json\'}\n  if DB_API_TOKEN: h.update({\'authorization\':\'Bearer \'+DB_API_TOKEN,\'x-brickpilot-token\':DB_API_TOKEN})\n  if extra: h.update(extra)\n  return h\n\ndef db_req(method,path,payload=None):\n  if not DB_ENABLED: raise RuntimeError(\'DB API not configured\')\n  data=None if payload is None else json.dumps(payload).encode()\n  req=urllib.request.Request(DB_API_URL+path, data=data, method=method, headers=headers({\'content-type\':\'application/json\'} if data is not None else None))\n  try:\n    with urllib.request.urlopen(req,timeout=30) as r:\n      raw=r.read(); ct=r.headers.get(\'content-type\',\'\')\n      return json.loads(raw.decode() or \'{}\') if \'json\' in ct else raw\n  except urllib.error.HTTPError as e:\n    raw=e.read().decode(errors=\'replace\')\n    try: body=json.loads(raw)\n    except Exception: body={\'error\':raw or str(e)}\n    body.setdefault(\'status\',e.code); raise RuntimeError(json.dumps(body))\n\ndef db_get(p): return db_req(\'GET\',p)\ndef db_post(p,d): return db_req(\'POST\',p,d)\n\ndef parse_jsonish(v, default):\n  if v is None: return default\n  if isinstance(v,(list,dict)): return v\n  try: return json.loads(v)\n  except Exception: return default\n\n_LEGACY_CACHE={}\ndef legacy_payload_for_route(route_id):\n  if not route_id: return None\n  global _LEGACY_CACHE\n  if not _LEGACY_CACHE:\n    roots=[ROOT.parent, Path(os.environ.get("BRICKPILOT_LABELER_OUTPUT_ROOT", str(Path.home()/"BrickpilotDriveDB"/"labeler_outputs"))).expanduser()]\n    roots += [p for p in (Path(os.environ.get("BRICKPILOT_LABELER_OUTPUT_ROOT", str(Path.home()/"BrickpilotDriveDB"/"labeler_outputs"))).expanduser()/"recovered_legacy").glob(\'*\') if p.is_dir()]\n    for root in roots:\n      if not root.exists(): continue\n      for base in sorted(root.glob(\'manual_drive_labeler_*\'), reverse=True):\n        for fp in [base/\'drive_data.json\', *base.glob(\'jobs/*/drive_data.json\')]:\n          try:\n            obj=json.loads(fp.read_text())\n          except Exception:\n            continue\n          rid=(obj.get(\'route\') or {}).get(\'route_id\')\n          if rid and rid not in _LEGACY_CACHE:\n            _LEGACY_CACHE[rid]=obj\n  return _LEGACY_CACHE.get(route_id)\n\ndef miles_from_samples(samples):\n  total=0.0; prev=None\n  for sm in sorted(samples or [], key=lambda x: _fnum(x.get(\'t\') or x.get(\'t_sec\'))):\n    t=_fnum(sm.get(\'t\') or sm.get(\'t_sec\'), None); mph=_fnum(sm.get(\'speed_mph\'), None)\n    if t is None or mph is None:\n      continue\n    if prev is not None:\n      pt,pmph=prev; dt=max(0,min(5,t-pt)); total += ((pmph+mph)/2.0)*dt/3600.0\n    prev=(t,mph)\n  return round(total,2) if total>0 else None\n\ndef legacy_miles(payload):\n  if not payload: return None\n  for key in (\'distance_miles\',\'route_miles\',\'miles\'):\n    v=payload.get(key)\n    if v not in (None,\'\'):\n      try: return round(float(v),2)\n      except Exception: pass\n  return miles_from_samples(((payload.get(\'telemetry\') or {}).get(\'samples\') or []))\n\ndef label_from_db(r):\n  return {\'id\':r.get(\'id\'),\'version\':r.get(\'version\'),\'review_job_id\':r.get(\'review_job_id\'),\'route_uuid\':r.get(\'route_uuid\'),\'job_id\':\'db:\'+str(r.get(\'review_job_id\') or \'\'),\'route_id\':r.get(\'route_uuid\'),\'label_kind\':r.get(\'label_kind\') or \'drive\',\'label\':r.get(\'label\') or \'other\',\'severity\':r.get(\'severity\') or \'reviewed\',\'start_time_sec\':r.get(\'start_sec\') or 0,\'end_time_sec\':r.get(\'end_sec\') or r.get(\'start_sec\') or 0,\'notes\':r.get(\'notes\') or \'\',\'tags\':parse_jsonish(r.get(\'tags\'),[])}\n\ndef jobs_payload():\n  jobs=[]\n  for inbox in db_get(\'/api/review/inboxes\').get(\'inboxes\',[]):\n    if inbox.get(\'name\') != \'logdrive_label_validation\':\n      continue\n    for j in db_get(f"/api/review/inboxes/{inbox[\'id\']}/jobs").get(\'jobs\',[]):\n      rid=j.get(\'route_id\') or j.get(\'route_uuid\')\n      legacy=legacy_payload_for_route(rid)\n      jobs.append({\'job_id\':\'db:\'+str(j[\'id\']),\'db\':True,\'review_job_id\':j[\'id\'],\'job_version\':j.get(\'version\'),\'inbox_id\':inbox[\'id\'],\'inbox_name\':inbox.get(\'name\'),\'route_uuid\':j.get(\'route_uuid\'),\'route_id\':rid,\'route_label\':j.get(\'route_label\') or j.get(\'canonical_name\') or rid,\'analysis_name\':inbox.get(\'name\'),\'ride_type\':j.get(\'ride_type\') or \'label validation\',\'ride_metadata\':j.get(\'ride_type\') or \'label validation\',\'duration_sec\':j.get(\'duration_sec\') or (legacy or {}).get(\'duration_sec\') or 0,\'route_start_wall_time\':j.get(\'started_at\') or (legacy or {}).get(\'route_start_wall_time\') or \'\',\'route_end_wall_time\':j.get(\'ended_at\') or (legacy or {}).get(\'route_end_wall_time\') or \'\',\'route_miles\':legacy_miles(legacy),\'data_path\':\'/api/job-data?job_id=db:\'+str(j[\'id\'])})\n  return {\'jobs\':jobs,\'default_job_id\':jobs[0][\'job_id\'] if jobs else \'\', \'source\':\'db\'}\n\ndef find_job(job_id):\n  if not str(job_id).startswith(\'db:\'): return None\n  jid=int(str(job_id).split(\':\',1)[1])\n  return next((j for j in jobs_payload()[\'jobs\'] if j[\'review_job_id\']==jid),None)\n\ndef _fnum(v, default=0):\n  try: return float(v)\n  except Exception: return default\n\ndef _duration(j, tl):\n  vals=[_fnum(j.get(\'duration_sec\'))]\n  vals += [_fnum(a.get(\'start_sec\'))+_fnum(a.get(\'duration_sec\')) for a in tl.get(\'artifacts\',[])]\n  vals += [_fnum(v.get(\'route_start_sec\'))+_fnum(v.get(\'duration_sec\')) for v in tl.get(\'video_sync\',[])]\n  vals += [_fnum(x.get(\'t\') or x.get(\'t_sec\')) for x in (tl.get(\'telemetry\') or {}).get(\'samples\',[])]\n  vals += [_fnum(x.get(\'end_t\') or x.get(\'end_t_sec\') or x.get(\'t\') or x.get(\'t_sec\')) for x in tl.get(\'events\',[])]\n  vals += [_fnum(x.get(\'end_sec\') or x.get(\'t_sec\')) for x in tl.get(\'bookmarks\',[])]\n  return max([v for v in vals if v and v > 0], default=0)\n\ndef event_from_db(e):\n  return {\'id\':e.get(\'id\'),\'t\':e.get(\'t\') or e.get(\'t_sec\') or 0,\'end_t\':e.get(\'end_t\') or e.get(\'end_t_sec\'),\'type\':e.get(\'type\') or e.get(\'event_type\') or e.get(\'source\') or \'event\',\'label\':e.get(\'summary\') or e.get(\'type\') or e.get(\'event_type\') or \'event\',\'reason\':e.get(\'summary\') or \'\', \'severity\':e.get(\'severity\') or \'\', \'raw\':e.get(\'raw_jsonb\')}\n\ndef sample_from_db(s):\n  return {\'t\':s.get(\'t\') or s.get(\'t_sec\') or 0,\'speed_mph\':s.get(\'speed_mph\'),\'set_speed_mph\':s.get(\'set_speed_mph\'),\'a_ego_mps2\':s.get(\'a_ego_mps2\'),\'gas_pressed\':s.get(\'gas_pressed\'),\'brake_pressed\':s.get(\'brake_pressed\'),\'lead_status\':s.get(\'lead_status\'),\'lead_d_rel_m\':s.get(\'lead_d_rel_m\'),\'lead_v_rel_mps\':s.get(\'lead_v_rel_mps\')}\n\ndef job_data(job_id):\n  j=find_job(job_id)\n  if not j: raise ValueError(\'unknown DB job\')\n  tl=db_get(\'/api/routes/\'+urllib.parse.quote(j[\'route_uuid\'])+\'/timeline\')\n  legacy=legacy_payload_for_route(j.get(\'route_id\'))\n  if legacy:\n    if not ((tl.get(\'telemetry\') or {}).get(\'samples\')):\n      tl[\'telemetry\']=legacy.get(\'telemetry\') or {\'samples\': []}\n    if not tl.get(\'video_sync\'):\n      tl[\'video_sync\']=legacy.get(\'video_sync\') or []\n    if not tl.get(\'events\'):\n      tl[\'events\']=legacy.get(\'events\') or []\n    if not tl.get(\'bookmarks\'):\n      tl[\'bookmarks\']=legacy.get(\'bookmarks\') or []\n    if not tl.get(\'can\') or not (tl.get(\'can\') or {}).get(\'samples\'):\n      tl[\'can\']=legacy.get(\'can\') or tl.get(\'can\') or {\'samples\': []}\n  arts=tl.get(\'artifacts\',[]); video=next((a for a in arts if a.get(\'kind\') in (\'full_drive_video\',\'clip\')),None) or next((a for a in arts if a.get(\'kind\') in (\'qcamera\',\'fcamera\',\'ecamera\')),None)\n  bookmarks=[{\'id\':b.get(\'id\'),\'t\':b.get(\'t_sec\') or b.get(\'t\') or 0,\'end_t\':b.get(\'end_sec\') or b.get(\'end_t\'),\'type\':\'bookmark\',\'label\':b.get(\'text\') or b.get(\'label\') or b.get(\'source\') or \'bookmark\',\'reason\':b.get(\'text\') or b.get(\'reason\') or \'\', \'tags\':parse_jsonish(b.get(\'tags\'),[])} for b in tl.get(\'bookmarks\',[])]\n  labels=[label_from_db(x) for x in tl.get(\'labels\',[]) if x.get(\'review_job_id\') in (None,j[\'review_job_id\'])]\n  samples=[sample_from_db(x) for x in (tl.get(\'telemetry\') or {}).get(\'samples\',[])]\n  return {\'route\':{\'route_id\':j.get(\'route_id\'),\'route_uuid\':j.get(\'route_uuid\'),\'route_label\':j.get(\'route_label\')},\'duration_sec\':_duration(j,tl) or (legacy or {}).get(\'duration_sec\') or 0,\'route_start_wall_time\':j.get(\'route_start_wall_time\') or (legacy or {}).get(\'route_start_wall_time\') or \'\',\'route_end_wall_time\':j.get(\'route_end_wall_time\') or (legacy or {}).get(\'route_end_wall_time\') or \'\',\'route_miles\':legacy_miles(legacy) or miles_from_samples(samples),\'video_status\':\'ok\' if video else ((legacy or {}).get(\'video_status\') or \'missing video artifact\'),\'video_url\':(\'/api/artifact-proxy/\'+str(video[\'id\'])) if video else \'\',\'video_sync\':tl.get(\'video_sync\',[]),\'video_fps\':20,\'events\':[event_from_db(e) for e in tl.get(\'events\',[])],\'bookmarks\':bookmarks,\'labels\':labels,\'artifacts\':arts,\'telemetry\':{\'samples\':samples},\'radar\':(legacy or {}).get(\'radar\') or {\'samples\':[]},\'can\':tl.get(\'can\') or {\'samples\':[]}}\n\ndef write_dataset(payload):\n  DATASET_DIR.mkdir(parents=True,exist_ok=True); ds=read_json(DATASET_JSON,{\'schema_version\':1,\'reviews\':{}}); ds.setdefault(\'reviews\',{})\n  now=dt.datetime.now(dt.timezone.utc).isoformat(); rid=str(payload.get(\'review_id\') or payload.get(\'route_id\') or payload.get(\'job_id\'))\n  entry={\'review_id\':rid,\'job_id\':payload.get(\'job_id\'),\'route_id\':payload.get(\'route_id\'),\'route_label\':payload.get(\'route_label\') or \'\', \'ride_type\':payload.get(\'ride_type\') or \'label validation\',\'finished_at\':now,\'label_count\':len(payload.get(\'labels\') or []),\'labels\':payload.get(\'labels\') or [],\'notes\':payload.get(\'notes\') or \'\', \'privacy\':{\'local_only\':True}}\n  ds[\'reviews\'][rid]=entry; DATASET_JSON.write_text(json.dumps(ds,indent=2,sort_keys=True)+\'\\n\'); DATASET_HISTORY.open(\'a\').write(json.dumps({\'saved_at\':now,\'review_id\':rid,\'action\':\'finish\',\'entry\':entry},sort_keys=True)+\'\\n\'); return entry\n\nclass Handler(SimpleHTTPRequestHandler):\n  def send_json(self,code,obj):\n    data=json.dumps(obj,default=str).encode(); self.send_response(code); self.send_header(\'content-type\',\'application/json\'); self.send_header(\'cache-control\',\'no-store\'); self.send_header(\'content-length\',str(len(data))); self.end_headers(); self.wfile.write(data)\n  def do_HEAD(self):\n    try:\n      u=urllib.parse.urlparse(self.path); path=u.path\n      if DB_ENABLED and path.startswith(\'/api/artifact-proxy/\'):\n        aid=path.rsplit(\'/\',1)[1]; req=urllib.request.Request(DB_API_URL+\'/api/artifacts/\'+aid,method=\'HEAD\',headers=headers())\n        with urllib.request.urlopen(req,timeout=30) as r:\n          self.send_response(200); self.send_header(\'content-type\',r.headers.get(\'content-type\',\'application/octet-stream\'))\n          if r.headers.get(\'content-length\'): self.send_header(\'content-length\',r.headers.get(\'content-length\'))\n          self.end_headers(); return\n      return super().do_HEAD()\n    except Exception as e:\n      return self.send_json(400,{\'ok\':False,\'error\':str(e)})\n  def do_GET(self):\n    try:\n      u=urllib.parse.urlparse(self.path); qs=urllib.parse.parse_qs(u.query); path=u.path\n      if DB_ENABLED and path==\'/api/drive-jobs\': return self.send_json(200,jobs_payload())\n      if DB_ENABLED and path==\'/api/job-data\': return self.send_json(200,job_data((qs.get(\'job_id\') or [\'\'])[0]))\n      if DB_ENABLED and path.startswith(\'/api/artifact-proxy/\'):\n        aid=path.rsplit(\'/\',1)[1]; req=urllib.request.Request(DB_API_URL+\'/api/artifacts/\'+aid,headers=headers())\n        with urllib.request.urlopen(req,timeout=60) as r:\n          self.send_response(200); self.send_header(\'content-type\',r.headers.get(\'content-type\',\'application/octet-stream\')); self.end_headers(); self.wfile.write(r.read()); return\n      if path==\'/api/labels\':\n        jid=(qs.get(\'job_id\') or [\'\'])[0]\n        if DB_ENABLED and jid.startswith(\'db:\'):\n          labs=job_data(jid).get(\'labels\',[]); return self.send_json(200,{\'drive\':[x for x in labs if (x.get(\'label_kind\') or \'drive\')==\'drive\'],\'phev\':[x for x in labs if x.get(\'label_kind\')==\'phev\']})\n        root=safe_job_dir(jid) if jid else ROOT; return self.send_json(200,{\'drive\':iter_jsonl(root/\'drive_labels.jsonl\'),\'phev\':iter_jsonl(root/\'phev_labels.jsonl\')})\n      if path==\'/api/finished\':\n        if DB_ENABLED:\n          try: return self.send_json(200,{\'reviews\':db_get(\'/api/finished-reviews\').get(\'reviews\',[]),\'dataset\':\'drive_db\'})\n          except Exception: pass\n        ds=read_json(DATASET_JSON,{\'reviews\':{}}); return self.send_json(200,{\'reviews\':sorted(ds.get(\'reviews\',{}).values(),key=lambda x:x.get(\'finished_at\',\'\'),reverse=True),\'dataset\':str(DATASET_JSON)})\n      if path==\'/api/voice-sessions\':\n        voice_root=Path(os.environ.get(\'BRICKPILOT_VOICE_SESSION_ROOT\',str(Path.home()/"BrickpilotDriveDB"/"voice_bookmarks"/"sessions"))).expanduser(); sessions=[]\n        for vp in sorted(voice_root.glob(\'*\'), reverse=True)[:100]:\n          if not vp.is_dir(): continue\n          meta=read_json(vp/\'session.json\',{}) or {}; sid=meta.get(\'session_id\') or vp.name\n          if not sid: continue\n          transcript_count=len(iter_jsonl(vp/\'transcript.jsonl\'))\n          event_count=len(iter_jsonl(vp/\'events.jsonl\'))\n          sessions.append({\'id\':sid,\'name\':meta.get(\'display_title\') or meta.get(\'custom_title\') or meta.get(\'ride_type\') or sid,\'created_at\':meta.get(\'started_at_wall\'),\'ended_at\':meta.get(\'ended_at_wall\'),\'duration_sec\':meta.get(\'duration_sec\'),\'bookmark_count\':transcript_count or event_count,\'audio\':(vp/\'audio\').exists() or any(vp.glob(\'*.wav\')) or any(vp.glob(\'*.m4a\')),\'needs_transcription\':meta.get(\'needs_transcription\'),\'path\':str(vp)})\n        return self.send_json(200,{\'sessions\':sessions,\'voice_root\':str(voice_root)})\n      return super().do_GET()\n    except Exception as e:\n      status=400\n      msg=str(e)\n      try:\n        body=json.loads(msg); status=int(body.get(\'status\') or status); msg=body.get(\'error\') or msg\n      except Exception: pass\n      return self.send_json(status,{\'ok\':False,\'error\':msg})\n  def do_POST(self):\n    try:\n      n=int(self.headers.get(\'content-length\',\'0\') or 0); data=json.loads(self.rfile.read(n) or b\'{}\')\n      if self.path==\'/api/labels\':\n        if DB_ENABLED and str(data.get(\'job_id\') or \'\').startswith(\'db:\'):\n          j=find_job(data.get(\'job_id\')); payload={\'id\':data.get(\'id\'),\'expected_version\':data.get(\'expected_version\'),\'route_uuid\':j[\'route_uuid\'],\'review_job_id\':j[\'review_job_id\'],\'label_kind\':data.get(\'label_kind\') or \'drive\',\'label\':data.get(\'label\'),\'severity\':data.get(\'severity\'),\'start_sec\':data.get(\'start_time_sec\'),\'end_sec\':data.get(\'end_time_sec\'),\'notes\':data.get(\'notes\'),\'tags\':data.get(\'tags\') or [],\'metadata\':{\'manual_labeler\':True},\'created_by\':\'manual_labeler\',\'updated_by\':\'manual_labeler\'}; out=db_post(\'/api/labels\',payload); return self.send_json(200,{\'ok\':True,\'target\':payload[\'label_kind\'],\'label\':{**data,**out}})\n        root=safe_job_dir(str(data.get(\'job_id\') or \'\')) if data.get(\'job_id\') else ROOT; typ=\'phev\' if data.get(\'label_kind\')==\'phev\' else \'drive\'; data[\'saved_at\']=dt.datetime.now(dt.timezone.utc).isoformat(); (root/(\'phev_labels.jsonl\' if typ==\'phev\' else \'drive_labels.jsonl\')).open(\'a\').write(json.dumps(data,sort_keys=True)+\'\\n\'); return self.send_json(200,{\'ok\':True,\'target\':typ,\'label\':data})\n      if self.path==\'/api/delete-label\':\n        if DB_ENABLED and str(data.get(\'job_id\') or \'\').startswith(\'db:\'):\n          out=db_post(\'/api/delete-label\',{\'id\':data.get(\'id\'),\'expected_version\':data.get(\'expected_version\'),\'review_job_id\':find_job(data.get(\'job_id\'))[\'review_job_id\'],\'deleted_by\':\'manual_labeler\'}); return self.send_json(200,{\'ok\':True,**out})\n        root=safe_job_dir(str(data.get(\'job_id\') or \'\')) if data.get(\'job_id\') else ROOT; typ=\'phev\' if data.get(\'label_kind\')==\'phev\' else \'drive\'; idx=int(data.get(\'index\')); p=root/(\'phev_labels.jsonl\' if typ==\'phev\' else \'drive_labels.jsonl\'); rows=iter_jsonl(p); removed=rows.pop(idx); p.write_text(\'\'.join(json.dumps(x,sort_keys=True)+\'\\n\' for x in rows)); return self.send_json(200,{\'ok\':True,\'removed\':removed})\n      if self.path==\'/api/delete-review-job\':\n        if not (DB_ENABLED and str(data.get(\'job_id\') or \'\').startswith(\'db:\')):\n          raise ValueError(\'delete-review-job requires DB-backed job\')\n        j=find_job(data.get(\'job_id\'))\n        out=db_post(\'/api/delete-review-job\',{\'confirm\':data.get(\'confirm\') is True,\'review_job_id\':j[\'review_job_id\'],\'route_uuid\':j[\'route_uuid\'],\'expected_version\':data.get(\'expected_version\') or j.get(\'job_version\'),\'deleted_by\':\'manual_labeler\'})\n        return self.send_json(200,{\'ok\':True,**out})\n      if self.path==\'/api/finish\':\n        if DB_ENABLED and str(data.get(\'job_id\') or \'\').startswith(\'db:\'):\n          j=find_job(data.get(\'job_id\')); out=db_post(\'/api/finish-review\',{\'review_job_id\':j[\'review_job_id\'],\'route_uuid\':j[\'route_uuid\'],\'expected_version\':data.get(\'expected_version\') or j.get(\'job_version\'),\'finished_by\':\'manual_labeler\',\'label_count\':len(data.get(\'labels\') or []),\'notes\':data.get(\'notes\') or \'\', \'snapshot\':data}); return self.send_json(200,{\'ok\':True,\'review\':out,\'dataset\':\'drive_db\'})\n        return self.send_json(200,{\'ok\':True,\'review\':write_dataset(data),\'dataset\':str(DATASET_JSON)})\n      if self.path==\'/api/import-voice\':\n        if not (DB_ENABLED and str(data.get(\'job_id\') or \'\').startswith(\'db:\')): raise ValueError(\'voice import requires DB-backed job\')\n        j=find_job(data.get(\'job_id\')); sid=str(data.get(\'session_id\') or \'\')\n        script=Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", str(Path.cwd())))/"scripts"/"drive_tests"/"voice_bookmark_app.py"; py_s=os.environ.get(\'BRICKPILOT_PYTHON\',\'\'); py=Path(py_s) if py_s else None\n        cmd=[str(py if py and py.exists() else sys.executable), str(script), \'import\', \'--session\', sid, \'--review-job-id\', str(j[\'review_job_id\'])]\n        if data.get(\'offset_sec\') not in (None,\'\'):\n          cmd += [\'--offset-sec\', str(data.get(\'offset_sec\'))]\n        r=subprocess.run(cmd,capture_output=True,text=True,timeout=120)\n        if r.returncode!=0: raise RuntimeError((r.stderr or r.stdout or \'voice import failed\').strip())\n        out=json.loads(r.stdout or \'{}\'); return self.send_json(200,{\'ok\':True,\'count\':out.get(\'bookmarks\') or out.get(\'imported\') or out.get(\'count\') or 0,\'result\':out})\n      return self.send_error(404)\n    except Exception as e:\n      status=400\n      msg=str(e)\n      try:\n        body=json.loads(msg); status=int(body.get(\'status\') or status); msg=body.get(\'error\') or msg\n      except Exception: pass\n      return self.send_json(status,{\'ok\':False,\'error\':msg})\n\nif __name__==\'__main__\':\n  ap=argparse.ArgumentParser(); ap.add_argument(\'--host\',default=\'127.0.0.1\'); ap.add_argument(\'--port\',type=int,default=8770); ap.add_argument(\'--db-api-url\',default=os.environ.get(\'BRICKPILOT_DRIVE_API_URL\',\'http://127.0.0.1:8766\')); ap.add_argument(\'--db-token\',default=os.environ.get(\'BRICKPILOT_DRIVE_API_TOKEN\',\'\')); ap.add_argument(\'--db-token-file\',default=os.environ.get(\'BRICKPILOT_DRIVE_API_TOKEN_FILE\',\'\')); ap.add_argument(\'--legacy-json-only\',action=\'store_true\')\n  a=ap.parse_args(); DB_API_URL=a.db_api_url.rstrip(\'/\'); DB_API_TOKEN=a.db_token or (Path(a.db_token_file).read_text().strip() if a.db_token_file else \'\'); DB_ENABLED=bool(DB_API_URL and DB_API_TOKEN and not a.legacy_json_only)\n  print(f\'Serving manual drive labeler at http://{a.host}:{a.port}/\'); print(\'Source: \'+(\'drive DB API\' if DB_ENABLED else \'legacy JSON files\'))\n  if a.host not in (\'127.0.0.1\',\'localhost\',\'::1\'): print(\'WARNING: non-localhost bind exposes private drive data\')\n  ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()\n'
README_MD = "# Brickpilot Manual Full-Drive Labeler\n\nGenerated local-only manual drive tagging UI with an inbox for the last three ingested rides and a finished-review dataset.\n\n```bash\ncd {out}\npython3 manual_label_server.py --port 8770\n# open http://127.0.0.1:8770/\n```\n\n- Inbox rides default to `label validation` `ride_type`; allowed values are `test drive`, `label validation`, and `normal drive`.\n- Per-ride draft labels append to each job's local `drive_labels.jsonl` / `phev_labels.jsonl`.\n- Pressing **Finish review** writes/replaces that route's entry in `../validated_manual_labels/validated_label_dataset.json` and appends an audit row to `validated_label_dataset_history.jsonl`.\n- Finished reviews can be reopened, edited in the UI, and finished again to replace the corresponding validated dataset entry.\n- Raw logs/video stay local/private.\n"

INDEX_HTML = '<!doctype html><html><head><meta charset="utf-8"><title>Brickpilot manual drive labeler inbox</title><style>\nbody{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0d1117;color:#e6edf3;margin:0}button,select,input,textarea{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:8px;min-height:42px;font:inherit}select,input{height:48px}textarea{min-height:42px}button{cursor:pointer;background:#238636}button:disabled{opacity:.45;cursor:not-allowed}.pageTop{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:10px}.pageTop h1{margin:0 0 8px 0}#summary{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.routeSummaryText{display:inline-flex;align-items:center}.sectionHeader{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 12px 0}.sectionHeader h3{margin:0}#topFinish,#deleteDrive,#saveDraftLabel{font-weight:800;padding:3px 12px;min-height:0;height:24px;line-height:16px;white-space:nowrap;border-radius:999px;background:#238636;border-color:#2ea043;color:#fff;font-size:12px;box-shadow:0 0 0 1px rgba(46,160,67,.25)}#deleteDrive{background:#7d2638;border-color:#da3633;color:#ffdce0;box-shadow:0 0 0 1px rgba(218,54,51,.25)}#deleteDrive:hover{background:#a40e26;color:white}.secondary{background:#21262d}.app{display:block;min-height:100vh;opacity:1;transition:opacity .08s ease}.app.sidebarCollapsed{grid-template-columns:48px minmax(0,1fr)}body.preload .app{opacity:0;pointer-events:none}.side{border-right:1px solid #30363d;background:#111820;padding:12px;overflow:auto;position:fixed;left:0;top:0;bottom:0;width:320px;height:100vh;max-height:100vh;box-sizing:border-box;align-self:start;z-index:10}.sideTop{display:flex;align-items:center;gap:8px;margin-bottom:12px;min-width:0}.sideTop h2{margin:0;font-size:28px;line-height:1}.collapseBtn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;min-height:32px;padding:0;white-space:nowrap}.main{transition:opacity .08s ease;margin-left:320px}.app.sidebarCollapsed .main{margin-left:48px}.side.collapsed{padding:8px;overflow:hidden;width:48px}.side.collapsed>*:not(.sideTop){display:none}.side.collapsed .sideTop{margin-bottom:0}.side.collapsed .sideTop h2{display:none}.side.collapsed .sideTop{margin:0;justify-content:center}.side.collapsed #collapseBtn{width:32px}.main{padding:14px;min-width:0;width:auto;box-sizing:border-box}.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:12px;margin-bottom:12px}.job,.fin{border:1px solid #30363d;border-radius:10px;padding:8px;margin:7px 0;cursor:pointer;overflow:hidden}.job b,.fin b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}.job .small,.fin .small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.job.active{border-color:#58a6ff;background:#1f6feb22}.videoWrap{position:relative}video{width:100%;max-height:58vh;background:#000;border-radius:10px}.telemetryOverlay{position:absolute;left:14px;top:14px;background:#000a;border:1px solid #58a6ff55;border-radius:10px;padding:10px 12px;font:13px ui-monospace,Menlo,monospace;line-height:1.45;display:none;min-width:210px}.telemetryOverlay.on{display:block}.canOverlay{position:absolute;right:14px;top:14px;background:#000c;border:1px solid #58a6ff55;border-radius:10px;padding:10px 12px;font:11px ui-monospace,Menlo,monospace;line-height:1.35;display:none;width:370px;height:285px;max-width:38vw;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.35)}.canOverlay.on{display:block}.canOverlay b{color:#f0f6fc}.canRow{display:grid;grid-template-columns:44px 30px minmax(108px,1fr);gap:5px;white-space:nowrap}.canRaw{color:#7ee787}.canUnknown{color:#ff7b72}.canKnown{color:#79c0ff}.canHint{color:#f2cc60;overflow:hidden;text-overflow:ellipsis}.speedBtn.active{background:#1f6feb;border-color:#58a6ff;color:#fff}canvas{width:100%;background:#08101d;border:1px solid #2b3d59;border-radius:12px}#timeline{height:96px;cursor:crosshair;touch-action:none}#zoom{height:34px;margin-top:6px;cursor:ew-resize}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.timelineControls{margin:8px 0 7px;padding:1px 0;gap:4px;flex-wrap:nowrap;overflow:hidden}.timelineControls button,.timelineControls .badge{height:28px;min-height:28px;padding:3px 6px;font-size:11px;line-height:1;border-radius:7px;white-space:nowrap}.timelineControls button{flex:0 1 auto;min-width:0}.timelineControls .primaryAction{font-weight:750;padding-inline:8px}.timelineControls .badge{display:inline-flex;align-items:center;flex:0 0 auto}.timelineControls #time{height:24px;min-height:24px;padding:2px 6px;font-size:10.5px;line-height:1;border-radius:7px}.timelineControls #status{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.timelineLegend{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:8px;color:#8b949e;font-size:12px}.legendItem{display:inline-flex;align-items:center;gap:5px}.swatch{width:16px;height:8px;border-radius:999px;display:inline-block}.swatch.speed{background:#355377}.swatch.lead{background:#e9c46a}.swatch.gas{background:#75e0a4}.swatch.brake{background:#ff7b8a}.swatch.bookmark{background:#c084fc}.swatch.label{background:#c084fc}.swatch.tool{background:#ff4d6d}.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(520px,1fr);gap:12px;align-items:stretch;height:clamp(430px,48vh,680px)}.grid>.card{margin-bottom:0;height:100%;min-height:0;overflow:hidden}.eventsCard{display:flex;flex-direction:column;align-self:stretch;min-height:0}.eventsCard .eventlist{flex:1 1 auto;max-height:none;min-height:0;overflow:auto}.reviewCard{display:flex;flex-direction:column;min-height:0}.grid>.reviewCard{overflow:auto}.reviewCard .formgrid,.reviewCard .actions,.reviewCard .draftTitle,#save{flex:0 0 auto}.reviewCard .actions{position:sticky;bottom:0;z-index:5;background:linear-gradient(180deg,rgba(22,27,34,.88),#161b22 35%);border-top:1px solid #30363d;margin:12px -18px 0;padding:10px 18px}.reviewCard .actions button{width:100%;font-weight:850;background:#2ea043;border-color:#3fb950;color:white}.reviewCard .labels{flex:1 1 auto;min-height:70px;max-height:none;overflow:auto}@media(max-width:1000px){.app{grid-template-columns:1fr}.side{position:relative;width:auto;height:auto;max-height:none;border-right:0;border-bottom:1px solid #30363d}.main,.app.sidebarCollapsed .main{margin-left:0}.grid{grid-template-columns:1fr}}.small{font-size:12px;color:#8b949e}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#1f6feb33;margin:2px}.warn{color:#f2cc60}.ok{color:#56d364}.labels{max-height:220px;overflow:auto}.eventlist{max-height:260px;overflow:auto}.event{border-bottom:1px solid #30363d;padding:5px;cursor:pointer}.event:hover{background:#21262d}.draftRow{display:flex;align-items:flex-start;gap:8px}.draftRowText{flex:1;min-width:0}.deleteDraft{background:#3a1620;border-color:#7d2638;color:#ffb3c0;width:24px;height:24px;min-height:24px;padding:0;border-radius:999px;font-weight:900;line-height:1;flex:0 0 auto}.deleteDraft:hover{background:#7d2638;color:white}.modalBackdrop{position:fixed;inset:0;background:rgba(1,4,9,.62);backdrop-filter:blur(3px);display:none;align-items:center;justify-content:center;z-index:1000}.modalBackdrop.on{display:flex}.confirmModal{width:min(460px,calc(100vw - 32px));background:#161b22;border:1px solid #30363d;border-radius:16px;box-shadow:0 18px 55px rgba(0,0,0,.42);padding:18px}.confirmModal h3{margin:0 0 8px 0}.confirmModal p{margin:0;color:#c9d1d9;line-height:1.35}.modalActions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}.dangerBtn{background:#7d2638;border-color:#da3633;color:#ffdce0}.dangerBtn:hover{background:#a40e26}textarea,input,select{box-sizing:border-box;max-width:100%}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px 18px}.reviewCard{padding:18px}.reviewCard h3{margin-top:0}.reviewCard input,.reviewCard select,.reviewCard textarea{width:100%;background:#0b1220}.reviewCard input,.reviewCard select{height:48px;padding:0 14px}.actions{margin-top:8px}.draftTitle{margin-top:18px;border-top:1px solid #30363d;padding-top:14px}.field{display:flex;flex-direction:column;gap:5px}.field.full{grid-column:1/-1}.field label{font-weight:650;color:#c9d1d9}.notesField textarea{min-height:132px;resize:vertical}.tagsField{max-width:560px}.tagsField input{height:32px;font-size:12px;padding:6px 8px}.tagsField label{font-size:12px;color:#8b949e}.timegrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.timeInput{display:grid;grid-template-columns:minmax(0,1fr) 44px;gap:6px;align-items:center}.timeInput input{width:100%;height:48px;padding:0 14px}.nowBtn{width:44px;height:48px;min-height:48px;padding:0;border-radius:10px;background:#21262d;font-weight:800;font-size:12px;line-height:1}.timegrid input{width:100%}@media(max-width:1150px){.grid{grid-template-columns:1fr}.formgrid{grid-template-columns:1fr}}</style></head><body class="preload"><div id=app class=app><aside id=side class=side><div class=sideTop><button id=collapseBtn class="secondary collapseBtn" aria-label="Collapse inbox" title="Collapse inbox" onclick="toggleSide()"><span aria-hidden="true">☰</span></button><h2>Inbox</h2></div><div class=small>Label validation rides awaiting review.</div><div id=inbox></div><h2>Finished</h2><div id=finished></div></aside><main class=main><div class="pageTop"><div><h1>Brickpilot manual full-drive labeler</h1><div id=summary class=small></div></div></div><section class=card><div class=videoWrap><video id=v controls playsinline></video><div id=telemetryOverlay class=telemetryOverlay></div><div id=canOverlay class=canOverlay></div></div><div class="row timelineControls"><button class=secondary title="Seek back 10 seconds" onclick="seekDelta(-10)">−10s</button><button class=secondary title="Seek back one video frame" onclick="frameStep(-1)">−1 frame</button><button class=secondary title="Seek forward one video frame" onclick="frameStep(1)">+1 frame</button><button class=secondary title="Seek forward 10 seconds" onclick="seekDelta(10)">+10s</button><button class=secondary title="Jump to nearest event or bookmark" onclick="nearestEvent()">Nearest</button><button class=primaryAction title="Add label at current time" onclick="prepareLabel()">+ Label</button><button id="undoMove" class=secondary title="Undo last bookmark/label move" onclick="undoMove()" disabled>Undo</button><button id="hudToggle" class=secondary title="Toggle telemetry overlay" onclick="toggleTelemetry()">Telem</button><button id="canToggle" class=secondary title="Toggle raw CAN telemetry overlay" onclick="toggleCanTelemetry()">CAN: off</button><button class="secondary speedBtn active" id="speed1" title="1x playback" onclick="setPlaybackSpeed(1)">1x</button><button class="secondary speedBtn" id="speed05" title="0.5x playback" onclick="setPlaybackSpeed(0.5)">0.5x</button><button class="secondary speedBtn" id="speed025" title="0.25x playback" onclick="setPlaybackSpeed(0.25)">0.25x</button><button id="bookmarkToggle" class=secondary title="Toggle ride bookmarks" onclick="toggleBookmarkOverlay()">Bk: on</button><button id="toolToggle" class=secondary title="Toggle tool detections" onclick="toggleToolEvents()">Tool: off</button><button id="activityToggle" class=secondary title="Toggle activity bars" onclick="toggleActivityBars()">Act: on</button><button class=secondary title="Reset timeline zoom" onclick="resetZoom()">Reset</button><span id=time class=badge title="Current route time">0.0s</span><span id=status class=small></span></div><canvas id=timeline width=1200 height=96></canvas><canvas id=zoom width=1200 height=34></canvas><div class="timelineLegend"><span class="legendItem"><i class="swatch speed"></i>speed/activity</span><span class="legendItem"><i class="swatch lead"></i>lead present</span><span class="legendItem"><i class="swatch gas"></i>gas</span><span class="legendItem"><i class="swatch brake"></i>brake</span><span class="legendItem"><i class="swatch bookmark"></i>bookmark/reaction</span><span class="legendItem"><i class="swatch label"></i>saved manual label</span><span class="legendItem"><i class="swatch tool"></i>tool detection</span><span class="small">Drag zoom handles below to focus a time range.</span><span class="legendItem"><i class="swatch" style="background:#7ee787"></i>CAN known/observed raw</span><span class="legendItem"><i class="swatch" style="background:#79c0ff"></i>CAN tentative hint</span><span class="legendItem"><i class="swatch" style="background:#ff7b72"></i>CAN unrecognized candidate</span></div></section><div class=grid><section class="card eventsCard"><h3>Events & bookmarks</h3><div id=events class=eventlist></div></section><section class="card reviewCard"><div class="sectionHeader"><h3>Review label</h3><button id="saveDraftLabel" onclick="saveLabel()">Save draft</button></div><input id=kind type=hidden value=drive><div class=formgrid><div class=field><label>Ride type</label><select id=rideMeta><option>test drive</option><option selected>label validation</option><option>normal drive</option></select></div><div class=field><label>Label</label><select id=label><option>good_behavior</option><option>good_phev_transition</option><option>too_lazy</option><option>no_lead_lazy</option><option>lead_resume_lazy</option><option>too_eager</option><option>too_eager_surge</option><option>too_harsh_brake</option><option>late_brake</option><option>brake_blend</option><option>stop_creep_issue</option><option>steering_jerk</option><option>lead_or_traffic_limited</option><option>driver_override</option><option>ev_glide</option><option>ev_launch_lag</option><option>hev_engine_on</option><option>hev_transition</option><option>drive_mode_switch</option><option>medium_regen_charge</option><option>regen_blend</option><option>hard_brake_regen</option><option>ev_charger_plugged_in</option><option>regen_drag</option><option>engine_transition</option><option>other</option></select></div><div class=field><label>Severity/usefulness</label><select id=severity><option>reviewed</option><option>important</option><option>critical</option><option>not_useful</option></select></div><div class=field full><label>Time window</label><div class=timegrid><div class=timeInput><input id=start placeholder=start><button class="secondary nowBtn" title="Set start to current video time" onclick="markStart()">now</button></div><div class=timeInput><input id=end placeholder=end><button class="secondary nowBtn" title="Set end to current video time" onclick="markEnd()">now</button></div></div></div><div class="field full notesField"><label>Notes</label><textarea id=notes rows=6></textarea></div><div class="field full tagsField"><label>Tags CSV</label><input id=tags placeholder="optional: close_lead, uphill, engine_on"></div></div><div id=save class=small></div><h3 class="draftTitle">Voice bookmarks</h3><div class="small">Pick a voice log, then import its bookmarks into the currently open drive.</div><div class="row"><select id=voiceSession style="min-width:260px"></select><button class=secondary onclick="loadVoiceSessions()">Refresh</button><button onclick="importVoiceBookmarks()">Import to this drive</button></div><div id=voiceStatus class="small"></div><h3 class="draftTitle">Draft labels</h3><div id=saved class="labels small"></div></section></div></main></div><div id=confirmBackdrop class=modalBackdrop><div class=confirmModal role=dialog aria-modal=true aria-labelledby=confirmTitle><h3 id=confirmTitle>Delete draft label?</h3><p id=confirmText></p><div class=modalActions><button class=secondary id=confirmCancel>Cancel</button><button class=dangerBtn id=confirmOk>Delete</button></div></div></div><script>\nlet jobs=[],data=null,job=null,labels={drive:[],phev:[]},finished=[],editingReview=null,currentEditRef=null,bookmarkEdits={},showBookmarkOverlay=true,showToolEvents=false,showActivityBars=true,viewStart=0,viewEnd=null,dragZoom=null,dragTimeline=null,undoStack=[],suppressTimelineClick=false; const v=document.getElementById(\'v\'); const esc=s=>(s??\'\').toString().replace(/[&<>]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\'}[c]));\nlet pendingConfirm=null; function softConfirm(message,{title=\'Are you sure?\',ok=\'Delete\'}={}){return new Promise(resolve=>{const b=document.getElementById(\'confirmBackdrop\'),t=document.getElementById(\'confirmTitle\'),m=document.getElementById(\'confirmText\'),c=document.getElementById(\'confirmCancel\'),o=document.getElementById(\'confirmOk\'),active=document.activeElement; let settled=false; const close=value=>{if(settled)return; settled=true; b.classList.remove(\'on\'); document.removeEventListener(\'keydown\',onKey); pendingConfirm=null; c.onclick=null; o.onclick=null; b.onclick=null; if(active&&active.focus)active.focus(); resolve(value)}; const onKey=e=>{if(e.key===\'Escape\')close(false)}; t.textContent=title; m.textContent=message; o.textContent=ok; b.classList.add(\'on\'); pendingConfirm=close; c.onclick=()=>close(false); o.onclick=()=>close(true); b.onclick=e=>{if(e.target===b)close(false)}; document.addEventListener(\'keydown\',onKey); o.focus()})}\nlet telemetryOn=false,canTelemetryOn=false; function toggleSide(){document.getElementById(\'side\').classList.toggle(\'collapsed\'); document.querySelector(\'.app\').classList.toggle(\'sidebarCollapsed\'); setTimeout(()=>{drawTimeline();drawZoom()},60)} function toggleTelemetry(){telemetryOn=!telemetryOn; document.getElementById(\'telemetryOverlay\').classList.toggle(\'on\',telemetryOn); updateTelemetryOverlay()} function toggleCanTelemetry(){canTelemetryOn=!canTelemetryOn; const el=document.getElementById(\'canOverlay\'); if(el)el.classList.toggle(\'on\',canTelemetryOn); const b=document.getElementById(\'canToggle\'); if(b)b.textContent=\'CAN: \'+(canTelemetryOn?\'on\':\'off\'); updateCanOverlay()} function setPlaybackSpeed(rate){v.playbackRate=rate; for(const [id,val] of [[\'speed1\',1],[\'speed05\',0.5],[\'speed025\',0.25]]){const el=document.getElementById(id); if(el)el.classList.toggle(\'active\',Math.abs(rate-val)<0.001)}} function nearest(arr,t){let b=null,bd=1e9; for(const x of arr||[]){const d=Math.abs((+x.t||+x.start_time_sec||0)-t); if(d<bd){b=x;bd=d}} return b} function fmt(t){return (Number(t)||0).toFixed(1)+\'s\'}\nfunction localDate(d){return d.toLocaleDateString([], {weekday:\'short\', month:\'short\', day:\'numeric\', year:\'numeric\'})}\nfunction localTime(d){return d.toLocaleTimeString([], {hour:\'numeric\', minute:\'2-digit\'})}\nfunction rangeLine(start,end){if(!start)return \'\'; const s=new Date(start), e=end?new Date(end):null; if(Number.isNaN(s.getTime()))return \'\'; return e&&!Number.isNaN(e.getTime()) ? `${localDate(s)} · ${localTime(s)}–${localTime(e)}` : `${localDate(s)} · ${localTime(s)}`}\nfunction milesLine(j){const m=Number(j.route_miles||j.distance_miles||j.miles||0); return m>0 ? `${m.toFixed(1)} miles` : ``} function jobTimeLine(j){const bits=[rangeLine(j.route_start_wall_time,j.route_end_wall_time),milesLine(j)].filter(Boolean); return bits.join(` · `)}\nasync function fetchJson(url){const r=await fetch(url,{cache:\'no-store\'}); if(!r.ok)throw Error(url+\' HTTP \'+r.status); return await r.json()}\nasync function loadDriveJobs(){try{return await fetchJson(\'/api/drive-jobs\')}catch(e){console.warn(\'DB drive-jobs endpoint unavailable; falling back to legacy drive_jobs.json\',e); return await fetchJson(\'drive_jobs.json\')}}\nasync function init(){const m=await loadDriveJobs(); jobs=m.jobs||[]; renderInbox(); await loadFinished(); await loadVoiceSessions(); const def=m.default_job_id||(jobs[0]&&jobs[0].job_id); if(def)openJob(def); else document.body.classList.remove(\'preload\')}\nfunction renderInbox(){document.getElementById(\'inbox\').innerHTML=jobs.map(j=>{const tl=jobTimeLine(j); return `<div class="job ${job&&job.job_id===j.job_id?\'active\':\'\'}" onclick="openJob(\'${esc(j.job_id)}\')"><b>${esc(j.route_label||j.analysis_name)}</b><br><span class=small>${esc(j.route_id||\'\')} · ${esc(j.ride_type||j.ride_metadata)} · ${Math.round(j.duration_sec||0)}s</span>${tl?`<br><span class=small>${esc(tl)}</span>`:\'\'}</div>`}).join(\'\')}\nasync function openJob(id){job=jobs.find(j=>j.job_id===id)||jobs[0]; editingReview=null; currentEditRef=null; renderInbox(); data=await (await fetch(job.data_path)).json(); labels=await (await fetch(\'/api/labels?job_id=\'+encodeURIComponent(job.job_id))).json(); document.getElementById(\'rideMeta\').value=job.ride_type||job.ride_metadata||\'label validation\'; const vu=data.video_url||data.video_path||\'\'; v.src=vu; document.getElementById(\'summary\').innerHTML=`<span class=badge>${esc(data.route.route_label)}</span> <span class=badge>${esc(data.route.route_id)}</span> <span class=routeSummaryText>${fmt(data.duration_sec)} · ${esc(data.video_status||\'\')}</span> <button id="topFinish" onclick="finishReview()">Finish review</button> <button id="deleteDrive" title="Delete this imported validation ride from the DB and artifact store" onclick="deleteCurrentDrive()">Delete</button>`; viewStart=0; viewEnd=null; undoStack=[]; setUndoEnabled(); renderEvents(); renderSaved(); updateToggleLabels(); drawTimeline(); drawZoom(); document.body.classList.remove(\'preload\');}\n\nfunction updateTelemetryOverlay(){if(!data)return; const el=document.getElementById(\'telemetryOverlay\'); if(!telemetryOn){el.classList.remove(\'on\');return} const t=currentRouteTime(); const smp=nearest((data.telemetry&&data.telemetry.samples)||[],t); const radar=nearest((data.radar&&data.radar.samples)||[],t); el.classList.add(\'on\'); el.innerHTML=`<b>Telemetry</b><br>route ${fmt(t)}<br>speed ${smp&&smp.speed_mph!=null?(+smp.speed_mph).toFixed(1)+\' mph\':\'—\'}<br>set ${smp&&smp.set_speed_mph!=null?(+smp.set_speed_mph).toFixed(1)+\' mph\':\'—\'}<br>accel ${smp&&smp.a_ego_mps2!=null?(+smp.a_ego_mps2).toFixed(2)+\' m/s²\':\'—\'}<br>lead ${smp&&smp.lead_status?\'yes\':\'no\'}${radar&&radar.leadOne&&radar.leadOne.dRel_m!=null?\'<br>dRel \'+(+radar.leadOne.dRel_m).toFixed(1)+\'m\':\'\'}`}\nfunction updateCanOverlay(){if(!data)return; const el=document.getElementById(\'canOverlay\'); if(!el)return; if(!canTelemetryOn){el.classList.remove(\'on\');return} const t=currentRouteTime(); const samples=(data.can&&data.can.samples)||[]; const smp=nearest(samples,t); el.classList.add(\'on\'); if(!smp){el.innerHTML=\'<b>CAN telemetry</b><br><span class=small>No CAN samples near \'+esc(fmt(t))+\'</span>\';return} const dt=Math.abs((+smp.t||0)-t); const rows=(smp.frames||[]).slice(0,16).map(f=>`<div class="canRow"><span class="${f.unknown?\'canUnknown\':\'canKnown\'}">${esc(f.name||(\'0x\'+(+f.addr||0).toString(16)))}</span><span>b${esc(f.src)}</span><span>${esc((f.data||\'\').slice(0,18))}</span>${f.hint?`<span></span><span></span><span class=canHint>${esc(f.hint)}</span>`:\'\'}</div>`).join(\'\'); el.innerHTML=`<b>CAN telemetry</b> <span class=small>${fmt(smp.t)} Δ${dt.toFixed(2)}s · ${smp.frames?smp.frames.length:0} frames</span><br>${rows}`}\n\n\nfunction videoToRoute(vt){const sync=data.video_sync||[]; if(!sync.length)return vt; let last=sync[0]; for(const sg of sync){if(vt>=sg.video_start_sec&&vt<=sg.video_start_sec+sg.duration_sec)return sg.route_start_sec+(vt-sg.video_start_sec); if(vt>=sg.video_start_sec)last=sg} return last.route_start_sec+Math.max(0,vt-last.video_start_sec)}\nfunction routeToVideo(rt){const sync=data.video_sync||[]; if(!sync.length)return rt; let last=sync[0]; for(const sg of sync){if(rt>=sg.route_start_sec&&rt<=sg.route_start_sec+sg.duration_sec)return sg.video_start_sec+(rt-sg.route_start_sec); if(rt>=sg.route_start_sec)last=sg} return last.video_start_sec+Math.max(0,rt-last.route_start_sec)}\nfunction currentRouteTime(){return videoToRoute(+(v.currentTime||0))}\nfunction currentView(){const dur=data.duration_sec||Math.max(1,v.duration||1); if(viewEnd===null) viewEnd=dur; viewStart=Math.max(0,Math.min(viewStart,dur-1)); viewEnd=Math.max(viewStart+1,Math.min(viewEnd,dur)); return {dur,start:viewStart,end:viewEnd,span:viewEnd-viewStart}}\nfunction seek(t){v.currentTime=Math.max(0,routeToVideo(Math.max(0,Math.min(+t||0,data.duration_sec||1)))); drawTimeline(); drawZoom(); updateTelemetryOverlay(); updateCanOverlay()}\nfunction seekDelta(d){seek(currentRouteTime()+d)} function frameStep(dir){const fps=(data&&data.video_fps)||20; v.pause(); v.currentTime=Math.max(0,Math.min((v.duration||1),+(v.currentTime||0)+(dir/Math.max(1,fps)))); drawTimeline(); drawZoom(); updateTelemetryOverlay(); updateCanOverlay()}\nfunction nearestEvent(){const e=nearest([...(data.events||[]),...(data.bookmarks||[])],currentRouteTime()); if(e)seek(e.t)}\nfunction eventX(ev,el){const r=el.getBoundingClientRect(); return Math.max(0,Math.min(r.width,((ev.clientX ?? (ev.touches&&ev.touches[0].clientX)) || 0)-r.left))}\nfunction eY(ev,el){const r=el.getBoundingClientRect(); return Math.max(0,Math.min(r.height,((ev.clientY ?? (ev.touches&&ev.touches[0].clientY)) || 0)-r.top))}\nfunction timelineRouteTime(ev){const c=document.getElementById(\'timeline\'),r=c.getBoundingClientRect(),vw=currentView(); return vw.start+eventX(ev,c)/r.width*vw.span}\nfunction updateToggleLabels(){const set=(id,on,label)=>{const el=document.getElementById(id); if(el)el.textContent=label+\': \'+(on?\'on\':\'off\')}; set(\'bookmarkToggle\',showBookmarkOverlay,\'Bk\'); set(\'toolToggle\',showToolEvents,\'Tool\'); set(\'activityToggle\',showActivityBars,\'Act\')}\nfunction toggleBookmarkOverlay(){showBookmarkOverlay=!showBookmarkOverlay; updateToggleLabels(); drawTimeline(); drawZoom()}\nfunction toggleToolEvents(){showToolEvents=!showToolEvents; updateToggleLabels(); drawTimeline()}\nfunction toggleActivityBars(){showActivityBars=!showActivityBars; updateToggleLabels(); drawTimeline()}\nfunction resetZoom(){viewStart=0; viewEnd=(data&&data.duration_sec)||Math.max(1,v.duration||1); drawTimeline(); drawZoom()}\nfunction bookmarkTime(b){const e=bookmarkEdits[b.id]||{}; return Number.isFinite(+e.t)?+e.t:(+b.t||0)}\nfunction clampTime(t){const dur=currentView().dur||data?.duration_sec||0; return Math.max(0,Math.min(dur,+t||0))}\nfunction setUndoEnabled(){const b=document.getElementById(\'undoMove\'); if(b)b.disabled=!undoStack.length}\nfunction pushUndo(u){undoStack.push(u); if(undoStack.length>25)undoStack.shift(); setUndoEnabled()}\nfunction undoMove(){const u=undoStack.pop(); if(!u)return; if(u.kind===\'bookmark\'){ if(u.hadEdit) bookmarkEdits[u.id]={...(bookmarkEdits[u.id]||{}),t:u.oldT}; else delete bookmarkEdits[u.id]; } else if(u.kind===\'label\'){u.obj.start_time_sec=u.oldStart; u.obj.end_time_sec=u.oldEnd} renderEvents(); renderSaved(); drawTimeline(); drawZoom(); setUndoEnabled(); document.getElementById(\'save\').innerHTML=\'<span class=ok>Undid last move.</span>\'}\nfunction simpleEventLabel(e){const ty=(e&&e.type)||\'\'; if(ty.includes(\'steer\')||ty.includes(\'lateral\'))return \'steer\'; if(ty.includes(\'brake\')||ty.includes(\'stop\'))return \'brake\'; if(ty.includes(\'accel\'))return \'accel\'; if(ty.includes(\'pinned\'))return \'pin\'; return ty.replace(/_.*/,\'\')||\'bookmark\'}\nfunction labelShort(x){return (x||\'label\').toString().replace(/^too_/,\'\').replace(/_/g,\' \')}\nfunction labelAtTime(t){let best=null,bd=1e9; for(const group of [\'drive\',\'phev\']) for(const l of labels[group]||[]){const st=+l.start_time_sec||0,en=Math.max(st,+l.end_time_sec||st),d=t>=st&&t<=en?0:Math.min(Math.abs(t-st),Math.abs(t-en)); if(d<bd){best=l;bd=d}} return bd<=2?best:null}\nfunction bookmarkLabel(b){const e=bookmarkEdits[b.id]||{},bt=bookmarkTime(b),lab=labelAtTime(bt); if(lab)return labelShort(lab.label); let raw=(e.label||b.label||b.reason||(b.tags&&b.tags.join(\',\'))||\'\').toString().trim(); if(raw && !/^userBookmark$|^bookmarkButton$|^bookmark$|^in-car bookmark$/i.test(raw))return raw; const ev=nearest(data.events||[], bt); return (ev&&Math.abs((+ev.t||0)-bt)<=8)?simpleEventLabel(ev):\'bookmark\'}\nfunction populateBookmark(b){const t=bookmarkTime(b),lab=bookmarkLabel(b); const sel=document.getElementById(\'label\'); const opt=[...sel.options].find(o=>o.value===lab||o.textContent===lab); sel.value=opt?opt.value:\'other\'; document.getElementById(\'start\').value=Math.max(0,t-1.5).toFixed(1); document.getElementById(\'end\').value=(t+1.5).toFixed(1); document.getElementById(\'tags\').value=lab===\'bookmark\'?\'\':lab; document.getElementById(\'notes\').value=(b.reason||b.source||\'in-car bookmark\')+\' @ \'+t.toFixed(1)+\'s\'; document.getElementById(\'save\').innerHTML=\'<span class=ok>Loaded bookmark into review label box.</span>\'; document.getElementById(\'notes\').focus()}\nfunction renderEvents(){const items=[...(data.bookmarks||[]).map(x=>({...x,type:\'bookmark\',summary:bookmarkLabel?bookmarkLabel(x):(x.label||x.reason||\'bookmark\')})),...(data.events||[])].sort((a,b)=>(+a.t||0)-(+b.t||0)); window.items=items; document.getElementById(\'events\').innerHTML=items.map((x,i)=>`<div class=event onclick="eventRowClick(${i})"><b>${fmt(x.t)}</b> <span class=badge>${esc(x.type)}</span> ${esc(x.summary||x.reason||\'\')}</div>`).join(\'\')}\nfunction eventRowClick(i){const x=(window.items||[])[i]; if(!x)return; seek(+x.t||0); if(x.type===\'bookmark\') populateBookmark(x); else seedFromEvent(i)}\nfunction seedFromEvent(i){const x=(window.items||[])[i]; if(!x)return; document.getElementById(\'start\').value=Math.max(0,(+x.t||0)-1.5).toFixed(1); document.getElementById(\'end\').value=((+x.end_t||+x.t||0)+1.5).toFixed(1); document.getElementById(\'tags\').value=x.type||\'\'; document.getElementById(\'notes\').value=(x.summary||x.reason||x.type||\'\')+\' @ \'+fmt(x.t)}\nfunction prepareLabel(){const t=currentRouteTime(); document.getElementById(\'start\').value=t.toFixed(1); document.getElementById(\'end\').value=(t+3).toFixed(1); document.getElementById(\'notes\').focus()} function markStart(){document.getElementById(\'start\').value=currentRouteTime().toFixed(1)} function markEnd(){document.getElementById(\'end\').value=currentRouteTime().toFixed(1)}\nfunction labelPayload(){const t=currentRouteTime(); return {job_id:job.job_id, route_label:data.route.route_label, route_id:data.route.route_id, ride_type:document.getElementById(\'rideMeta\').value, ride_metadata:document.getElementById(\'rideMeta\').value, label_kind:\'drive\', start_time_sec:+document.getElementById(\'start\').value||t, end_time_sec:+document.getElementById(\'end\').value||t, label:document.getElementById(\'label\').value, severity:document.getElementById(\'severity\').value, tags:document.getElementById(\'tags\').value.split(\',\').map(s=>s.trim()).filter(Boolean), notes:document.getElementById(\'notes\').value, nearest_event:nearest(data.events,t), nearest_bookmark:nearest(data.bookmarks,t)}}\nasync function saveLabel(){const s=document.getElementById(\'save\'); const payload=labelPayload(); if(editingReview){ if(currentEditRef) Object.assign(currentEditRef,payload); else labels[payload.label_kind].push(payload); currentEditRef=null; s.innerHTML=\'<span class=ok>Edited finished review in memory. Press Finish again to update the validated dataset.</span>\'; renderSaved(); renderEvents(); drawTimeline(); drawZoom(); return } try{const r=await fetch(\'/api/labels\',{method:\'POST\',headers:{\'content-type\':\'application/json\'},body:JSON.stringify(payload)}); const j=await r.json(); if(!j.ok)throw Error(j.error||\'save failed\'); s.innerHTML=\'<span class=ok>Draft saved.</span>\'; labels=await (await fetch(\'/api/labels?job_id=\'+encodeURIComponent(job.job_id))).json(); renderSaved(); renderEvents(); drawTimeline(); drawZoom()}catch(e){s.innerHTML=\'<span class=warn>\'+esc(e.message)+\'</span>\'}}\nfunction renderSaved(){const rows=[...(labels.drive||[]).map((x,i)=>({...x,_group:\'drive\',_index:i})),...(labels.phev||[]).map((x,i)=>({...x,_group:\'phev\',_index:i}))].sort((a,b)=>(+a.start_time_sec||0)-(+b.start_time_sec||0)); document.getElementById(\'saved\').innerHTML=rows.map((x,i)=>`<div class="event draftRow"><button class=deleteDraft title="Delete draft label" onclick="deleteDraft(${i},event)">×</button><div class=draftRowText onclick="editDraft(${i})"><b>${fmt(x.start_time_sec)}–${fmt(x.end_time_sec)}</b> <span class=badge>${esc(x.label_kind||\'drive\')}</span> ${esc(x.label)} <span class=small>${esc(x.notes||\'\')}</span></div></div>`).join(\'\')||\'No draft labels yet.\'; window.draftRows=rows}\nfunction editDraft(i){const x=(window.draftRows||[])[i]; if(!x)return; editLabelObj((labels[x._group]||[])[x._index]||x)}\nfunction editLabelObj(x){if(!x)return; currentEditRef=x; document.getElementById(\'label\').value=[...document.getElementById(\'label\').options].some(o=>o.value===x.label)?x.label:\'other\'; document.getElementById(\'start\').value=(+x.start_time_sec||0).toFixed(1); document.getElementById(\'end\').value=(+x.end_time_sec||0).toFixed(1); document.getElementById(\'severity\').value=x.severity||\'reviewed\'; document.getElementById(\'tags\').value=(x.tags||[]).join(\', \'); document.getElementById(\'notes\').value=x.notes||\'\'; document.getElementById(\'save\').innerHTML=\'<span class=ok>Loaded draft label into review box.</span>\'; seek(x.start_time_sec||0)}\nasync function deleteDraft(i,ev){if(ev)ev.stopPropagation(); const x=(window.draftRows||[])[i]; if(!x)return; if(!await softConfirm(`Delete draft label ${labelShort(x.label)} at ${fmt(x.start_time_sec)}?`,{title:\'Delete draft label?\',ok:\'Delete\'}))return; if(editingReview){(labels[x._group]||[]).splice(x._index,1); document.getElementById(\'save\').innerHTML=\'<span class=ok>Draft label removed from this finished-review edit. Press Finish again to update the validated dataset.</span>\'; renderSaved(); renderEvents(); drawTimeline(); drawZoom(); return} try{const r=await fetch(\'/api/delete-label\',{method:\'POST\',headers:{\'content-type\':\'application/json\'},body:JSON.stringify({job_id:job.job_id,label_kind:x._group,index:x._index,id:x.id,expected_version:x.version})}); const j=await r.json(); if(!j.ok)throw Error(j.error||\'delete failed\'); labels=await (await fetch(\'/api/labels?job_id=\'+encodeURIComponent(job.job_id))).json(); currentEditRef=null; document.getElementById(\'save\').innerHTML=\'<span class=ok>Draft label deleted.</span>\'; renderSaved(); renderEvents(); drawTimeline(); drawZoom()}catch(e){document.getElementById(\'save\').innerHTML=\'<span class=warn>\'+esc(e.message)+\'</span>\'}}\nasync function deleteCurrentDrive(){if(!job||!data)return; const name=data.route.route_label||job.route_label||job.job_id; const msg=`Delete imported label-validation ride "${name}"? This removes the drive from the inbox and deletes its DB route, video/log artifact links, draft labels, bookmarks, timeline samples, and unshared copied artifact files. This only happens if you press Yes.`; if(!await softConfirm(msg,{title:"Delete imported drive?",ok:"Yes, delete drive"}))return; try{const r=await fetch("/api/delete-review-job",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({job_id:job.job_id,review_job_id:job.review_job_id,route_uuid:job.route_uuid,expected_version:job.job_version,confirm:true})}); const j=await r.json(); if(!j.ok)throw Error(j.error||"delete failed"); document.getElementById("save").innerHTML="<span class=ok>Deleted imported ride. Refreshing inbox...</span>"; const m=await loadDriveJobs(); jobs=m.jobs||[]; data=null; job=null; labels={drive:[],phev:[]}; v.removeAttribute("src"); v.load(); document.getElementById("summary").innerHTML=""; document.getElementById("events").innerHTML=""; document.getElementById("saved").innerHTML=""; renderInbox(); const def=m.default_job_id||(jobs[0]&&jobs[0].job_id); if(def) await openJob(def); else document.getElementById("inbox").innerHTML="<span class=small>No imported validation rides awaiting review.</span>"}catch(e){document.getElementById("save").innerHTML="<span class=warn>"+esc(e.message)+"</span>"}}\nasync function finishReview(){const rows=[...(labels.drive||[]),...(labels.phev||[])]; if(!confirm(`Finish review for ${data.route.route_label}? This will write/replace ${rows.length} labels in the validated local dataset.`))return; const payload={review_id:data.route.route_id||job.job_id,job_id:job.job_id,route_id:data.route.route_id,route_label:data.route.route_label,expected_version:job.job_version,ride_type:document.getElementById(\'rideMeta\').value,ride_metadata:document.getElementById(\'rideMeta\').value,labels:rows,notes:\'\'}; const r=await fetch(\'/api/finish\',{method:\'POST\',headers:{\'content-type\':\'application/json\'},body:JSON.stringify(payload)}); const j=await r.json(); if(!j.ok){document.getElementById(\'save\').innerHTML=\'<span class=warn>\'+esc(j.error)+\'</span>\';return} document.getElementById(\'save\').innerHTML=\'<span class=ok>Finished review saved to validated dataset.</span>\'; editingReview=null; await loadFinished()}\nasync function loadFinished(){const j=await (await fetch(\'/api/finished\')).json(); finished=j.reviews||[]; document.getElementById(\'finished\').innerHTML=finished.map((r,i)=>`<div class=fin onclick="openFinished(${i})"><b>${esc(r.route_label||r.route_id)}</b><br><span class=small>${esc(r.ride_type||r.ride_metadata)} · ${r.label_count||0} labels · ${esc(r.finished_at||\'\')}</span></div>`).join(\'\')||\'<span class=small>No finished reviews yet.</span>\'}\nasync function loadVoiceSessions(){const el=document.getElementById(\'voiceSession\'),st=document.getElementById(\'voiceStatus\'); if(!el)return; try{const j=await (await fetch(\'/api/voice-sessions\')).json(); const sessions=j.sessions||[]; el.innerHTML=sessions.map(s=>{const tl=rangeLine(s.created_at,s.ended_at); const dur=s.duration_sec?` · ${Math.round(s.duration_sec)}s`:\'\'; return `<option value="${esc(s.id)}">${esc(tl||s.created_at||s.id)} · ${esc(s.name||s.id)}${dur} · ${s.bookmark_count||0} bookmarks${s.audio?\' · audio\':\'\'}</option>`}).join(\'\'); st.innerHTML=sessions.length?`Found ${sessions.length} local voice session(s). Voice logs show their recorded time range for matching against the drive inbox.`:\'<span class=warn>No voice sessions found yet.</span>\'}catch(e){st.innerHTML=\'<span class=warn>\'+esc(e.message)+\'</span>\'}}\nasync function importVoiceBookmarks(){const sel=document.getElementById(\'voiceSession\'),st=document.getElementById(\'voiceStatus\'); if(!sel||!sel.value){st.innerHTML=\'<span class=warn>Pick a voice session first.</span>\';return} if(!job){st.innerHTML=\'<span class=warn>Open a drive first.</span>\';return} const payload={session_id:sel.value,job_id:job.job_id,offset_sec:0,window_sec:3}; st.textContent=\'Importing voice bookmarks...\'; try{const r=await fetch(\'/api/import-voice\',{method:\'POST\',headers:{\'content-type\':\'application/json\'},body:JSON.stringify(payload)}); const j=await r.json(); if(!j.ok)throw Error(j.error||\'voice import failed\'); st.innerHTML=\'<span class=ok>Imported \'+j.count+\' voice bookmark label(s) into this drive.</span>\'; labels=await (await fetch(\'/api/labels?job_id=\'+encodeURIComponent(job.job_id))).json(); renderSaved(); renderEvents(); drawTimeline(); drawZoom()}catch(e){st.innerHTML=\'<span class=warn>\'+esc(e.message)+\'</span>\'}}\nfunction openFinished(i){const r=finished[i]; if(!r)return; const j=jobs.find(x=>x.route_id===r.route_id)||jobs.find(x=>x.job_id===r.job_id); if(j)openJob(j.job_id).then(()=>{editingReview=r; labels={drive:(r.labels||[]).filter(x=>(x.label_kind||\'drive\')===\'drive\'), phev:(r.labels||[]).filter(x=>x.label_kind===\'phev\')}; document.getElementById(\'rideMeta\').value=r.ride_type||r.ride_metadata||\'label validation\'; renderSaved(); drawTimeline(); document.getElementById(\'save\').innerHTML=\'<span class=ok>Opened finished review for editing; Finish again to replace dataset entry.</span>\'})}\nfunction roundedRect(ctx,x,y,w,h,r){r=Math.min(r,w/2,h/2);ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath()}\nfunction drawTimeline(){if(!data)return; const c=document.getElementById(\'timeline\'),ctx=c.getContext(\'2d\'),dpr=window.devicePixelRatio||1,r=c.getBoundingClientRect(); const cssH=96; if(Math.abs(c.width-r.width*dpr)>2||Math.abs(c.height-cssH*dpr)>2){c.width=Math.max(1,Math.round(r.width*dpr)); c.height=Math.round(cssH*dpr)} ctx.setTransform(dpr,0,0,dpr,0,0); ctx.imageSmoothingEnabled=false; const W=r.width,H=cssH,vw=currentView(),t=currentRouteTime(),tel=((data.telemetry&&data.telemetry.samples)||[]).filter(s=>s.t>=vw.start&&s.t<=vw.end); const x=tt=>Math.max(0,Math.min(W,((+tt||0)-vw.start)/vw.span*W)); const C={speed:\'#355377\',lead:\'#e9c46a\',gas:\'#75e0a4\',brake:\'#ff7b8a\',bookmark:\'#c084fc\',bookmarkStem:\'rgba(192,132,252,.98)\',bookmarkStroke:\'rgba(243,232,255,.86)\',bookmarkText:\'#f8f2ff\'}; const uiFont=\'Arial,Helvetica,sans-serif\',minuteFont=\'600 10px \'+uiFont,bookmarkFont=\'800 9px \'+uiFont,timeFont=\'700 10px \'+uiFont; ctx.clearRect(0,0,W,H); ctx.textAlign=\'left\'; ctx.textBaseline=\'top\'; ctx.font=minuteFont; const bg=ctx.createLinearGradient(0,0,0,H); bg.addColorStop(0,\'#0d1729\'); bg.addColorStop(1,\'#07101d\'); ctx.fillStyle=bg; ctx.fillRect(0,0,W,H); ctx.strokeStyle=\'#263955\'; ctx.lineWidth=1; ctx.strokeRect(.5,.5,W-1,H-1); const step=Math.max(30,Math.round(vw.span/8/30)*30),minuteY=6; for(let m=Math.ceil(vw.start/step)*step;m<=vw.end;m+=step){const xx=x(m),tx=Math.round(xx)+4; ctx.fillStyle=\'rgba(147,166,190,.16)\';ctx.fillRect(xx,0,1,H); ctx.font=minuteFont;ctx.textBaseline=\'top\';ctx.fillStyle=\'#91a4bd\';ctx.fillText(Math.round(m/60)+\'m\',tx,minuteY)} if(showActivityBars) for(const sm of tel){const xx=x(sm.t),sp=Math.max(0,Math.min(90,Number(sm.speed_mph)||0)),h=Math.max(8,Math.min(H-14,sp*.85)); ctx.fillStyle=sm.brake_pressed?C.brake:(sm.gas_pressed?C.gas:(sm.lead_status?C.lead:C.speed)); ctx.globalAlpha=(sm.brake_pressed||sm.gas_pressed)?0.9:0.72; ctx.fillRect(xx,H-h,2,h)} ctx.globalAlpha=1; if(showToolEvents) for(const e of (data.events||[]).filter(e=>e.t>=vw.start&&e.t<=vw.end)){ctx.fillStyle=\'rgba(255,77,109,.9)\'; ctx.fillRect(x(e.t)-1,0,2,H)} if(showBookmarkOverlay){window.labelHitRects=[]; for(const group of [\'drive\',\'phev\']) for(const l of (labels[group]||[]).filter(l=>(+l.end_time_sec||+l.start_time_sec||0)>=vw.start&&(+l.start_time_sec||0)<=vw.end)){const ls=+l.start_time_sec||0,le=Math.max(ls,+l.end_time_sec||ls),lx=x(ls),rx=x(le),barW=Math.max(4,rx-lx),name=labelShort(l.label),range=le-ls>0.05; ctx.fillStyle=\'rgba(192,132,252,.22)\'; ctx.fillRect(lx,H-24,barW,10); window.labelHitRects.push({obj:l,x1:Math.min(lx,rx),x2:Math.max(lx,rx),y1:H-32,y2:H}); ctx.strokeStyle=C.bookmark; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(Math.round(lx)+.5,H-28); ctx.lineTo(Math.round(lx)+.5,H); ctx.stroke(); if(le>ls&&le<=vw.end){ctx.beginPath(); ctx.moveTo(Math.round(rx)+.5,H-28); ctx.lineTo(Math.round(rx)+.5,H); ctx.stroke()} if(range){ctx.font=bookmarkFont; ctx.textBaseline=\'middle\'; for(const side of [[\'start\',lx],[\'end\',rx]]){const txt=side[0]+\': \'+name,tw=Math.min(112,Math.max(52,ctx.measureText(txt).width+14)),bx=Math.max(3,Math.min(W-tw-3,side[1]-tw/2)),by=H-52; roundedRect(ctx,bx,by,tw,18,9); ctx.fillStyle=C.bookmark;ctx.fill(); ctx.strokeStyle=C.bookmarkStroke;ctx.stroke(); ctx.fillStyle=C.bookmarkText;ctx.fillText(txt,bx+7,by+9.5); window.labelHitRects.push({obj:l,x1:bx,x2:bx+tw,y1:by,y2:by+18})}} else {const txt=name,tw=Math.min(96,Math.max(40,ctx.measureText(txt).width+14)),bx=Math.max(3,Math.min(W-tw-3,lx-tw/2)),by=H-52; ctx.font=bookmarkFont;ctx.textBaseline=\'middle\';roundedRect(ctx,bx,by,tw,18,9);ctx.fillStyle=C.bookmark;ctx.fill();ctx.strokeStyle=C.bookmarkStroke;ctx.stroke();ctx.fillStyle=C.bookmarkText;ctx.fillText(txt,bx+7,by+9.5); window.labelHitRects.push({obj:l,x1:bx,x2:bx+tw,y1:by,y2:by+18})}}} if(showBookmarkOverlay){ctx.font=bookmarkFont; ctx.textBaseline=\'middle\'; for(const b of data.bookmarks||[]){const bt=bookmarkTime(b); if(bt<vw.start||bt>vw.end)continue; const covered=labelAtTime(bt); if(covered&&((+covered.end_time_sec||0)-(+covered.start_time_sec||0)>2))continue; const xx=x(bt),label=bookmarkLabel(b).slice(0,14),tw=Math.min(104,Math.max(42,ctx.measureText(label).width+14)),pillH=18,bx=Math.max(3,Math.min(W-tw-3,xx-tw/2)),by=7; ctx.strokeStyle=C.bookmarkStem; ctx.fillStyle=C.bookmark; ctx.lineWidth=1; ctx.beginPath();ctx.moveTo(Math.round(xx)+.5,by+pillH);ctx.lineTo(Math.round(xx)+.5,H);ctx.stroke(); ctx.shadowColor=\'rgba(0,0,0,.25)\';ctx.shadowBlur=3; roundedRect(ctx,bx,by,tw,pillH,9); ctx.fillStyle=C.bookmark;ctx.fill(); ctx.shadowBlur=0; ctx.strokeStyle=C.bookmarkStroke;ctx.lineWidth=1;ctx.stroke(); ctx.fillStyle=C.bookmarkText; ctx.fillText(label,bx+7,by+pillH/2+0.5)}} const px=x(t); ctx.strokeStyle=\'rgba(255,255,255,.92)\';ctx.lineWidth=2;ctx.shadowColor=\'rgba(255,255,255,.55)\';ctx.shadowBlur=6;ctx.beginPath();ctx.moveTo(px,0);ctx.lineTo(px,H);ctx.stroke();ctx.shadowBlur=0; ctx.font=timeFont; ctx.textBaseline=\'top\'; const tt=t.toFixed(1)+\'s\',tw=ctx.measureText(tt).width+10,tx=Math.max(4,Math.min(W-tw-4,px+6)),ty=H-21; roundedRect(ctx,tx,ty,tw,15,7); ctx.fillStyle=\'rgba(13,17,23,.58)\';ctx.fill(); ctx.strokeStyle=\'rgba(255,255,255,.20)\';ctx.stroke(); ctx.fillStyle=\'#f0f6fc\'; ctx.fillText(tt,tx+5,ty+4); document.getElementById(\'time\').textContent=t.toFixed(1)+\'s route\'; updateTelemetryOverlay(); updateCanOverlay()}\nfunction drawZoom(){if(!data)return; const c=document.getElementById(\'zoom\'); if(!c)return; const ctx=c.getContext(\'2d\'),dpr=window.devicePixelRatio||1,r=c.getBoundingClientRect(); const cssH=34; if(Math.abs(c.width-r.width*dpr)>2||Math.abs(c.height-cssH*dpr)>2){c.width=Math.max(1,Math.round(r.width*dpr)); c.height=Math.round(cssH*dpr)} ctx.setTransform(dpr,0,0,dpr,0,0); ctx.imageSmoothingEnabled=false; const W=r.width,H=cssH,vw=currentView(),dur=vw.dur,zx=t=>Math.max(0,Math.min(W,(+t||0)/dur*W)),BOOKMARK=\'#c084fc\'; ctx.clearRect(0,0,W,H); ctx.fillStyle=\'#07101d\';ctx.fillRect(0,0,W,H); ctx.strokeStyle=\'#263955\';ctx.strokeRect(.5,.5,W-1,H-1); ctx.fillStyle=\'rgba(53,83,119,.75)\'; for(const sm of ((data.telemetry&&data.telemetry.samples)||[])){const h=Math.max(3,Math.min(H-8,(+sm.speed_mph||0)*.32)); ctx.fillRect(zx(sm.t),H-h,1,h)} if(showBookmarkOverlay){for(const group of [\'drive\',\'phev\']) for(const l of (labels[group]||[])){const ls=Math.max(0,+l.start_time_sec||0),le=Math.max(ls,+l.end_time_sec||ls),lx=zx(ls),rx=zx(le); ctx.fillStyle=\'rgba(192,132,252,.32)\'; ctx.fillRect(lx,5,Math.max(3,rx-lx),H-10); ctx.fillStyle=BOOKMARK; ctx.fillRect(Math.round(lx),3,1,H-6)} for(const b of data.bookmarks||[]){const xx=zx(bookmarkTime(b)); ctx.fillStyle=BOOKMARK; ctx.fillRect(Math.round(xx),2,1,H-4)}} const x1=zx(vw.start),x2=zx(vw.end); ctx.fillStyle=\'rgba(255,255,255,.08)\';ctx.fillRect(0,0,x1,H);ctx.fillRect(x2,0,W-x2,H); ctx.strokeStyle=\'#fff\';ctx.lineWidth=2;ctx.strokeRect(x1+.5,2,Math.max(4,x2-x1)-1,H-4); ctx.fillStyle=\'#fff\';ctx.fillRect(x1-3,0,6,H);ctx.fillRect(x2-3,0,6,H)}\nfunction hitBookmark(ev){if(!showBookmarkOverlay||!data)return null; const c=document.getElementById(\'timeline\'),r=c.getBoundingClientRect(),mx=eventX(ev,c),vw=currentView(); let best=null,bd=999; for(const b of data.bookmarks||[]){const bt=bookmarkTime(b); if(bt<vw.start||bt>vw.end)continue; const bx=(bt-vw.start)/vw.span*r.width,d=Math.abs(mx-bx); if(d<bd&&d<32){best=b;bd=d}} return best}\nfunction hitLabel(ev){if(!data)return null; const c=document.getElementById(\'timeline\'),r=c.getBoundingClientRect(),mx=eventX(ev,c),my=eY(ev,c),vw=currentView(); for(const h of window.labelHitRects||[]){if(mx>=h.x1&&mx<=h.x2&&my>=h.y1&&my<=h.y2){const st=+h.obj.start_time_sec||0,en=Math.max(st,+h.obj.end_time_sec||st); return {obj:h.obj,start:st,end:en}}} let best=null,bd=999; for(const group of [\'drive\',\'phev\']) for(const l of labels[group]||[]){const st=+l.start_time_sec||0,en=Math.max(st,+l.end_time_sec||st); if(en<vw.start||st>vw.end)continue; const lx=(st-vw.start)/vw.span*r.width,rx=(en-vw.start)/vw.span*r.width,cx=Math.max(Math.min(mx,rx),lx),d=Math.abs(mx-cx); if(d<bd&&d<18){best={obj:l,group,start:st,end:en};bd=d}} return best}\nconst tl=document.getElementById(\'timeline\');\ntl.addEventListener(\'pointerdown\',e=>{const b=hitBookmark(e),l=b?null:hitLabel(e); if(!b&&!l)return; const t0=timelineRouteTime(e); dragTimeline=b?{kind:\'bookmark\',id:b.id,obj:b,startX:eventX(e,tl),oldT:bookmarkTime(b),hadEdit:!!bookmarkEdits[b.id],startT:t0,moved:false}:{kind:\'label\',obj:l.obj,startX:eventX(e,tl),oldStart:l.start,oldEnd:l.end,startT:t0,moved:false}; tl.setPointerCapture(e.pointerId); e.preventDefault()});\ntl.addEventListener(\'pointermove\',e=>{if(!dragTimeline)return; const t=timelineRouteTime(e),dt=t-dragTimeline.startT; if(Math.abs(eventX(e,tl)-dragTimeline.startX)>3)dragTimeline.moved=true; if(dragTimeline.kind===\'bookmark\'){bookmarkEdits[dragTimeline.id]={...(bookmarkEdits[dragTimeline.id]||{}),t:clampTime(dragTimeline.oldT+dt)}} else {const len=Math.max(0,dragTimeline.oldEnd-dragTimeline.oldStart),ns=clampTime(dragTimeline.oldStart+dt),ne=clampTime(ns+len); dragTimeline.obj.start_time_sec=ns; dragTimeline.obj.end_time_sec=ne} renderEvents(); renderSaved(); drawTimeline(); drawZoom()});\ntl.addEventListener(\'pointerup\',e=>{if(!dragTimeline)return; const d=dragTimeline; tl.releasePointerCapture(e.pointerId); dragTimeline=null; if(d.moved){suppressTimelineClick=true; setTimeout(()=>suppressTimelineClick=false,0); if(d.kind===\'bookmark\')pushUndo({kind:\'bookmark\',id:d.id,oldT:d.oldT,hadEdit:d.hadEdit}); else pushUndo({kind:\'label\',obj:d.obj,oldStart:d.oldStart,oldEnd:d.oldEnd}); document.getElementById(\'save\').innerHTML=\'<span class=ok>Moved \'+(d.kind===\'bookmark\'?\'bookmark\':\'label\')+\'. Use Undo if that was accidental.</span>\'}});\ntl.addEventListener(\'pointercancel\',()=>{dragTimeline=null});\ntl.addEventListener(\'click\',e=>{if(suppressTimelineClick)return; const l=hitLabel(e); if(l){editLabelObj(l.obj); return} const b=hitBookmark(e); if(b){seek(bookmarkTime(b)); populateBookmark(b)} else seek(timelineRouteTime(e))}); const zl=document.getElementById(\'zoom\'); function zoomTime(e){const r=zl.getBoundingClientRect(),dur=currentView().dur; return eventX(e,zl)/r.width*dur} zl.addEventListener(\'pointerdown\',e=>{const vw=currentView(),r=zl.getBoundingClientRect(),mx=eventX(e,zl),x1=vw.start/vw.dur*r.width,x2=vw.end/vw.dur*r.width; dragZoom=Math.abs(mx-x1)<12?\'left\':(Math.abs(mx-x2)<12?\'right\':\'window\'); zl.setPointerCapture(e.pointerId); if(dragZoom===\'window\'){const t=zoomTime(e),span=vw.span; viewStart=Math.max(0,Math.min(vw.dur-span,t-span/2)); viewEnd=viewStart+span} drawTimeline();drawZoom()}); zl.addEventListener(\'pointermove\',e=>{if(!dragZoom)return; const vw=currentView(),t=zoomTime(e); if(dragZoom===\'left\')viewStart=Math.max(0,Math.min(t,vw.end-3)); else if(dragZoom===\'right\')viewEnd=Math.min(vw.dur,Math.max(t,vw.start+3)); else {const span=vw.span; viewStart=Math.max(0,Math.min(vw.dur-span,t-span/2)); viewEnd=viewStart+span} drawTimeline();drawZoom()}); zl.addEventListener(\'pointerup\',()=>{dragZoom=null}); zl.addEventListener(\'pointercancel\',()=>{dragZoom=null}); v.addEventListener(\'timeupdate\',()=>{drawTimeline();updateTelemetryOverlay();updateCanOverlay()}); v.addEventListener(\'loadedmetadata\',()=>setPlaybackSpeed(v.playbackRate||1)); setInterval(()=>{drawTimeline();drawZoom()},500); init();\n</script></body></html>'
def _replace_generated_asset_once(asset: str, old: str, new: str) -> str:
    if asset.count(old) != 1:
        raise RuntimeError("manual labeler generated asset patch target missing or ambiguous")
    return asset.replace(old, new, 1)


SERVER_PY = _replace_generated_asset_once(
    SERVER_PY,
    "'route_start_wall_time':j.get('started_at') or (legacy or {}).get('route_start_wall_time') or '','route_end_wall_time':j.get('ended_at') or (legacy or {}).get('route_end_wall_time') or ''",
    "'route_start_wall_time':j.get('route_start_wall_time') or j.get('drive_start_wall_time') or (legacy or {}).get('route_start_wall_time') or (legacy or {}).get('drive_start_wall_time') or (legacy or {}).get('start_wall_time') or '','route_end_wall_time':j.get('route_end_wall_time') or j.get('drive_end_wall_time') or (legacy or {}).get('route_end_wall_time') or (legacy or {}).get('drive_end_wall_time') or (legacy or {}).get('end_wall_time') or ''",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "let jobs=[],data=null,job=null,labels={drive:[],phev:[]},finished=[],editingReview=null,currentEditRef=null,bookmarkEdits={},showBookmarkOverlay=true,showToolEvents=false,showActivityBars=true,viewStart=0,viewEnd=null,dragZoom=null,dragTimeline=null,undoStack=[],suppressTimelineClick=false;",
    "let jobs=[],data=null,job=null,labels={drive:[],phev:[]},finished=[],editingReview=null,currentEditRef=null,bookmarkEdits={},showBookmarkOverlay=true,showToolEvents=false,showActivityBars=true,viewStart=0,viewEnd=null,routeCursor=0,lastVideoTime=0,refreshingDrive=false,dragZoom=null,dragTimeline=null,undoStack=[],suppressTimelineClick=false;",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    ".videoWrap{position:relative}video{width:100%;max-height:58vh;background:#000;border-radius:10px}",
    ".videoWrap{position:relative}.videoWrap.empty video{min-height:220px}.noVideo{display:none;position:absolute;inset:0;align-items:center;justify-content:center;flex-direction:column;gap:6px;background:linear-gradient(180deg,rgba(13,17,23,.92),rgba(13,17,23,.82));border:1px dashed #3d536f;border-radius:10px;color:#c9d1d9;text-align:center}.noVideo.on{display:flex}.noVideo b{color:#f0f6fc}video{width:100%;max-height:58vh;background:#000;border-radius:10px}",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    '<div class=videoWrap><video id=v controls playsinline></video><div id=telemetryOverlay class=telemetryOverlay></div>',
    '<div id=videoWrap class=videoWrap><video id=v controls playsinline></video><div id=noVideo class=noVideo><b>No video ingested</b><span>Timeline, telemetry, bookmarks, and labels still work.</span></div><div id=telemetryOverlay class=telemetryOverlay></div>',
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "st.innerHTML='<span class=ok>Imported '+j.count+' voice bookmark label(s) into this drive.</span>';",
    "const imported=j.bookmarks??j.count??j.timeline_voice_bookmarks??0; st.innerHTML='<span class=ok>Imported '+imported+' voice bookmark(s) into this drive.</span>';",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "const imported=j.bookmarks??j.count??j.timeline_voice_bookmarks??0; st.innerHTML='<span class=ok>Imported '+imported+' voice bookmark(s) into this drive.</span>'; labels=await (await fetch('/api/labels?job_id='+encodeURIComponent(job.job_id))).json(); renderSaved(); renderEvents(); drawTimeline(); drawZoom()",
    "const imported=j.bookmarks??j.count??j.timeline_voice_bookmarks??0; st.innerHTML='<span class=ok>Imported '+imported+' voice bookmark(s) into this drive.</span>'; await refreshCurrentDrive(true,currentRouteTime())",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    '<button class=secondary title="Reset timeline zoom" onclick="resetZoom()">Reset</button><span id=time class=badge title="Current route time">0.0s</span>',
    '<button class=secondary title="Reset timeline zoom" onclick="resetZoom()">Reset</button><button class=secondary title="Reload current drive data and bookmarks" onclick="refreshCurrentDrive(false)">Refresh</button><span id=time class=badge title="Current route time">0.0s</span>',
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    '<div class="row"><select id=voiceSession style="min-width:260px"></select><button class=secondary onclick="loadVoiceSessions()">Refresh</button><button onclick="importVoiceBookmarks()">Import to this drive</button></div>',
    '<div class="row"><select id=voiceSession style="min-width:260px"></select><input id=voiceOffset type=number step=0.1 value=0 style="width:92px" title="Shift imported voice bookmarks in seconds"><button class=secondary onclick="loadVoiceSessions()">Refresh</button><button onclick="importVoiceBookmarks()">Import to this drive</button></div>',
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "const payload={session_id:sel.value,job_id:job.job_id,offset_sec:0,window_sec:3};",
    "const off=Number(document.getElementById('voiceOffset')?.value||0)||0; const payload={session_id:sel.value,job_id:job.job_id,offset_sec:off,window_sec:3};",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "function openFinished(i){const r=finished[i]; if(!r)return; const j=jobs.find(x=>x.route_id===r.route_id)||jobs.find(x=>x.job_id===r.job_id); if(j)openJob(j.job_id).then(()=>{editingReview=r; labels={drive:(r.labels||[]).filter(x=>(x.label_kind||'drive')==='drive'), phev:(r.labels||[]).filter(x=>x.label_kind==='phev')}; document.getElementById('rideMeta').value=r.ride_type||r.ride_metadata||'label validation'; renderSaved(); drawTimeline(); document.getElementById('save').innerHTML='<span class=ok>Opened finished review for editing; Finish again to replace dataset entry.</span>'})}",
    "async function refreshCurrentDrive(silent=false,keepT=null){if(!job||!data||refreshingDrive)return false; refreshingDrive=true; const t=keepT==null?currentRouteTime():keepT; try{data=await fetchJson(job.data_path||('/api/job-data?job_id='+encodeURIComponent(job.job_id))); labels=await (await fetch('/api/labels?job_id='+encodeURIComponent(job.job_id),{cache:'no-store'})).json(); renderInbox(); renderSaved(); renderEvents(); drawTimeline(); drawZoom(); seek(t); if(!silent)document.getElementById('save').innerHTML='<span class=ok>Refreshed current drive data.</span>'; return true}catch(e){if(!silent)document.getElementById('save').innerHTML='<span class=warn>'+esc(e.message)+'</span>'; return false}finally{refreshingDrive=false}}\nfunction openFinished(i){const r=finished[i]; if(!r)return; const j=jobs.find(x=>x.route_id===r.route_id)||jobs.find(x=>x.job_id===r.job_id); if(j)openJob(j.job_id).then(()=>{editingReview=r; labels={drive:(r.labels||[]).filter(x=>(x.label_kind||'drive')==='drive'), phev:(r.labels||[]).filter(x=>x.label_kind==='phev')}; document.getElementById('rideMeta').value=r.ride_type||r.ride_metadata||'label validation'; renderSaved(); drawTimeline(); document.getElementById('save').innerHTML='<span class=ok>Opened finished review for editing; Finish again to replace dataset entry.</span>'})}",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "function milesLine(j){const m=Number(j.route_miles||j.distance_miles||j.miles||0); return m>0 ? `${m.toFixed(1)} miles` : ``} function jobTimeLine(j){const bits=[rangeLine(j.route_start_wall_time,j.route_end_wall_time),milesLine(j)].filter(Boolean); return bits.join(` · `)}",
    "function milesLine(j){const m=Number(j.route_miles||j.distance_miles||j.miles||0); return m>0 ? `${m.toFixed(1)} miles` : ``} function jobTimeLine(j){const driveTime=rangeLine(j.route_start_wall_time,j.route_end_wall_time)||'drive time unavailable'; const bits=[driveTime,milesLine(j)].filter(Boolean); return bits.join(` · `)}",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "function renderInbox(){document.getElementById('inbox').innerHTML=jobs.map(j=>{const tl=jobTimeLine(j); return `<div class=\"job ${job&&job.job_id===j.job_id?'active':''}\" onclick=\"openJob('${esc(j.job_id)}')\"><b>${esc(j.route_label||j.analysis_name)}</b><br><span class=small>${esc(j.route_id||'')} · ${esc(j.ride_type||j.ride_metadata)} · ${Math.round(j.duration_sec||0)}s</span>${tl?`<br><span class=small>${esc(tl)}</span>`:''}</div>`}).join('')}",
    "function renderInbox(){document.getElementById('inbox').innerHTML=jobs.map(j=>{const tl=jobTimeLine(j); return `<div class=\"job ${job&&job.job_id===j.job_id?'active':''}\" onclick=\"openJob('${esc(j.job_id)}')\"><b>${esc(j.route_label||j.analysis_name)}</b><br><span class=small>${esc(j.route_id||'')} · ${esc(j.ride_type||j.ride_metadata)} · ${Math.round(j.duration_sec||0)}s</span><br><span class=small>${esc(tl)}</span></div>`}).join('')}",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "viewStart=0; viewEnd=null; undoStack=[];",
    "routeCursor=0; lastVideoTime=+(v.currentTime||0); viewStart=0; viewEnd=null; undoStack=[];",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "const vu=data.video_url||data.video_path||''; v.src=vu; document.getElementById('summary').innerHTML=",
    "const vu=data.video_url||data.video_path||''; const vw=document.getElementById('videoWrap'),nv=document.getElementById('noVideo'); if(vu){v.src=vu;v.load();vw&&vw.classList.remove('empty');nv&&nv.classList.remove('on')}else{v.pause();v.removeAttribute('src');v.load();vw&&vw.classList.add('empty');nv&&nv.classList.add('on')} document.getElementById('summary').innerHTML=",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "function videoToRoute(vt){const sync=data.video_sync||[]; if(!sync.length)return vt; let last=sync[0]; for(const sg of sync){if(vt>=sg.video_start_sec&&vt<=sg.video_start_sec+sg.duration_sec)return sg.route_start_sec+(vt-sg.video_start_sec); if(vt>=sg.video_start_sec)last=sg} return last.route_start_sec+Math.max(0,vt-last.video_start_sec)}\nfunction routeToVideo(rt){const sync=data.video_sync||[]; if(!sync.length)return rt; let last=sync[0]; for(const sg of sync){if(rt>=sg.route_start_sec&&rt<=sg.route_start_sec+sg.duration_sec)return sg.video_start_sec+(rt-sg.route_start_sec); if(rt>=sg.route_start_sec)last=sg} return last.video_start_sec+Math.max(0,rt-last.route_start_sec)}\nfunction currentRouteTime(){return videoToRoute(+(v.currentTime||0))}\nfunction currentView(){const dur=data.duration_sec||Math.max(1,v.duration||1); if(viewEnd===null) viewEnd=dur; viewStart=Math.max(0,Math.min(viewStart,dur-1)); viewEnd=Math.max(viewStart+1,Math.min(viewEnd,dur)); return {dur,start:viewStart,end:viewEnd,span:viewEnd-viewStart}}\nfunction seek(t){v.currentTime=Math.max(0,routeToVideo(Math.max(0,Math.min(+t||0,data.duration_sec||1)))); drawTimeline(); drawZoom(); updateTelemetryOverlay(); updateCanOverlay()}",
    "function videoToRoute(vt){const sync=data.video_sync||[]; if(!sync.length)return vt; let last=sync[0]; for(const sg of sync){if(vt>=sg.video_start_sec&&vt<=sg.video_start_sec+sg.duration_sec)return sg.route_start_sec+(vt-sg.video_start_sec); if(vt>=sg.video_start_sec)last=sg} return last.route_start_sec+Math.max(0,vt-last.video_start_sec)}\nfunction routeToVideo(rt){const sync=data.video_sync||[]; if(!sync.length)return rt; for(const sg of sync){if(rt>=sg.route_start_sec&&rt<=sg.route_start_sec+sg.duration_sec)return sg.video_start_sec+(rt-sg.route_start_sec)} return null}\nfunction currentRouteTime(){const vt=+(v.currentTime||0),fps=Number(data&&data.video_fps)||20,tol=Math.max(0.01,0.45/Math.max(1,fps)); if(!Number.isFinite(routeCursor)||!v.paused||Math.abs(vt-lastVideoTime)>tol){routeCursor=clampTime(videoToRoute(vt)); lastVideoTime=vt} return clampTime(routeCursor)}\nfunction currentView(){const dur=data.duration_sec||Math.max(1,v.duration||1); if(viewEnd===null) viewEnd=dur; viewStart=Math.max(0,Math.min(viewStart,dur-1)); viewEnd=Math.max(viewStart+1,Math.min(viewEnd,dur)); return {dur,start:viewStart,end:viewEnd,span:viewEnd-viewStart}}\nfunction seek(t){const rt=clampTime(t); routeCursor=rt; let vt=routeToVideo(rt); const vd=Number(v.duration); if(vt!==null&&Number.isFinite(vd)&&vd>0&&vt>vd+0.25)vt=null; if(vt!==null&&Number.isFinite(vt)){const target=Math.max(0,Math.min(Number.isFinite(vd)&&vd>0?vd:vt,vt)); v.currentTime=target; lastVideoTime=target} drawTimeline(); drawZoom(); updateTelemetryOverlay(); updateCanOverlay()}",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "function seekDelta(d){seek(currentRouteTime()+d)} function frameStep(dir){const fps=(data&&data.video_fps)||20; v.pause(); v.currentTime=Math.max(0,Math.min((v.duration||1),+(v.currentTime||0)+(dir/Math.max(1,fps)))); drawTimeline(); drawZoom(); updateTelemetryOverlay(); updateCanOverlay()}",
    "function seekDelta(d){seek(currentRouteTime()+d)} function frameStep(dir){const fps=Number(data&&data.video_fps)||20; v.pause(); const base=Number.isFinite(routeCursor)?routeCursor:currentRouteTime(); seek(base+dir/Math.max(1,fps))}",
)

INDEX_HTML = _replace_generated_asset_once(
    INDEX_HTML,
    "function timelineRouteTime(ev){const c=document.getElementById('timeline'),r=c.getBoundingClientRect(),vw=currentView(); return vw.start+eventX(ev,c)/r.width*vw.span}",
    "function timelineRatio(ev,el){const r=el.getBoundingClientRect(); return r.width>0?eventX(ev,el)/r.width:0}\nfunction timelineRouteTime(ev){const c=document.getElementById('timeline'),vw=currentView(); return vw.start+timelineRatio(ev,c)*vw.span}",
)


if __name__ == "__main__":
    main()
