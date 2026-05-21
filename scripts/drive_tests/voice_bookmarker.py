#!/usr/bin/env python3
"""Local-only voice bookmark recorder/importer for Brickpilot drive tests.

Phase 1 is deliberately small and dependency-light:

* `record` starts a local mic recorder when ffmpeg/sox are available and lets the
  driver/operator type timestamped spoken/manual bookmarks during the drive.
* `transcribe-template` creates an editable transcript JSONL from a saved session
  (or from plain text with timestamps), so a missing STT dependency is not a
  blocker.
* `import` converts voice bookmark artifacts into draft rows compatible with the
  generated manual_drive_labeler `drive_labels.jsonl` files.

No cloud upload is used or required. Audio and transcripts stay under the local
analysis tree unless an explicit output directory is provided.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Iterable

ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2]))
DEFAULT_OUT_BASE = Path(os.environ.get("BRICKPILOT_VOICE_BOOKMARK_ROOT", Path.home() / "BrickpilotDriveDB" / "voice_bookmarks")).expanduser()
DEFAULT_LABEL = "other"
VOICE_BOOKMARK_SCHEMA_VERSION = 2

# Keep inferred draft labels aligned with the manual_drive_labeler dropdown so
# imported voice drafts round-trip cleanly through the UI. Nuance stays in tags.
MANUAL_LABELER_LABELS = {
    "good_behavior",
    "too_lazy",
    "no_lead_lazy",
    "lead_resume_lazy",
    "too_eager",
    "too_eager_surge",
    "too_harsh_brake",
    "late_brake",
    "brake_blend",
    "stop_creep_issue",
    "steering_jerk",
    "lead_or_traffic_limited",
    "driver_override",
    "ev_glide",
    "ev_launch_lag",
    "hev_engine_on",
    "hev_transition",
    "drive_mode_switch",
    "medium_regen_charge",
    "hard_brake_regen",
    "regen_blend",
    "ev_charger_plugged_in",
    "regen_drag",
    "engine_transition",
    "good_phev_transition",
    "other",
}

TIME_RE = re.compile(r"^\s*(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:\.\d+)?)\s+(?P<text>.+?)\s*$")
SIMPLE_TIME_RE = re.compile(r"^\s*(?P<s>\d+(?:\.\d+)?)\s+(?P<text>.+?)\s*$")

LABEL_HINTS: list[tuple[str, str, list[str]]] = [
    ("good_phev_transition", "good", ["good phev", "good transition", "smooth transition"]),
    ("ev_launch_lag", "phev", ["ev lag", "ev launch", "electric launch"]),
    ("brake_blend", "phev", ["brake blend", "blended brake", "brake blending"]),
    ("regen_blend", "phev", ["regen blend", "regen transition"]),
    ("hev_transition", "phev", ["hev transition", "hybrid transition"]),
    ("engine_transition", "phev", ["engine kicked", "engine transition"]),
    ("good_behavior", "good", ["good", "nice", "smooth", "better", "perfect"]),
    ("late_brake", "brake", ["late brake", "brake late", "too late", "late stop"]),
    ("too_harsh_brake", "brake", ["harsh brake", "hard brake", "braking hard", "stopped hard"]),
    ("no_lead_lazy", "accel", ["no lead lazy", "open road lazy", "speed hold", "holding speed", "too lazy"]),
    ("lead_resume_lazy", "lead", ["lead resume lazy", "resume slow", "following resume"]),
    ("too_eager", "accel", ["accelerat", "gas", "launch", "too fast", "eager"]),
    ("lead_or_traffic_limited", "lead", ["lead", "following", "close", "cut in", "cut-in", "traffic"]),
    ("steering_jerk", "steer", ["steer", "lane", "wobble", "ping pong", "curve", "turn"]),
    ("medium_regen_charge", "phev", ["regen", "charge"]),
    ("hev_engine_on", "phev", ["engine", "hev", "hybrid"]),
    ("ev_glide", "phev", ["ev glide", "ev mode"]),
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL in {path}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"Expected JSON objects in {path}")
        rows.append(obj)
    return rows


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    materialized = list(rows)
    if not materialized:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in materialized:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(materialized)


def parse_time_text(line: str) -> tuple[float, str] | None:
    """Parse `MM:SS text`, `HH:MM:SS text`, or `12.3 text` transcript rows."""
    m = TIME_RE.match(line)
    if m:
        h = int(m.group("h") or 0)
        minutes = int(m.group("m"))
        seconds = float(m.group("s"))
        return h * 3600 + minutes * 60 + seconds, m.group("text").strip()
    m = SIMPLE_TIME_RE.match(line)
    if m:
        return float(m.group("s")), m.group("text").strip()
    return None


def read_transcript(path: Path) -> list[dict[str, Any]]:
    """Read transcript JSONL/JSON/plain-text into voice bookmark dicts."""
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    rows: list[dict[str, Any]] = []
    if suffix == ".jsonl":
        rows = jsonl_rows(path)
    elif suffix == ".json":
        obj = json.loads(text or "{}")
        if isinstance(obj, dict):
            rows = list(obj.get("bookmarks") or obj.get("segments") or [])
        elif isinstance(obj, list):
            rows = obj
        else:
            raise ValueError("Transcript JSON must be a list or object with bookmarks/segments")
    else:
        for i, line in enumerate(text.splitlines(), 1):
            parsed = parse_time_text(line)
            if not parsed:
                if line.strip():
                    raise ValueError(f"Cannot parse transcript line {i}: {line!r}")
                continue
            t, words = parsed
            rows.append({"elapsed_sec": t, "text": words, "source": "manual_transcript"})
    return [normalize_bookmark(r, i) for i, r in enumerate(rows, 1)]


def normalize_bookmark(row: dict[str, Any], index: int) -> dict[str, Any]:
    t = row.get("route_time_sec", row.get("elapsed_sec", row.get("start", row.get("start_time_sec"))))
    if t is None:
        raise ValueError(f"Bookmark {index} is missing elapsed_sec/route_time_sec")
    text = str(row.get("text") or row.get("transcript") or row.get("utterance") or row.get("notes") or "").strip()
    out = dict(row)
    out["elapsed_sec"] = round(float(t), 3)
    out["text"] = text
    out.setdefault("id", f"voice:{index:04d}:{out['elapsed_sec']:.3f}")
    out.setdefault("source", "voice_bookmarker")
    return out


def infer_label_and_tags(text: str) -> tuple[str, list[str]]:
    lower = text.lower()
    tags: list[str] = ["voice"]
    label = DEFAULT_LABEL
    for candidate, tag, needles in LABEL_HINTS:
        if any(n in lower for n in needles):
            if label == DEFAULT_LABEL:
                label = candidate
            tags.append(tag)
    if label not in MANUAL_LABELER_LABELS:
        tags.append(label)
        label = DEFAULT_LABEL
    # Stable de-dupe, preserve order.
    seen: set[str] = set()
    return label, [t for t in tags if not (t in seen or seen.add(t))]


def bookmark_to_label(bookmark: dict[str, Any], *, job: dict[str, Any] | None = None, route: dict[str, Any] | None = None, window_sec: float = 3.0, offset_sec: float = 0.0) -> dict[str, Any]:
    elapsed = float(bookmark["elapsed_sec"]) + offset_sec
    start = max(0.0, elapsed - max(0.0, window_sec) / 2.0)
    end = max(start, elapsed + max(0.0, window_sec) / 2.0)
    text = str(bookmark.get("text") or "").strip()
    inferred_label, inferred_tags = infer_label_and_tags(text)
    tags = list(bookmark.get("tags") or []) + inferred_tags
    seen: set[str] = set()
    tags = [str(t) for t in tags if str(t) and not (str(t) in seen or seen.add(str(t)))]
    route = route or {}
    job = job or {}
    return {
        "job_id": job.get("job_id") or bookmark.get("job_id") or "",
        "route_id": route.get("route_id") or job.get("route_id") or bookmark.get("route_id") or "",
        "route_label": route.get("route_label") or job.get("route_label") or bookmark.get("route_label") or "",
        "ride_type": route.get("ride_type") or job.get("ride_type") or bookmark.get("ride_type") or "label validation",
        "ride_metadata": route.get("ride_metadata") or job.get("ride_metadata") or bookmark.get("ride_metadata") or "label validation",
        "label_kind": "drive",
        "start_time_sec": round(start, 3),
        "end_time_sec": round(end, 3),
        "label": str(bookmark.get("label") or inferred_label),
        "severity": str(bookmark.get("severity") or "reviewed"),
        "tags": tags,
        "notes": text,
        "voice_bookmark_id": bookmark.get("id"),
        "voice_elapsed_sec": round(elapsed, 3),
        "source": "voice_bookmarker.py",
        "saved_at": utc_now(),
    }


def load_manual_labeler_job(labeler_dir: Path, job_id: str | None = None) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    labeler_dir = labeler_dir.resolve()
    jobs_path = labeler_dir / "drive_jobs.json"
    if jobs_path.exists():
        jobs_obj = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs = list(jobs_obj.get("jobs") or [])
        chosen: dict[str, Any] | None = None
        if job_id:
            chosen = next((j for j in jobs if str(j.get("job_id")) == job_id), None)
            if chosen is None:
                raise ValueError(f"No job_id {job_id!r} in {jobs_path}")
        else:
            default_job_id = jobs_obj.get("default_job_id")
            chosen = next((j for j in jobs if j.get("job_id") == default_job_id), None) or next((j for j in jobs if j.get("selected")), None) or (jobs[0] if jobs else None)
        if chosen is None:
            raise ValueError(f"No jobs found in {jobs_path}")
        job_dir = (labeler_dir / str(chosen.get("job_dir") or ".")).resolve()
        data_path = (labeler_dir / str(chosen.get("data_path") or "drive_data.json")).resolve()
        if not (job_dir == labeler_dir or labeler_dir in job_dir.parents):
            raise ValueError("Refusing unsafe job_dir outside labeler directory")
        if not (data_path == labeler_dir or labeler_dir in data_path.parents):
            raise ValueError("Refusing unsafe data_path outside labeler directory")
        data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else {}
        return job_dir, chosen, data
    data_path = labeler_dir / "drive_data.json"
    if data_path.exists():
        data = json.loads(data_path.read_text(encoding="utf-8"))
        route = data.get("route") or {}
        job = {"job_id": job_id or route.get("analysis_name") or route.get("route_id") or "manual_drive_labeler", **route}
        return labeler_dir, job, data
    raise FileNotFoundError(f"No drive_jobs.json or drive_data.json in {labeler_dir}")


def import_bookmarks(*, bookmarks: list[dict[str, Any]], labeler_dir: Path, job_id: str | None = None, offset_sec: float = 0.0, window_sec: float = 3.0, dry_run: bool = False) -> dict[str, Any]:
    job_dir, job, data = load_manual_labeler_job(labeler_dir, job_id)
    route = data.get("route") or {}
    labels = [bookmark_to_label(b, job=job, route=route, window_sec=window_sec, offset_sec=offset_sec) for b in bookmarks]
    target = job_dir / "drive_labels.jsonl"
    if not dry_run:
        append_jsonl(target, labels)
    return {"target": str(target), "count": len(labels), "dry_run": dry_run, "job_id": job.get("job_id"), "labels": labels}


def recorder_command(audio_path: Path, backend: str = "auto", device: str = "") -> list[str] | None:
    if backend in {"auto", "ffmpeg"} and shutil.which("ffmpeg"):
        if sys.platform == "darwin":
            dev = device or ":0"
            return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-f", "avfoundation", "-i", dev, "-ac", "1", "-ar", "16000", str(audio_path)]
        if sys.platform.startswith("linux"):
            dev = device or "default"
            return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-f", "alsa", "-i", dev, "-ac", "1", "-ar", "16000", str(audio_path)]
    if backend in {"auto", "sox"} and shutil.which("rec"):
        return ["rec", "-q", str(audio_path), "rate", "16000", "channels", "1"]
    return None


def stop_process(proc: subprocess.Popen[Any] | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def write_session_metadata(session_dir: Path, metadata: dict[str, Any]) -> None:
    (session_dir / "metadata.json").write_text(jdump(metadata) + "\n", encoding="utf-8")


def command_record(args: argparse.Namespace) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.name or "drive")[:60].strip("_") or "drive"
    session_dir = Path(args.out_dir or DEFAULT_OUT_BASE / f"{stamp}_{slug}")
    session_dir.mkdir(parents=True, exist_ok=True)
    audio_path = session_dir / "mic.wav"
    bookmarks_path = session_dir / "bookmarks.jsonl"
    transcript_path = session_dir / "transcript_template.jsonl"
    cmd = None if args.no_audio else recorder_command(audio_path, args.backend, args.device)
    proc: subprocess.Popen[Any] | None = None
    if cmd:
        print("Starting local mic recorder:", " ".join(shlex.quote(x) for x in cmd))
        # Keep the recorder fully away from the terminal input stream. ffmpeg
        # accepts interactive stdin commands by default, which can steal `/q`
        # from the Python prompt and make the terminal feel frozen.
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL)
    elif not args.no_audio:
        print("No local recorder backend found (ffmpeg/sox). Continuing in bookmark-only mode.")
    else:
        print("Audio disabled; recording timestamped bookmark text only.")

    start_wall = time.time()
    metadata = {
        "schema_version": VOICE_BOOKMARK_SCHEMA_VERSION,
        "tool": "voice_bookmarker.py",
        "created_at": utc_now(),
        "session_name": args.name or slug,
        "audio_path": str(audio_path.relative_to(session_dir)) if proc else "",
        "bookmarks_path": str(bookmarks_path.relative_to(session_dir)),
        "privacy": {"local_only": True, "cloud_upload": False},
        "recorder_command": cmd or [],
        "notes": args.notes or "",
    }
    write_session_metadata(session_dir, metadata)
    print("Type spoken/manual labels and press Enter. Blank Enter adds a bookmark. Commands: /q to stop, /note text.")
    idx = 0
    try:
        while True:
            line = input("> ").strip()
            now = time.time()
            if line in {"/q", "/quit", "/stop"}:
                break
            if line.startswith("/note "):
                metadata["notes"] = (metadata.get("notes") or "") + ("\n" if metadata.get("notes") else "") + line[6:].strip()
                write_session_metadata(session_dir, metadata)
                continue
            idx += 1
            row = {
                "id": f"voice:{idx:04d}:{now - start_wall:.3f}",
                "elapsed_sec": round(now - start_wall, 3),
                "wall_time": dt.datetime.now().astimezone().isoformat(),
                "text": line or "bookmark",
                "source": "operator_live_entry",
            }
            append_jsonl(bookmarks_path, [row])
            print(f"[{row['elapsed_sec']:7.2f}s] saved: {row['text']}")
    finally:
        stop_process(proc)
    bookmarks = jsonl_rows(bookmarks_path)
    transcript_path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in bookmarks), encoding="utf-8")
    metadata["ended_at"] = utc_now()
    metadata["duration_sec"] = round(time.time() - start_wall, 3)
    metadata["bookmark_count"] = len(bookmarks)
    write_session_metadata(session_dir, metadata)
    print(f"Saved voice bookmark session: {session_dir}")
    print(f"Artifacts: {bookmarks_path.name}, {transcript_path.name}" + (", mic.wav" if audio_path.exists() else ""))
    return session_dir


def command_transcribe_template(args: argparse.Namespace) -> Path:
    session_dir = Path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    source = Path(args.input) if args.input else session_dir / "bookmarks.jsonl"
    rows = read_transcript(source) if source.exists() else []
    out = Path(args.output) if args.output else session_dir / "transcript_template.jsonl"
    append_mode = bool(args.append and out.exists())
    if append_mode:
        append_jsonl(out, rows)
    else:
        out.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    print(f"Wrote editable transcript template: {out} ({len(rows)} row(s))")
    return out


def command_import(args: argparse.Namespace) -> dict[str, Any]:
    artifact = Path(args.artifact)
    if artifact.is_dir():
        for name in ("transcript.jsonl", "transcript_template.jsonl", "bookmarks.jsonl"):
            candidate = artifact / name
            if candidate.exists():
                artifact = candidate
                break
    bookmarks = read_transcript(artifact)
    result = import_bookmarks(bookmarks=bookmarks, labeler_dir=Path(args.labeler_dir), job_id=args.job_id or None, offset_sec=args.offset_sec, window_sec=args.window_sec, dry_run=args.dry_run)
    manifest = {
        "schema_version": VOICE_BOOKMARK_SCHEMA_VERSION,
        "imported_at": utc_now(),
        "artifact": str(artifact),
        "labeler_dir": str(Path(args.labeler_dir)),
        "target": result["target"],
        "count": result["count"],
        "dry_run": args.dry_run,
        "offset_sec": args.offset_sec,
        "window_sec": args.window_sec,
    }
    if not args.dry_run:
        manifest_path = Path(result["target"]).with_name("voice_bookmark_import_manifest.json")
        manifest_path.write_text(jdump(manifest) + "\n", encoding="utf-8")
    print(jdump(manifest))
    return result


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="Record mic audio if available and collect timestamped bookmark text")
    rec.add_argument("--name", default="", help="Session name used in output directory")
    rec.add_argument("--out-dir", default="", help=f"Output session dir (default {DEFAULT_OUT_BASE}/<timestamp>_<name>)")
    rec.add_argument("--backend", choices=("auto", "ffmpeg", "sox"), default="auto", help="Local audio backend")
    rec.add_argument("--device", default="", help="Backend-specific mic device (macOS ffmpeg default is :0)")
    rec.add_argument("--no-audio", action="store_true", help="Do not attempt mic recording; bookmark text only")
    rec.add_argument("--notes", default="", help="Session notes stored in metadata.json")
    rec.set_defaults(func=command_record)

    tmpl = sub.add_parser("transcribe-template", help="Create/editable transcript JSONL from bookmarks or timestamped text")
    tmpl.add_argument("session_dir", help="Voice bookmark session directory")
    tmpl.add_argument("--input", default="", help="bookmarks.jsonl, transcript JSON/JSONL, or timestamped text")
    tmpl.add_argument("--output", default="", help="Output transcript JSONL")
    tmpl.add_argument("--append", action="store_true", help="Append to output instead of replacing")
    tmpl.set_defaults(func=command_transcribe_template)

    imp = sub.add_parser("import", help="Import voice bookmark artifact into manual_drive_labeler draft labels")
    imp.add_argument("artifact", help="Voice session dir or transcript/bookmarks file")
    imp.add_argument("--labeler-dir", required=True, help="Generated manual_drive_labeler directory")
    imp.add_argument("--job-id", default="", help="Specific drive_jobs.json job_id (default selected/default job)")
    imp.add_argument("--offset-sec", type=float, default=0.0, help="Add this offset to all bookmark times before import")
    imp.add_argument("--window-sec", type=float, default=3.0, help="Draft label window centered on each bookmark")
    imp.add_argument("--dry-run", action="store_true", help="Print import manifest without writing drive_labels.jsonl")
    imp.set_defaults(func=command_import)
    return ap


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
