#!/usr/bin/env python3
"""Build a local Brickpilot human-review queue and tiny review UI.

The queue is intentionally offline/local-only. It reads existing analysis CSVs and
local copied route artifacts, optionally creates short clips if camera files and
ffmpeg are available, then emits metadata + a small browser UI that appends the
reviewer's labels to a local JSONL file through a localhost-only Python server.
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
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from typing import Any, Iterable

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2]))
ROOT = TOOLS_ROOT
OPENPILOT_REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
DEFAULT_DATA_ROOT = Path(os.environ.get("BRICKPILOT_DRIVE_DATA_ROOT", os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB"))).expanduser()
DEFAULT_LOGDRIVE_DIR = Path(os.environ.get("BRICKPILOT_LOGDRIVE_ANALYSIS_ROOT", DEFAULT_DATA_ROOT / "logdrive_runs"))
DEFAULT_RAW_DIR = Path(os.environ.get("BRICKPILOT_LOGDRIVE_RAW_ROOT", DEFAULT_DATA_ROOT / "imports/raw/from_comma"))
DEFAULT_OUT = Path(os.environ.get("BRICKPILOT_REVIEW_QUEUE_OUT", DEFAULT_DATA_ROOT / "labeler_outputs/review_queue_20260512_batch2"))
SEGMENT_LEN_SEC = 60.0
MPH_PER_MPS = 2.2369362920544
DEFAULT_LOOKBACK_SEC = 60.0
DEFAULT_AFTER_SEC = 25.0
OPENPILOT_UI_FAKE_DONGLE = "0000000000000000"

VIN_RE = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]{17}\b)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])(?=[A-HJ-NPR-Z0-9]*\d)[A-HJ-NPR-Z0-9]{17}\b")
CAMERA_CANDIDATES = (
    "qcamera.ts",
    "qcamera.hevc",
    "qcamera.mp4",
    "dcamera.hevc",
    "dcamera.ts",
    "ecamera.hevc",
    "fcamera.hevc",
)
EVENT_FILES = {
    "accel_lazy_or_eager": "acceleration_events.csv",
    "stop_creep": "near_stop_creep_events.csv",
    "stop_go": "stop_go_events.csv",
    "brake_regen_blend": "intervention_events.csv",
    "low_speed_pinned": "pinned_bursts.csv",
    "steering_jerk": "steering_jerk_events.csv",
    "lateral_watch": "lateral_events.csv",
}
GPS_CACHE: dict[str, dict[str, Any]] = {}
RADAR_CACHE: dict[str, dict[str, Any]] = {}
RADAR_MAX_SAMPLES = 260
RADAR_MAX_POINTS_PER_SAMPLE = 32


def relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except Exception:
        return str(path)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in keys})


def latest_reviews(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    revisions: dict[str, int] = {}
    for row in load_jsonl(path):
        event_id = row.get("event_id")
        if not event_id:
            continue
        revisions[event_id] = revisions.get(event_id, 0) + 1
        d = dict(row)
        d["review_revision"] = revisions[event_id]
        latest[event_id] = d
    return latest


def _review_themes(review: dict[str, Any]) -> list[str]:
    label = str(review.get("actual_label", "")).lower()
    notes = str(review.get("notes", "")).lower()
    text = f"{label} {notes}"
    themes: list[str] = []
    if any(s in text for s in ["starts after", "right at the start", "no context", "before this video", "after this clip", "doesn't show", "doesnt show", "not in this clip", "not in this video", "not useful", "can't see", "cant see", "cut out", "no video"]):
        themes.append("clip_too_late_or_not_useful")
    if "too_lazy" in label or "too lazy" in notes or "not getting up" in notes or "not accelerating" in notes or "target speed" in notes or "set speed" in notes or "speed limit" in notes:
        themes.append("too_lazy_acceleration")
    if "steering_jerk" in label or "jerky" in notes or "pingpong" in notes or "steering" in notes:
        themes.append("steering_jerk_or_pingpong")
    if "speed bump" in notes or "manual" in notes or "driver_override" in label or "parking lot" in notes or "pulling into" in notes or "had to brake" in notes:
        themes.append("driver_override_or_parking_speed_bump")
    if "traffic_light_or_stop_sign" in label or "stop sign" in notes or "red light" in notes or "stoplight" in notes or "failed to stop" in notes or "didn't fully stop" in notes:
        themes.append("stop_or_brake_context")
    if "brake_regen" in label or "regen" in notes or "phev" in notes or "phew" in notes or "ev" in notes or "engine" in notes:
        themes.append("phev_or_regen_uncertain")
    if "bad_tool_match" in label or "elsewhere" in notes or "wrong" in notes:
        themes.append("bad_tool_match")
    if not themes:
        themes.append("other")
    return sorted(set(themes))


def write_review_feedback_summary(out_dir: Path, queue: list[dict[str, Any]]) -> dict[str, Any]:
    latest = latest_reviews(out_dir / "reviews.jsonl")
    by_event = {ev["event_id"]: ev for ev in queue}
    rows: list[dict[str, Any]] = []
    label_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    theme_counts: dict[str, int] = {}
    category_theme_counts: dict[tuple[str, str], int] = {}
    for event_id, review in sorted(latest.items(), key=lambda kv: inum(kv[1].get("queue_rank"), 999999)):
        ev = by_event.get(event_id, {})
        themes = _review_themes(review)
        label = review.get("actual_label", "") or "(blank)"
        severity = review.get("severity", "") or "(blank)"
        label_counts[label] = label_counts.get(label, 0) + 1
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        for theme in themes:
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
            category = review.get("category") or ev.get("category") or "unknown"
            category_theme_counts[(category, theme)] = category_theme_counts.get((category, theme), 0) + 1
        rows.append({
            "queue_rank": review.get("queue_rank"),
            "event_id": event_id,
            "category": review.get("category") or ev.get("category"),
            "route_label": review.get("route_label") or ev.get("route_label"),
            "actual_label": review.get("actual_label", ""),
            "severity": review.get("severity", ""),
            "themes": ";".join(themes),
            "clip_timing_complaint": "1" if "clip_too_late_or_not_useful" in themes else "0",
            "notes": review.get("notes", ""),
            "tool_category": ev.get("category", ""),
            "video_status": ev.get("video_status", ""),
            "lookback_sec": ev.get("lookback_sec", ""),
            "event_offset_sec": ev.get("event_offset_sec", ""),
        })
    write_csv(out_dir / "review_feedback_summary.csv", rows)
    lines = [
        "# Brickpilot Completed Review Feedback Summary",
        "",
        f"Unique reviewed events: {len(latest)}",
        "",
        "## Actual labels",
        "",
    ]
    for k, v in sorted(label_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Severity/usefulness", ""]
    for k, v in sorted(severity_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Note themes", ""]
    for k, v in sorted(theme_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Category × theme", ""]
    for (cat, theme), v in sorted(category_theme_counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])):
        lines.append(f"- {cat} / {theme}: {v}")
    lines += [
        "",
        "## Generator implications",
        "",
        "- Treat bookmarks as after-the-fact reactions: default clips now look back from the human bookmark/marker rather than starting at it.",
        "- Preserve bad-tool-match/not-useful reviews; they are useful negatives for queue selection and label matching.",
        "- Too-lazy acceleration is the dominant actionable theme and often appears in clips originally categorized as lateral/brake/intervention.",
        "- Steering jerk/ping-pong and manual speed-bump/parking/override contexts should stay separately taggable.",
    ]
    (out_dir / "review_feedback_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"unique_reviews": len(latest), "theme_counts": theme_counts, "label_counts": label_counts}


def fnum(value: Any, default: float = math.nan) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value: Any, default: int = -1) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse_catalog_minimal(path: Path) -> dict[str, str]:
    """Extract route metadata without requiring PyYAML."""
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    out: dict[str, str] = {}
    for key in ("label", "model", "route_id", "local_segments_dir", "notes"):
        m = re.search(rf"^\s*{re.escape(key)}:\s*['\"]?([^'\"\n]+)", text, re.M)
        if m:
            out[key] = m.group(1).strip().strip('"').strip("'")
    for key in ("test_date", "branch", "version", "selfdrive_profile"):
        m = re.search(rf"^\s*{re.escape(key)}:\s*['\"]?([^'\"\n]+)", text, re.M)
        if m:
            out[key] = m.group(1).strip().strip('"').strip("'")
    return out


def route_from_logdrive_dir(route_dir: Path) -> dict[str, Any]:
    meta = parse_catalog_minimal(route_dir / "catalog.yaml")
    label = meta.get("label") or route_dir.name
    return {
        "analysis_dir": route_dir,
        "analysis_name": route_dir.name,
        "route_label": label,
        "model": meta.get("model", ""),
        "route_id": meta.get("route_id", ""),
        "local_segments_dir": meta.get("local_segments_dir", ""),
        "test_date": meta.get("test_date", ""),
        "branch": meta.get("branch", ""),
        "version": meta.get("version", ""),
        "selfdrive_profile": meta.get("selfdrive_profile", ""),
    }


def event_time(row: dict[str, str]) -> float:
    for k in ("start_route_time_sec", "route_time_sec", "event_start_route_time_sec", "bookmark_route_time_sec"):
        v = fnum(row.get(k))
        if math.isfinite(v):
            return v
    return fnum(row.get("segment_index"), 0) * SEGMENT_LEN_SEC + fnum(row.get("start_segment_time_sec") or row.get("segment_time_sec"), 0)


def segment_index(row: dict[str, str]) -> int:
    for k in ("segment_index", "segment", "segment_id"):
        i = inum(row.get(k))
        if i >= 0:
            return i
    t = event_time(row)
    return int(t // SEGMENT_LEN_SEC) if math.isfinite(t) else -1


def event_end_time(row: dict[str, str]) -> float:
    v = fnum(row.get("end_route_time_sec"))
    if math.isfinite(v):
        return v
    start = event_time(row)
    dur = fnum(row.get("duration_sec"), 0)
    return start + max(0.0, dur)


def summarize_metrics(row: dict[str, str], keys: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        val = row.get(key)
        if val not in (None, ""):
            out[key] = val
    return out


def question_for(category: str, row: dict[str, str], labels: list[dict[str, Any]]) -> str:
    label_text = ", ".join(sorted({str(x.get("reason", "")) for x in labels if x.get("reason")}))
    prefix = f"Nearby human bookmark(s): {label_text}. " if label_text else ""
    if category == "phev_label_uncertain":
        return prefix + "What was the actual PHEV context here: EV glide, engine-on HEV, regen drag, stop/creep, too-lazy, too-eager, MADS/LFA, steering jerk, or something else?"
    if category == "accel_lazy_or_eager":
        return prefix + "Was Brickpilot too lazy, too eager, appropriately lead-limited, or overridden by traffic/driver intent?"
    if category in {"stop_creep", "stop_go"}:
        return prefix + "At the stop/creep, should the system have held, crept sooner, launched sooner, or stayed stopped because of traffic/light/lead?"
    if category == "brake_regen_blend":
        return prefix + "Was this braking/regen blend appropriate, too harsh, too late, too soft, or caused by driver/lead/light context?"
    if category in {"low_speed_pinned", "steering_jerk", "lateral_watch"}:
        return prefix + "Did the lateral behavior feel like a real steering jerk/pinned output, normal curve tracking, driver input, or road geometry?"
    if category == "label_tool_disagreement":
        return prefix + "The bookmark reason and matched tool event disagree. Which label/reason is actually correct?"
    return prefix + "What actually happened here, and what label should future training/evaluation use?"


def guess_for(category: str, row: dict[str, str], labels: list[dict[str, Any]]) -> str:
    labels_str = ", ".join(sorted({str(x.get("reason", "")) for x in labels if x.get("reason")})) or "no nearby bookmark"
    if category == "phev_label_uncertain":
        tags = []
        for lab in labels:
            tags.extend(lab.get("tags") or [])
        return f"PHEV context bookmark near this point; selectable tags observed: {', '.join(sorted(set(tags))) or 'unknown'}. Needs human disambiguation."
    if category == "accel_lazy_or_eager":
        deficit = fnum(row.get("max_speed_deficit_mph"), 0)
        final = fnum(row.get("final_speed_deficit_mph"), 0)
        lazy = boolish(row.get("lazy_flag")) or final > 4 or deficit > 12
        achieved = boolish(row.get("achieved_within_2mph"))
        return f"Tool suspects {'lazy/undershoot' if lazy else 'possibly eager/normal accel'}; max deficit {deficit:.1f} mph, final deficit {final:.1f} mph, achieved target={achieved}; labels={labels_str}."
    if category in {"stop_creep", "stop_go"}:
        return f"Tool found low-speed stop/creep window; driver gas/brake={row.get('driver_gas_or_brake','')}, lead_start={row.get('lead_status_at_start','')}, incomplete={row.get('incomplete','')}; labels={labels_str}."
    if category == "brake_regen_blend":
        return f"Tool saw {row.get('intervention_type','intervention')} intervention around braking/longitudinal context; lead_limited={row.get('lead_limited','')}, speed_regime={row.get('speed_regime','')}; labels={labels_str}."
    if category in {"low_speed_pinned", "steering_jerk", "lateral_watch"}:
        err = row.get("max_abs_lateral_error") or row.get("abs_lateral_error") or ""
        speed = row.get("avg_mph") or row.get("speed_mph") or ""
        return f"Tool suspects lateral watch item at {speed} mph with abs lateral error {err}; steering_pressed={row.get('steering_pressed') or row.get('driver_steering','')}; labels={labels_str}."
    if category == "label_tool_disagreement":
        return f"Bookmark says {row.get('reason')} but matched tool event is {row.get('event_type')} (confidence {row.get('confidence')}, delay {row.get('delay_sec')}s)."
    return f"Tool event selected for human review; labels={labels_str}."


def priority_for(category: str, row: dict[str, str], labels: list[dict[str, Any]]) -> tuple[float, str]:
    p = 50.0
    why: list[str] = []
    if row.get("post_warmup") == "1":
        p += 6; why.append("post-warmup")
    if labels:
        p += 15; why.append("near bookmark")
    if category == "phev_label_uncertain":
        p += 35; why.append("explicit PHEV context bookmark")
    elif category == "label_tool_disagreement":
        p += 30; why.append("bookmark/tool disagreement")
        if row.get("confidence") == "high":
            p += 8; why.append("high-confidence match")
    elif category == "accel_lazy_or_eager":
        p += min(22, max(0, fnum(row.get("max_speed_deficit_mph"), 0)))
        if boolish(row.get("lazy_flag")):
            p += 10; why.append("lazy flag")
        if not boolish(row.get("achieved_within_2mph")):
            p += 8; why.append("did not reach target")
    elif category in {"stop_creep", "stop_go"}:
        p += 14
        if boolish(row.get("driver_gas_or_brake")):
            p += 8; why.append("driver override")
        if boolish(row.get("incomplete")):
            p += 6; why.append("incomplete stop/go")
        p += min(10, fnum(row.get("duration_sec"), 0))
    elif category == "brake_regen_blend":
        p += 12
        itype = row.get("intervention_type", "")
        if "brake" in itype:
            p += 12; why.append("brake intervention")
    elif category == "low_speed_pinned":
        p += 12
        if fnum(row.get("avg_mph"), 99) < 25:
            p += 8; why.append("low speed")
        p += min(15, 10 * fnum(row.get("max_abs_lateral_error"), 0))
    elif category == "steering_jerk":
        p += 12 + min(18, fnum(row.get("max_abs_output_rate"), 0))
        if fnum(row.get("avg_mph"), 99) < 30:
            p += 5; why.append("low/mid speed")
    elif category == "lateral_watch":
        p += 8 + min(20, 20 * fnum(row.get("abs_lateral_error"), 0))
    return round(p, 2), "; ".join(why) or "heuristic sample"


def load_route_labels(route_id: str) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    if not route_id:
        return labels
    p = DEFAULT_RAW_DIR / route_id / "bookmark_tags_route.jsonl"
    if not p.exists():
        # Some early copied routes include a dongle-ish prefix. Avoid emitting it; just use for lookup.
        matches = list(DEFAULT_RAW_DIR.glob(f"*_{route_id}/bookmark_tags_route.jsonl"))
        if matches:
            p = matches[0]
    for row in load_jsonl(p):
        seg = inum(row.get("segment"))
        mono = fnum(row.get("bookmark_button_log_mono_time"), math.nan)
        route_time = seg * SEGMENT_LEN_SEC
        # log_mono_time is route-relative-ish in ns for these copied tags; use it if plausible.
        if math.isfinite(mono):
            route_time = mono / 1e9
        labels.append({
            "reason": row.get("reason", ""),
            "route_time_sec": route_time,
            "segment_index": seg,
            "source": row.get("source", ""),
            "tags": row.get("tags", []),
            "wall_time": row.get("wall_time", ""),
        })
    return labels


def nearby_labels(labels: list[dict[str, Any]], time_sec: float, window: float = 20.0) -> list[dict[str, Any]]:
    out = []
    for lab in labels:
        lt = fnum(lab.get("route_time_sec"))
        if math.isfinite(lt) and abs(lt - time_sec) <= window:
            d = dict(lab)
            d["delta_sec"] = round(lt - time_sec, 3)
            out.append(d)
    return sorted(out, key=lambda x: abs(fnum(x.get("delta_sec"), 999)))[:8]


def segment_dir_for(route: dict[str, Any], segment: int) -> Path | None:
    seg_base = route.get("local_segments_dir") or ""
    route_id = route.get("route_id") or ""
    candidates = []
    if seg_base:
        candidates.append(Path(seg_base) / f"{route_id}--{segment}")
        candidates.append(Path(seg_base) / str(segment))
    if route_id:
        candidates.append(DEFAULT_RAW_DIR / route_id / "segments" / f"{route_id}--{segment}")
        candidates.extend(DEFAULT_RAW_DIR.glob(f"*_{route_id}/segments/{route_id}--{segment}"))
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return None


def find_camera_file(route: dict[str, Any], segment: int) -> Path | None:
    sd = segment_dir_for(route, segment)
    if not sd:
        return None
    for name in CAMERA_CANDIDATES:
        p = sd / name
        if p.exists() and p.stat().st_size > 0:
            return p
    for pat in ("*camera*", "*.hevc", "*.mp4", "*.ts"):
        for p in sd.glob(pat):
            if p.is_file() and p.stat().st_size > 0:
                return p
    return None


def _segment_from_camera_path(path: Path) -> int | None:
    m = re.search(r"--(\d+)$", path.parent.name)
    if not m:
        return None
    return int(m.group(1))


def _clip_anchor_route_time(event: dict[str, Any]) -> tuple[float, str]:
    """Pick the marker clips should look back from.

    Human bookmarks are reactions after the reviewer noticed something, so for labelled
    items prefer the nearest bookmark at/after the tool event. This keeps the
    causal lead-up in frame while still preserving tool-event offsets.
    """
    event_t = fnum(event.get("start_route_time_sec"), 0.0)
    labels = [x for x in event.get("nearby_labels", []) if math.isfinite(fnum(x.get("route_time_sec")))]
    label_driven = event.get("category") in {"phev_label_uncertain", "label_tool_disagreement"} or bool(labels)
    if label_driven and labels:
        future = [x for x in labels if fnum(x.get("route_time_sec")) >= event_t - 0.25]
        pool = future or labels
        best = min(pool, key=lambda x: abs(fnum(x.get("route_time_sec")) - event_t))
        return fnum(best.get("route_time_sec"), event_t), "human_bookmark"
    return event_t, "tool_event"


def build_video_sync(event: dict[str, Any], seconds_before: float, seconds_after: float) -> dict[str, Any]:
    """Describe how video.currentTime maps back to route/segment time."""
    anchor_t, anchor_kind = _clip_anchor_route_time(event)
    event_start = fnum(event.get("start_route_time_sec"), anchor_t)
    event_end = fnum(event.get("end_route_time_sec"), event_start + max(1.5, fnum(event.get("duration_sec"), 1.5)))
    clip_start_route_t = max(0.0, anchor_t - seconds_before)
    clip_end_route_t = max(anchor_t + seconds_after, event_end + seconds_after, clip_start_route_t + 1.5)
    start_seg = int(clip_start_route_t // SEGMENT_LEN_SEC)
    end_seg = int(max(clip_end_route_t - 0.001, clip_start_route_t) // SEGMENT_LEN_SEC)
    bookmarks = []
    for lab in event.get("nearby_labels", []):
        lt = fnum(lab.get("route_time_sec"))
        if math.isfinite(lt):
            bookmarks.append({
                "reason": lab.get("reason", ""),
                "route_time_sec": round(lt, 3),
                "offset_sec": round(lt - clip_start_route_t, 3),
                "delta_from_event_sec": round(lt - event_start, 3),
                "source": lab.get("source", ""),
            })
    return {
        "status": "ok",
        "anchor_kind": anchor_kind,
        "anchor_route_time_sec": round(anchor_t, 3),
        "clip_start_segment_index": start_seg,
        "clip_end_segment_index": end_seg,
        "clip_start_segment_time_sec": round(clip_start_route_t - start_seg * SEGMENT_LEN_SEC, 3),
        "clip_start_route_time_sec": round(clip_start_route_t, 3),
        "clip_end_route_time_sec": round(clip_end_route_t, 3),
        "clip_duration_sec": round(clip_end_route_t - clip_start_route_t, 3),
        "event_route_time_sec": event.get("start_route_time_sec"),
        "event_offset_sec": round(event_start - clip_start_route_t, 3),
        "anchor_offset_sec": round(anchor_t - clip_start_route_t, 3),
        "lookback_sec": round(anchor_t - clip_start_route_t, 3),
        "lookback_requested_sec": seconds_before,
        "after_sec": round(clip_end_route_t - anchor_t, 3),
        "after_requested_sec": seconds_after,
        "bookmark_offsets_sec": bookmarks,
        "mapping": "route_time_sec = clip_start_route_time_sec + video.currentTime",
    }


def _camera_segments_for_window(event: dict[str, Any]) -> list[dict[str, Any]]:
    sync = event.get("video_sync") or {}
    route = {
        "route_id": event.get("route_id", ""),
        "local_segments_dir": "",
    }
    start = fnum(sync.get("clip_start_route_time_sec"), fnum(event.get("start_route_time_sec"), 0.0))
    end = fnum(sync.get("clip_end_route_time_sec"), start + 1.5)
    start_seg = inum(sync.get("clip_start_segment_index"), int(start // SEGMENT_LEN_SEC))
    end_seg = inum(sync.get("clip_end_segment_index"), int(max(start, end - 0.001) // SEGMENT_LEN_SEC))
    parts: list[dict[str, Any]] = []
    missing: list[int] = []
    for seg in range(start_seg, end_seg + 1):
        cam = find_camera_file(route, seg)
        part_start = max(start, seg * SEGMENT_LEN_SEC)
        part_end = min(end, (seg + 1) * SEGMENT_LEN_SEC)
        if part_end <= part_start:
            continue
        if not cam:
            missing.append(seg)
            continue
        parts.append({
            "segment_index": seg,
            "path": relpath(cam),
            "start_segment_time_sec": round(part_start - seg * SEGMENT_LEN_SEC, 3),
            "duration_sec": round(part_end - part_start, 3),
            "start_route_time_sec": round(part_start, 3),
            "end_route_time_sec": round(part_end, 3),
        })
    if missing:
        event["missing_camera_segments"] = missing
    return parts


def _video_duration_sec(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.exists():
        return math.nan
    try:
        out = subprocess.check_output([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nokey=1:noprint_wrappers=1", str(path)], text=True, timeout=20).strip()
        return float(out)
    except Exception:
        return math.nan


def _reencode_clip(ffmpeg: str, sources: list[dict[str, Any]], clip_path: Path) -> None:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for src in sources:
        cmd += ["-ss", f"{src['start_segment_time_sec']:.3f}", "-t", f"{src['duration_sec']:.3f}", "-i", str(ROOT / src["path"])]
    if len(sources) == 1:
        cmd += ["-map", "0:v:0"]
    else:
        parts = "".join(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}];" for i in range(len(sources)))
        inputs = "".join(f"[v{i}]" for i in range(len(sources)))
        filt = f"{parts}{inputs}concat=n={len(sources)}:v=1:a=0[v]"
        cmd += ["-filter_complex", filt, "-map", "[v]"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-an", str(clip_path)]
    subprocess.run(cmd, check=True, timeout=180)


def create_clip_if_possible(event: dict[str, Any], out_dir: Path, dry_run: bool) -> tuple[str, str]:
    sources = _camera_segments_for_window(event)
    event["clip_source_segments"] = sources
    if sources:
        event["camera_source_path"] = sources[0]["path"]
    if not sources:
        return "", "missing camera artifact for clip window"
    if event.get("missing_camera_segments"):
        miss = ",".join(str(x) for x in event["missing_camera_segments"])
        return "", f"missing camera segment(s) for continuous lookback window: {miss}"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "", "ffmpeg not installed"
    if dry_run:
        return "", "clip extraction dry-run"
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / f"{event['event_id']}.mp4"
    if len(sources) == 1:
        src = sources[0]
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{src['start_segment_time_sec']:.3f}", "-i", str(ROOT / src["path"]), "-t", f"{src['duration_sec']:.3f}", "-c:v", "copy", "-an", str(clip_path)]
    else:
        cmd = []
    try:
        if cmd:
            subprocess.run(cmd, check=True, timeout=60)
        else:
            _reencode_clip(ffmpeg, sources, clip_path)
        dur = _video_duration_sec(clip_path)
        if clip_path.exists() and clip_path.stat().st_size > 0 and math.isfinite(dur) and dur > 0.5:
            event["clip_actual_duration_sec"] = round(dur, 3)
            return relpath(clip_path), "created"
        # Packet-copy can produce an empty MP4 if it starts between keyframes; reencode once.
        _reencode_clip(ffmpeg, sources, clip_path)
        dur = _video_duration_sec(clip_path)
        if clip_path.exists() and clip_path.stat().st_size > 0 and math.isfinite(dur) and dur > 0.5:
            event["clip_actual_duration_sec"] = round(dur, 3)
            return relpath(clip_path), "created"
        return "", "ffmpeg produced no valid clip"
    except Exception as e:
        return "", f"ffmpeg failed: {str(e).splitlines()[0][:180]}"


def _openpilot_route_name(route_id: str) -> str | None:
    """Return a local-only synthetic canonical route name for tools/clip.

    The copied comma `/realdata` directories used by the drive-test tooling are
    intentionally route-id-only (for example `00000145--bed323a7ea--8`) and do
    not always retain the dongle id that openpilot's Route helper expects.
    `tools/clip/run.py` only needs a canonical-looking route name to match local
    segment directory names, so use an all-zero synthetic dongle id and build a
    temporary symlink tree. This does not alter logs and does not upload data.
    """
    route_id = str(route_id or "").strip()
    if re.fullmatch(r"[a-f0-9]{8}--[a-z0-9]{10}", route_id):
        return f"{OPENPILOT_UI_FAKE_DONGLE}|{route_id}"
    return None


def _safe_symlink_or_copy(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        return
    try:
        dst.symlink_to(src.resolve())
    except OSError:
        # Fall back to a copy on filesystems where symlinks are unavailable.
        # This is only used under the generated local out dir.
        shutil.copy2(src, dst)


def _prepare_openpilot_ui_data_dir(event: dict[str, Any], out_dir: Path) -> tuple[str | None, Path | None, str]:
    route_name = _openpilot_route_name(str(event.get("route_id") or ""))
    if not route_name:
        return None, None, "route id is not openpilot Route-compatible"
    if event.get("missing_camera_segments"):
        miss = ",".join(str(x) for x in event.get("missing_camera_segments", []))
        return route_name, None, f"missing qcamera segment(s) for continuous UI render: {miss}"
    sources = event.get("clip_source_segments") or _camera_segments_for_window(event)
    if event.get("missing_camera_segments"):
        miss = ",".join(str(x) for x in event.get("missing_camera_segments", []))
        return route_name, None, f"missing qcamera segment(s) for continuous UI render: {miss}"
    if not sources:
        return route_name, None, "missing qcamera source segments"
    link_root = out_dir / ".openpilot_ui_links" / event["event_id"]
    link_root.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for src in sources:
        seg = inum(src.get("segment_index"), -1)
        src_cam = ROOT / str(src.get("path") or "")
        src_dir = src_cam.parent
        if seg < 0 or not src_cam.exists():
            missing.append(f"segment {seg}: qcamera missing")
            continue
        link_seg = link_root / f"{route_name}--{seg}"
        link_seg.mkdir(parents=True, exist_ok=True)
        _safe_symlink_or_copy(src_cam, link_seg / "qcamera.ts")
        log_src = None
        for name in ("rlog.zst", "qlog.zst"):
            p = src_dir / name
            if p.exists() and p.stat().st_size > 0:
                _safe_symlink_or_copy(p, link_seg / name)
                if name == "rlog.zst":
                    log_src = p
        if log_src is None and not (link_seg / "rlog.zst").exists():
            missing.append(f"segment {seg}: rlog.zst missing")
    if missing:
        return route_name, None, "; ".join(missing[:4])
    return route_name, link_root, "prepared"


def create_openpilot_ui_clip_if_possible(event: dict[str, Any], out_dir: Path, timeout_sec: float) -> tuple[str, str]:
    """Experimentally render a real openpilot onroad UI clip with local tools/clip.

    This is deliberately opt-in because it is heavy: it decodes qcamera frames,
    replays local rlog messages into `ui_state`, feeds VisionIPC, and records the
    actual Python onroad UI. It is local-only and writes under `out_dir`.
    """
    route_name, data_dir, prep_status = _prepare_openpilot_ui_data_dir(event, out_dir)
    if not route_name or data_dir is None:
        return "", f"openpilot UI render unavailable: {prep_status}"
    sync = event.get("video_sync") or {}
    start = max(0, int(math.floor(fnum(sync.get("clip_start_route_time_sec"), fnum(event.get("start_route_time_sec"), 0.0)))))
    end = int(math.ceil(fnum(sync.get("clip_end_route_time_sec"), fnum(event.get("end_route_time_sec"), start + 2.0))))
    if end <= start:
        end = start + 2
    clips_dir = out_dir / "openpilot_ui_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    out_path = clips_dir / f"{event['event_id']}.mp4"
    title = f"{event.get('category', 'review')} · tool +{fnum(sync.get('event_offset_sec'), 0.0):.1f}s"
    cmd = [
        sys.executable,
        str(OPENPILOT_REPO_ROOT / "tools/clip/run.py"),
        route_name,
        "--data-dir", str(data_dir),
        "--start", str(start),
        "--end", str(end),
        "--output", str(out_path),
        "--qcam",
        "--big",
        "--no-metadata",
        "--title", title,
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(TOOLS_ROOT), str(OPENPILOT_REPO_ROOT), env["PYTHONPATH"]]) if env.get("PYTHONPATH") else os.pathsep.join([str(TOOLS_ROOT), str(OPENPILOT_REPO_ROOT)])
    try:
        proc = subprocess.run(cmd, cwd=OPENPILOT_REPO_ROOT, env=env, capture_output=True, text=True, timeout=max(30.0, timeout_sec))
    except subprocess.TimeoutExpired:
        return "", f"openpilot UI render timed out after {timeout_sec:.0f}s"
    except Exception as e:
        return "", f"openpilot UI render failed to start: {str(e).splitlines()[0][:180]}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").splitlines()
        return "", f"openpilot UI render failed: {(detail[-1] if detail else 'unknown error')[:180]}"
    dur = _video_duration_sec(out_path)
    if out_path.exists() and out_path.stat().st_size > 0 and (not math.isfinite(dur) or dur > 0.5):
        event["openpilot_ui_command"] = " ".join(cmd)
        if math.isfinite(dur):
            event["openpilot_ui_clip_actual_duration_sec"] = round(dur, 3)
        return relpath(out_path), "created by tools/clip/run.py local replay"
    return "", "openpilot UI render produced no valid clip"


def _round_float(value: Any, digits: int = 3) -> float | None:
    v = fnum(value)
    return round(v, digits) if math.isfinite(v) else None


def _sqlite_rows(con: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    cur = con.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _select_existing_timeseries(
    con: sqlite3.Connection,
    table: str,
    desired_cols: list[str],
    start_t: float,
    end_t: float,
) -> list[dict[str, Any]]:
    cols = _table_columns(con, table)
    keep = [c for c in desired_cols if c in cols]
    if "route_time_sec" not in keep:
        return []
    sql = f"""
            SELECT {', '.join(keep)}
            FROM {table}
            WHERE route_time_sec BETWEEN ? AND ?
            ORDER BY route_time_sec
        """
    return _sqlite_rows(con, sql, (start_t, end_t))


def _nearest_by_time(rows: list[dict[str, Any]], route_time: float, max_delta: float = 0.35) -> dict[str, Any] | None:
    if not rows:
        return None
    best = min(rows, key=lambda r: abs(fnum(r.get("route_time_sec"), -999999) - route_time))
    return best if abs(fnum(best.get("route_time_sec"), -999999) - route_time) <= max_delta else None


def load_telemetry_for_event(event: dict[str, Any]) -> dict[str, Any]:
    """Attach a compact local telemetry window from analysis SQLite, if available."""
    sync = event.get("video_sync") or build_video_sync(event, 6.0, 8.0)
    start_t = fnum(sync.get("clip_start_route_time_sec"), fnum(event.get("start_route_time_sec"), 0) - 6.0) - 1.0
    end_t = fnum(sync.get("clip_end_route_time_sec"), fnum(event.get("end_route_time_sec"), start_t + 15.0) + 8.0) + 1.0
    sqlite_path = ROOT / str(event.get("analysis_dir", "")) / "results.sqlite"
    if not sqlite_path.exists():
        return {"status": "telemetry unavailable: results.sqlite missing", "samples": [], "source": relpath(sqlite_path)}
    try:
        con = sqlite3.connect(sqlite_path)
        con.row_factory = sqlite3.Row
        long_rows = _select_existing_timeseries(con, "longitudinal_timeseries", [
            "route_time_sec", "segment_index", "segment_time_sec", "speed_mph", "a_ego_mps2",
            "accel_cmd", "set_speed_mps", "speed_deficit_mph", "long_active",
            "lead_limited", "lead_status", "lead_d_rel_m", "lead_v_rel_mps", "gas_pressed", "brake_pressed",
            "lazy_candidate", "carcontrol_long_control_state", "controls_long_control_state",
        ], start_t, end_t)
        lat_rows = _select_existing_timeseries(con, "lateral_timeseries", [
            "route_time_sec", "lateral_error", "desired_lateral_accel", "actual_lateral_accel",
            "torque_output", "carcontrol_torque", "steering_angle_deg", "steering_pressed",
            "clean_lateral", "pinned",
        ], start_t, end_t)
        con.close()
    except Exception as e:
        return {"status": f"telemetry unavailable: sqlite read failed: {e}", "samples": [], "source": relpath(sqlite_path)}

    base = long_rows or lat_rows
    samples: list[dict[str, Any]] = []
    # Keep payload small while preserving enough density for the timeline/overlay.
    stride = max(1, math.ceil(len(base) / 260))
    for row in base[::stride]:
        rt = fnum(row.get("route_time_sec"))
        if not math.isfinite(rt):
            continue
        lat = _nearest_by_time(lat_rows, rt) if long_rows else row
        set_speed = _round_float(fnum(row.get("set_speed_mps")) * MPH_PER_MPS if row.get("set_speed_mps") not in (None, "") else None, 1)
        sample = {
            "t": round(rt - fnum(sync.get("clip_start_route_time_sec"), start_t + 1.0), 3),
            "route_time_sec": round(rt, 3),
            "speed_mph": _round_float(row.get("speed_mph"), 1),
            "set_speed_mph": set_speed,
            "speed_deficit_mph": _round_float(row.get("speed_deficit_mph"), 1),
            "a_ego_mps2": _round_float(row.get("a_ego_mps2"), 2),
            "accel_cmd": _round_float(row.get("accel_cmd"), 2),
            "long_active": row.get("long_active"),
            "lead_status": row.get("lead_status"),
            "lead_limited": row.get("lead_limited"),
            "lead_d_rel_m": _round_float(row.get("lead_d_rel_m"), 1),
            "lead_v_rel_mps": _round_float(row.get("lead_v_rel_mps"), 2),
            "gas_pressed": row.get("gas_pressed"),
            "brake_pressed": row.get("brake_pressed"),
            "lazy_candidate": row.get("lazy_candidate"),
            "carcontrol_long_control_state": row.get("carcontrol_long_control_state"),
            "controls_long_control_state": row.get("controls_long_control_state"),
        }
        if lat:
            sample.update({
                "lateral_error": _round_float(lat.get("lateral_error"), 3),
                "desired_lateral_accel": _round_float(lat.get("desired_lateral_accel"), 2),
                "actual_lateral_accel": _round_float(lat.get("actual_lateral_accel"), 2),
                "torque_output": _round_float(lat.get("torque_output"), 3),
                "carcontrol_torque": _round_float(lat.get("carcontrol_torque"), 3),
                "steering_angle_deg": _round_float(lat.get("steering_angle_deg"), 1),
                "steering_pressed": lat.get("steering_pressed"),
                "clean_lateral": lat.get("clean_lateral"),
                "pinned": lat.get("pinned"),
            })
        samples.append(sample)
    status = "ok" if samples else "telemetry unavailable: no timeseries rows in event window"
    return {"status": status, "samples": samples, "source": relpath(sqlite_path), "sample_count": len(samples)}


def _radar_lead_dict(lead: Any) -> dict[str, Any]:
    return {
        "status": bool(getattr(lead, "status", False)),
        "dRel_m": _round_float(getattr(lead, "dRel", None), 2),
        "yRel_m": _round_float(getattr(lead, "yRel", None), 2),
        "vRel_mps": _round_float(getattr(lead, "vRel", None), 2),
        "aRel_mps2": _round_float(getattr(lead, "aRel", None), 2),
        "vLead_mps": _round_float(getattr(lead, "vLead", None), 2),
        "vLeadK_mps": _round_float(getattr(lead, "vLeadK", None), 2),
        "aLeadK_mps2": _round_float(getattr(lead, "aLeadK", None), 2),
        "modelProb": _round_float(getattr(lead, "modelProb", None), 3),
        "radar": bool(getattr(lead, "radar", False)),
        "radarTrackId": inum(getattr(lead, "radarTrackId", -1)),
        "fcw": bool(getattr(lead, "fcw", False)),
    }


def _radar_point_dict(point: Any) -> dict[str, Any]:
    return {
        "trackId": inum(getattr(point, "trackId", -1)),
        "dRel_m": _round_float(getattr(point, "dRel", None), 2),
        "yRel_m": _round_float(getattr(point, "yRel", None), 2),
        "vRel_mps": _round_float(getattr(point, "vRel", None), 2),
        "aRel_mps2": _round_float(getattr(point, "aRel", None), 2),
        "measured": bool(getattr(point, "measured", False)),
    }


def _segment_log_candidates(route_id: str, segment: int, names: tuple[str, ...] = ("rlog.zst", "qlog.zst"), camera_source_path: str = "") -> list[Path]:
    candidates: list[Path] = []
    if camera_source_path:
        cam = ROOT / camera_source_path
        seg_root = cam.parent.parent if cam.parent.name.startswith(f"{route_id}--") else cam.parent
        for name in names:
            candidates.append(seg_root / f"{route_id}--{segment}" / name)
    for name in names:
        candidates.append(DEFAULT_RAW_DIR / route_id / "segments" / f"{route_id}--{segment}" / name)
        candidates.extend(DEFAULT_RAW_DIR.glob(f"*/segments/{route_id}--{segment}/{name}"))
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            out.append(p)
    return out


def _route_time_from_log_mono(log_mono_time: int, first_mono_time: int, segment_index: int) -> float:
    """Convert logMonoTime to route-relative seconds for copied segment logs.

    Some copied rlog/qlog artifacts preserve route-relative monotime (the first
    message is near route start), while others are segment-local.  Adding
    segment_index*60 unconditionally double-counts route-relative logs and drops
    radar/liveTracks windows ~one segment-block too late.  Detect the preserved
    route-relative shape by comparing the monotime delta to the expected segment
    base; otherwise keep the old segment-local conversion.
    """
    delta = (int(log_mono_time) - int(first_mono_time)) / 1e9
    route_base = segment_index * SEGMENT_LEN_SEC
    if segment_index > 0 and (route_base - SEGMENT_LEN_SEC * 0.75) <= delta <= (route_base + SEGMENT_LEN_SEC * 1.75):
        return delta
    return route_base + delta


def _read_radar_segment(path: Path, segment_index: int) -> dict[str, Any]:
    """Read one local rlog/qlog segment and cache compact radar rows."""
    key = f"{path}:{segment_index}"
    if key in RADAR_CACHE:
        return RADAR_CACHE[key]
    radar_rows: list[dict[str, Any]] = []
    live_rows: list[dict[str, Any]] = []
    car_rows: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []
    counts = {"radarState": 0, "liveTracks": 0, "carState": 0, "longitudinalPlan": 0}
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import zstandard as zstd  # type: ignore
        from cereal import log as capnp_log  # type: ignore
        dat = path.read_bytes()
        if dat.startswith(b"\x28\xB5\x2F\xFD"):
            with zstd.ZstdDecompressor().stream_reader(dat) as reader:
                dat = reader.read()
        first_mono: int | None = None
        route_base = segment_index * SEGMENT_LEN_SEC
        for msg in capnp_log.Event.read_multiple_bytes(dat):
            if first_mono is None:
                first_mono = int(msg.logMonoTime)
            try:
                typ = msg.which()
            except Exception:
                continue
            if typ not in counts:
                continue
            counts[typ] += 1
            rt = _route_time_from_log_mono(int(msg.logMonoTime), first_mono, segment_index)
            if typ == "radarState":
                rs = msg.radarState
                radar_rows.append({
                    "route_time_sec": rt,
                    "valid": bool(msg.valid),
                    "leadOne": _radar_lead_dict(rs.leadOne),
                    "leadTwo": _radar_lead_dict(rs.leadTwo),
                })
            elif typ == "liveTracks":
                pts = [_radar_point_dict(p) for p in msg.liveTracks.points]
                pts = [p for p in pts if p.get("dRel_m") is not None and p.get("vRel_mps") is not None]
                if pts:
                    live_rows.append({"route_time_sec": rt, "valid": bool(msg.valid), "points": pts})
            elif typ == "carState":
                cs = msg.carState
                car_rows.append({
                    "route_time_sec": rt,
                    "vEgo_mps": _round_float(getattr(cs, "vEgo", None), 2),
                    "vEgo_mph": _round_float(fnum(getattr(cs, "vEgo", None)) * MPH_PER_MPS, 1),
                    "aEgo_mps2": _round_float(getattr(cs, "aEgo", None), 2),
                    "gasPressed": bool(getattr(cs, "gasPressed", False)),
                    "brakePressed": bool(getattr(cs, "brakePressed", False)),
                    "steeringPressed": bool(getattr(cs, "steeringPressed", False)),
                    "vCruise_mph": _round_float(fnum(getattr(cs, "vCruise", None)) * 0.62137119223733, 1),
                })
            elif typ == "longitudinalPlan":
                lp = msg.longitudinalPlan
                accels = list(getattr(lp, "accels", []))
                plan_rows.append({
                    "route_time_sec": rt,
                    "source": str(getattr(lp, "longitudinalPlanSource", "")).replace("<", "").replace(" enum>", ""),
                    "accel0_mps2": _round_float(accels[0] if accels else None, 2),
                })
        out = {"status": "ok", "radar_rows": radar_rows, "live_rows": live_rows, "car_rows": car_rows, "plan_rows": plan_rows, "counts": counts, "source": relpath(path)}
    except Exception as e:
        detail = str(e).splitlines()[0][:180]
        out = {"status": f"radar raw read failed/corrupt: {detail}", "radar_rows": [], "live_rows": [], "car_rows": [], "plan_rows": [], "counts": counts, "source": relpath(path)}
    RADAR_CACHE[key] = out
    return out


def _nearest_raw(rows: list[dict[str, Any]], route_time: float, max_delta: float = 0.35) -> dict[str, Any] | None:
    if not rows:
        return None
    best = min(rows, key=lambda r: abs(fnum(r.get("route_time_sec"), -999999) - route_time))
    return best if abs(fnum(best.get("route_time_sec"), -999999) - route_time) <= max_delta else None


def _plot_y_for_point(point: dict[str, Any], lead1: dict[str, Any] | None, lead2: dict[str, Any] | None) -> tuple[float | None, str]:
    y = fnum(point.get("yRel_m"))
    d = fnum(point.get("dRel_m"))
    if math.isfinite(y):
        return round(y, 2), "raw"
    for name, lead in (("leadOne", lead1), ("leadTwo", lead2)):
        if not lead or not lead.get("status") or lead.get("yRel_m") is None:
            continue
        ly = fnum(lead.get("yRel_m"))
        ld = fnum(lead.get("dRel_m"))
        same_track = inum(point.get("trackId"), -999999) == inum(lead.get("radarTrackId"), -888888)
        close_range = math.isfinite(d) and math.isfinite(ld) and abs(d - ld) <= 3.0
        if same_track or close_range:
            return round(ly, 2), f"matched_{name}"
    return 0.0, "centerline_fallback"


def _lead_only_radar_from_telemetry(event: dict[str, Any], reason: str = "raw radar logs unavailable") -> dict[str, Any]:
    telemetry = event.get("telemetry") or {}
    tel_samples = telemetry.get("samples") or []
    if telemetry.get("status") != "ok" or not tel_samples:
        return {
            "status": f"radar unavailable: {reason}; no local telemetry samples for fallback",
            "mode": "unavailable",
            "samples": [],
            "source": telemetry.get("source", ""),
            "local_only": True,
            "raw_point_sample_count": 0,
            "raw_point_count": 0,
            "lead_only": True,
        }

    samples: list[dict[str, Any]] = []
    for row in tel_samples:
        samples.append({
            "t": row.get("t"),
            "route_time_sec": row.get("route_time_sec"),
            "lead_status": row.get("lead_status"),
            "lead_d_rel_m": row.get("lead_d_rel_m"),
            "lead_v_rel_mps": row.get("lead_v_rel_mps"),
            "lead_limited": row.get("lead_limited"),
            "ego_speed_mph": row.get("speed_mph"),
            "set_speed_mph": row.get("set_speed_mph"),
            "a_ego_mps2": row.get("a_ego_mps2"),
            "accel_cmd": row.get("accel_cmd"),
            "long_active": row.get("long_active"),
            "brake_pressed": row.get("brake_pressed"),
            "gas_pressed": row.get("gas_pressed"),
            "long_control_state": row.get("carcontrol_long_control_state") or row.get("controls_long_control_state"),
        })

    valid = [i for i, s in enumerate(samples) if bool(s.get("lead_status")) and fnum(s.get("lead_d_rel_m"), 0) > 0]
    for pos, i in enumerate(valid):
        if samples[i].get("lead_v_rel_mps") is not None:
            continue
        neighbors = []
        if pos > 0:
            neighbors.append(samples[valid[pos - 1]])
        if pos + 1 < len(valid):
            neighbors.append(samples[valid[pos + 1]])
        slopes = []
        for other in neighbors:
            dt_s = fnum(samples[i].get("t")) - fnum(other.get("t"))
            dd = fnum(samples[i].get("lead_d_rel_m")) - fnum(other.get("lead_d_rel_m"))
            if abs(dt_s) >= 0.15 and math.isfinite(dd):
                slopes.append(dd / dt_s)
        if slopes:
            samples[i]["lead_v_rel_mps"] = round(sum(slopes) / len(slopes), 2)
            samples[i]["lead_v_rel_source"] = "distance_delta"

    return {
        "status": "ok" if samples else "radar unavailable: no local lead timeline samples",
        "mode": "lead_only",
        "samples": samples,
        "sample_count": len(samples),
        "lead_sample_count": len(valid),
        "raw_point_sample_count": 0,
        "raw_point_count": 0,
        "source": telemetry.get("source", ""),
        "local_only": True,
        "lead_only": True,
        "note": f"Lead-only fallback from local analysis telemetry ({reason}); raw liveTracks point cloud is not available for this event.",
    }


def load_radar_for_event(event: dict[str, Any]) -> dict[str, Any]:
    """Build a compact local radar timeline from rlog/qlog when available.

    Prefer liveTracks point cloud plus radarState leadOne/leadTwo, synced to the
    clip via clip_start_route_time_sec. Fall back explicitly to the older
    lead-only telemetry timeline when raw radar logs are missing or empty.
    """
    sync = event.get("video_sync") or build_video_sync(event, DEFAULT_LOOKBACK_SEC, DEFAULT_AFTER_SEC)
    clip_start = fnum(sync.get("clip_start_route_time_sec"), fnum(event.get("start_route_time_sec"), 0) - 6.0)
    start_t = clip_start - 1.0
    end_t = fnum(sync.get("clip_end_route_time_sec"), fnum(event.get("end_route_time_sec"), start_t + 15.0) + 8.0) + 1.0
    start_seg = max(0, int(start_t // SEGMENT_LEN_SEC))
    end_seg = max(start_seg, int(max(end_t - 0.001, start_t) // SEGMENT_LEN_SEC))
    route_id = str(event.get("route_id") or "")
    if not route_id:
        return _lead_only_radar_from_telemetry(event, "route id missing")

    source_logs: list[str] = []
    raw_statuses: list[str] = []
    radar_rows: list[dict[str, Any]] = []
    live_rows: list[dict[str, Any]] = []
    car_rows: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []
    counts = {"radarState": 0, "liveTracks": 0, "carState": 0, "longitudinalPlan": 0}
    for seg in range(start_seg, end_seg + 1):
        paths = _segment_log_candidates(route_id, seg, ("rlog.zst", "qlog.zst"), str(event.get("camera_source_path") or ""))
        if not paths:
            raw_statuses.append(f"segment {seg}: no rlog/qlog")
            continue
        # Prefer rlog for full liveTracks/radarState; qlog is a fallback if it is all we have.
        path = sorted(paths, key=lambda p: 0 if p.name == "rlog.zst" else 1)[0]
        seg_data = _read_radar_segment(path, seg)
        source_logs.append(seg_data.get("source") or relpath(path))
        if seg_data.get("status") != "ok":
            raw_statuses.append(f"segment {seg}: {seg_data.get('status')}")
            continue
        for k in counts:
            counts[k] += inum((seg_data.get("counts") or {}).get(k), 0)
        radar_rows.extend([r for r in seg_data.get("radar_rows", []) if start_t <= fnum(r.get("route_time_sec")) <= end_t])
        live_rows.extend([r for r in seg_data.get("live_rows", []) if start_t <= fnum(r.get("route_time_sec")) <= end_t])
        car_rows.extend([r for r in seg_data.get("car_rows", []) if start_t <= fnum(r.get("route_time_sec")) <= end_t])
        plan_rows.extend([r for r in seg_data.get("plan_rows", []) if start_t <= fnum(r.get("route_time_sec")) <= end_t])

    radar_rows.sort(key=lambda r: fnum(r.get("route_time_sec"), 0))
    live_rows.sort(key=lambda r: fnum(r.get("route_time_sec"), 0))
    car_rows.sort(key=lambda r: fnum(r.get("route_time_sec"), 0))
    plan_rows.sort(key=lambda r: fnum(r.get("route_time_sec"), 0))
    if not radar_rows and not live_rows:
        reason = "; ".join(raw_statuses[:4]) if raw_statuses else "no radarState/liveTracks messages in event window"
        return _lead_only_radar_from_telemetry(event, reason)

    base_rows = radar_rows if radar_rows else live_rows
    stride = max(1, math.ceil(len(base_rows) / RADAR_MAX_SAMPLES))
    samples: list[dict[str, Any]] = []
    point_samples = 0
    total_points = 0
    y_fallback_counts = {"raw": 0, "matched": 0, "centerline": 0}
    for row in base_rows[::stride]:
        rt = fnum(row.get("route_time_sec"))
        if not math.isfinite(rt):
            continue
        radar = row if "leadOne" in row else _nearest_raw(radar_rows, rt, 0.25)
        live = row if "points" in row else _nearest_raw(live_rows, rt, 0.35)
        car = _nearest_raw(car_rows, rt, 0.35) or {}
        plan = _nearest_raw(plan_rows, rt, 0.35) or {}
        lead1 = (radar or {}).get("leadOne") if radar else None
        lead2 = (radar or {}).get("leadTwo") if radar else None
        pts: list[dict[str, Any]] = []
        for point in (live or {}).get("points", []):
            d = fnum(point.get("dRel_m"))
            v = fnum(point.get("vRel_mps"))
            if not (math.isfinite(d) and math.isfinite(v)) or d < -2 or d > 160:
                continue
            p = dict(point)
            plot_y, source = _plot_y_for_point(p, lead1, lead2)
            p["plot_y_rel_m"] = plot_y
            p["plot_y_source"] = source
            if source == "raw":
                y_fallback_counts["raw"] += 1
            elif source.startswith("matched"):
                y_fallback_counts["matched"] += 1
            else:
                y_fallback_counts["centerline"] += 1
            pts.append(p)
        pts.sort(key=lambda p: abs(fnum(p.get("dRel_m"), 999) - fnum((lead1 or {}).get("dRel_m"), 0)) if lead1 and lead1.get("status") else fnum(p.get("dRel_m"), 999))
        if len(pts) > RADAR_MAX_POINTS_PER_SAMPLE:
            pts = pts[:RADAR_MAX_POINTS_PER_SAMPLE]
        if pts:
            point_samples += 1
            total_points += len(pts)
        samples.append({
            "t": round(rt - clip_start, 3),
            "route_time_sec": round(rt, 3),
            "leadOne": lead1,
            "leadTwo": lead2,
            "points": pts,
            "ego_speed_mph": car.get("vEgo_mph"),
            "set_speed_mph": car.get("vCruise_mph"),
            "a_ego_mps2": car.get("aEgo_mps2"),
            "gas_pressed": car.get("gasPressed"),
            "brake_pressed": car.get("brakePressed"),
            "steering_pressed": car.get("steeringPressed"),
            "plan_source": plan.get("source"),
            "accel_cmd": plan.get("accel0_mps2"),
        })

    lead_samples = sum(1 for s in samples if (s.get("leadOne") or {}).get("status") or (s.get("leadTwo") or {}).get("status"))
    if not samples:
        return _lead_only_radar_from_telemetry(event, "raw radar logs had no samples in event window")
    if point_samples == 0 and lead_samples == 0:
        return _lead_only_radar_from_telemetry(event, "raw rlog/qlog had radar messages but no liveTrack points or fused lead locks in this event window")
    mode = "point_cloud" if point_samples else "lead_only_raw"
    note = "Raw local rlog/qlog radarState + liveTracks point-cloud timeline; yRel NaNs are plotted with the same matched-lead/centerline fallback used by the prior animation."
    if not point_samples:
        note = "Raw local rlog/qlog radarState leadOne/leadTwo timeline; no liveTracks point-cloud samples were present in this event window, so the UI is lead-only."
    return {
        "status": "ok",
        "mode": mode,
        "samples": samples,
        "sample_count": len(samples),
        "lead_sample_count": lead_samples,
        "raw_point_sample_count": point_samples,
        "raw_point_count": total_points,
        "raw_y_fallback_counts": y_fallback_counts,
        "source_logs": source_logs,
        "source": "; ".join(source_logs[:3]) + ("; …" if len(source_logs) > 3 else ""),
        "counts_in_window_logs": counts,
        "local_only": True,
        "lead_only": point_samples == 0,
        "note": note,
    }

def qlog_path_for_event(event: dict[str, Any]) -> Path | None:
    cam = event.get("camera_source_path")
    candidates: list[Path] = []
    if cam:
        candidates.append((ROOT / cam).parent / "qlog.zst")
        candidates.append((ROOT / cam).parent / "rlog.zst")
    route_id = event.get("route_id") or ""
    seg = inum(event.get("segment_index"), -1)
    if route_id and seg >= 0:
        candidates.append(DEFAULT_RAW_DIR / route_id / "segments" / f"{route_id}--{seg}" / "qlog.zst")
        candidates.extend(DEFAULT_RAW_DIR.glob(f"*_{route_id}/segments/{route_id}--{seg}/qlog.zst"))
    for p in candidates:
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _read_gps_segment(path: Path) -> dict[str, Any]:
    key = str(path)
    if key in GPS_CACHE:
        return GPS_CACHE[key]
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import zstandard as zstd  # type: ignore
        from cereal import log as capnp_log  # type: ignore
        dat = path.read_bytes()
        if dat.startswith(b"\x28\xB5\x2F\xFD"):
            with zstd.ZstdDecompressor().stream_reader(dat) as reader:
                dat = reader.read()
        seg_idx = _segment_from_camera_path(path) or inum(path.parent.name.rsplit("--", 1)[-1], 0)
        route_base = seg_idx * SEGMENT_LEN_SEC
        first_mono: int | None = None
        samples: list[dict[str, Any]] = []
        for msg in capnp_log.Event.read_multiple_bytes(dat):
            if first_mono is None:
                first_mono = int(msg.logMonoTime)
            try:
                typ = msg.which()
            except Exception:
                continue
            if typ not in {"gpsLocationExternal", "gpsLocation"}:
                continue
            g = getattr(msg, typ)
            lat = fnum(getattr(g, "latitude", None))
            lon = fnum(getattr(g, "longitude", None))
            has_fix = bool(getattr(g, "hasFix", False))
            hacc = fnum(getattr(g, "horizontalAccuracy", None), math.nan)
            if not (has_fix and abs(lat) > 0.001 and abs(lon) > 0.001):
                continue
            if math.isfinite(hacc) and hacc > 150:
                continue
            samples.append({
                "route_time_sec": round(_route_time_from_log_mono(int(msg.logMonoTime), first_mono, seg_idx), 3),
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "speed_mps": _round_float(getattr(g, "speed", None), 2),
                "bearing_deg": _round_float(getattr(g, "bearingDeg", None), 1),
                "horizontal_accuracy_m": _round_float(hacc, 1),
            })
        out = {"status": "ok" if samples else "map telemetry unavailable: no valid GPS fix in qlog", "samples": samples, "source": relpath(path)}
    except Exception as e:
        detail = str(e).splitlines()[0][:180]
        out = {"status": f"map telemetry unavailable: qlog GPS read failed/corrupt: {detail}", "samples": [], "source": relpath(path)}
    GPS_CACHE[key] = out
    return out


def load_map_for_event(event: dict[str, Any]) -> dict[str, Any]:
    sync = event.get("video_sync") or {}
    start_t = fnum(sync.get("clip_start_route_time_sec"), fnum(event.get("start_route_time_sec"), 0) - 6.0) - 5.0
    end_t = fnum(sync.get("clip_end_route_time_sec"), fnum(event.get("end_route_time_sec"), start_t + 15.0) + 8.0) + 5.0
    qlog = qlog_path_for_event(event)
    if not qlog:
        return {"status": "map telemetry unavailable: qlog not copied for this segment", "samples": []}
    seg = _read_gps_segment(qlog)
    clip_start = fnum(sync.get("clip_start_route_time_sec"), start_t + 5.0)
    samples = []
    for s in seg.get("samples", []):
        rt = fnum(s.get("route_time_sec"))
        if start_t <= rt <= end_t:
            d = dict(s)
            d["t"] = round(rt - clip_start, 3)
            samples.append(d)
    if not samples:
        return {"status": seg.get("status") if seg.get("status") != "ok" else "map telemetry unavailable: no GPS samples in event window", "samples": [], "source": seg.get("source")}
    event_t = fnum(event.get("start_route_time_sec"), start_t)
    nearest = min(samples, key=lambda s: abs(fnum(s.get("route_time_sec"), 0) - event_t))
    return {
        "status": "ok",
        "samples": samples,
        "event_position": nearest,
        "source": seg.get("source"),
        "tile_provider": "OpenStreetMap standard tiles (browser fetches tile images only)",
        "privacy_note": "No logs are uploaded by this UI, but enabling/viewing OSM tiles requests public map tile images from OpenStreetMap.",
    }


def event_id(route: dict[str, Any], category: str, row: dict[str, str]) -> str:
    route_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", route.get("route_label") or route.get("analysis_name") or "route")[:48]
    idx = row.get("event_index") or row.get("burst_index") or row.get("rank") or row.get("bookmark_tag_index") or "x"
    seg = segment_index(row)
    t = event_time(row)
    return f"{route_slug}__{category}__s{seg:02d}__t{int(round(t)):05d}__e{idx}"


def make_candidate(route: dict[str, Any], category: str, source_file: str, row: dict[str, str], labels_for_route: list[dict[str, Any]]) -> dict[str, Any]:
    start = event_time(row)
    end = event_end_time(row)
    seg = segment_index(row)
    near = nearby_labels(labels_for_route, start)
    priority, rationale = priority_for(category, row, near)
    cam = find_camera_file(route, seg)
    metrics_keys = [
        "duration_sec", "start_speed_mph", "avg_mph", "speed_mph", "max_mph",
        "target_set_speed_mph", "max_speed_deficit_mph", "final_speed_deficit_mph",
        "time_to_within_2mph_sec", "mean_accel_cmd", "max_accel_cmd",
        "mean_actual_a_ego", "max_actual_a_ego", "min_mph", "max_abs_lateral_error",
        "mean_abs_lateral_error", "max_abs_output_rate", "mean_abs_output_rate",
        "pinned_seen", "pinned_time_sec", "driver_gas_or_brake", "driver_gas",
        "driver_brake", "driver_steering", "steering_pressed", "gas_pressed",
        "brake_pressed", "lead_limited", "lead_status_at_start", "lead_d_rel_m_at_start",
        "min_lead_d_rel_m", "long_control_states", "intervention_type", "speed_regime",
        "turn_regime", "lazy_flag", "achieved_within_2mph", "incomplete", "notes",
        "confidence", "reason", "event_type", "delay_sec",
    ]
    ev = {
        "event_id": event_id(route, category, row),
        "category": category,
        "source_file": source_file,
        "route_label": route.get("route_label", ""),
        "model": route.get("model", ""),
        "route_id": route.get("route_id", ""),
        "test_date": route.get("test_date", ""),
        "branch": route.get("branch", ""),
        "version": route.get("version", ""),
        "selfdrive_profile": route.get("selfdrive_profile", ""),
        "segment_index": seg,
        "start_segment_time_sec": round(fnum(row.get("start_segment_time_sec") or row.get("segment_time_sec"), start - seg * SEGMENT_LEN_SEC), 3),
        "end_segment_time_sec": round(fnum(row.get("end_segment_time_sec"), end - seg * SEGMENT_LEN_SEC), 3),
        "start_route_time_sec": round(start, 3),
        "end_route_time_sec": round(end, 3),
        "duration_sec": round(max(0.0, end - start), 3),
        "priority": priority,
        "priority_rationale": rationale,
        "tool_guess": "",  # filled below
        "question_for_dan": "",
        "nearby_labels": near,
        "metrics": summarize_metrics(row, metrics_keys),
        "analysis_dir": relpath(route["analysis_dir"]),
        "camera_source_path": relpath(cam) if cam else "",
        "video_clip_path": "",
        "video_status": "camera artifact found" if cam else "missing camera artifact",
        "privacy": {"local_only": True, "vin_redacted": True, "no_upload": True},
    }
    ev["tool_guess"] = guess_for(category, row, near)
    ev["question_for_dan"] = question_for(category, row, near)
    return ev


def collect_candidates(limit_routes: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    routes = [route_from_logdrive_dir(p) for p in sorted(DEFAULT_LOGDRIVE_DIR.glob("*")) if p.is_dir() and (p / "catalog.yaml").exists()]
    if limit_routes:
        routes = routes[-limit_routes:]
    video_files = list(DEFAULT_RAW_DIR.rglob("*.hevc")) + list(DEFAULT_RAW_DIR.rglob("*.mp4")) + list(DEFAULT_RAW_DIR.rglob("*camera*"))
    ffmpeg = shutil.which("ffmpeg")
    raw_segment_count = len(list(DEFAULT_RAW_DIR.glob("*/segments/*"))) if DEFAULT_RAW_DIR.exists() else 0
    inspection = {
        "routes_scanned": len(routes),
        "raw_segments_seen": raw_segment_count,
        "camera_video_artifacts_seen": len([p for p in video_files if p.is_file()]),
        "ffmpeg": ffmpeg or "not found",
        "best_clip_source": "qcamera/dcamera/ecamera/fcamera if present in copied segment dirs; none found in current local raw route dirs" if not video_files else "segment camera file",
    }
    for route in routes:
        labels = load_route_labels(route.get("route_id", ""))
        # Explicit PHEV context labels from the down-swipe route are high value.
        for lab_i, lab in enumerate(labels):
            if lab.get("reason") == "phev_context":
                row = {
                    "event_index": str(lab_i + 1),
                    "segment_index": str(lab.get("segment_index", -1)),
                    "start_route_time_sec": str(lab.get("route_time_sec", 0)),
                    "end_route_time_sec": str(fnum(lab.get("route_time_sec"), 0) + 1.0),
                    "start_segment_time_sec": str(fnum(lab.get("route_time_sec"), 0) % SEGMENT_LEN_SEC),
                    "duration_sec": "1.0",
                    "reason": "phev_context",
                    "post_warmup": "1" if fnum(lab.get("route_time_sec"), 0) >= 90 else "0",
                }
                candidates.append(make_candidate(route, "phev_label_uncertain", "bookmark_tags_route.jsonl", row, labels))
        for category, fname in EVENT_FILES.items():
            for row in read_csv(route["analysis_dir"] / fname):
                # Keep the queue useful: favor post-warmup and notable rows.
                if row.get("post_warmup") == "0" and category not in {"label_tool_disagreement", "phev_label_uncertain"}:
                    continue
                include = False
                if category == "accel_lazy_or_eager":
                    include = boolish(row.get("lazy_flag")) or fnum(row.get("final_speed_deficit_mph"), 0) > 3.5 or not boolish(row.get("achieved_within_2mph"))
                elif category in {"stop_creep", "stop_go"}:
                    include = True
                elif category == "brake_regen_blend":
                    itype = row.get("intervention_type", "")
                    include = "brake" in itype or fnum(row.get("avg_mph"), 99) < 18
                elif category == "low_speed_pinned":
                    include = fnum(row.get("avg_mph"), 99) < 28 or fnum(row.get("max_abs_lateral_error"), 0) > 0.25
                elif category == "steering_jerk":
                    include = fnum(row.get("max_abs_output_rate"), 0) > 5
                elif category == "lateral_watch":
                    include = fnum(row.get("abs_lateral_error"), 0) > 0.35 and fnum(row.get("speed_mph"), 99) < 40
                if include:
                    candidates.append(make_candidate(route, category, fname, row, labels))
        # Explicit bookmark/tool disagreements.
        for row in read_csv(route["analysis_dir"] / "bookmark_event_matches.csv"):
            reason = row.get("reason", "")
            et = row.get("event_type", "")
            mismatch = reason and et and not any(part in et for part in ([reason] if reason != "braking" else ["brak", "stop"]))
            if mismatch or row.get("confidence") in {"low", "medium"}:
                candidates.append(make_candidate(route, "label_tool_disagreement", "bookmark_event_matches.csv", row, labels))
    return candidates, inspection


def select_diverse(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    candidates = sorted(candidates, key=lambda x: (-fnum(x.get("priority"), 0), x.get("route_label", ""), x.get("start_route_time_sec", 0)))
    selected: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    caps = {
        "phev_label_uncertain": 18,
        "label_tool_disagreement": 14,
        "accel_lazy_or_eager": 14,
        "stop_creep": 10,
        "stop_go": 10,
        "brake_regen_blend": 8,
        "low_speed_pinned": 8,
        "steering_jerk": 8,
        "lateral_watch": 8,
    }
    category_floor = {
        "phev_label_uncertain": 10,
        "label_tool_disagreement": 8,
        "accel_lazy_or_eager": 8,
        "stop_creep": 5,
        "stop_go": 5,
        "brake_regen_blend": 6,
        "low_speed_pinned": 6,
        "steering_jerk": 6,
        "lateral_watch": 6,
    }
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for ev in candidates:
        by_cat.setdefault(ev["category"], []).append(ev)

    def can_add(ev: dict[str, Any], *, allow_route_overflow: bool = False) -> bool:
        cat = ev["category"]
        route = ev["route_label"]
        if ev in selected:
            return False
        if category_counts.get(cat, 0) >= caps.get(cat, 8):
            return False
        if not allow_route_overflow and route_counts.get(route, 0) >= max(8, limit // 6):
            return False
        t = fnum(ev.get("start_route_time_sec"), 0)
        if any(s["route_label"] == route and abs(fnum(s.get("start_route_time_sec"), 0) - t) < 3.0 for s in selected):
            return False
        return True

    def add(ev: dict[str, Any]) -> None:
        selected.append(ev)
        cat = ev["category"]
        route = ev["route_label"]
        category_counts[cat] = category_counts.get(cat, 0) + 1
        route_counts[route] = route_counts.get(route, 0) + 1

    # First guarantee useful coverage of every requested review mode when data exists.
    for cat, floor in category_floor.items():
        for ev in by_cat.get(cat, []):
            if len(selected) >= limit or category_counts.get(cat, 0) >= min(floor, caps.get(cat, floor), len(by_cat.get(cat, []))):
                break
            if can_add(ev):
                add(ev)

    # Then fill by global priority with category/route caps.
    for ev in candidates:
        if len(selected) >= limit:
            break
        if can_add(ev):
            add(ev)
    # If caps were too restrictive, top off with best remaining non-duplicates.
    if len(selected) < min(limit, len(candidates)):
        ids = {ev["event_id"] for ev in selected}
        for ev in candidates:
            if ev["event_id"] in ids:
                continue
            if category_counts.get(ev["category"], 0) >= caps.get(ev["category"], 8):
                continue
            route = ev["route_label"]
            t = fnum(ev.get("start_route_time_sec"), 0)
            if any(s["route_label"] == route and abs(fnum(s.get("start_route_time_sec"), 0) - t) < 3.0 for s in selected):
                continue
            add(ev); ids.add(ev["event_id"])
            if len(selected) >= limit:
                break
    for i, ev in enumerate(selected, start=1):
        ev["queue_rank"] = i
    return selected


def flatten_for_csv(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "queue_rank": ev.get("queue_rank"),
        "event_id": ev.get("event_id"),
        "category": ev.get("category"),
        "priority": ev.get("priority"),
        "route_label": ev.get("route_label"),
        "model": ev.get("model"),
        "route_id": ev.get("route_id"),
        "segment_index": ev.get("segment_index"),
        "start_route_time_sec": ev.get("start_route_time_sec"),
        "start_segment_time_sec": ev.get("start_segment_time_sec"),
        "clip_start_route_time_sec": ev.get("clip_start_route_time_sec"),
        "clip_end_route_time_sec": ev.get("clip_end_route_time_sec"),
        "event_offset_sec": ev.get("event_offset_sec"),
        "lookback_sec": ev.get("lookback_sec"),
        "after_sec": ev.get("after_sec"),
        "duration_sec": ev.get("duration_sec"),
        "tool_guess": ev.get("tool_guess"),
        "question_for_dan": ev.get("question_for_dan"),
        "nearby_labels": "; ".join(f"{x.get('reason')}@{x.get('delta_sec')}s" for x in ev.get("nearby_labels", [])),
        "video_clip_path": ev.get("video_clip_path"),
        "video_status": ev.get("video_status"),
        "openpilot_ui_clip_path": ev.get("openpilot_ui_clip_path"),
        "openpilot_ui_status": ev.get("openpilot_ui_status"),
        "analysis_dir": ev.get("analysis_dir"),
    }


def write_ui(out_dir: Path) -> None:
    out_prefix = relpath(out_dir).rstrip("/") + "/"
    html = INDEX_HTML.replace("const outPrefix = '__OUT_PREFIX__/';", f"const outPrefix = '{out_prefix}';")
    readme = README_MD.replace("~/BrickpilotDriveDB/analysis_exports/drive_tests/review_queue_20260512_batch2", relpath(out_dir).rstrip("/"))
    (out_dir / "review_server.py").write_text(SERVER_PY, encoding="utf-8")
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / "README.md").write_text(readme, encoding="utf-8")


def write_report(out_dir: Path, queue: list[dict[str, Any]], inspection: dict[str, Any], args: argparse.Namespace) -> None:
    counts: dict[str, int] = {}
    video_counts: dict[str, int] = {}
    telemetry_ok = 0
    map_ok = 0
    radar_ok = 0
    radar_point_cloud = 0
    radar_lead_only = 0
    for ev in queue:
        counts[ev["category"]] = counts.get(ev["category"], 0) + 1
        video_counts[ev["video_status"]] = video_counts.get(ev["video_status"], 0) + 1
        telemetry_ok += 1 if (ev.get("telemetry") or {}).get("status") == "ok" else 0
        map_ok += 1 if (ev.get("map") or {}).get("status") == "ok" else 0
        radar = ev.get("radar") or {}
        radar_ok += 1 if radar.get("status") == "ok" else 0
        radar_point_cloud += 1 if inum(radar.get("raw_point_sample_count"), 0) > 0 else 0
        radar_lead_only += 1 if radar.get("status") == "ok" and inum(radar.get("raw_point_sample_count"), 0) <= 0 else 0
    lines = [
        "# Brickpilot Human Review Queue Prototype (2026-05-12)",
        "",
        "Local-only prototype for human review / RL-style labels. It does **not** change vehicle behavior and does not upload logs or clips.",
        "",
        "## Outputs",
        "",
        f"- Queue JSON: `{relpath(out_dir / 'queue.json')}`",
        f"- Queue CSV: `{relpath(out_dir / 'queue.csv')}`",
        f"- UI: `{relpath(out_dir / 'index.html')}`",
        f"- Local review server: `{relpath(out_dir / 'review_server.py')}`",
        f"- Review labels append to: `{relpath(out_dir / 'reviews.jsonl')}`",
        "",
        "## How to run",
        "",
        "```bash",
        f"cd {out_dir}",
        "python3 review_server.py --port 8765",
        "# LAN option, if the reviewer wants to open from a phone on the same WiFi:",
        "# python3 review_server.py --host 0.0.0.0 --port 8765",
        "# open http://127.0.0.1:8765/",
        "```",
        "",
        "Regenerate the queue:",
        "",
        "```bash",
        f"python3 scripts/drive_tests/review_queue.py --out {relpath(out_dir)} --limit {args.limit}",
        "```",
        "",
        "## Artifact inspection",
        "",
        f"- Logdrive analysis routes scanned: {inspection.get('routes_scanned')}",
        f"- Local raw segment dirs seen: {inspection.get('raw_segments_seen')}",
        f"- Camera/qcamera/dcamera/ecamera/fcamera/video artifacts found in raw route dirs: {inspection.get('camera_video_artifacts_seen')}",
        f"- ffmpeg: `{inspection.get('ffmpeg')}`",
        f"- Best current clip source: {inspection.get('best_clip_source')}",
        "",
        "Current result: entries remain reviewable even when clips, GPS, or telemetry are unavailable; each event carries explicit `video_status`, `map.status`, and `telemetry.status` fields. The generator uses copied `qcamera`/`dcamera`/`ecamera`/`fcamera` plus ffmpeg when available.",
        f"Clip timing now defaults to {args.seconds_before:.0f}s lookback and {args.seconds_after:.0f}s after the selected marker. For bookmark/tag-driven items, the selected marker is the human bookmark because reviewer labels are after-the-fact reactions; clip metadata exposes route start/end, event offset, bookmark offsets, and source segments.",
        "",
        "## Queue composition",
        "",
    ]
    for k, v in sorted(counts.items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Video status", ""]
    for k, v in sorted(video_counts.items()):
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "## Cockpit data availability",
        "",
        f"- Telemetry timelines available: {telemetry_ok}/{len(queue)} events",
        f"- Radar/lead timelines available: {radar_ok}/{len(queue)} events",
        f"- Radar point-cloud timelines from local rlog/qlog liveTracks: {radar_point_cloud}/{len(queue)} events; lead-only fallback: {radar_lead_only}/{len(queue)} events",
        f"- GPS/map timelines available: {map_ok}/{len(queue)} events",
        "- OpenStreetMap tiles require browser network access when viewed; route logs/clips/reviews remain local files and are not uploaded by the UI.",
        "- Context panel: desktop shows the existing OSM map and local-only radar view together; mobile keeps the Map/Radar tabs as a space-saving toggle. Both sync to video.currentTime. Radar prefers local rlog/qlog liveTracks point-cloud + radarState leadOne/leadTwo, and explicitly marks lead-only fallback when raw points are absent.",
        "- Real comma UI source behavior: when `openpilot_ui_clip_path` exists, that real comma/rainbow-road source is primary and the local telemetry fallback overlay stays hidden. Raw qcamera can still be selected for comparison, with speed/set-speed/lead/path fallback overlay and optional telemetry cards.",
        "- Experimental real openpilot UI clips: pass `--openpilot-ui-clips` to render a small number of local `tools/clip/run.py` onroad UI videos from copied qcamera+rlog segments. This is heavier and dependency-sensitive, so it is opt-in and writes to `openpilot_ui_clips/` when successful.",
        "- Real openpilot replay inspection: `tools/replay/ui.py`/`tools/replay/lib/ui_helpers.py` draw actual model/radar overlays from live replay messaging/VisionIPC, `selfdrive/ui/ui.py` is the production UI entrypoint, and `selfdrive/ui/tests/diff/replay.py` records deterministic UI scripts. For static review clips, `tools/clip/run.py` is the most practical local bridge because it feeds qcamera frames through VisionIPC and patches ui_state from local rlogs.",
        "",
        "## Selection strategy",
        "",
        "The generator samples high-priority, diverse events rather than asking the reviewer to review every event:",
        "",
        "- explicit PHEV-context down-swipe bookmarks needing disambiguation;",
        "- acceleration undershoot/lazy or possibly eager events;",
        "- stop/creep and stop/go candidates;",
        "- brake/regen/intervention candidates;",
        "- low-speed pinned output, steering jerk, and lateral watch events;",
        "- bookmark/tool disagreement or low/medium confidence matches.",
        "",
        "It caps categories/routes and deduplicates nearby events to keep the review queue manageable.",
        "",
        "## Privacy guard",
        "",
        "- Local-only files under this workspace.",
        "- No upload or external hosting code.",
        "- Review labels are written to local `reviews.jsonl` by the localhost server only.",
        "- VIN-like token grep was run during smoke gates; see final subagent summary for result.",
        "",
        "## Remaining work",
        "",
        "- Where camera artifacts are missing for part of a cross-segment window, copy the missing qcamera segments and regenerate; available parts stay reviewable with explicit missing segment metadata.",
        "- If `--openpilot-ui-clips` reports dependency failures, install/run inside the normal openpilot Python environment with pyray, requests, coverage/ffmpeg, then regenerate only a few events first via `--openpilot-ui-limit`.",
        "- After reviewer labels are complete, ingest `reviews.jsonl` into the Brickpilot labeling/training analysis loop.",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def privacy_scan(paths: list[Path]) -> list[str]:
    hits = []
    for path in paths:
        if not path.exists() or path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in VIN_RE.finditer(text):
            token = m.group(0)
            # openpilot-ish route names are shorter; this is a true 17-char token check.
            hits.append(f"{path}: {token}")
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=60, help="target queue size (30-80 recommended)")
    ap.add_argument("--limit-routes", type=int, default=None, help="debug/smoke: only scan newest N logdrive analysis dirs")
    ap.add_argument("--seconds-before", type=float, default=DEFAULT_LOOKBACK_SEC, help="lookback before the chosen tool/bookmark marker; bookmark-driven clips default to 60s because labels are reactions after the event")
    ap.add_argument("--seconds-after", type=float, default=DEFAULT_AFTER_SEC, help="context after the chosen tool/bookmark marker")
    ap.add_argument("--no-clips", action="store_true", help="do not invoke ffmpeg even if camera files are present")
    ap.add_argument("--openpilot-ui-clips", action="store_true", help="experimental/heavy: render real openpilot onroad UI clips with local tools/clip from copied qcamera+rlog segments")
    ap.add_argument("--openpilot-ui-limit", type=int, default=3, help="max openpilot UI render attempts when --openpilot-ui-clips is enabled")
    ap.add_argument("--openpilot-ui-timeout-sec", type=float, default=300.0, help="per-event timeout for experimental openpilot UI renders")
    args = ap.parse_args()

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates, inspection = collect_candidates(args.limit_routes)
    queue = select_diverse(candidates, args.limit)
    openpilot_ui_attempts = 0
    for ev in queue:
        ev["video_sync"] = build_video_sync(ev, args.seconds_before, args.seconds_after)
        sync = ev["video_sync"]
        ev["clip_start_route_time_sec"] = sync.get("clip_start_route_time_sec")
        ev["clip_end_route_time_sec"] = sync.get("clip_end_route_time_sec")
        ev["event_offset_sec"] = sync.get("event_offset_sec")
        ev["anchor_offset_sec"] = sync.get("anchor_offset_sec")
        ev["lookback_sec"] = sync.get("lookback_sec")
        ev["after_sec"] = sync.get("after_sec")
        ev["bookmark_offsets_sec"] = sync.get("bookmark_offsets_sec", [])
        clip, status = create_clip_if_possible(ev, out_dir, dry_run=args.no_clips)
        if clip:
            ev["video_clip_path"] = clip
        if status:
            ev["video_status"] = status
        if args.openpilot_ui_clips and openpilot_ui_attempts < max(0, args.openpilot_ui_limit):
            openpilot_ui_attempts += 1
            ui_clip, ui_status = create_openpilot_ui_clip_if_possible(ev, out_dir, timeout_sec=args.openpilot_ui_timeout_sec)
            ev["openpilot_ui_status"] = ui_status
            if ui_clip:
                ev["openpilot_ui_clip_path"] = ui_clip
        ev["telemetry"] = load_telemetry_for_event(ev)
        ev["radar"] = load_radar_for_event(ev)
        ev["map"] = load_map_for_event(ev)
    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workspace": str(ROOT),
        "local_only": True,
        "queue_size": len(queue),
        "candidate_count": len(candidates),
        "openpilot_ui_attempts": openpilot_ui_attempts,
        "openpilot_ui_requested": bool(args.openpilot_ui_clips),
        "inspection": inspection,
    }
    (out_dir / "queue.json").write_text(json.dumps({"meta": meta, "events": queue}, indent=2), encoding="utf-8")
    write_csv(out_dir / "queue.csv", [flatten_for_csv(ev) for ev in queue])
    write_ui(out_dir)
    write_report(out_dir, queue, inspection, args)
    feedback = write_review_feedback_summary(out_dir, queue)
    hits = privacy_scan([out_dir / "queue.json", out_dir / "queue.csv", out_dir / "report.md", out_dir / "review_feedback_summary.csv", out_dir / "review_feedback_report.md", out_dir / "index.html", out_dir / "review_server.py"])
    (out_dir / "privacy_scan.txt").write_text("\n".join(hits) + ("\n" if hits else ""), encoding="utf-8")
    print(json.dumps({"out": relpath(out_dir), "events": len(queue), "candidates": len(candidates), "camera_artifacts": inspection.get("camera_video_artifacts_seen"), "unique_reviews": feedback.get("unique_reviews", 0), "privacy_vin_hits": len(hits)}, indent=2))


SERVER_PY = r'''#!/usr/bin/env python3
"""Local review server for Brickpilot review_queue.

Run from the generated review_queue directory:
  python3 review_server.py --port 8765

Default binding is loopback-only. Use --host 0.0.0.0 only when LAN
phone/tablet access on trusted WiFi; the server still only writes local files.
"""
from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import ipaddress
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
REVIEWS = ROOT / "reviews.jsonl"

def load_reviews():
    latest = {}
    history = []
    if not REVIEWS.exists():
        return latest, history
    for line in REVIEWS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_id = row.get("event_id")
        if not event_id:
            continue
        history.append(row)
        latest[event_id] = row
    return latest, history

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/reviews":
            latest, history = load_reviews()
            self.send_json(200, {"ok": True, "latest": latest, "history": history, "count": len(latest)})
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/reviews":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("content-length", "0"))
            if n > 1_000_000:
                raise ValueError("review payload too large")
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            data["saved_at_unix"] = time.time()
            data["local_only"] = True
            data["review_revision"] = int(data.get("review_revision") or 0) + 1
            with REVIEWS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
            self.send_json(200, {"ok": True, "path": "reviews.jsonl", "review": data})
        except Exception as e:
            self.send_json(400, {"ok": False, "error": str(e)})

def is_loopback(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="bind host; default is local-only 127.0.0.1. Use 0.0.0.0 for trusted LAN/mobile access.")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"Serving review UI at http://{url_host}:{args.port}/")
    if not is_loopback(args.host):
        print("WARNING: bound beyond localhost for LAN access. Keep this on trusted WiFi; logs/clips/reviews stay local and are not uploaded by the server.")
    else:
        print("Local-only loopback bind. For phone/tablet on trusted LAN: --host 0.0.0.0")
    print(f"Reviews append to {REVIEWS}")
    server.serve_forever()
'''

INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Brickpilot Review Cockpit</title>
<style>
:root { color-scheme: dark; --bg:#070b14; --panel:#101827; --panel2:#0d1422; --line:#24344d; --muted:#92a4bd; --text:#e8f1ff; --blue:#6aa8ff; --green:#75e0a4; --warn:#ffd166; --bad:#ff7b8a; --primary:#c084fc; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
* { box-sizing: border-box; }
body { margin:0; background:linear-gradient(180deg,#080d19,#0a1020 30%,#070b14); color:var(--text); }
header { position:sticky; top:0; z-index:10; display:flex; gap:16px; align-items:center; justify-content:space-between; padding:12px 16px; border-bottom:1px solid var(--line); background:rgba(10,16,30,.95); backdrop-filter:blur(10px); }
h1 { font-size:18px; margin:0; letter-spacing:.2px; }.small { color:var(--muted); font-size:12px; }.warn { color:var(--warn); }.ok { color:var(--green); }.bad { color:var(--bad); }
.layout { display:grid; grid-template-columns:minmax(300px,380px) 1fr; height:calc(100vh - 58px); min-height:0; overflow:hidden; transition:grid-template-columns .18s ease; }
.layout.sidebar-collapsed { grid-template-columns:56px 1fr; }.layout.sidebar-collapsed .sidebar-body,.layout.sidebar-collapsed .sidebar-title{display:none}.layout.sidebar-collapsed #sidebarToggle{writing-mode:vertical-rl; min-height:120px; padding:10px 7px;}
.sidebar { border-right:1px solid var(--line); background:rgba(11,18,32,.95); min-width:0; min-height:0; overflow:hidden; display:flex; flex-direction:column; }.sidebar-head{flex:0 0 auto;z-index:9;display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--line);background:#0b1323}.sidebar-title{font-weight:750;color:#cfe0f4;font-size:12px;}
.sidebar-body{min-height:0;display:flex;flex-direction:column;flex:1 1 auto;overflow:hidden}.sidebar-tabs{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:10px;border-bottom:1px solid var(--line);background:#0b1323}.sidebar-tabs button.active{background:var(--green);color:#06101f}.sidebar-pane{min-height:0;flex:1 1 auto;display:flex;flex-direction:column;overflow:hidden}.sidebar-pane.hidden{display:none}.sidebar-context{padding:10px;gap:10px;overflow:auto;overscroll-behavior:contain}.filters { flex:0 0 auto; z-index:8; padding:10px; border-bottom:1px solid var(--line); background:#0b1323; display:grid; gap:8px; }
.filter-row { display:grid; grid-template-columns:1fr 120px; gap:8px; }
#list { overflow:auto; flex:1 1 auto; min-height:0; max-height:none; overscroll-behavior:contain; }
.item { padding:12px 14px; border-bottom:1px solid #1d2b41; cursor:pointer; border-left:4px solid transparent; }
.item:hover,.item.active { background:#17243a; }.item.done { border-left-color:var(--green); }.item .title { display:flex; justify-content:space-between; gap:8px; align-items:center; }
.badge { display:inline-block; padding:2px 7px; border-radius:999px; background:#273a59; color:#d5e6ff; font-size:11px; margin:0 4px 4px 0; white-space:nowrap; }.badge.hot{background:#493258;color:#ffd8fb}.badge.okb{background:#224936;color:#caffdf}.badge.warnb{background:#594a24;color:#ffe4a3}
main { padding:14px; min-width:0; min-height:0; overflow:auto; overscroll-behavior:contain; }.cockpit { display:block; max-width:1180px; margin:0 auto; }.cockpit-col{display:flex;flex-direction:column;gap:14px;min-width:0;}
.card { background:rgba(16,24,39,.96); border:1px solid var(--line); border-radius:16px; padding:14px; box-shadow:0 10px 30px rgba(0,0,0,.22); min-width:0; }.card h2,.card h3 { margin:0 0 10px; }.card h2 { font-size:18px; }.card h3 { font-size:14px; color:#c8d8ef; }
.stage { position:relative; background:#000; border-radius:14px; overflow:hidden; border:1px solid #1d2a3c; min-height:220px; display:flex; align-items:center; justify-content:center; }
video { width:100%; max-height:56vh; background:#000; display:block; }.empty-stage { padding:28px; text-align:center; color:var(--warn); }
.comma-canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;}.comma-canvas.hidden{display:none}.overlay { position:absolute; left:10px; right:10px; bottom:10px; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; pointer-events:none; }.overlay.hidden{display:none}.ov { background:rgba(4,8,15,.78); border:1px solid rgba(255,255,255,.14); border-radius:10px; padding:8px; }.ov b{display:block;font-size:16px}.ov span{color:#b8c8dd;font-size:11px;text-transform:uppercase;letter-spacing:.04em} .legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;color:#b9cbe2;font-size:11px}.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:4px;vertical-align:-2px}.legend .tool{background:#ff4d6d}.legend .human{background:#ffd166}.legend .play{background:#fff}
.controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px; }.controls button { margin:0; }
.timeline { margin-top:12px; }.strip { position:relative; height:58px; border:1px solid #2b3d59; border-radius:12px; overflow:hidden; background:#08101d; touch-action:none; }.bar { position:absolute; bottom:0; width:2px; background:#355377; opacity:.85; }.bar.lead{background:#e9c46a}.bar.brake{background:#ff7b8a}.bar.gas{background:#75e0a4}.playhead { position:absolute; top:0; bottom:0; width:2px; background:#fff; box-shadow:0 0 10px #fff; z-index:5; }.event-marker{position:absolute; top:0; bottom:0; width:2px; background:#ff4d6d; z-index:3;}.bookmark-marker{position:absolute; top:2px; bottom:2px; min-width:2px; border-left:2px solid #ffd166; z-index:4;}.bookmark-marker span{position:absolute; top:2px; transform:translateX(-50%); max-width:135px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:2px 6px; border-radius:8px; background:rgba(255,209,102,.94); color:#111827; font-size:10px; font-weight:800; box-shadow:0 2px 8px rgba(0,0,0,.35);}.timeline-readout { display:flex; justify-content:space-between; gap:10px; margin-top:6px; color:#b9cbe2; font-size:12px; }
.context-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.context-tabs{display:flex;gap:6px;flex-wrap:wrap}.context-tabs button{padding:7px 10px}.context-tabs button.active{background:var(--green);color:#06101f}.context-pane.hidden{display:none}.context-subhead{margin:8px 0 6px;color:#c8d8ef;font-size:12px;font-weight:800;letter-spacing:.02em}.map,.radar { position:relative; height:300px; border-radius:14px; overflow:hidden; border:1px solid #263955; background:#08101d; }.radar{height:340px}.tile { position:absolute; width:256px; height:256px; image-rendering:auto; }.map svg { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }.map-msg,.radar-msg { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; padding:20px; text-align:center; background:rgba(8,16,29,.85); color:var(--warn); z-index:3; }.radar svg{position:absolute;inset:0;width:100%;height:100%;}.radar-stats,.radar-readout{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px;margin-top:6px}.radar-chip{background:rgba(4,8,15,.68);border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:4px 6px;min-width:0}.radar-chip span{display:block;color:#b8c8dd;font-size:8px;text-transform:uppercase;line-height:1.05}.radar-chip b{display:block;font-size:11px;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.radar-note{margin-top:6px;color:#b9cbe2;font-size:11px}
.meta-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }.metric { background:#0a1120; border:1px solid #22324a; border-radius:12px; padding:9px; }.metric span{display:block;color:#90a4be;font-size:11px}.metric b{font-size:15px}
pre { white-space:pre-wrap; background:#080e1b; padding:12px; border-radius:12px; border:1px solid #223149; max-height:260px; overflow:auto; }
textarea,input,select { width:100%; background:#08101f; color:#edf5ff; border:1px solid #334b68; border-radius:11px; padding:10px; font:inherit; } label { display:block; color:#b7c8df; font-size:12px; margin:10px 0 5px; }
button { background:var(--blue); color:#06101f; border:0; border-radius:11px; padding:10px 13px; font-weight:750; cursor:pointer; } button.secondary { background:#263852; color:#dce9f8; } button.ghost { background:#101a2b; border:1px solid #324760; color:#dce9f8; }
@media (max-width: 980px) { body{overflow:auto} header{align-items:flex-start; flex-direction:column; gap:5px}.layout{grid-template-columns:1fr;height:auto;overflow:visible}.layout.sidebar-collapsed{grid-template-columns:1fr}.layout.sidebar-collapsed .sidebar{display:none}.sidebar{overflow:visible;display:block;border-right:0;border-bottom:1px solid var(--line)}.sidebar-body{display:block;overflow:visible}.sidebar-head{display:none}.filters{top:78px}.filter-row{grid-template-columns:1fr 110px}#list{max-height:34vh}.cockpit{grid-template-columns:1fr}.overlay{grid-template-columns:repeat(2,minmax(0,1fr))}.map,.radar{height:280px}main{padding:10px}.card{border-radius:14px;padding:12px}.meta-grid{grid-template-columns:1fr 1fr} }
@media (max-width: 560px) { h1{font-size:16px}.filters{top:84px}.item{padding:10px}.stage{min-height:180px}.overlay{position:static; background:#050910; padding:8px; grid-template-columns:1fr 1fr}.cockpit{gap:10px}.meta-grid{grid-template-columns:1fr}.controls button{flex:1 1 44%}.map,.radar{height:240px}.radar-readout,.radar-stats{grid-template-columns:1fr 1fr} }
</style>
</head>
<body>
<header>
  <div><h1>Brickpilot Human Review Cockpit</h1><div class="small">Local queue/reviews. OSM map tiles load from the browser when GPS is available; logs and clips are not uploaded by this UI.</div></div>
  <div class="controls" style="margin-top:0"><button id="headerSidebarToggle" class="ghost" onclick="toggleSidebar()">Hide panel</button><button id="headerCommaToggle" class="ghost" onclick="toggleCommaOverlay()">Telemetry fallback overlay: on</button></div>
  <div id="stats" class="small">Loading…</div>
</header>
<div id="layout" class="layout">
  <aside id="sidebar" class="sidebar"><div class="sidebar-head"><button id="sidebarToggle" class="ghost" onclick="toggleSidebar()">Hide panel</button><span class="sidebar-title">Review tools</span></div><div class="sidebar-body"><div class="sidebar-tabs"><button id="sideInboxBtn" class="ghost" onclick="setSidebarPanel('inbox')">Inbox</button><button id="sideContextBtn" class="ghost" onclick="setSidebarPanel('context')">Context / Radar</button></div><div id="inboxPane" class="sidebar-pane"><div class="filters"><div class="filter-row"><input id="search" placeholder="Search route, label, guess…" /><select id="cat"><option value="">All categories</option></select></div><div class="controls"><button class="ghost" onclick="prevItem()">Prev</button><button class="ghost" onclick="nextItem()">Next</button><button class="ghost" onclick="showInbox()" id="inboxBtn">Inbox</button><button class="ghost" onclick="showReviewed()" id="reviewedBtn">Reviewed basket</button></div></div><div id="list"></div></div><div id="contextPane" class="sidebar-pane sidebar-context hidden"><div class="card"><div class="context-head"><h3>Context / Radar</h3><div class="context-tabs"><button class="context-tab" data-view="map" onclick="setContextView('map')">Map</button><button class="context-tab" data-view="radar" onclick="setContextView('radar')">Radar</button></div></div><div class="context-subhead">Map</div><div id="map" class="map"></div><div class="context-subhead">Radar point cloud <span class="small">primary tracked target = purple star/ring</span></div><div id="radar" class="radar"></div><div class="radar-readout"><div class="radar-chip"><span>mode</span><b id="rmode">—</b></div><div class="radar-chip"><span>leadOne</span><b id="rdist">—</b></div><div class="radar-chip"><span>rel speed</span><b id="rvrel">—</b></div><div class="radar-chip"><span>raw pts</span><b id="rpoints">—</b></div></div><div class="small">Yellow = your bookmark/reaction. Red = tool peak/problem point. Judge surrounding behavior, not only the red line.</div></div></div></div></aside>
  <main><div id="detail" class="card">Loading queue…</div></main>
</div>
<script>
let queue = [], filtered = [], idx = 0, reviewMode = 'inbox', commaOverlayOn = true, telemetryCardsOn = false;
let reviewById = {};
let done = new Set(JSON.parse(localStorage.getItem('bp_review_done') || '[]'));
let sidebarCollapsed = localStorage.getItem('bp_sidebar_collapsed') === '1';
let sidebarPanel = localStorage.getItem('bp_sidebar_panel') || 'inbox';
let contextView = localStorage.getItem('bp_context_view') || 'map';
let currentVideo = null, currentEvent = null, currentMap = null, currentRadar = null, currentVideoSource = 'raw';
const outPrefix = '__OUT_PREFIX__/';
function esc(s){return String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function num(v,d=1){return Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '—';}
function bool(v){return v===true || v===1 || v==='1' || v==='true';}
function isSyntheticCommaOverlayVisible(){return commaOverlayOn && currentVideoSource !== 'openpilot';}
function clipPath(p){p=String(p||''); const m=p.match(/(?:^|\/)(?:clips|openpilot_ui_clips)\/[^?#]+$/); if(m) return m[0].replace(/^\//,''); return p.startsWith(outPrefix) ? p.slice(outPrefix.length) : p;}
function hasRealCommaClip(ev){return !!(ev&&ev.openpilot_ui_clip_path);}
function hasRawClip(ev){return !!(ev&&ev.video_clip_path);}
function sourceLabel(ev){if(!hasRawClip(ev)&&!hasRealCommaClip(ev))return ''; if(currentVideoSource==='openpilot')return 'Real comma 4 UI / rainbow-road source (primary when available)'; if(hasRealCommaClip(ev))return 'Raw qcamera source with local telemetry overlay fallback (secondary)'; return 'Raw qcamera source with local telemetry overlay fallback';}
function sourceButtons(ev){if(!hasRawClip(ev)&&!hasRealCommaClip(ev))return ''; const real=hasRealCommaClip(ev)?'<button id="srcOpenpilot" class="secondary" onclick="setVideoSource(\'openpilot\')">Real comma UI source</button>':''; const raw=hasRawClip(ev)?'<button id="srcRaw" class="secondary" onclick="setVideoSource(\'raw\')">Raw clip source</button>':''; return real+raw;}
function updateSourceUi(){const label=document.getElementById('sourceLabel'); if(label&&currentEvent)label.textContent=sourceLabel(currentEvent); document.getElementById('srcOpenpilot')?.classList.toggle('ghost', currentVideoSource!=='openpilot'); document.getElementById('srcRaw')?.classList.toggle('ghost', currentVideoSource!=='raw');}
function eventText(ev){const r=reviewById[ev.event_id]||{}; return [ev.route_label,ev.category,ev.model,ev.branch,ev.tool_guess,ev.question_for_dan,r.actual_label,r.notes,(r.tags||[]).join(' '),JSON.stringify(ev.metrics||{})].join(' ').toLowerCase();}
function isReviewed(ev){return !!reviewById[ev.event_id] || done.has(ev.event_id);}
function applyFilters(){const q=document.getElementById('search').value.toLowerCase().trim(), cat=document.getElementById('cat').value; filtered=queue.filter(ev=>(!cat||ev.category===cat)&&(reviewMode==='reviewed'?isReviewed(ev):!isReviewed(ev))&&(!q||eventText(ev).includes(q))); if(!filtered.includes(currentEvent)) idx=0; else idx=filtered.indexOf(currentEvent); document.getElementById('inboxBtn')?.classList.toggle('secondary',reviewMode==='inbox'); document.getElementById('reviewedBtn')?.classList.toggle('secondary',reviewMode==='reviewed'); renderList(); show(idx, false);}
function renderStats(){const maps=queue.filter(e=>(e.map||{}).status==='ok').length, tel=queue.filter(e=>(e.telemetry||{}).status==='ok').length, radar=queue.filter(e=>(e.radar||{}).status==='ok').length, clips=queue.filter(e=>e.video_clip_path).length, opui=queue.filter(e=>e.openpilot_ui_clip_path).length, reviewed=queue.filter(isReviewed).length; document.getElementById('stats').textContent=`${queue.length-reviewed} inbox · ${reviewed} reviewed · ${clips} clips · ${opui} OP UI · ${tel} telemetry · ${radar} radar · ${maps} maps`;}
function renderList(){const list=document.getElementById('list'); list.innerHTML=filtered.map((ev,i)=>`<div class="item ${i===idx?'active':''} ${isReviewed(ev)?'done':''}" onclick="show(${i})"><div class="title"><div><span class="badge">#${ev.queue_rank}</span><span class="badge hot">${esc(ev.category)}</span></div><span class="small">P ${esc(ev.priority)}</span></div><strong>${esc(ev.route_label)}</strong><br><span class="small">seg ${ev.segment_index} · route ${num(ev.start_route_time_sec,1)}s · ${esc(ev.model||'model?')}</span><br><span class="badge ${ev.video_clip_path?'okb':'warnb'}">${ev.video_clip_path?'clip':'no clip'}</span><span class="badge ${ev.openpilot_ui_clip_path?'okb':'warnb'}">${ev.openpilot_ui_clip_path?'OP UI':'no OP UI'}</span><span class="badge ${(ev.telemetry||{}).status==='ok'?'okb':'warnb'}">telemetry</span><span class="badge ${(ev.radar||{}).status==='ok'?'okb':'warnb'}">radar</span><span class="badge ${(ev.map||{}).status==='ok'?'okb':'warnb'}">map</span></div>`).join('') || '<div class="item small">No events match the filters.</div>';}
function labelsHtml(labels){return labels&&labels.length ? labels.map(l=>`<span class="badge">${esc(l.reason)} ${num(l.delta_sec,1)}s</span>`).join(' ') : '<span class="small">No nearby labels/bookmarks.</span>';}
function nearestTelemetry(ev,t){const a=(ev.telemetry||{}).samples||[]; if(!a.length) return null; return a.reduce((best,s)=>Math.abs((s.t??0)-t)<Math.abs((best.t??0)-t)?s:best,a[0]);}
function setRadarReadout(mode='—',dist='—',vrel='—',points='—'){for(const [id,val] of Object.entries({rmode:mode,rdist:dist,rvrel:vrel,rpoints:points})){const n=document.getElementById(id); if(n)n.textContent=val;}}
function nearestMap(ev,t){const a=(ev.map||{}).samples||[]; if(!a.length) return null; return a.reduce((best,s)=>Math.abs((s.t??0)-t)<Math.abs((best.t??0)-t)?s:best,a[0]);}
function radarSamples(ev){const r=(ev.radar||{}).samples||[]; return r.length?r:((ev.telemetry||{}).samples||[]).map(s=>({t:s.t,route_time_sec:s.route_time_sec,lead_status:s.lead_status,lead_d_rel_m:s.lead_d_rel_m,lead_v_rel_mps:s.lead_v_rel_mps,lead_limited:s.lead_limited,ego_speed_mph:s.speed_mph,set_speed_mph:s.set_speed_mph,a_ego_mps2:s.a_ego_mps2,accel_cmd:s.accel_cmd,long_active:s.long_active,brake_pressed:s.brake_pressed,gas_pressed:s.gas_pressed,long_control_state:s.carcontrol_long_control_state||s.controls_long_control_state}));}
function nearestRadar(ev,t){const a=radarSamples(ev); if(!a.length) return null; let bestI=0; for(let i=1;i<a.length;i++) if(Math.abs((a[i].t??0)-t)<Math.abs((a[bestI].t??0)-t)) bestI=i; return {...a[bestI],_idx:bestI};}
function leadFromRadarSample(s,name='leadOne'){const lead=s?.[name]; if(lead&&typeof lead==='object')return lead; if(name==='leadOne'&&s&&bool(s.lead_status)&&Number(s.lead_d_rel_m)>0)return{status:true,dRel_m:Number(s.lead_d_rel_m),yRel_m:0,vRel_mps:Number(s.lead_v_rel_mps),radarTrackId:null,radar:false,synthetic:true}; return {status:false};}
function samplePoints(s){return ((s||{}).points||[]).filter(p=>Number.isFinite(Number(p.dRel_m))&&Number.isFinite(Number(p.vRel_mps)));}
function estimateRadarVRel(samples,i){const s=samples[i]; const l=leadFromRadarSample(s,'leadOne'); if(Number.isFinite(Number(l?.vRel_mps)))return Number(l.vRel_mps); if(Number.isFinite(Number(s?.lead_v_rel_mps)))return Number(s.lead_v_rel_mps); const d=Number(l?.dRel_m ?? s?.lead_d_rel_m); if(!s||!(d>0))return NaN; for(let step=1;step<8;step++){const p=samples[i-step], n=samples[i+step]; for(const o of [p,n]){const ol=leadFromRadarSample(o,'leadOne'), od=Number(ol?.dRel_m ?? o?.lead_d_rel_m); if(!o||!(od>0))continue; const dt=Number(s.t)-Number(o.t), dd=d-od; if(Math.abs(dt)>.15&&Number.isFinite(dd))return dd/dt;}} return NaN;}
function radarColor(v){v=Number(v); if(!Number.isFinite(v))return '#94a3b8'; if(v<-6)return '#b91c1c'; if(v<-1.2)return '#ef4444'; if(v<-.25)return '#fb923c'; if(v>.8)return '#60a5fa'; if(v>.25)return '#38bdf8'; return '#facc15';}
function radarKind(v){v=Number(v); if(!Number.isFinite(v))return 'unknown'; if(v<-0.3)return 'closing'; if(v>0.3)return 'opening'; return 'steady';}
function radarXY(obj){const d=Math.max(0,Math.min(130,Number(obj?.dRel_m)||0)); const yRel=Number.isFinite(Number(obj?.plot_y_rel_m))?Number(obj.plot_y_rel_m):Number(obj?.yRel_m); const x=210+Math.max(-12,Math.min(12,Number.isFinite(yRel)?yRel:0))*13.5; const y=288-(d/130)*244; return [x,y,d,yRel];}
function makeRadar(el,ev){
  if(!el)return{update(){}};
  const data=ev.radar||{}, samples=radarSamples(ev);
  const setReadout=(id,val)=>{const n=document.querySelector(id); if(n)n.textContent=val;};
  if(!samples.length){
    el.innerHTML=`<div class="radar-msg">${esc(data.status||'radar unavailable: no local radar/lead telemetry')}</div>`;
    setReadout('#rmode','unavailable'); setReadout('#rdist','—'); setReadout('#rvrel','—'); setReadout('#rpoints','0');
    return {update(){}};
  }
  el.innerHTML=`<svg viewBox="0 0 420 340" preserveAspectRatio="none"><defs><linearGradient id="radarRoad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#13233d"/><stop offset="1" stop-color="#060d18"/></linearGradient><filter id="primaryGlow" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs><rect width="420" height="340" fill="url(#radarRoad)"/><path d="M210 308 L72 44 Q210 20 348 44 Z" fill="#10213a" stroke="#375575" stroke-width="1.6" opacity=".82"/><g opacity=".62"><line x1="210" y1="46" x2="210" y2="308" stroke="#7b8fac" stroke-width="1.8" stroke-dasharray="7 10"/><path d="M148 46 L116 308" stroke="#71839e" stroke-width="2" stroke-dasharray="9 12"/><path d="M272 46 L304 308" stroke="#71839e" stroke-width="2" stroke-dasharray="9 12"/><path d="M86 46 L22 308" stroke="#425a77" stroke-width="1.2" stroke-dasharray="3 10"/><path d="M334 46 L398 308" stroke="#425a77" stroke-width="1.2" stroke-dasharray="3 10"/></g><g id="rangeGrid"></g><g id="pointG"></g><g id="leadG"></g><g id="egoG"><rect x="183" y="290" width="54" height="30" rx="8" fill="#6aa8ff" stroke="#dff0ff" stroke-width="2"/><text x="210" y="309" text-anchor="middle" fill="#06101f" font-size="12" font-weight="900">EGO</text></g><text id="radarTitle" x="210" y="20" text-anchor="middle" fill="#dbeafe" font-size="13" font-weight="800"></text><text id="radarStatus" x="210" y="38" text-anchor="middle" fill="#ffd166" font-size="11" font-weight="700"></text></svg>`;
  const grid=el.querySelector('#rangeGrid');
  if(grid){grid.innerHTML=[25,50,75,100,125].map(d=>{const y=304-(d/130)*252; return `<path d="M${210-d*.95} ${y} L${210+d*.95} ${y}" stroke="#2e4664" stroke-width="1" opacity=".65"/><text x="398" y="${y+4}" text-anchor="end" fill="#91a4bd" font-size="10">${d}m</text>`;}).join('');}
  function xy(obj){const d=Math.max(0,Math.min(130,Number(obj?.dRel_m)||0)); const yRel=Number.isFinite(Number(obj?.plot_y_rel_m))?Number(obj.plot_y_rel_m):Number(obj?.yRel_m); const x=210+Math.max(-12,Math.min(12,Number.isFinite(yRel)?yRel:0))*13.5; const y=304-(d/130)*252; return [x,y,d,yRel];}
  let lastT=null;
  function update(t){
    if(lastT!==null&&Math.abs(t-lastT)<0.04)return; lastT=t;
    const s=nearestRadar(ev,t)||samples[0], i=Number.isFinite(s._idx)?s._idx:samples.indexOf(s);
    const lead1=leadFromRadarSample(s,'leadOne'), lead2=leadFromRadarSample(s,'leadTwo'), pts=samplePoints(s);
    const rawSampleCount=Number(data.raw_point_sample_count)||samples.reduce((n,x)=>n+(samplePoints(x).length?1:0),0);
    const rawPointTotal=Number(data.raw_point_count)||samples.reduce((n,x)=>n+samplePoints(x).length,0);
    const leadOnly=!rawSampleCount||data.mode==='lead_only'||data.lead_only;
    const pointG=el.querySelector('#pointG'), leadG=el.querySelector('#leadG'), title=el.querySelector('#radarTitle'), status=el.querySelector('#radarStatus');
    if(title)title.textContent='LOCAL RADAR POINT CLOUD';
    if(status)status.textContent=leadOnly?'lead-only fallback: no raw liveTracks points in this frame/window':'purple star = primary tracked target';
    if(pointG){pointG.innerHTML=pts.map(p=>{const [x,y,d,yr]=xy(p), v=Number(p.vRel_mps), c=radarColor(v), r=p.measured===false?4.5:6.5, src=p.plot_y_source&&p.plot_y_source!=='raw'?` · y:${esc(p.plot_y_source)}`:''; return `<g><circle cx="${x}" cy="${y}" r="${r}" fill="${c}" stroke="#f8fafc" stroke-width="1" opacity=".88"><title>track ${esc(p.trackId)} · ${num(d,1)}m · y ${num(yr,1)}m · vRel ${num(v,2)} m/s${src}</title></circle></g>`;}).join('');}
    function leadSvg(lead,kind){
      if(!lead||!bool(lead.status)||!(Number(lead.dRel_m)>0))return '';
      const [x,y,d,yr]=xy(lead), v=Number(lead.vRel_mps), primary=kind==='leadOne', c=primary?'#c084fc':'#f97316';
      const mark=primary?`<g filter="url(#primaryGlow)"><circle cx="${x}" cy="${y}" r="19" fill="none" stroke="${c}" stroke-width="4"/><path d="M ${x} ${y-17} L ${x+5} ${y-5} L ${x+18} ${y-5} L ${x+8} ${y+3} L ${x+12} ${y+16} L ${x} ${y+9} L ${x-12} ${y+16} L ${x-8} ${y+3} L ${x-18} ${y-5} L ${x-5} ${y-5} Z" fill="${c}" stroke="#fff" stroke-width="1.5"/></g>`:`<circle cx="${x}" cy="${y}" r="13" fill="none" stroke="${c}" stroke-width="3"/>`;
      const label=`${primary?'PRIMARY':'L2'} ${num(d,0)}m ${Number.isFinite(v)?num(v,1)+'m/s':''}${lead.radarTrackId!=null&&lead.radarTrackId>=0?' #'+esc(lead.radarTrackId):''}`;
      return `<g>${mark}<rect x="${Math.max(4,Math.min(288,x-56))}" y="${Math.max(44,y-36)}" width="128" height="19" rx="6" fill="rgba(4,8,15,.84)" stroke="rgba(255,255,255,.18)"/><text x="${Math.max(68,Math.min(352,x+8))}" y="${Math.max(58,y-22)}" text-anchor="middle" fill="#fff" font-size="10" font-weight="800">${esc(label)}</text></g>`;
    }
    if(leadG)leadG.innerHTML=leadSvg(lead1,'leadOne')+leadSvg(lead2,'leadTwo');
    const hasLead=bool(lead1?.status)&&Number(lead1.dRel_m)>0, vrel=Number.isFinite(Number(lead1?.vRel_mps))?Number(lead1.vRel_mps):estimateRadarVRel(samples,i<0?0:i);
    setReadout('#rmode',leadOnly?'lead-only':`${pts.length} pts/frame`);
    setReadout('#rdist',hasLead?`${num(lead1.dRel_m,1)} m${lead1.radarTrackId!=null&&lead1.radarTrackId>=0?' #'+lead1.radarTrackId:''}`:'no lead lock');
    setReadout('#rvrel',Number.isFinite(vrel)?`${num(vrel,2)} m/s ${radarKind(vrel)}`:'unavailable');
    setReadout('#rpoints',pts.length?`${pts.length} now · ${num(rawPointTotal,0)} kept`:(leadOnly?'none; fallback':`0 now · ${num(rawSampleCount,0)} frames`));
  }
  update((ev.video_sync||{}).event_offset_sec||0);
  return {update};
}

function metricGrid(ev){const t=nearestTelemetry(ev,(ev.video_sync||{}).event_offset_sec||0)||{}; const cells=[['Speed',num(t.speed_mph,1)+' mph'],['Set speed',num(t.set_speed_mph,1)+' mph'],['Lead',t.lead_status?`${num(t.lead_d_rel_m,1)} m`:'—'],['State',[bool(t.long_active)?'LONG':'',bool(t.brake_pressed)?'BRAKE':'',bool(t.gas_pressed)?'GAS':''].filter(Boolean).join(' ')||'—']]; return `<div class="meta-grid">${cells.map(c=>`<div class="metric"><span>${c[0]}</span><b>${c[1]}</b></div>`).join('')}</div>`;}
function renderOverlay(ev,t){}
function resizeCommaCanvas(){const c=document.getElementById('commaCanvas'); if(!c)return; const r=c.getBoundingClientRect(); const scale=window.devicePixelRatio||1; const w=Math.max(320,Math.round(r.width*scale)), h=Math.max(180,Math.round(r.height*scale)); if(c.width!==w||c.height!==h){c.width=w;c.height=h;}}
function drawPoly(ctx,pts,color,width,alpha=1){ctx.save();ctx.globalAlpha=alpha;ctx.strokeStyle=color;ctx.lineWidth=width;ctx.lineCap='round';ctx.lineJoin='round';ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.stroke();ctx.restore();}
function roundRect(ctx,x,y,w,h,r){if(ctx.roundRect){ctx.roundRect(x,y,w,h,r);return;} const rr=Math.max(0,Math.min(Number(r)||0,Math.abs(w)/2,Math.abs(h)/2)); ctx.moveTo(x+rr,y); ctx.lineTo(x+w-rr,y); ctx.quadraticCurveTo(x+w,y,x+w,y+rr); ctx.lineTo(x+w,y+h-rr); ctx.quadraticCurveTo(x+w,y+h,x+w-rr,y+h); ctx.lineTo(x+rr,y+h); ctx.quadraticCurveTo(x,y+h,x,y+h-rr); ctx.lineTo(x,y+rr); ctx.quadraticCurveTo(x,y,x+rr,y);}
function renderCommaOverlay(ev,t){const c=document.getElementById('commaCanvas'); if(!c)return; const visible=isSyntheticCommaOverlayVisible(); c.classList.toggle('hidden',!visible); if(!visible)return; resizeCommaCanvas(); const ctx=c.getContext('2d'), scale=window.devicePixelRatio||1, W=c.width/scale,H=c.height/scale,CX=W/2,HZ=H*.45,BOT=H*.92; ctx.setTransform(scale,0,0,scale,0,0); ctx.clearRect(0,0,W,H); const s=nearestTelemetry(ev,t)||{}, steer=Number(s.steering_angle_deg)||0, lat=Number(s.lateral_error)||0, curve=Math.max(-80,Math.min(80,steer*2.2+lat*22)); const path=[]; for(let i=0;i<36;i++){const u=i/35, y=BOT-(BOT-HZ)*u, width=(1-u)*175+28, x=CX+curve*u*u; path.push([x,y,width,u]);} const left=path.map(p=>[p[0]-p[2]*.42,p[1]]), right=path.map(p=>[p[0]+p[2]*.42,p[1]]), center=path.map(p=>[p[0],p[1]]); drawPoly(ctx,left,'#b9c7d8',3,.72); drawPoly(ctx,right,'#b9c7d8',3,.72); ctx.save(); ctx.globalAlpha=.48; for(let i=0;i<path.length-1;i++){const p0=path[i], p1=path[i+1], hue=275-(i/path.length)*220, g=ctx.createLinearGradient(p0[0],p0[1],p1[0],p1[1]); g.addColorStop(0,`hsla(${hue},100%,64%,.95)`); g.addColorStop(1,`hsla(${hue-34},100%,58%,.95)`); ctx.strokeStyle=g; ctx.lineWidth=Math.max(12,32*(1-p0[3])+8); ctx.lineCap='round'; ctx.beginPath(); ctx.moveTo(p0[0],p0[1]); ctx.lineTo(p1[0],p1[1]); ctx.stroke(); } ctx.restore(); drawPoly(ctx,center,bool(s.long_active)?'#f8fafc':'#93c5fd',3,.9); const leadD=Number(s.lead_d_rel_m); if(s.lead_status&&Number.isFinite(leadD)){const u=Math.max(.12,Math.min(.92,1-leadD/80)); const p=path[Math.round(u*(path.length-1))]; ctx.fillStyle='#ffd166'; ctx.strokeStyle='#111'; ctx.lineWidth=3; ctx.beginPath(); roundRect(ctx,p[0]-20,p[1]-10,40,20,7); ctx.fill(); ctx.stroke(); ctx.fillStyle='#111'; ctx.font='bold 11px system-ui'; ctx.textAlign='center'; ctx.fillText(`${num(leadD,0)}m`,p[0],p[1]+4);} ctx.fillStyle='rgba(0,0,0,.42)'; ctx.beginPath(); roundRect(ctx,14,14,92,72,14); ctx.fill(); ctx.fillStyle='#fff'; ctx.font='700 38px system-ui'; ctx.textAlign='center'; ctx.fillText(num(s.speed_mph,0),60,56); ctx.font='700 12px system-ui'; ctx.fillStyle='#a7d8ff'; ctx.fillText('mph',60,76); ctx.beginPath(); ctx.fillStyle=bool(s.long_active)?'#24d56a':'#8695a7'; roundRect(ctx,W-106,14,92,72,14); ctx.fill(); ctx.fillStyle='#06101f'; ctx.font='700 13px system-ui'; ctx.fillText('SET',W-60,36); ctx.font='800 30px system-ui'; ctx.fillText(num(s.set_speed_mph,0),W-60,67); ctx.fillStyle='rgba(0,0,0,.46)'; ctx.beginPath(); roundRect(ctx,CX-92,H-50,184,34,14); ctx.fill(); ctx.fillStyle='#fff'; ctx.font='700 13px system-ui'; ctx.fillText(`${bool(s.long_active)?'LONG ':'— '}${bool(s.clean_lateral)?'LAT ':'— '}${bool(s.brake_pressed)?'BRAKE ':''}${bool(s.gas_pressed)?'GAS ':''}${bool(s.steering_pressed)?'DRIVER ':''}`,CX,H-29);}
function setupReviewForm(ev){const r=reviewById[ev.event_id]; if(!r)return; const label=document.getElementById('actual_label'), sev=document.getElementById('severity'), notes=document.getElementById('notes'), tags=document.getElementById('tags'); if(label)label.value=r.actual_label||''; if(sev)sev.value=r.severity||'reviewed'; if(notes)notes.value=r.notes||''; if(tags)tags.value=(r.tags||[]).join(', ');}
function show(i, scroll=true){
  if(!filtered.length){document.getElementById('detail').innerHTML='<div class="card">No matching events.</div>'; return;}
  idx=Math.max(0,Math.min(filtered.length-1,i)); const ev=filtered[idx]; currentEvent=ev; renderList();
  const sync=ev.video_sync||{};
  currentVideoSource=ev.openpilot_ui_clip_path?'openpilot':'raw'; const primaryVideo=ev.openpilot_ui_clip_path||ev.video_clip_path; const video=primaryVideo?`<video id="clip" src="${esc(clipPath(primaryVideo))}" controls preload="metadata" playsinline></video>`:`<div class="empty-stage">No clip available: ${esc(ev.video_status)}<br><span class="small">Use route/segment/time metadata for now.</span></div>`; const sourceHelp=primaryVideo?`<div class="small" id="sourceLabel">${esc(sourceLabel(ev))}</div>`:'';
  document.getElementById('detail').innerHTML=`<div class="cockpit"><div class="cockpit-col"><section class="card"><h2>${esc(ev.category)} <span class="small">#${ev.queue_rank} / ${filtered.length}</span></h2><div class="stage">${video}<canvas id="commaCanvas" class="comma-canvas hidden"></canvas><div id="overlay" class="overlay hidden"></div></div>${sourceHelp}<div class="controls"><button class="secondary" onclick="seekEvent()">Jump to tool peak</button><button class="secondary" onclick="toggleCommaOverlay()">Telemetry fallback overlay</button><button class="secondary" onclick="toggleTelemetryCards()">Telemetry cards</button>${sourceButtons(ev)}<button class="secondary" onclick="prevItem()">Prev</button><button class="secondary" onclick="nextItem()">Next</button></div><div class="timeline"><div id="strip" class="strip"></div><div class="legend"><span><i class="tool"></i>tool peak/problem point</span><span><i class="human"></i>your bookmark/reaction</span><span><i class="play"></i>playhead</span></div><div class="timeline-readout"><span id="readout">Telemetry sync pending…</span><span>${esc((ev.telemetry||{}).status||'telemetry unavailable')}</span></div></div></section><section class="card"><h3>Question</h3><div><span class="badge">${esc(ev.route_label)}</span><span class="badge">clip ${num(sync.clip_start_route_time_sec,1)}–${num(sync.clip_end_route_time_sec,1)}s</span><span class="badge">tool peak @ +${num(sync.event_offset_sec,1)}s</span><span class="badge">lookback ${num(sync.lookback_sec,1)}s</span></div><p><b>Question:</b> ${esc(ev.question_for_dan)}</p><p><b>Tool guess:</b> ${esc(ev.tool_guess)}</p><h3>${reviewById[ev.event_id]?'Edit saved review':'Review label'}</h3>${reviewById[ev.event_id]?'<p class="small ok">Saved in reviewed basket. Edits append a new revision to reviews.jsonl.</p>':''}<label>Actual reason / label</label><select id="actual_label"><option value="">choose…</option><option>too_lazy</option><option>no_lead_lazy</option><option>lead_resume_lazy</option><option>too_eager</option><option>too_eager_surge</option><option>good_behavior</option><option>good_phev_transition</option><option>lead_or_traffic_limited</option><option>traffic_light_or_stop_sign</option><option>ev_glide</option><option>ev_launch_lag</option><option>hev_engine_on</option><option>hev_transition</option><option>engine_transition</option><option>regen_drag</option><option>regen_blend</option><option>stop_creep_issue</option><option>brake_regen_blend_issue</option><option>brake_blend</option><option>steering_jerk</option><option>normal_curve</option><option>driver_override</option><option>bad_tool_match</option><option>other</option></select><label>Severity / usefulness</label><select id="severity"><option>reviewed</option><option>important</option><option>critical</option><option>not_useful</option></select><label>Notes</label><textarea id="notes" rows="4" placeholder="What actually happened? What should training/eval learn from this?"></textarea><label>Extra tags (comma-separated)</label><input id="tags" placeholder="e.g. uphill, close_lead, yellow_light, engine_on" /><p><button onclick="saveReview()">Save review</button><button class="secondary" onclick="nextItem()">Skip / next</button></p><div id="save_status" class="small"></div><h3>Nearby labels</h3>${labelsHtml(ev.nearby_labels)}</section><section class="card"><h3>Metadata</h3><button class="secondary" onclick="toggleMetadata()">Show/hide raw metadata</button><pre id="metadata" style="display:none"></pre></section></div></div>`
  setupDetail(ev); setupReviewForm(ev); if(scroll)document.querySelector('main').scrollIntoView({block:'start'});
}
function setupDetail(ev){currentVideo=document.getElementById('clip'); renderTimeline(ev); currentMap=makeMap(document.getElementById('map'),ev); currentRadar=makeRadar(document.getElementById('radar'),ev); const c=document.getElementById('commaCanvas'); if(c)c.classList.toggle('hidden',!isSyntheticCommaOverlayVisible()); updateOverlayButtons(); updateSourceUi(); setSidebarPanel(sidebarPanel); setContextView(contextView); if(currentVideo){currentVideo.addEventListener('timeupdate',()=>syncAt(currentVideo.currentTime)); currentVideo.addEventListener('loadedmetadata',()=>syncAt(currentVideo.currentTime)); seekEvent(false);} else syncAt((ev.video_sync||{}).event_offset_sec||0);}

function bookmarkName(l){const tags=(l.tags||[]).filter(Boolean); return [l.reason||'bookmark', tags.length?tags.join('/'):''].filter(Boolean).join(': ');}function bookmarkClipTime(ev,l){const sync=ev.video_sync||{}; if(Number.isFinite(Number(l.route_time_sec))&&Number.isFinite(Number(sync.clip_start_route_time_sec))) return Number(l.route_time_sec)-Number(sync.clip_start_route_time_sec); return Number(sync.event_offset_sec||0)+Number(l.delta_sec||0);}function renderTimeline(ev){const strip=document.getElementById('strip'); const samples=(ev.telemetry||{}).samples||[], sync=ev.video_sync||{}; const bookmarks=(ev.nearby_labels||[]).map(l=>({...l,clip_t:bookmarkClipTime(ev,l)})).filter(l=>Number.isFinite(l.clip_t)); const end=Math.max(1,...samples.map(s=>Number(s.t)||0),...bookmarks.map(l=>Number(l.clip_t)||0),Number(sync.event_offset_sec||0)+1); const bars=samples.map(s=>{const x=Math.max(0,Math.min(100,100*(s.t||0)/end)); const h=Math.max(8,Math.min(56,(Number(s.speed_mph)||0)*1.1)); const cls=bool(s.brake_pressed)?'bar brake':bool(s.gas_pressed)?'bar gas':s.lead_status?'bar lead':'bar'; return `<span class="${cls}" style="left:${x}%;height:${h}px"></span>`;}).join(''); const marks=bookmarks.map(l=>{const x=Math.max(0,Math.min(100,100*l.clip_t/end)); const name=bookmarkName(l); return `<span class="bookmark-marker" style="left:${x}%" title="${esc(name)} @ ${num(l.clip_t,1)}s"><span>${esc(name)}</span></span>`;}).join(''); strip.innerHTML=bars+marks+`<span class="event-marker" title="tool-detected peak/problem point" style="left:${Math.max(0,Math.min(100,100*(sync.event_offset_sec||0)/end))}%"></span><span id="playhead" class="playhead" style="left:0%"></span>`; strip.onclick=e=>{if(currentVideo){const r=strip.getBoundingClientRect(); currentVideo.currentTime=end*((e.clientX-r.left)/r.width);}};}
function syncAt(t){const ev=currentEvent;if(!ev)return; const s=nearestTelemetry(ev,t)||{}; renderOverlay(ev,t); renderCommaOverlay(ev,t); const ph=document.getElementById('playhead'); if(ph){const samples=(ev.telemetry||{}).samples||[], bookmarks=(ev.nearby_labels||[]).map(l=>bookmarkClipTime(ev,l)).filter(Number.isFinite), end=Math.max(1,...samples.map(x=>Number(x.t)||0),...bookmarks,Number((ev.video_sync||{}).event_offset_sec||0)+1); ph.style.left=Math.max(0,Math.min(100,100*t/end))+'%';} const ro=document.getElementById('readout'); if(ro) ro.textContent=`clip ${num(t,1)}s · route ${num(((ev.video_sync||{}).clip_start_route_time_sec||0)+t,1)}s · speed ${num(s.speed_mph,1)} mph · lead ${s.lead_status||'—'}`; if(currentMap) currentMap.update(t); if(currentRadar) currentRadar.update(t);}
function setVideoSource(kind){if(!currentEvent)return; const v=document.getElementById('clip'); if(!v)return; const t=v.currentTime||0, paused=v.paused; const requested=(kind==='openpilot'&&currentEvent.openpilot_ui_clip_path)?'openpilot':'raw'; const p=requested==='openpilot'?currentEvent.openpilot_ui_clip_path:currentEvent.video_clip_path; if(!p)return; currentVideoSource=requested; v.src=clipPath(p); const restore=()=>{try{v.currentTime=Math.min(t,Number.isFinite(v.duration)?Math.max(0,v.duration-.05):t);}catch(e){} if(!paused)v.play().catch(()=>{}); syncAt(v.currentTime||t);}; if(v.readyState>=1)restore(); else v.addEventListener('loadedmetadata',restore,{once:true}); updateOverlayButtons(); updateSourceUi(); syncAt(t);}
function updateOverlayButtons(){const b=document.getElementById('headerCommaToggle'); if(b)b.textContent='Telemetry fallback overlay: '+(currentVideoSource==='openpilot'?'off on real UI source':(commaOverlayOn?'on':'off'));}
function toggleCommaOverlay(){commaOverlayOn=!commaOverlayOn; const c=document.getElementById('commaCanvas'); if(c)c.classList.toggle('hidden',!isSyntheticCommaOverlayVisible()); updateOverlayButtons(); syncAt(currentVideo?.currentTime||0);} function toggleTelemetryCards(){telemetryCardsOn=!telemetryCardsOn; const o=document.getElementById('overlay'); if(o)o.classList.toggle('hidden',!telemetryCardsOn);} function toggleOverlay(){toggleCommaOverlay();}
function toggleMetadata(){const p=document.getElementById('metadata'); if(!p||!currentEvent)return; if(p.style.display==='none'){p.textContent=JSON.stringify(currentEvent,null,2); p.style.display='block';} else {p.style.display='none';}} function seekEvent(play=false){if(currentVideo&&currentEvent){currentVideo.currentTime=(currentEvent.video_sync||{}).event_offset_sec||0; if(play) currentVideo.play(); syncAt(currentVideo.currentTime);}}
function project(lat,lon,z){const n=Math.pow(2,z)*256; const x=(lon+180)/360*n; const r=lat*Math.PI/180; const y=(1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2*n; return {x,y};}
function makeMap(el,ev){const data=ev.map||{}, samples=data.samples||[]; if(data.status!=='ok'||!samples.length){el.innerHTML=`<div class="map-msg">${esc(data.status||'map telemetry unavailable')}</div>`; return {update(){}};} const z=16, fixedCenter=data.event_position||samples[0]; el.innerHTML='<div class="tiles"></div><svg><polyline id="routeLine" fill="none" stroke="#64a4ff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" opacity=".85"/><circle id="carDot" r="7" fill="#6aa8ff" stroke="white" stroke-width="2"/><circle id="eventDot" r="5" fill="#ff4d6d" stroke="#111" stroke-width="2"/></svg>'; const tiles=el.querySelector('.tiles'), line=el.querySelector('#routeLine'), dot=el.querySelector('#carDot'), eventDot=el.querySelector('#eventDot'); function drawTiles(){const w=el.clientWidth,h=el.clientHeight,c=project(fixedCenter.lat,fixedCenter.lon,z), tx=Math.floor(c.x/256), ty=Math.floor(c.y/256); tiles.innerHTML=''; for(let dx=-2;dx<=2;dx++)for(let dy=-2;dy<=2;dy++){const x=tx+dx,y=ty+dy,img=document.createElement('img'); img.className='tile'; img.loading='lazy'; img.referrerPolicy='no-referrer'; img.src=`https://tile.openstreetmap.org/${z}/${x}/${y}.png`; img.style.left=(x*256-c.x+w/2)+'px'; img.style.top=(y*256-c.y+h/2)+'px'; tiles.appendChild(img);}} function xy(s){const w=el.clientWidth,h=el.clientHeight,c=project(fixedCenter.lat,fixedCenter.lon,z), p=project(s.lat,s.lon,z); return [p.x-c.x+w/2,p.y-c.y+h/2];} let lastLayout=''; function redrawLayout(){const w=el.clientWidth,h=el.clientHeight; if(w<2||h<2)return false; const key=w+'x'+h; if(key===lastLayout)return true; lastLayout=key; drawTiles(); line.setAttribute('points',samples.map(s=>xy(s).join(',')).join(' ')); const ep=xy(fixedCenter); eventDot.setAttribute('cx',ep[0]); eventDot.setAttribute('cy',ep[1]); return true;} let lastT=null; function update(t){const laidOut=redrawLayout(); if(!laidOut)return; if(lastT!==null&&Math.abs(t-lastT)<0.20)return; lastT=t; const pos=nearestMap(ev,t)||fixedCenter; const p=xy(pos); dot.setAttribute('cx',p[0]); dot.setAttribute('cy',p[1]);} update((ev.video_sync||{}).event_offset_sec||0); return {update};}

function setSidebarPanel(panel){sidebarPanel=panel==='context'?'context':'inbox'; localStorage.setItem('bp_sidebar_panel', sidebarPanel); const inbox=document.getElementById('inboxPane'), context=document.getElementById('contextPane'); if(inbox)inbox.classList.toggle('hidden', sidebarPanel!=='inbox'); if(context)context.classList.toggle('hidden', sidebarPanel!=='context'); document.getElementById('sideInboxBtn')?.classList.toggle('active', sidebarPanel==='inbox'); document.getElementById('sideContextBtn')?.classList.toggle('active', sidebarPanel==='context'); if(sidebarPanel==='context'){setContextView(contextView); if(currentVideo)syncAt(currentVideo.currentTime||0);}}
function applySidebar(){const layout=document.getElementById('layout'), sb=document.getElementById('sidebar'), btn=document.getElementById('sidebarToggle'); if(layout) layout.classList.toggle('sidebar-collapsed', sidebarCollapsed); if(sb) sb.classList.toggle('collapsed', sidebarCollapsed); if(btn) btn.textContent=sidebarCollapsed?'Show panel':'Hide panel'; const hb=document.getElementById('headerSidebarToggle'); if(hb) hb.textContent=sidebarCollapsed?'Show panel':'Hide panel'; localStorage.setItem('bp_sidebar_collapsed', sidebarCollapsed?'1':'0');}
function toggleSidebar(){sidebarCollapsed=!sidebarCollapsed; applySidebar();}
function setContextView(view){contextView=view==='radar'?'radar':'map'; localStorage.setItem('bp_context_view', contextView); const map=document.getElementById('map'), radar=document.getElementById('radar'), desktop=window.matchMedia&&window.matchMedia('(min-width: 981px)').matches; document.querySelectorAll('.context-tab').forEach(b=>b.classList.toggle('active', b.dataset.view===contextView)); if(desktop){if(map)map.style.display='block'; if(radar)radar.style.display='block';} else {if(map) map.style.display=contextView==='map'?'block':'none'; if(radar) radar.style.display=contextView==='radar'?'block':'none';} if(currentEvent)syncAt(currentVideo?.currentTime||((currentEvent.video_sync||{}).event_offset_sec||0));}
async function saveReview(){const ev=currentEvent, prior=reviewById[ev.event_id]||{}; const payload={...prior,event_id:ev.event_id, queue_rank:ev.queue_rank, route_label:ev.route_label, route_id:ev.route_id, category:ev.category, segment_index:ev.segment_index, start_route_time_sec:ev.start_route_time_sec, actual_label:document.getElementById('actual_label').value, severity:document.getElementById('severity').value, notes:document.getElementById('notes').value, tags:document.getElementById('tags').value.split(',').map(s=>s.trim()).filter(Boolean), tool_guess:ev.tool_guess, question_for_dan:ev.question_for_dan}; const status=document.getElementById('save_status'); try{const res=await fetch('/api/reviews',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)}); const data=await res.json(); if(!data.ok) throw new Error(data.error||'save failed'); reviewById[ev.event_id]=data.review||payload; done.add(ev.event_id); localStorage.setItem('bp_review_done',JSON.stringify([...done])); renderStats(); status.innerHTML='<span class="ok">Saved to reviewed basket</span>'; setTimeout(()=>{ if(reviewMode==='inbox') applyFilters(); else show(idx,false); },350);}catch(e){status.innerHTML='<span class="warn">Save failed. Are you using python3 review_server.py, not file://? '+esc(e.message)+'</span>';}}
function prevItem(){if(filtered.length)show(Math.max(0,idx-1));}
function nextItem(){if(filtered.length)show(Math.min(filtered.length-1,idx+1));}
function showInbox(){reviewMode='inbox'; currentEvent=null; applyFilters();}
function showReviewed(){reviewMode='reviewed'; currentEvent=null; applyFilters();}
document.getElementById('search').addEventListener('input',applyFilters); document.getElementById('cat').addEventListener('change',applyFilters); updateOverlayButtons(); applySidebar(); setSidebarPanel(sidebarPanel); window.addEventListener('resize',()=>setContextView(contextView)); window.addEventListener('keydown',e=>{if(e.target.matches('input,textarea,select'))return; if(e.key==='j'||e.key==='ArrowDown')nextItem(); if(e.key==='k'||e.key==='ArrowUp')prevItem(); if(e.key==='o')toggleOverlay(); if(e.key==='e')seekEvent(); if(e.key==='b')toggleSidebar(); if(e.key==='m')setContextView(contextView==='map'?'radar':'map');});
Promise.all([fetch('queue.json').then(r=>r.json()), fetch('/api/reviews').then(r=>r.ok?r.json():{latest:{}}).catch(()=>({latest:{}}))]).then(([data,revs])=>{queue=data.events||[]; reviewById=revs.latest||{}; for(const id of Object.keys(reviewById)) done.add(id); localStorage.setItem('bp_review_done',JSON.stringify([...done])); filtered=queue.slice(); const cats=[...new Set(queue.map(e=>e.category))].sort(); document.getElementById('cat').innerHTML='<option value="">All categories</option>'+cats.map(c=>`<option>${esc(c)}</option>`).join(''); renderStats(); applyFilters();}).catch(e=>{document.getElementById('detail').textContent='Failed to load queue/reviews: '+e;});
window.show=show; window.setSidebarPanel=setSidebarPanel; window.saveReview=saveReview; window.toggleMetadata=toggleMetadata; window.prevItem=prevItem; window.nextItem=nextItem; window.showInbox=showInbox; window.showReviewed=showReviewed; window.toggleOverlay=toggleOverlay; window.setVideoSource=setVideoSource; window.toggleCommaOverlay=toggleCommaOverlay; window.toggleTelemetryCards=toggleTelemetryCards; window.seekEvent=seekEvent; window.toggleSidebar=toggleSidebar; window.setContextView=setContextView;
</script>
</body>
</html>
'''

README_MD = """# Brickpilot Review Queue Prototype

Run locally by default:

```bash
cd ~/BrickpilotDriveDB/analysis_exports/drive_tests/review_queue_20260512_batch2
python3 review_server.py --port 8765
```

Open <http://127.0.0.1:8765/>. Reviews append to `reviews.jsonl`.

For trusted same-WiFi phone/tablet review:

```bash
python3 review_server.py --host 0.0.0.0 --port 8765
```

Regenerate from repo root:

```bash
python3 scripts/drive_tests/review_queue.py --out ~/BrickpilotDriveDB/analysis_exports/drive_tests/review_queue_20260512_batch2 --limit 60
```

If copied camera artifacts and ffmpeg are available, clips are created into `clips/`. Bookmark/tag-driven clips use 60s lookback by default because human labels are reactions after the event; cross-segment windows concatenate available qcamera segments. Otherwise events remain reviewable as metadata-only and `video_status` explains why.

When real comma UI clips are generated, the UI defaults to that source and keeps the local telemetry fallback overlay off so it does not double-draw rainbow-road graphics. Raw qcamera remains available as a secondary source with the fallback overlay. The left sidebar switches between the review inbox and a compact Context/Radar panel synced to video.currentTime; on desktop the Context/Radar panel stacks Map and Radar together, while narrow/mobile widths use the Map/Radar toggle. The desktop panel can be collapsed with a persisted sidebar button to widen the video.
"""

if __name__ == "__main__":
    main()
