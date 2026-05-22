#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_ROOT = SCRIPT_DIR.parents[1]
if str(TOOLS_ROOT) not in sys.path:
  sys.path.insert(0, str(TOOLS_ROOT))

from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore
from scripts.drive_tests import voice_alignment_calibrator as voice_align
from scripts.drive_tests.train_voice_labeler_0325 import (
  aligned_voice_window,
  json_object,
  label_family,
  label_polarity,
  normalize_labels,
  safe_float,
  tag_list,
)


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
DEFAULT_NORMALIZATION_VERSION = "voice_norm_057_native_pacing_v1"
SUBJECTIVE_BACK_SEC = 5.0
SUBJECTIVE_FORWARD_SEC = 5.0


def json_safe(value: Any) -> Any:
  if isinstance(value, dict):
    return {str(k): json_safe(v) for k, v in value.items()}
  if isinstance(value, (list, tuple, set)):
    return [json_safe(v) for v in value]
  if isinstance(value, (str, int, bool)) or value is None:
    return value
  if isinstance(value, float):
    return value if math.isfinite(value) else None
  return str(value)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fields: list[str] = []
  for row in rows:
    for key in row:
      if key not in fields:
        fields.append(key)
  if not fields:
    fields = ["empty"]
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)


def route_row(store: DriveStore, route_id_or_uuid: str) -> dict[str, Any]:
  row = store.one(
    """SELECT id, route_id, route_label, drive_type, brickpilot_version, model_bundle,
              started_at, ended_at, duration_sec, metadata_jsonb
       FROM routes
       WHERE id::text=? OR route_id=?
       ORDER BY updated_at DESC LIMIT 1""",
    (route_id_or_uuid, route_id_or_uuid),
  )
  if not row:
    raise RuntimeError(f"route not found: {route_id_or_uuid}")
  return dict(row)


def review_job_row(store: DriveStore, route_uuid: str) -> dict[str, Any]:
  row = store.one(
    """SELECT id, route_uuid, status, ride_type, route_label, version
       FROM review_jobs
       WHERE route_uuid=?
       ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, updated_at DESC, id DESC
       LIMIT 1""",
    (route_uuid,),
  )
  if not row:
    raise RuntimeError(f"review job not found for route_uuid={route_uuid}")
  return dict(row)


def fetch_voice_bookmarks(store: DriveStore, route_uuid: str) -> list[dict[str, Any]]:
  return [dict(row) for row in store.execute(
    """SELECT id, t_sec, end_sec, text, tags, source, metadata_jsonb
       FROM bookmarks
       WHERE route_uuid=? AND deleted_at IS NULL AND source='voice_narration'
       ORDER BY t_sec, id""",
    (route_uuid,),
  ).fetchall()]


def label_kind_for(canonical: str) -> str:
  return "phev" if label_family(canonical) == "phev" else "drive"


def phase_for(canonical: str) -> str:
  if canonical in {"pacing_good", "pacing_bad", "pacing_bursty", "follow_distance_good", "follow_distance_bad", "follow_distance_too_far"}:
    return "lead_pacing"
  if canonical in {"rolling_follow_good", "overbraked_rolling_traffic", "unnecessary_braking", "braking_early"}:
    return "rolling_follow"
  if "stop" in canonical or canonical.startswith(("brake", "braking", "lead_brake")):
    return "stop_brake"
  if canonical.startswith("resume") or canonical.startswith("accel"):
    return "resume_accel"
  if canonical.startswith(("phev_", "regen_", "ev_", "ice_", "power_meter")):
    return "phev_state"
  if canonical.startswith("steering") or canonical in {"driver_steering", "human_intervention_steering"}:
    return "lateral"
  if canonical.startswith(("gear_", "auto_hold", "drive_", "ready_park")):
    return "stationary_state"
  return label_family(canonical)


def window_for_label(bookmark: dict[str, Any], canonical: str, route_duration: float) -> tuple[float, float]:
  t = safe_float(bookmark.get("t_sec"))
  end = safe_float(bookmark.get("end_sec"), t)
  if end < t:
    t, end = end, t
  if end - t < 0.5:
    end = t + 1.0

  phase = phase_for(canonical)
  if canonical in {"driver_brake_intervention", "human_intervention_brake", "missed_stop", "braking_bad", "braking_late", "lead_brake_bad"}:
    start, stop = t - 6.0, t + 3.0
  elif phase in {"lead_pacing", "rolling_follow", "stop_brake", "resume_accel"}:
    start, stop = t - SUBJECTIVE_BACK_SEC, t + SUBJECTIVE_FORWARD_SEC
  elif phase in {"phev_state", "stationary_state"}:
    state_len = max(1.0, min(3.0, end - t))
    start, stop = t, t + state_len
  else:
    start, stop = t - 3.0, t + 3.0

  max_end = route_duration if route_duration > 0.0 else stop
  return max(0.0, start), min(max_end, stop)


def label_identity(route_uuid: str, review_job_id: int, bookmark_id: int, canonical: str,
                   start_sec: float, end_sec: float, normalization_version: str) -> str:
  payload = {
    "route_uuid": route_uuid,
    "review_job_id": review_job_id,
    "bookmark_id": bookmark_id,
    "canonical": canonical,
    "start_sec": round(start_sec, 3),
    "end_sec": round(end_sec, 3),
    "normalization_version": normalization_version,
  }
  return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def db_tags(store: DriveStore, tags: list[str]) -> Any:
  return tags if store.dialect == "postgres" else json.dumps(tags)


def clear_prior_auto_voice_labels(store: DriveStore, route_uuid: str, review_job_id: int, by: str) -> int:
  rows = [dict(row) for row in store.execute(
    """SELECT id, tags, metadata_jsonb
       FROM labels
       WHERE route_uuid=? AND review_job_id=? AND deleted_at IS NULL""",
    (route_uuid, review_job_id),
  ).fetchall()]
  deleted = 0
  for row in rows:
    tags = tag_list(row.get("tags"))
    metadata = json_object(row.get("metadata_jsonb"))
    if "voice_normalized" not in tags and not metadata.get("auto_voice_normalized"):
      continue
    store.execute(
      "UPDATE labels SET deleted_at=CURRENT_TIMESTAMP, deleted_by=?, version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
      (by, row["id"]),
    )
    store.insert_label_event(review_job_id, "supersede_auto_voice_normalize", {
      "route_uuid": route_uuid,
      "label_id": row["id"],
      "reason": "replacing prior auto-normalized voice labels",
    }, int(row["id"]), by=by)
    deleted += 1
  return deleted


def upsert_label(store: DriveStore, payload: dict[str, Any]) -> int:
  row = store.one(
    """INSERT INTO labels(route_uuid,review_job_id,label_identity_hash,label_kind,label,severity,
                          start_sec,end_sec,notes,tags,created_by,updated_by,metadata_jsonb,deleted_at,deleted_by)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)
       ON CONFLICT(label_identity_hash) DO UPDATE SET
         label_kind=excluded.label_kind,
         label=excluded.label,
         severity=excluded.severity,
         start_sec=excluded.start_sec,
         end_sec=excluded.end_sec,
         notes=excluded.notes,
         tags=excluded.tags,
         updated_by=excluded.updated_by,
         updated_at=CURRENT_TIMESTAMP,
         version=labels.version+1,
         metadata_jsonb=excluded.metadata_jsonb,
         deleted_at=NULL,
         deleted_by=NULL
       RETURNING id""",
    (
      payload["route_uuid"],
      payload["review_job_id"],
      payload["label_identity_hash"],
      payload["label_kind"],
      payload["label"],
      payload["severity"],
      payload["start_sec"],
      payload["end_sec"],
      payload["notes"],
      db_tags(store, payload["tags"]),
      payload["created_by"],
      payload["updated_by"],
      store._json(payload["metadata_jsonb"]),
    ),
  )
  label_id = int(row["id"])
  store.insert_label_event(int(payload["review_job_id"]), "auto_voice_normalize", payload, label_id, by=str(payload["updated_by"]))
  return label_id


def update_trusted_route_setting(store: DriveStore, route: dict[str, Any], alpha_long: str | None, normalization_version: str) -> None:
  if not alpha_long:
    return
  metadata = json_object(route.get("metadata_jsonb"))
  trusted = json_object(metadata.get("trusted_settings"))
  trusted["alpha_long"] = alpha_long
  trusted["alpha_long_source"] = "user_reported_route_pair_2026_05_22"
  trusted["alpha_long_note"] = "Trust user report; ingest metadata can reflect latest comma settings rather than per-route settings."
  trusted["updated_by"] = normalization_version
  metadata["trusted_settings"] = trusted
  store.execute(
    "UPDATE routes SET metadata_jsonb=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
    (store._json(metadata), route["id"]),
  )


def finish_review(store: DriveStore, route: dict[str, Any], job: dict[str, Any], label_ids: list[int],
                  notes: str, finished_by: str, dry_run: bool) -> None:
  if dry_run:
    return
  existing = store.one("SELECT id FROM finished_reviews WHERE review_job_id=? ORDER BY finished_at DESC LIMIT 1", (job["id"],))
  snapshot = {
    "review_job_id": job["id"],
    "route_uuid": str(route["id"]),
    "route_id": route["route_id"],
    "label_count": len(label_ids),
    "label_ids": label_ids,
    "finished_by": finished_by,
    "notes": notes,
  }
  if existing:
    store.execute(
      """UPDATE finished_reviews
         SET finished_by=?, label_count=?, notes=?, snapshot_jsonb=?
         WHERE id=?""",
      (finished_by, len(label_ids), notes, store._json(snapshot), existing["id"]),
    )
  else:
    store.execute(
      "INSERT INTO finished_reviews(review_job_id,route_uuid,finished_by,label_count,notes,snapshot_jsonb) VALUES(?,?,?,?,?,?)",
      (job["id"], route["id"], finished_by, len(label_ids), notes, store._json(snapshot)),
    )
  store.execute(
    "UPDATE review_jobs SET status='finished', version=version+1, updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE id=?",
    (finished_by, job["id"]),
  )
  store.insert_label_event(int(job["id"]), "finish_review", snapshot, None, by=finished_by)


def normalize_route(store: DriveStore, route_id: str, *, alpha_long: str | None, normalization_version: str,
                    finish: bool, dry_run: bool, output_dir: Path) -> dict[str, Any]:
  route = route_row(store, route_id)
  route_uuid = str(route["id"])
  job = review_job_row(store, route_uuid)
  bookmarks = fetch_voice_bookmarks(store, route_uuid)
  samples = voice_align.fetch_route_samples(store, route_uuid)
  _, suggestions, route_offset = voice_align.calibrate_route(str(route["route_id"]), bookmarks, samples)
  suggestion_by_id = {int(s.bookmark_id): s for s in suggestions}
  route_duration = safe_float(route.get("duration_sec"), 0.0)

  update_trusted_route_setting(store, route, alpha_long, normalization_version)
  if not dry_run:
    clear_prior_auto_voice_labels(store, route_uuid, int(job["id"]), "brickpilot-research")

  created_rows: list[dict[str, Any]] = []
  label_ids: list[int] = []
  for bookmark in bookmarks:
    raw_text = str(bookmark.get("text") or "").strip()
    if not raw_text or raw_text == ".":
      continue
    canonical_labels = sorted(x for x in normalize_labels(raw_text) if x != "narration_other")
    if not canonical_labels:
      continue
    bookmark_tags = tag_list(bookmark.get("tags"))
    bookmark_metadata = json_object(bookmark.get("metadata_jsonb"))
    for canonical in canonical_labels:
      start_sec, end_sec = window_for_label(bookmark, canonical, route_duration)
      tags = [
        "voice_normalized",
        "voice_narration",
        normalization_version,
        f"bookmark_{bookmark['id']}",
        f"family_{label_family(canonical)}",
        f"polarity_{label_polarity(canonical)}",
        f"phase_{phase_for(canonical)}",
      ]
      if alpha_long:
        tags.append(f"alpha_long_{alpha_long}")
      tags.extend(x for x in bookmark_tags if x not in tags)
      start_sec, end_sec, tags = aligned_voice_window(
        start_sec,
        end_sec,
        tags,
        suggestion_by_id.get(int(bookmark["id"])),
        route_offset,
      )
      metadata = {
        "auto_voice_normalized": True,
        "normalization_version": normalization_version,
        "source_bookmark_id": int(bookmark["id"]),
        "source_bookmark_text": raw_text,
        "source_bookmark_metadata": bookmark_metadata,
        "source_bookmark_t_sec": safe_float(bookmark.get("t_sec")),
        "source_bookmark_end_sec": safe_float(bookmark.get("end_sec"), safe_float(bookmark.get("t_sec"))),
        "route_offset": json_safe(route_offset.__dict__ if route_offset else None),
        "alignment_suggestion": json_safe(suggestion_by_id.get(int(bookmark["id"])).__dict__ if int(bookmark["id"]) in suggestion_by_id else None),
        "alpha_long_trusted": alpha_long,
      }
      payload = {
        "route_uuid": route_uuid,
        "review_job_id": int(job["id"]),
        "label_identity_hash": label_identity(route_uuid, int(job["id"]), int(bookmark["id"]), canonical, start_sec, end_sec, normalization_version),
        "label_kind": label_kind_for(canonical),
        "label": canonical,
        "severity": label_polarity(canonical),
        "start_sec": round(start_sec, 3),
        "end_sec": round(max(end_sec, start_sec + 0.5), 3),
        "notes": f"Normalized from voice bookmark {bookmark['id']}: {raw_text}",
        "tags": tags,
        "created_by": "brickpilot-research",
        "updated_by": "brickpilot-research",
        "metadata_jsonb": metadata,
      }
      created_rows.append({
        "route_id": route["route_id"],
        "review_job_id": job["id"],
        "bookmark_id": bookmark["id"],
        "bookmark_t_sec": bookmark.get("t_sec"),
        "raw_text": raw_text,
        "label": canonical,
        "family": label_family(canonical),
        "polarity": label_polarity(canonical),
        "phase": phase_for(canonical),
        "start_sec": payload["start_sec"],
        "end_sec": payload["end_sec"],
      })
      if not dry_run:
        label_ids.append(upsert_label(store, payload))

  if finish:
    notes = f"Auto-normalized {len(created_rows)} labels from {len(bookmarks)} voice bookmarks with {normalization_version}."
    finish_review(store, route, job, label_ids, notes, "brickpilot-research", dry_run)

  if not dry_run:
    store.commit()

  output_dir.mkdir(parents=True, exist_ok=True)
  write_csv(output_dir / f"{route['route_id']}_normalized_labels.csv", created_rows)
  summary = {
    "route_id": route["route_id"],
    "route_uuid": route_uuid,
    "review_job_id": job["id"],
    "review_status_before": job.get("status"),
    "alpha_long_trusted": alpha_long,
    "bookmark_count": len(bookmarks),
    "label_count": len(created_rows),
    "label_ids": label_ids,
    "finished": bool(finish),
    "dry_run": bool(dry_run),
    "normalization_version": normalization_version,
    "route_offset": json_safe(route_offset.__dict__ if route_offset else None),
    "output_dir": str(output_dir),
  }
  (output_dir / f"{route['route_id']}_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  return summary


def main() -> int:
  parser = argparse.ArgumentParser(description="Normalize imported voice bookmarks into reviewed DriveDB labels and optionally finish reviews.")
  parser.add_argument("route_id", nargs="+", help="DriveDB route_id or route UUID. May be repeated.")
  parser.add_argument("--config", default=None)
  parser.add_argument("--alpha-long", action="append", choices=("on", "off"), default=[], help="Trusted Alpha Long state for each route, in route order.")
  parser.add_argument("--normalization-version", default=DEFAULT_NORMALIZATION_VERSION)
  parser.add_argument("--output-dir", type=Path, default=None)
  parser.add_argument("--finish", action="store_true")
  parser.add_argument("--dry-run", action="store_true")
  args = parser.parse_args()

  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  out_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / f"voice_review_normalization_{stamp}"
  store = DriveStore(load_config(args.config))
  summaries: list[dict[str, Any]] = []
  try:
    for idx, route_id in enumerate(args.route_id):
      alpha_long = args.alpha_long[idx] if idx < len(args.alpha_long) else None
      summaries.append(normalize_route(
        store,
        route_id,
        alpha_long=alpha_long,
        normalization_version=args.normalization_version,
        finish=args.finish,
        dry_run=args.dry_run,
        output_dir=out_dir,
      ))
  finally:
    store.close()
  (out_dir / "run_summary.json").write_text(json.dumps(json_safe(summaries), indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(out_dir)
  for summary in summaries:
    print(f"{summary['route_id']} labels={summary['label_count']} bookmarks={summary['bookmark_count']} finished={summary['finished']} alpha_long={summary['alpha_long_trusted']}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
