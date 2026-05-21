#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])).resolve()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser().resolve()
for root in (REPO_ROOT, TOOLS_ROOT):
    root_text = str(root)
    if root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)

from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore


def _rows(store: DriveStore, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in store.execute(sql, params).fetchall()]


def _quote_concat(path: Path) -> str:
    return "file '" + str(path).replace("'", "'\\''") + "'"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def generate_review_video(route_uuid: str, *, config: str | None = None, output_root: Path | None = None) -> dict:
    cfg = load_config(config)
    store = DriveStore(cfg)
    store.migrate()
    if _is_uuid(route_uuid):
        route = store.one("SELECT id, route_id FROM routes WHERE id=? OR route_id=?", (route_uuid, route_uuid))
    else:
        route = store.one("SELECT id, route_id FROM routes WHERE route_id=? OR canonical_name=?", (route_uuid, route_uuid))
    if not route:
        raise SystemExit(f"route not found: {route_uuid}")
    route_uuid = str(route["id"])
    route_id = str(route["route_id"])
    existing = store.one(
        """SELECT a.id, a.artifact_path
           FROM artifacts a JOIN route_artifacts ra ON ra.artifact_id=a.id
           WHERE ra.route_uuid=?
             AND (a.kind='full_drive_video' OR ra.role='full_drive_video' OR a.mime_type='video/mp4')
           ORDER BY a.imported_at DESC LIMIT 1""",
        (route_uuid,),
    )
    if existing:
        return {"ok": True, "route_uuid": route_uuid, "route_id": route_id, "artifact_id": existing["id"], "status": "already_exists"}

    rows = _rows(
        store,
        """SELECT a.id, a.kind, a.artifact_path, a.original_path, ra.role, rs.segment_index
           FROM artifacts a
           JOIN route_artifacts ra ON ra.artifact_id=a.id
           LEFT JOIN route_segments rs ON rs.id=ra.segment_id
           WHERE ra.route_uuid=?
             AND (a.kind IN ('qcamera','fcamera','ecamera','dcamera','camera') OR ra.role IN ('qcamera','fcamera','ecamera','dcamera','camera'))
           ORDER BY CASE WHEN a.kind='qcamera' OR ra.role='qcamera' THEN 0 WHEN a.kind='fcamera' OR ra.role='fcamera' THEN 1 ELSE 9 END,
                    rs.segment_index NULLS LAST, a.id""",
        (route_uuid,),
    )
    inputs: list[Path] = []
    for row in rows:
        p = (cfg.artifact_root / row["artifact_path"]).resolve()
        if str(p).startswith(str(cfg.artifact_root.resolve())) and p.exists():
            inputs.append(p)
    if not inputs:
        raise SystemExit(f"no raw camera artifacts for {route_id}")

    output_root = output_root or Path(os.environ.get("BRICKPILOT_LABELER_OUTPUT_ROOT", Path.home() / "BrickpilotDriveDB" / "labeler_outputs")).expanduser()
    out_dir = output_root / "review_videos" / route_id
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "full_drive_video.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found")
    with tempfile.TemporaryDirectory(prefix="brickpilot-review-video-") as td:
        concat = Path(td) / "concat.txt"
        concat.write_text("\n".join(_quote_concat(p) for p in inputs) + "\n")
        copy_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-map", "0:v:0", "-c:v", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", "-y", str(output)]
        proc = subprocess.run(copy_cmd, text=True, capture_output=True, timeout=1800)
        if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            transcode_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-map", "0:v:0", "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", "-y", str(output)]
            proc = subprocess.run(transcode_cmd, text=True, capture_output=True, timeout=3600)
            if proc.returncode != 0:
                raise SystemExit((proc.stderr or "ffmpeg failed").strip()[:1000])
    art = store.import_artifact(output, kind="full_drive_video")
    store.link_route_artifact(route_uuid, art.id, "full_drive_video", None)
    store.commit()
    store.close()
    return {"ok": True, "route_uuid": route_uuid, "route_id": route_id, "artifact_id": art.id, "output": str(output), "input_count": len(inputs), "size_bytes": output.stat().st_size}


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a playable MP4 review video for a raw-video-only Brickpilot ride.")
    ap.add_argument("--route", required=True, help="Route UUID or route id")
    ap.add_argument("--config")
    ap.add_argument("--output-root", type=Path)
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()
    out = generate_review_video(ns.route, config=ns.config, output_root=ns.output_root)
    print(json.dumps(out, indent=2, sort_keys=True) if ns.json else out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
