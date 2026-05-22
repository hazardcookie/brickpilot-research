#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_ROOT = SCRIPT_DIR.parents[1]
TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])).expanduser()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
# Keep brickpilot-tools first. The driving repo also has a top-level `scripts`
# directory, and it can otherwise shadow this repo's `scripts.drive_tests`.
for root in (REPO_ROOT, TOOLS_ROOT):
  while str(root) in sys.path:
    sys.path.remove(str(root))
for root in (REPO_ROOT, TOOLS_ROOT):
  sys.path.insert(0, str(root))

from openpilot.tools.lib.logreader import LogReader

from scripts.drive_tests import analyze_phev_can_candidates_0330 as phev_can
from scripts.drive_tests import train_voice_labeler_0325 as voice
from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
KEY_SHADOW_FIELDS = (
  "vEgo",
  "nearStandstill",
  "standstill",
  "cruiseStandstill",
  "longitudinalAssistActive",
  "longitudinalAssistShadowCandidate",
  "longitudinalPlannerFloorShadowCandidate",
  "longitudinalAssistDelta",
  "longitudinalAssistATarget",
  "longitudinalAssistAssistedATarget",
  "longitudinalAssistSuppressors",
  "longitudinalAssistVersionCode",
  "longitudinalAssistCandidateHash",
  "longitudinalAssistHoldTimer",
  "longitudinalAssistHoldTarget",
  "longitudinalAssistHeldActivation",
  "longitudinalAccelLag",
  "longitudinalLateralDemand",
  "longitudinalLeadClosing",
  "longitudinalPlanSource",
  "longitudinalAllowThrottle",
  "speedDeficit",
  "aEgo",
  "accelCmd",
  "accelOutputCan",
  "lazyCandidate",
  "longActive",
  "latActive",
  "hasLead",
  "leadStatus",
  "leadDistance",
  "stopping",
  "stopSource",
  "stopBrakeState",
  "stopActive",
  "stopShadowCandidate",
  "stopRequiredDecel",
  "stopPlannerDebt",
  "stopControllerDebt",
  "stopBrakeDebt",
  "stopTtc",
  "stopAssistDelta",
  "stopDistanceBuffer",
  "stopSourceValid",
  "stopRequiredDecelValid",
  "stopTtcValid",
  "stopLeadDistance",
  "stopLeadVRel",
  "stopRequestedDecel",
  "stopActualDecel",
  "stopDebtBucket",
  "stopGeometryInvalidReason",
  "stopProfile",
  "stopAssistReason",
  "stopSourcePersistSec",
  "stopMode",
  "leadAbsSpeed",
  "leadNearStoppedPersistSec",
  "rollingLeadConfidence",
  "finalStopAllowed",
  "finalStopBlockedReason",
  "leadPacingMode",
  "leadPacingTargetGap",
  "leadPacingGapError",
  "leadPacingVRel",
  "leadPacingAssistDelta",
  "leadPacingJerkLimited",
  "steeringGuardSuppressed",
  "steeringGuardUpstreamWouldSuppress",
  "steeringGuardAboveLimitFrames",
)
KEY_CARSTATESP_FIELDS = (
  "brickpilotPhevCanLoggerVersion",
  "brickpilotPhevCanCandidatePresentMask",
  "brickpilotPhevCanFrameUpdateMask",
  "brickpilotPhevCanCandidateSourceMask",
  "brickpilotPhevFaSourceMask",
  "brickpilotPhevSelectedSource",
  "brickpilotPhevCanFrameCounter",
  "brickpilotPhevHybridFlagSet",
  "brickpilotPhevCanfdLkaSteerMsg",
  "brickpilotPhevCanfdEcanBus",
  "brickpilotPhevCanfdAcanBus",
  "brickpilotPhevCanfdCamBus",
  "brickpilotPhevFaB4U8",
  "brickpilotPhevFaB4S8",
  "brickpilotPhevFaB4U8Bus0",
  "brickpilotPhevFaB4S8Bus0",
  "brickpilotPhevFaB4U8Bus130",
  "brickpilotPhevFaB4S8Bus130",
  "brickpilotPhevFaB4MirrorConsistent",
  "brickpilotPhevFaB7U8",
  "brickpilotPhevFaB7S8",
  "brickpilotPhevFaB7U8Bus0",
  "brickpilotPhevFaB7S8Bus0",
  "brickpilotPhevFaB7U8Bus130",
  "brickpilotPhevFaB7S8Bus130",
  "brickpilotPhevE0S16Byte08Le",
  "brickpilotPhevE0S16Byte10Le",
  "brickpilotPhevE0S16Byte16Le",
  "brickpilotPhevBaB11S8",
  "brickpilotPhevBaB14U8",
  "brickpilotPhev1C5B5U8",
  "brickpilotPhev10AB10U8",
  "brickpilotPhev10AB18U8",
  "brickpilotPhev120B3U8",
  "brickpilotBrake065B3U8",
  "brickpilotBrake065B9U8",
  "brickpilotBrake065B10U8",
  "brickpilotBrake065B11U8",
  "brickpilotBrake065B12U8",
  "brickpilotBrake065B14U8",
  "brickpilotAdas310B17U8",
  "brickpilotAdas310B18U8",
  "brickpilotPhev1A5B14U8",
  "brickpilotPhev1A5B15U8",
  "brickpilotPhev1A5B16U8",
  "brickpilotPhev1A5B17U8",
  "brickpilotPhev06FB4U8",
  "brickpilotPhev06FB4S8",
)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fieldnames: list[str] = []
  for row in rows:
    for key in row:
      if key not in fieldnames:
        fieldnames.append(key)
  if not fieldnames:
    fieldnames = ["empty"]
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
      writer.writerow({k: voice.json_safe(v) for k, v in row.items()})


def numeric(value: Any) -> float | None:
  if isinstance(value, bool):
    return 1.0 if value else 0.0
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return None
  return ret if math.isfinite(ret) else None


def summary_rows(rows: list[dict[str, Any]], field_names: tuple[str, ...]) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  for field in field_names:
    vals = [numeric(row.get(field)) for row in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
      continue
    unique = sorted({int(v) if float(v).is_integer() else round(v, 6) for v in vals})
    nonzero = sum(1 for v in vals if abs(v) > 1e-12)
    true_count = sum(1 for v in vals if v == 1.0)
    out.append({
      "field": field,
      "count": len(vals),
      "mean": sum(vals) / len(vals),
      "min": min(vals),
      "max": max(vals),
      "nonzero_frac": nonzero / len(vals),
      "unique_values": "|".join(str(x) for x in unique[:20]),
      "true_count": true_count if all(v in (0.0, 1.0) for v in vals) else "",
      "true_frac": true_count / len(vals) if all(v in (0.0, 1.0) for v in vals) else "",
    })
  return out


def route_info(store: DriveStore, route_id: str) -> dict[str, Any]:
  row = store.one(
    """SELECT id, route_id, route_label, drive_type, brickpilot_version, branch,
              model_bundle, duration_sec, segment_count, started_at, ended_at,
              metadata_jsonb
       FROM routes WHERE route_id=? ORDER BY updated_at DESC LIMIT 1""",
    (route_id,),
  )
  if not row:
    raise RuntimeError(f"route not found in DriveDB: {route_id}")
  out = dict(row)
  metadata = out.get("metadata_jsonb") or {}
  if isinstance(metadata, str):
    metadata = json.loads(metadata)
  out["metadata_jsonb"] = metadata
  return out


def patch_voice_route_set(test_route: str) -> None:
  training_routes = tuple(route_id for route_id in voice.TRAINING_ROUTES if route_id != test_route)
  voice.TEST_ROUTE = test_route
  voice.TRAINING_ROUTES = training_routes
  voice.ALL_ROUTES = (*training_routes, test_route)
  phev_can.TEST_ROUTE = test_route
  phev_can.ALL_ROUTES = (*training_routes, test_route)


def build_predictions(store: DriveStore, route_id: str, window_sec: float, step_sec: float,
                      max_can_features: int, min_pos: int) -> tuple[dict[str, voice.RouteInfo], list[voice.Window], dict[str, voice.Model],
                                                                   list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
  patch_voice_route_set(route_id)
  infos = voice.fetch_route_infos(store)
  atoms = voice.fetch_voice_atoms(store, infos)
  windows = voice.build_windows(store, infos, atoms, window_sec, step_sec, skip_can=False)
  model_features = voice.select_model_feature_names([w for w in windows if w.route_id in voice.VALIDATION_ROUTES], max_can_features)
  models = voice.train_models(windows, model_features, min_pos=min_pos)
  all_predictions = voice.predictions_for_route(windows, models, route_id, step_sec)
  predictions = voice.review_candidate_predictions(all_predictions, limit=140)
  model_rows = voice.model_rows(models)
  return infos, windows, models, model_rows, predictions, all_predictions


def route_counts(store: DriveStore, route_uuid: Any) -> dict[str, Any]:
  row = dict(store.one(
    """SELECT count(*) samples, min(t_sec) t_min, max(t_sec) t_max,
              min(speed_mph) speed_min_mph, max(speed_mph) speed_max_mph,
              avg(speed_mph) speed_avg_mph,
              avg(CASE WHEN speed_mph < 0.5 THEN 1.0 ELSE 0.0 END) stopped_frac,
              avg(CASE WHEN speed_mph < 6.7 THEN 1.0 ELSE 0.0 END) low_speed_frac,
              avg(CASE WHEN gas_pressed THEN 1.0 ELSE 0.0 END) gas_pressed_frac,
              avg(CASE WHEN brake_pressed THEN 1.0 ELSE 0.0 END) brake_pressed_frac,
              avg(CASE WHEN set_speed_mph BETWEEN 5 AND 95 THEN set_speed_mph END) set_speed_valid_avg_mph,
              avg(CASE WHEN set_speed_mph BETWEEN 5 AND 95 AND set_speed_mph > speed_mph THEN set_speed_mph - speed_mph END) positive_speed_deficit_valid_avg_mph,
              max(CASE WHEN set_speed_mph BETWEEN 5 AND 95 THEN set_speed_mph - speed_mph END) positive_speed_deficit_valid_max_mph
       FROM route_samples WHERE route_uuid=?""",
    (route_uuid,),
  ))
  return row


def prediction_label_summary(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
  by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
  for row in predictions:
    by_target[str(row["target"])].append(row)
  out: list[dict[str, Any]] = []
  for target, rows in by_target.items():
    best = max(rows, key=lambda r: float(r["peak_score"]))
    out.append({
      "target": target,
      "intervals": len(rows),
      "seconds": sum(float(r["end_sec"]) - float(r["start_sec"]) for r in rows),
      "best_peak_score": float(best["peak_score"]),
      "best_start_sec": best["start_sec"],
      "best_end_sec": best["end_sec"],
      "best_reason": best.get("reason", ""),
    })
  out.sort(key=lambda r: (float(r["best_peak_score"]), float(r["seconds"])), reverse=True)
  return out


def prediction_timeline(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
  out = sorted(predictions, key=lambda r: (float(r["start_sec"]), -float(r["peak_score"]), str(r["target"])))
  return [dict(row) for row in out]


def fetch_can_field_rows(store: DriveStore, route_id: str, route_uuid: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
  frames = phev_can.fetch_frames(store, {route_id: route_uuid})
  field_rows = phev_can.decoded_field_rows(frames)
  route_rows = phev_can.route_summary(field_rows, frames)
  return frames, field_rows, route_rows


def query_qlog_paths(store: DriveStore, route_uuid: Any) -> list[dict[str, Any]]:
  rows = store.execute(
    """SELECT rs.segment_index, rs.duration_sec, a.artifact_path
       FROM route_artifacts ra
       JOIN route_segments rs ON ra.segment_id=rs.id
       JOIN artifacts a ON ra.artifact_id=a.id
       WHERE ra.route_uuid=? AND ra.role='qlog'
       ORDER BY rs.segment_index""",
    (route_uuid,),
  ).fetchall()
  return [dict(row) for row in rows]


def segment_sample_starts(store: DriveStore, route_uuid: Any) -> dict[int, float]:
  rows = store.execute(
    """SELECT CAST(raw_jsonb->>'segment_index' AS INTEGER) AS segment_index, min(t_sec) AS start_sec
       FROM route_samples WHERE route_uuid=?
       GROUP BY CAST(raw_jsonb->>'segment_index' AS INTEGER)""",
    (route_uuid,),
  ).fetchall()
  return {int(row["segment_index"]): float(row["start_sec"]) for row in rows if row["segment_index"] is not None}


def extract_qlog_shadow(store: DriveStore, route_uuid: Any, artifact_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  starts = segment_sample_starts(store, route_uuid)
  carstatesp_rows: list[dict[str, Any]] = []
  shadow_rows: list[dict[str, Any]] = []
  for qlog in query_qlog_paths(store, route_uuid):
    seg = int(qlog["segment_index"])
    path = artifact_root / str(qlog["artifact_path"])
    if not path.exists():
      continue
    messages = list(LogReader(str(path)))
    if not messages:
      continue
    relevant_monos = [
      int(msg.logMonoTime)
      for msg in messages
      if msg.which() in {"carStateSP", "controlsState"}
    ]
    if not relevant_monos:
      continue
    mono0 = min(relevant_monos)
    route_start = starts.get(seg, seg * 60.0)
    for msg in messages:
      t_sec = route_start + (int(msg.logMonoTime) - mono0) / 1e9
      which = msg.which()
      if which == "carStateSP":
        data = msg.carStateSP.to_dict()
        row: dict[str, Any] = {"segment_index": seg, "route_time_sec": round(t_sec, 6)}
        for key in KEY_CARSTATESP_FIELDS:
          row[key] = data.get(key)
        carstatesp_rows.append(row)
      elif which == "controlsState":
        data = msg.controlsState.to_dict().get("brickpilotShadow") or {}
        row = {"segment_index": seg, "route_time_sec": round(t_sec, 6)}
        for key in KEY_SHADOW_FIELDS:
          row[key] = data.get(key)
        shadow_rows.append(row)
  return carstatesp_rows, shadow_rows


def interval_shadow_summary(predictions: list[dict[str, Any]], shadow_rows: list[dict[str, Any]],
                            carstatesp_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  for pred in predictions:
    start = float(pred["start_sec"])
    end = float(pred["end_sec"])
    shadow_bucket = [r for r in shadow_rows if start <= float(r["route_time_sec"]) <= end]
    sp_bucket = [r for r in carstatesp_rows if start <= float(r["route_time_sec"]) <= end]
    row: dict[str, Any] = {
      "rank": pred.get("rank", ""),
      "target": pred["target"],
      "start_sec": start,
      "end_sec": end,
      "peak_score": pred.get("peak_score", ""),
      "shadow_samples": len(shadow_bucket),
      "carstatesp_samples": len(sp_bucket),
    }
    for field in (
      "longitudinalAssistActive",
      "longitudinalAssistShadowCandidate",
      "longitudinalPlannerFloorShadowCandidate",
      "longitudinalAssistDelta",
      "speedDeficit",
      "accelCmd",
      "accelOutputCan",
      "stopActive",
      "stopShadowCandidate",
      "stopRequiredDecel",
      "stopPlannerDebt",
      "stopControllerDebt",
      "stopBrakeDebt",
      "stopTtc",
      "stopAssistDelta",
      "lazyCandidate",
      "longActive",
      "hasLead",
      "steeringGuardSuppressed",
      "steeringGuardUpstreamWouldSuppress",
    ):
      vals = [numeric(r.get(field)) for r in shadow_bucket]
      vals = [v for v in vals if v is not None]
      if vals:
        row[f"{field}_mean"] = sum(vals) / len(vals)
        row[f"{field}_max"] = max(vals)
    for field in (
      "brickpilotPhevFaB4U8",
      "brickpilotPhevFaB4S8",
      "brickpilotPhevFaB4MirrorConsistent",
      "brickpilotPhevBaB14U8",
      "brickpilotBrake065B9U8",
      "brickpilotBrake065B10U8",
    ):
      vals = [numeric(r.get(field)) for r in sp_bucket]
      vals = [v for v in vals if v is not None]
      if vals:
        row[f"{field}_mean"] = sum(vals) / len(vals)
        row[f"{field}_min"] = min(vals)
        row[f"{field}_max"] = max(vals)
    out.append(row)
  return out


def aggregate_prediction_dynamics(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
  by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
  for row in intervals:
    by_target[str(row["target"])].append(row)
  out: list[dict[str, Any]] = []
  for target, rows in sorted(by_target.items()):
    def mean_field(name: str) -> float | str:
      vals = [numeric(r.get(name)) for r in rows]
      vals = [v for v in vals if v is not None]
      return sum(vals) / len(vals) if vals else ""
    out.append({
      "target": target,
      "interval_count": len(rows),
      "samples": sum(int(r.get("shadow_samples") or 0) for r in rows),
      "assist_active_mean": mean_field("longitudinalAssistActive_mean"),
      "planner_floor_candidate_mean": mean_field("longitudinalPlannerFloorShadowCandidate_mean"),
      "assist_delta_mean": mean_field("longitudinalAssistDelta_mean"),
      "speed_deficit_mean": mean_field("speedDeficit_mean"),
      "stop_active_mean": mean_field("stopActive_mean"),
      "stop_shadow_candidate_mean": mean_field("stopShadowCandidate_mean"),
      "stop_required_decel_mean": mean_field("stopRequiredDecel_mean"),
      "stop_planner_debt_mean": mean_field("stopPlannerDebt_mean"),
      "stop_controller_debt_mean": mean_field("stopControllerDebt_mean"),
      "stop_brake_debt_mean": mean_field("stopBrakeDebt_mean"),
      "stop_ttc_mean": mean_field("stopTtc_mean"),
      "stop_assist_delta_mean": mean_field("stopAssistDelta_mean"),
      "fa_b4_u8_mean": mean_field("brickpilotPhevFaB4U8_mean"),
      "brake065_b9_mean": mean_field("brickpilotBrake065B9U8_mean"),
    })
  return out


def svg_label_chart(path: Path, label_rows: list[dict[str, Any]]) -> None:
  top = label_rows[:18]
  phev_can.svg_bar_chart(
    path,
    "Predicted labels by retained interval count",
    [str(r["target"]) for r in top],
    [float(r["intervals"]) for r in top],
  )


def svg_score_chart(path: Path, label_rows: list[dict[str, Any]]) -> None:
  top = label_rows[:18]
  phev_can.svg_bar_chart(
    path,
    "Predicted labels by best score",
    [str(r["target"]) for r in top],
    [float(r["best_peak_score"]) for r in top],
  )


def svg_shadow_chart(path: Path, shadow_rows: list[dict[str, Any]], carstatesp_rows: list[dict[str, Any]],
                     version: str) -> None:
  series = {
    "assist_delta_x10": [(float(r["route_time_sec"]), float(r.get("longitudinalAssistDelta") or 0.0) * 10.0) for r in shadow_rows],
    "floor_candidate": [(float(r["route_time_sec"]), float(r.get("longitudinalPlannerFloorShadowCandidate") or 0.0)) for r in shadow_rows],
    "fa_b4_s8": [(float(r["route_time_sec"]), float(r.get("brickpilotPhevFaB4S8") or 0.0)) for r in carstatesp_rows],
  }
  phev_can.svg_trace(path, f"{version} assist and PHEV candidate trace", series)


def report(route: dict[str, Any], out_dir: Path, counts: dict[str, Any], predictions: list[dict[str, Any]],
           label_rows: list[dict[str, Any]], interval_rows: list[dict[str, Any]],
           route_can_rows: list[dict[str, Any]], carstatesp_summary: list[dict[str, Any]],
           shadow_summary: list[dict[str, Any]], run_summary: dict[str, Any]) -> str:
  software = (route.get("metadata_jsonb") or {}).get("software") or {}
  settings = (route.get("metadata_jsonb") or {}).get("settings") or {}
  version = str(route.get("brickpilot_version") or "unknown")
  top_intervals = predictions[:24]
  highlight_fields = {r["field"]: r for r in route_can_rows if r["field"] in {
    "fa_b4_s8_bus0", "fa_b4_s8_bus130", "fa_b4_u8_bus0", "fa_b4_u8_bus130",
    "brake065_b9_u8_bus0", "brake065_b10_u8_bus0", "ba_b14_u8_bus0",
    "e0_s16_10_le_bus0", "a10_b10_u8_bus0", "a120_b3_u8_bus0", "c5_b5_u8_bus0",
  }}
  shadow_by_field = {r["field"]: r for r in shadow_summary}
  carsp_by_field = {r["field"]: r for r in carstatesp_summary}
  lines = [
    f"# Preliminary {version} Test Route",
    "",
    f"Route: `{route['route_id']}`",
    f"Output: `{out_dir}`",
    "",
    "## Route Snapshot",
    "",
    f"- Version: `{route.get('brickpilot_version')}`",
    f"- Branch: `{route.get('branch')}`",
    f"- Commit: `{software.get('commit', '')}`",
    f"- Model bundle: `{route.get('model_bundle')}`",
    f"- ExperimentalMode: `{settings.get('ExperimentalMode', '')}`; LongitudinalPersonality: `{settings.get('LongitudinalPersonality', '')}`",
    f"- Drive type: `{route.get('drive_type')}`",
    f"- Duration: {float(route.get('duration_sec') or 0):.1f}s across {route.get('segment_count')} segments",
    f"- Samples: {counts.get('samples')}; test windows: {run_summary['test_windows']}",
    f"- Speed: avg {float(counts.get('speed_avg_mph') or 0):.1f} mph, max {float(counts.get('speed_max_mph') or 0):.1f} mph",
    f"- Stopped fraction: {float(counts.get('stopped_frac') or 0):.3f}; low-speed fraction (<6.7 mph): {float(counts.get('low_speed_frac') or 0):.3f}",
    f"- Gas pressed fraction: {float(counts.get('gas_pressed_frac') or 0):.3f}; brake pressed fraction: {float(counts.get('brake_pressed_frac') or 0):.3f}",
    f"- Positive set-speed deficit, valid set speeds: avg {float(counts.get('positive_speed_deficit_valid_avg_mph') or 0):.1f} mph, max {float(counts.get('positive_speed_deficit_valid_max_mph') or 0):.1f} mph",
    "",
    "## Prediction Summary",
    "",
    f"- Trained targets: {run_summary['trained_targets']}",
    f"- Candidate predictions retained for review: {len(predictions)}",
    f"- All generated predictions: {run_summary['all_predictions']}",
    "",
  ]
  for row in label_rows[:24]:
    lines.append(
      f"- `{row['target']}`: {row['intervals']} intervals, best score={float(row['best_peak_score']):.3f} at {float(row['best_start_sec']):.1f}-{float(row['best_end_sec']):.1f}s"
    )
  lines.extend([
    "",
    "## Top Candidate Intervals",
    "",
  ])
  for row in top_intervals:
    reason = str(row.get("reason", "")).replace("\n", " ")
    lines.append(f"- {float(row['start_sec']):.1f}-{float(row['end_sec']):.1f}s `{row['target']}` score={float(row['peak_score']):.3f} reason={reason}")
  lines.extend([
    "",
    f"## {version} Shadow-Log Read",
    "",
  ])
  for field in (
    "longitudinalAssistActive",
    "longitudinalAssistShadowCandidate",
    "longitudinalPlannerFloorShadowCandidate",
    "longitudinalAssistDelta",
    "speedDeficit",
    "accelCmd",
    "accelOutputCan",
    "stopActive",
    "stopShadowCandidate",
    "stopRequiredDecel",
    "stopPlannerDebt",
    "stopControllerDebt",
    "stopBrakeDebt",
    "stopTtc",
    "stopAssistDelta",
    "stopDistanceBuffer",
    "steeringGuardSuppressed",
    "steeringGuardUpstreamWouldSuppress",
  ):
    row = shadow_by_field.get(field)
    if row:
      lines.append(f"- `{field}`: mean={float(row['mean']):.4g}, min={float(row['min']):.4g}, max={float(row['max']):.4g}, nonzero={float(row['nonzero_frac']):.3f}")
  lines.extend([
    "",
    "## Direct PHEV Logger Read",
    "",
  ])
  for field in (
    "brickpilotPhevCanLoggerVersion",
    "brickpilotPhevHybridFlagSet",
    "brickpilotPhevFaB4MirrorConsistent",
    "brickpilotPhevFaB4U8",
    "brickpilotPhevFaB4S8",
    "brickpilotPhevFaB7U8",
    "brickpilotPhevFaB7S8",
    "brickpilotPhevBaB14U8",
    "brickpilotBrake065B3U8",
    "brickpilotBrake065B9U8",
    "brickpilotBrake065B10U8",
    "brickpilotBrake065B11U8",
    "brickpilotBrake065B12U8",
    "brickpilotBrake065B14U8",
  ):
    row = carsp_by_field.get(field)
    if row:
      lines.append(f"- `{field}`: mean={float(row['mean']):.4g}, min={float(row['min']):.4g}, max={float(row['max']):.4g}, nonzero={float(row['nonzero_frac']):.3f}")
  lines.extend([
    "",
    "## Highlighted CAN Candidates",
    "",
  ])
  for field, row in sorted(highlight_fields.items()):
    lines.append(f"- `{field}`: mean={float(row['mean']):.3f} std={float(row['std']):.3f} min={float(row['min']):.3f} max={float(row['max']):.3f}")
  lines.extend([
    "",
    "## Preliminary Read",
    "",
    f"- This is a {version} test route, so the key question is whether the current Brickpilot behavior changed active assist, speed-deficit, steering texture, and PHEV veto patterns relative to earlier 0.4.x routes.",
    "- Treat predicted bad/good labels as review candidates, not ground truth. They are useful for deciding where to inspect the route and what to compare against the 0.4.0-beta routes.",
    "- The direct qlog read is more important than DB `route_samples` for 0.4.x because it includes the new `brickpilotShadow` and `carStateSP` fields.",
    "",
    "## Files",
    "",
  ])
  for name in sorted(p.name for p in out_dir.iterdir() if p.is_file()):
    lines.append(f"- `{name}`")
  return "\n".join(lines) + "\n"


def main() -> int:
  parser = argparse.ArgumentParser(description="Run a preliminary Brickpilot test-route pass using the 0.4.x weak labeler and direct qlog shadow extraction.")
  parser.add_argument("route_id")
  parser.add_argument("--config", default=None)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--output-dir", type=Path, default=None)
  parser.add_argument("--window-sec", type=float, default=6.0)
  parser.add_argument("--step-sec", type=float, default=3.0)
  parser.add_argument("--min-pos", type=int, default=3)
  parser.add_argument("--max-can-model-features", type=int, default=700)
  args = parser.parse_args()

  store = DriveStore(load_config(args.config))
  route = route_info(store, args.route_id)
  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  safe_version = str(route.get("brickpilot_version") or "unknown").replace(".", "_").replace("-", "_")
  out_dir = args.output_dir or (args.output_root / f"prelim_{safe_version}_{args.route_id}_{stamp}")
  out_dir.mkdir(parents=True, exist_ok=True)

  infos, windows, models, model_rows, predictions, all_predictions = build_predictions(
    store, args.route_id, args.window_sec, args.step_sec, args.max_can_model_features, args.min_pos)
  atoms = voice.fetch_voice_atoms(store, infos)
  model_features = voice.select_model_feature_names([w for w in windows if w.route_id in voice.VALIDATION_ROUTES], args.max_can_model_features)
  counts = route_counts(store, route["id"])
  label_rows = prediction_label_summary(predictions)
  timeline_rows = prediction_timeline(predictions)
  _, can_field_rows, route_can_rows = fetch_can_field_rows(store, args.route_id, route["id"])
  interval_can_rows = phev_can.summarize_interval_values(can_field_rows, predictions)
  highlight_fields = {
    "fa_b4_s8_bus0", "fa_b4_s8_bus130", "fa_b4_u8_bus0", "fa_b4_u8_bus130",
    "fa_b7_u8_bus0", "fa_b7_u8_bus130", "brake065_b3_u8_bus0", "brake065_b3_u8_bus130",
    "brake065_b9_u8_bus0", "brake065_b9_u8_bus130", "brake065_b10_u8_bus0",
    "brake065_b10_u8_bus130", "brake065_b11_u8_bus0", "brake065_b11_u8_bus130",
    "brake065_b12_u8_bus0", "brake065_b12_u8_bus130", "brake065_b14_u8_bus0",
    "brake065_b14_u8_bus130", "ba_b11_s8_bus0", "ba_b14_u8_bus0", "ba_b14_u8_bus130",
    "e0_s16_08_le_bus0", "e0_s16_10_le_bus0", "e0_s16_16_le_bus0",
    "a10_b10_u8_bus0", "a10_b18_u8_bus0", "a120_b3_u8_bus0", "c5_b5_u8_bus0",
    "f06f_b4_s8_bus0", "adas310_b17_u8_bus1", "adas310_b18_u8_bus1",
  }
  highlight_can_rows = [r for r in route_can_rows if r["field"] in highlight_fields]
  carstatesp_rows, shadow_rows = extract_qlog_shadow(store, route["id"], store.cfg.artifact_root)
  carstatesp_summary = summary_rows(carstatesp_rows, KEY_CARSTATESP_FIELDS)
  shadow_summary = summary_rows(shadow_rows, KEY_SHADOW_FIELDS)
  interval_shadow_rows = interval_shadow_summary(predictions, shadow_rows, carstatesp_rows)
  dynamics_rows = aggregate_prediction_dynamics(interval_shadow_rows)

  write_csv(out_dir / "route_summary.csv", voice.route_summary_rows(infos, atoms, windows))
  write_csv(out_dir / "model_feature_summary.csv", model_rows)
  write_csv(out_dir / "test_route_windows.csv", voice.window_rows([w for w in windows if w.route_id == args.route_id], model_features))
  write_csv(out_dir / "test_route_predictions.csv", predictions)
  write_csv(out_dir / "test_route_predictions_all.csv", all_predictions)
  write_csv(out_dir / "prediction_label_summary.csv", label_rows)
  write_csv(out_dir / "prediction_timeline.csv", timeline_rows)
  write_csv(out_dir / "route_can_candidate_summary.csv", route_can_rows)
  write_csv(out_dir / "prediction_interval_can_summary.csv", interval_can_rows)
  write_csv(out_dir / "highlight_can_candidate_summary.csv", highlight_can_rows)
  write_csv(out_dir / "carstatesp_shadow_samples.csv", carstatesp_rows)
  write_csv(out_dir / "carstatesp_shadow_summary.csv", carstatesp_summary)
  write_csv(out_dir / "brickpilot_shadow_samples.csv", shadow_rows)
  write_csv(out_dir / "brickpilot_shadow_summary.csv", shadow_summary)
  write_csv(out_dir / "prediction_carstatesp_shadow_summary.csv", interval_shadow_rows)
  write_csv(out_dir / "prediction_label_dynamics_aggregate.csv", dynamics_rows)
  svg_label_chart(out_dir / "prediction_label_chart.svg", label_rows)
  svg_score_chart(out_dir / "prediction_score_chart.svg", label_rows)
  svg_shadow_chart(out_dir / "carstatesp_shadow_chart.svg", shadow_rows, carstatesp_rows, str(route.get("brickpilot_version") or "unknown"))
  phev_can.svg_bar_chart(
    out_dir / "can_highlight_chart.svg",
    "Highlighted CAN candidate means",
    [str(r["field"]) for r in highlight_can_rows],
    [float(r["mean"]) for r in highlight_can_rows],
  )

  run_summary = {
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "route_id": args.route_id,
    "output_dir": str(out_dir),
    "route": voice.json_safe(route),
    "route_counts": voice.json_safe(counts),
    "trained_targets": len(models),
    "test_windows": sum(1 for w in windows if w.route_id == args.route_id),
    "all_predictions": len(all_predictions),
    "review_predictions": len(predictions),
    "top_prediction_labels": label_rows[:24],
    "carstatesp_samples": len(carstatesp_rows),
    "brickpilot_shadow_samples": len(shadow_rows),
    "carstatesp_summary": carstatesp_summary,
    "brickpilot_shadow_summary": shadow_summary,
    "highlight_can_fields": highlight_can_rows,
    "artifacts": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
  }
  (out_dir / "run_summary.json").write_text(json.dumps(voice.json_safe(run_summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
  (out_dir / "report.md").write_text(
    report(route, out_dir, counts, predictions, label_rows, interval_shadow_rows, route_can_rows,
           carstatesp_summary, shadow_summary, run_summary),
    encoding="utf-8",
  )
  run_summary["artifacts"] = sorted(p.name for p in out_dir.iterdir() if p.is_file())
  (out_dir / "run_summary.json").write_text(json.dumps(voice.json_safe(run_summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(out_dir)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
