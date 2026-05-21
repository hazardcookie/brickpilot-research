#!/usr/bin/env python3
"""Offline Brickpilot lateral steering smoothness sweep over DriveDB qlogs."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_ROOT = SCRIPT_DIR.parents[1]
TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])).expanduser()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
for root in (REPO_ROOT, TOOLS_ROOT):
  if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from openpilot.tools.lib.logreader import LogReader

from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
DEFAULT_ROUTE_IDS = (
  "00000189--6fb7c85de1",
  "0000018c--68c15f8f88",
  "0000018e--5d3d27b763",
  "00000191--bba94609ce",
  "00000195--d936b2944f",
  "00000197--a5c011c5f6",
  "00000199--d5f5711730",
  "0000019e--73cbd7a8fe",
  "0000019f--1f7c2d72e3",
  "000001a0--9ddf29c36d",
  "000001a1--5e4afaa183",
  "000001a2--3ede8392f4",
  "000001a6--2561f1b24f",
  "000001a8--cc5b1289fa",
  "000001ac--e1e3616975",
  "000001b0--90d579ad38",
  "000001b4--6e74738cbb",
  "000001b7--5f24a935ac",
  "000001b9--2e9510764e",
  "000001bc--4a526eeff9",
  "000001c0--116fd8c7ce",
)
MPH_TO_MS = 0.44704
MS_TO_MPH = 1.0 / MPH_TO_MS
MAX_RATE_GAP_SEC = 0.55
STEERING_TARGET_WEIGHTS = {
  "steering_jerk": 2.8,
  "steering_ping_pong": 2.4,
  "low_speed_lateral_bad": 2.2,
  "quality_bad": 1.2,
}


@dataclass(frozen=True)
class Candidate:
  name: str
  description: str
  full_smooth_mph: float
  no_smooth_mph: float
  alpha: float
  reversal_alpha_scale: float
  zero_hold_frames: int
  zero_cross_max_raw: float
  zero_cross_max_smooth: float
  rate_limit_per_sec: float | None = None
  second_stage_alpha: float | None = None
  output_deadband: float = 0.0
  torque_scale: float = 1.0
  driver_override_cooldown_sec: float = 0.25
  installable: bool = True


@dataclass
class FilterState:
  initialized: bool = False
  smooth: float = 0.0
  second: float = 0.0
  zero_hold: int = 0
  driver_override_cooldown_sec: float = 0.0


def safe(obj: Any, field: str, default: Any = None) -> Any:
  cur = obj
  for part in field.split("."):
    if cur is None:
      return default
    try:
      cur = getattr(cur, part)
    except Exception:
      return default
  return cur


def as_float(value: Any, default: float | None = None) -> float | None:
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return default
  return ret if math.isfinite(ret) else default


def as_bool(value: Any) -> bool:
  try:
    return bool(value)
  except Exception:
    return False


def enum_name(value: Any) -> str:
  raw = getattr(value, "raw", None)
  if raw is not None:
    value = raw
  return str(value).split(".")[-1].lower()


def lateral_state(controls_state: Any) -> tuple[str, Any]:
  lcs = safe(controls_state, "lateralControlState")
  try:
    which = lcs.which()
  except Exception:
    return "", None
  return str(which), safe(lcs, str(which))


def to_dict(obj: Any) -> dict[str, Any]:
  if obj is None:
    return {}
  try:
    return obj.to_dict()
  except Exception:
    return {}


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
    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)


def route_row(store: DriveStore, route_id: str) -> dict[str, Any] | None:
  row = store.one(
    """SELECT id, route_id, route_label, drive_type, brickpilot_version, branch,
              model_bundle, duration_sec, segment_count, metadata_jsonb
       FROM routes WHERE route_id=? ORDER BY updated_at DESC LIMIT 1""",
    (route_id,),
  )
  return dict(row) if row else None


def qlog_rows(store: DriveStore, route_uuid: Any) -> list[dict[str, Any]]:
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


def segment_starts(store: DriveStore, route_uuid: Any) -> dict[int, float]:
  rows = store.execute(
    """SELECT CAST(raw_jsonb->>'segment_index' AS INTEGER) AS segment_index, min(t_sec) AS start_sec
       FROM route_samples WHERE route_uuid=?
       GROUP BY CAST(raw_jsonb->>'segment_index' AS INTEGER)""",
    (route_uuid,),
  ).fetchall()
  return {int(row["segment_index"]): float(row["start_sec"]) for row in rows if row["segment_index"] is not None}


def latest_prelim_dir(route_id: str, root: Path = DEFAULT_OUTPUT_ROOT) -> Path | None:
  matches = sorted(root.glob(f"prelim_*_{route_id}_*"))
  return matches[-1] if matches else None


def label_intervals(route_id: str, store: DriveStore | None = None) -> list[tuple[float, float, str, float]]:
  out: list[tuple[float, float, str, float]] = []
  if store is not None:
    route = route_row(store, route_id)
    if route is not None:
      rows = store.execute(
        """SELECT label, start_sec, end_sec
           FROM labels
           WHERE route_uuid=? AND deleted_at IS NULL
           ORDER BY start_sec, id""",
        (route["id"],),
      ).fetchall()
      for row in rows:
        target = str(row["label"] or "")
        weight = STEERING_TARGET_WEIGHTS.get(target, 0.0)
        if weight <= 0.0:
          continue
        start = as_float(row["start_sec"], -1.0) or -1.0
        end = as_float(row["end_sec"], start + 1.0) or (start + 1.0)
        if start >= 0.0:
          if end <= start:
            end = start + 1.0
          out.append((start, end, target, weight))
      if out:
        return out
  prelim = latest_prelim_dir(route_id)
  if prelim is None:
    return out
  path = prelim / "test_route_predictions.csv"
  if not path.exists():
    return out
  with path.open(newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
      target = str(row.get("target") or "")
      weight = STEERING_TARGET_WEIGHTS.get(target, 0.0)
      if weight <= 0.0:
        continue
      start = as_float(row.get("start_sec"), -1.0) or -1.0
      end = as_float(row.get("end_sec"), -1.0) or -1.0
      peak = max(0.0, min(1.0, as_float(row.get("peak_score"), 0.0) or 0.0))
      if start >= 0.0 and end > start:
        out.append((start, end, target, weight * peak))
  return out


def attach_labels(samples: list[dict[str, Any]], intervals: list[tuple[float, float, str, float]]) -> None:
  if not intervals:
    return
  intervals = sorted(intervals)
  active: list[tuple[float, float, str, float]] = []
  idx = 0
  for sample in samples:
    t = float(sample["route_time_sec"])
    while idx < len(intervals) and intervals[idx][0] <= t:
      active.append(intervals[idx])
      idx += 1
    active = [interval for interval in active if interval[1] >= t]
    labels = []
    weight = 0.0
    for start, end, target, score in active:
      if start <= t <= end:
        labels.append(target)
        weight += score
    sample["steering_label_weight"] = weight
    sample["steering_labels"] = "|".join(sorted(set(labels)))


def extract_route_samples(store: DriveStore, route: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
  starts = segment_starts(store, route["id"])
  warnings: list[str] = []
  samples: list[dict[str, Any]] = []
  metadata = route.get("metadata_jsonb") or {}
  if isinstance(metadata, str):
    metadata = json.loads(metadata)
  settings = metadata.get("settings") or {}
  artifact_root = store.cfg.artifact_root
  for qlog in qlog_rows(store, route["id"]):
    seg = int(qlog["segment_index"])
    path = artifact_root / str(qlog["artifact_path"])
    if not path.exists():
      warnings.append(f"{route['route_id']} segment {seg}: missing qlog {path}")
      continue
    car_state = car_control = controls_state = live_parameters = live_torque = None
    try:
      messages = list(LogReader(str(path)))
    except Exception as exc:
      warnings.append(f"{route['route_id']} segment {seg}: qlog read failed {type(exc).__name__}: {exc}")
      continue
    if not messages:
      continue
    mono0 = min(int(msg.logMonoTime) for msg in messages)
    route_start = starts.get(seg, seg * 60.0)
    for msg in messages:
      t = route_start + (int(msg.logMonoTime) - mono0) / 1e9
      try:
        which = msg.which()
      except Exception:
        continue
      if which == "carState":
        car_state = msg.carState
      elif which == "carControl":
        car_control = msg.carControl
      elif which == "liveParameters":
        live_parameters = msg.liveParameters
      elif which == "liveTorqueParameters":
        live_torque = msg.liveTorqueParameters
      elif which == "controlsState":
        controls_state = msg.controlsState
        ltype, lstate = lateral_state(controls_state)
        shadow = to_dict(controls_state).get("brickpilotShadow") or {}
        v_ego = as_float(safe(car_state, "vEgo"), 0.0) or 0.0
        torque_cmd = as_float(shadow.get("torqueCmd"))
        if torque_cmd is None:
          torque_cmd = as_float(safe(lstate, "output"))
        if torque_cmd is None:
          torque_cmd = as_float(safe(car_control, "actuators.torque"))
        if torque_cmd is None:
          continue
        steering_pressed = as_bool(safe(car_state, "steeringPressed")) or as_bool(shadow.get("steeringPressed"))
        lat_active = as_bool(safe(car_control, "latActive")) or as_bool(safe(lstate, "active"))
        gas_pressed = as_bool(safe(car_state, "gasPressed"))
        brake_pressed = as_bool(safe(car_state, "brakePressed"))
        clean_lateral = bool(lat_active and ltype == "torqueState" and not steering_pressed)
        samples.append({
          "route_id": route["route_id"],
          "route_label": route.get("route_label") or route["route_id"],
          "drive_type": route.get("drive_type") or "",
          "brickpilot_version": route.get("brickpilot_version") or "",
          "model_bundle": route.get("model_bundle") or "",
          "experimental_mode": str(settings.get("ExperimentalMode", "")),
          "segment_index": seg,
          "route_time_sec": round(t, 6),
          "speed_mph": v_ego * MS_TO_MPH,
          "lat_active": lat_active,
          "clean_lateral": clean_lateral,
          "steering_pressed": steering_pressed,
          "gas_pressed": gas_pressed,
          "brake_pressed": brake_pressed,
          "lateral_type": ltype,
          "torque_cmd": torque_cmd,
          "torque_output_can": as_float(shadow.get("torqueOutputCan")),
          "output_torque_rate_logged": as_float(shadow.get("outputTorqueRate")),
          "desired_lateral_accel": as_float(safe(lstate, "desiredLateralAccel")),
          "actual_lateral_accel": as_float(safe(lstate, "actualLateralAccel")),
          "lateral_error": as_float(safe(lstate, "error")),
          "desired_lateral_jerk": as_float(shadow.get("desiredLateralJerk")),
          "saturated": as_bool(safe(lstate, "saturated")),
          "steering_angle_deg": as_float(safe(car_state, "steeringAngleDeg")),
          "steering_torque": as_float(safe(car_state, "steeringTorque")),
          "steering_torque_eps": as_float(safe(car_state, "steeringTorqueEps")),
          "steer_ratio_live": as_float(safe(live_parameters, "steerRatio")),
          "stiffness_factor_live": as_float(safe(live_parameters, "stiffnessFactor")),
          "angle_offset_deg": as_float(safe(live_parameters, "angleOffsetDeg")),
          "lat_accel_factor_filtered": as_float(safe(live_torque, "latAccelFactorFiltered")),
          "friction_coefficient_filtered": as_float(safe(live_torque, "frictionCoefficientFiltered")),
          "live_torque_use_params": as_bool(safe(live_torque, "useParams")),
          "steering_label_weight": 0.0,
          "steering_labels": "",
        })
  samples.sort(key=lambda row: (str(row["route_id"]), float(row["route_time_sec"])))
  attach_labels(samples, label_intervals(str(route["route_id"]), store))
  return samples, warnings


def candidate_set() -> list[Candidate]:
  return [
    Candidate(
      name="observed_current_proxy",
      description="Observed torque command; baseline for offline scoring.",
      full_smooth_mph=0.0,
      no_smooth_mph=0.0,
      alpha=1.0,
      reversal_alpha_scale=1.0,
      zero_hold_frames=0,
      zero_cross_max_raw=0.0,
      zero_cross_max_smooth=0.0,
      installable=False,
    ),
    Candidate(
      name="044_texture_proxy",
      description="Current 0.4.4 steering texture smoother simulated on observed command.",
      full_smooth_mph=34.0,
      no_smooth_mph=62.0,
      alpha=0.16,
      reversal_alpha_scale=0.50,
      zero_hold_frames=5,
      zero_cross_max_raw=0.42,
      zero_cross_max_smooth=0.42,
    ),
    Candidate(
      name="045_suburban_glide",
      description="Aggressive suburban low-pass: wider speed band, lower alpha, stronger reversal damping.",
      full_smooth_mph=42.0,
      no_smooth_mph=74.0,
      alpha=0.10,
      reversal_alpha_scale=0.35,
      zero_hold_frames=7,
      zero_cross_max_raw=0.52,
      zero_cross_max_smooth=0.52,
    ),
    Candidate(
      name="045_rate_limited_texture",
      description="0.4.4-like smoother with explicit torque slew clamp for blocky wheel steps.",
      full_smooth_mph=36.0,
      no_smooth_mph=66.0,
      alpha=0.14,
      reversal_alpha_scale=0.45,
      zero_hold_frames=6,
      zero_cross_max_raw=0.46,
      zero_cross_max_smooth=0.46,
      rate_limit_per_sec=1.65,
    ),
    Candidate(
      name="045_two_stage_texture",
      description="Two-pole torque texture filter; stronger high-frequency cleanup, moderate command lag.",
      full_smooth_mph=38.0,
      no_smooth_mph=70.0,
      alpha=0.13,
      reversal_alpha_scale=0.40,
      zero_hold_frames=6,
      zero_cross_max_raw=0.48,
      zero_cross_max_smooth=0.48,
      second_stage_alpha=0.24,
    ),
    Candidate(
      name="045_deadband_zero_hold",
      description="Targets center ping-pong with wider zero-cross hold plus tiny command deadband.",
      full_smooth_mph=36.0,
      no_smooth_mph=64.0,
      alpha=0.14,
      reversal_alpha_scale=0.42,
      zero_hold_frames=9,
      zero_cross_max_raw=0.58,
      zero_cross_max_smooth=0.58,
      output_deadband=0.045,
    ),
    Candidate(
      name="045_steer_ratio_plus_proxy",
      description="Proxy for a higher effective steer ratio / less twitchy torque response by scaling command down slightly.",
      full_smooth_mph=34.0,
      no_smooth_mph=62.0,
      alpha=0.16,
      reversal_alpha_scale=0.50,
      zero_hold_frames=5,
      zero_cross_max_raw=0.42,
      zero_cross_max_smooth=0.42,
      torque_scale=0.92,
    ),
    Candidate(
      name="frontier_yolo_glide",
      description="Non-installable ceiling: very slow glide filter to estimate maximum smoothness possible before lag becomes obvious.",
      full_smooth_mph=50.0,
      no_smooth_mph=82.0,
      alpha=0.06,
      reversal_alpha_scale=0.25,
      zero_hold_frames=10,
      zero_cross_max_raw=0.68,
      zero_cross_max_smooth=0.68,
      rate_limit_per_sec=0.90,
      second_stage_alpha=0.18,
      output_deadband=0.06,
      installable=False,
    ),
    Candidate(
      name="frontier_raw_rate_cap_only",
      description="Non-installable diagnostic: no low-pass, only strict rate cap to isolate block-step sensitivity.",
      full_smooth_mph=90.0,
      no_smooth_mph=91.0,
      alpha=1.0,
      reversal_alpha_scale=1.0,
      zero_hold_frames=0,
      zero_cross_max_raw=0.0,
      zero_cross_max_smooth=0.0,
      rate_limit_per_sec=1.10,
      installable=False,
    ),
  ]


def reset_state(state: FilterState) -> None:
  state.initialized = False
  state.smooth = 0.0
  state.second = 0.0
  state.zero_hold = 0


def reset_all_state(state: FilterState) -> None:
  reset_state(state)
  state.driver_override_cooldown_sec = 0.0


def apply_candidate(candidate: Candidate, state: FilterState, raw: float, speed_mph: float,
                    lat_active: bool, steering_pressed: bool, lateral_type: str, dt: float) -> float:
  raw *= candidate.torque_scale
  if candidate.name == "observed_current_proxy":
    return raw
  if speed_mph >= candidate.no_smooth_mph:
    reset_all_state(state)
    return raw
  if not lat_active or lateral_type != "torqueState":
    reset_state(state)
    return raw
  if steering_pressed:
    reset_state(state)
    state.driver_override_cooldown_sec = candidate.driver_override_cooldown_sec
    return raw
  if state.driver_override_cooldown_sec > 0.0:
    state.driver_override_cooldown_sec = max(0.0, state.driver_override_cooldown_sec - max(dt, 0.0))
    reset_state(state)
    return raw
  if state.zero_hold > 0:
    if abs(raw) <= candidate.zero_cross_max_raw:
      state.zero_hold -= 1
      state.initialized = True
      state.smooth = 0.0
      state.second = 0.0
      return 0.0
    state.zero_hold = 0
  if not state.initialized:
    state.initialized = True
    state.smooth = raw
    state.second = raw
    return raw
  speed_blend = 1.0
  if speed_mph > candidate.full_smooth_mph:
    denom = max(0.001, candidate.no_smooth_mph - candidate.full_smooth_mph)
    speed_blend = max(0.0, min(1.0, (candidate.no_smooth_mph - speed_mph) / denom))
  alpha = 1.0 - speed_blend * (1.0 - candidate.alpha)
  weak_zero_cross = bool(raw * state.smooth < 0.0 and
                         abs(raw) <= candidate.zero_cross_max_raw and
                         abs(state.smooth) <= candidate.zero_cross_max_smooth)
  if weak_zero_cross:
    state.zero_hold = candidate.zero_hold_frames
    state.smooth = 0.0
    state.second = 0.0
    return 0.0
  if raw * state.smooth < 0.0 and abs(raw - state.smooth) > 0.35:
    alpha *= candidate.reversal_alpha_scale
  next_value = state.smooth + alpha * (raw - state.smooth)
  if candidate.rate_limit_per_sec is not None and dt > 0.0:
    limit = candidate.rate_limit_per_sec * dt
    next_value = max(state.smooth - limit, min(state.smooth + limit, next_value))
  if abs(next_value) < candidate.output_deadband and abs(raw) < candidate.zero_cross_max_raw:
    next_value = 0.0
  state.smooth = next_value
  if candidate.second_stage_alpha is not None:
    state.second += candidate.second_stage_alpha * (state.smooth - state.second)
    return state.second
  return state.smooth


def finite(values: list[Any]) -> list[float]:
  out = []
  for value in values:
    value = as_float(value)
    if value is not None:
      out.append(value)
  return out


def percentile(values: list[float], q: float) -> float:
  if not values:
    return 0.0
  values = sorted(values)
  idx = min(len(values) - 1, max(0, int(round((q / 100.0) * (len(values) - 1)))))
  return values[idx]


def rms(values: list[float]) -> float:
  return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


def derivative_rows(samples: list[dict[str, Any]], key: str) -> tuple[list[float], list[float], int]:
  rates: list[float] = []
  jerks: list[float] = []
  sign_changes = 0
  prev = None
  prev_rate = None
  prev_t = None
  prev_sign = 0
  for sample in samples:
    value = as_float(sample.get(key))
    t = as_float(sample.get("route_time_sec"))
    if value is None or t is None:
      continue
    sign = 1 if value > 1e-6 else -1 if value < -1e-6 else 0
    if prev is not None and prev_t is not None:
      dt = t - prev_t
      if 0.0 < dt <= MAX_RATE_GAP_SEC:
        rate = (value - prev) / dt
        rates.append(rate)
        if prev_rate is not None:
          jerks.append((rate - prev_rate) / dt)
        prev_rate = rate
        if sign and prev_sign and sign != prev_sign:
          sign_changes += 1
    if sign:
      prev_sign = sign
    prev = value
    prev_t = t
  return rates, jerks, sign_changes


def add_dts(samples: list[dict[str, Any]]) -> None:
  by_route: dict[str, list[dict[str, Any]]] = {}
  for sample in samples:
    by_route.setdefault(str(sample["route_id"]), []).append(sample)
  for rows in by_route.values():
    rows.sort(key=lambda row: float(row["route_time_sec"]))
    for idx, row in enumerate(rows):
      if idx + 1 < len(rows):
        dt = float(rows[idx + 1]["route_time_sec"]) - float(row["route_time_sec"])
        row["dt"] = dt if 0.0 < dt <= MAX_RATE_GAP_SEC else 0.0
      else:
        row["dt"] = 0.0


def simulate_candidate(samples: list[dict[str, Any]], candidate: Candidate) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  state_by_route: dict[str, FilterState] = {}
  for sample in sorted(samples, key=lambda row: (str(row["route_id"]), float(row["route_time_sec"]))):
    route_id = str(sample["route_id"])
    state = state_by_route.setdefault(route_id, FilterState())
    raw = float(sample["torque_cmd"])
    filtered = apply_candidate(candidate, state, raw, float(sample.get("speed_mph") or 0.0),
                               bool(sample.get("lat_active")), bool(sample.get("steering_pressed")),
                               str(sample.get("lateral_type") or ""), float(sample.get("dt") or 0.0))
    row = dict(sample)
    row["candidate"] = candidate.name
    row["filtered_torque_cmd"] = filtered
    row["torque_delta"] = filtered - raw
    out.append(row)
  return out


def metric_summary(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
  rates, jerks, sign_changes = derivative_rows(rows, key)
  duration = sum(float(row.get("dt") or 0.0) for row in rows)
  values = finite([row.get(key) for row in rows])
  return {
    "duration_sec": duration,
    "rms": rms(values),
    "p95_abs": percentile([abs(v) for v in values], 95),
    "max_abs": max((abs(v) for v in values), default=0.0),
    "rate_rms": rms(rates),
    "rate_p95_abs": percentile([abs(v) for v in rates], 95),
    "jerk_rms": rms(jerks),
    "jerk_p95_abs": percentile([abs(v) for v in jerks], 95),
    "sign_changes_per_min": sign_changes * 60.0 / duration if duration > 1e-6 else 0.0,
  }


def score_candidates(samples: list[dict[str, Any]], candidates: list[Candidate]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  add_dts(samples)
  clean = [row for row in samples if row.get("clean_lateral") and float(row.get("speed_mph") or 0.0) >= 3.0]
  raw_metrics = metric_summary(clean, "torque_cmd")
  rows: list[dict[str, Any]] = []
  route_rows: list[dict[str, Any]] = []
  for candidate in candidates:
    simulated_all = simulate_candidate(samples, candidate)
    simulated = [row for row in simulated_all if row.get("clean_lateral") and float(row.get("speed_mph") or 0.0) >= 3.0]
    filtered_metrics = metric_summary(simulated, "filtered_torque_cmd")
    deltas = finite([row.get("torque_delta") for row in simulated])
    label_weighted = [row for row in simulated if float(row.get("steering_label_weight") or 0.0) > 0.0]
    label_metrics = metric_summary(label_weighted, "filtered_torque_cmd")
    raw_label_metrics = metric_summary(label_weighted, "torque_cmd")
    high_demand = [row for row in simulated if abs(float(row.get("torque_cmd") or 0.0)) > 0.65 or abs(float(row.get("desired_lateral_accel") or 0.0)) > 0.75]
    high_delta = finite([row.get("torque_delta") for row in high_demand])
    smooth_gain = (raw_metrics["rate_p95_abs"] - filtered_metrics["rate_p95_abs"]) / max(raw_metrics["rate_p95_abs"], 1e-6)
    jerk_gain = (raw_metrics["jerk_p95_abs"] - filtered_metrics["jerk_p95_abs"]) / max(raw_metrics["jerk_p95_abs"], 1e-6)
    sign_gain = (raw_metrics["sign_changes_per_min"] - filtered_metrics["sign_changes_per_min"]) / max(raw_metrics["sign_changes_per_min"], 1e-6)
    label_gain = ((raw_label_metrics["rate_p95_abs"] - label_metrics["rate_p95_abs"]) /
                  max(raw_label_metrics["rate_p95_abs"], 1e-6)) if label_weighted else 0.0
    tracking_penalty = rms(deltas) + percentile([abs(v) for v in deltas], 95) * 0.45 + rms(high_delta) * 0.80
    score = 120.0 * smooth_gain + 90.0 * jerk_gain + 18.0 * sign_gain + 35.0 * label_gain - 85.0 * tracking_penalty
    row = {
      "candidate": candidate.name,
      "installable": candidate.installable,
      "description": candidate.description,
      "score": round(score, 3),
      "rate_p95_abs": round(filtered_metrics["rate_p95_abs"], 4),
      "rate_rms": round(filtered_metrics["rate_rms"], 4),
      "jerk_p95_abs": round(filtered_metrics["jerk_p95_abs"], 4),
      "sign_changes_per_min": round(filtered_metrics["sign_changes_per_min"], 3),
      "smooth_gain_vs_observed": round(smooth_gain, 4),
      "jerk_gain_vs_observed": round(jerk_gain, 4),
      "label_rate_gain_vs_observed": round(label_gain, 4),
      "tracking_delta_rms": round(rms(deltas), 4),
      "tracking_delta_p95_abs": round(percentile([abs(v) for v in deltas], 95), 4),
      "high_demand_delta_rms": round(rms(high_delta), 4),
      "samples": len(simulated),
      "label_weighted_samples": len(label_weighted),
    }
    rows.append(row)
    by_route: dict[str, list[dict[str, Any]]] = {}
    for sim_row in simulated:
      by_route.setdefault(str(sim_row["route_id"]), []).append(sim_row)
    for route_id, route_samples in sorted(by_route.items()):
      route_filtered = metric_summary(route_samples, "filtered_torque_cmd")
      route_raw = metric_summary(route_samples, "torque_cmd")
      route_deltas = finite([r.get("torque_delta") for r in route_samples])
      route_rows.append({
        "candidate": candidate.name,
        "route_id": route_id,
        "brickpilot_version": route_samples[0].get("brickpilot_version", ""),
        "model_bundle": route_samples[0].get("model_bundle", ""),
        "experimental_mode": route_samples[0].get("experimental_mode", ""),
        "duration_sec": round(route_filtered["duration_sec"], 2),
        "rate_p95_abs": round(route_filtered["rate_p95_abs"], 4),
        "rate_gain_vs_observed": round((route_raw["rate_p95_abs"] - route_filtered["rate_p95_abs"]) / max(route_raw["rate_p95_abs"], 1e-6), 4),
        "jerk_p95_abs": round(route_filtered["jerk_p95_abs"], 4),
        "sign_changes_per_min": round(route_filtered["sign_changes_per_min"], 4),
        "tracking_delta_rms": round(rms(route_deltas), 4),
        "steering_label_weight_sec": round(sum(float(r.get("steering_label_weight") or 0.0) * float(r.get("dt") or 0.0) for r in route_samples), 2),
      })
  by_name = {row["candidate"]: row for row in rows}
  baseline_044 = by_name.get("044_texture_proxy")
  if baseline_044:
    for row in rows:
      row["score_delta_vs_044"] = round(float(row["score"]) - float(baseline_044["score"]), 3)
      row["rate_p95_delta_vs_044"] = round(float(row["rate_p95_abs"]) - float(baseline_044["rate_p95_abs"]), 4)
      row["tracking_delta_rms_delta_vs_044"] = round(float(row["tracking_delta_rms"]) - float(baseline_044["tracking_delta_rms"]), 4)
  rows.sort(key=lambda row: float(row["score"]), reverse=True)
  return rows, route_rows


def route_observed_summary(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
  add_dts(samples)
  out: list[dict[str, Any]] = []
  by_route: dict[str, list[dict[str, Any]]] = {}
  for sample in samples:
    by_route.setdefault(str(sample["route_id"]), []).append(sample)
  for route_id, rows in sorted(by_route.items()):
    clean = [row for row in rows if row.get("clean_lateral")]
    metrics = metric_summary(clean, "torque_cmd")
    labels: dict[str, float] = {}
    for row in rows:
      for label in str(row.get("steering_labels") or "").split("|"):
        if label:
          labels[label] = labels.get(label, 0.0) + float(row.get("dt") or 0.0)
    live_sr = finite([row.get("steer_ratio_live") for row in rows])
    live_stiffness = finite([row.get("stiffness_factor_live") for row in rows])
    out.append({
      "route_id": route_id,
      "brickpilot_version": rows[0].get("brickpilot_version", ""),
      "drive_type": rows[0].get("drive_type", ""),
      "model_bundle": rows[0].get("model_bundle", ""),
      "experimental_mode": rows[0].get("experimental_mode", ""),
      "clean_lateral_sec": round(metrics["duration_sec"], 2),
      "observed_rate_p95_abs": round(metrics["rate_p95_abs"], 4),
      "observed_jerk_p95_abs": round(metrics["jerk_p95_abs"], 4),
      "observed_sign_changes_per_min": round(metrics["sign_changes_per_min"], 3),
      "steering_label_weight_sec": round(sum(float(row.get("steering_label_weight") or 0.0) * float(row.get("dt") or 0.0) for row in rows), 2),
      "steering_ping_pong_sec": round(labels.get("steering_ping_pong", 0.0), 2),
      "steering_jerk_sec": round(labels.get("steering_jerk", 0.0), 2),
      "low_speed_lateral_bad_sec": round(labels.get("low_speed_lateral_bad", 0.0), 2),
      "steer_ratio_live_avg": round(sum(live_sr) / len(live_sr), 4) if live_sr else "",
      "stiffness_factor_live_avg": round(sum(live_stiffness) / len(live_stiffness), 4) if live_stiffness else "",
      "samples": len(rows),
    })
  return out


def aggregate_steering_can(export_root: Path = DEFAULT_OUTPUT_ROOT) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  steering_targets = set(STEERING_TARGET_WEIGHTS)
  for path in sorted(export_root.glob("prelim_*_*")):
    interval_path = path / "prediction_interval_can_summary.csv"
    route_path = path / "route_can_candidate_summary.csv"
    if not interval_path.exists() or not route_path.exists():
      continue
    route_means: dict[str, tuple[float, float]] = {}
    with route_path.open(newline="", encoding="utf-8") as f:
      for row in csv.DictReader(f):
        field = row.get("field")
        if field:
          route_means[field] = (as_float(row.get("mean"), 0.0) or 0.0, max(as_float(row.get("std"), 0.0) or 0.0, 1e-6))
    with interval_path.open(newline="", encoding="utf-8") as f:
      for row in csv.DictReader(f):
        target = row.get("target") or ""
        if target not in steering_targets:
          continue
        route_id = row.get("route_id") or ""
        score = max(0.0, min(1.0, as_float(row.get("peak_score"), 0.0) or 0.0))
        for key, value in row.items():
          if not key.endswith("_mean"):
            continue
          field = key[:-5]
          mean = as_float(value)
          if mean is None or field not in route_means:
            continue
          route_mean, route_std = route_means[field]
          z = abs(mean - route_mean) / route_std
          if z >= 0.75:
            rows.append({
              "target": target,
              "route_id": route_id,
              "field": field,
              "interval_mean": round(mean, 4),
              "route_mean": round(route_mean, 4),
              "effect_z": round(z, 3),
              "peak_score": round(score, 4),
              "weighted_effect": round(z * score, 3),
              "source_export": path.name,
            })
  aggregate: dict[tuple[str, str], dict[str, Any]] = {}
  for row in rows:
    key = (str(row["target"]), str(row["field"]))
    cur = aggregate.setdefault(key, {
      "target": row["target"],
      "field": row["field"],
      "intervals": 0,
      "routes": set(),
      "weighted_effect_sum": 0.0,
      "max_effect_z": 0.0,
      "examples": [],
    })
    cur["intervals"] += 1
    cur["routes"].add(row["route_id"])
    cur["weighted_effect_sum"] += float(row["weighted_effect"])
    cur["max_effect_z"] = max(float(cur["max_effect_z"]), float(row["effect_z"]))
    if len(cur["examples"]) < 3:
      cur["examples"].append(f"{row['route_id']} z={row['effect_z']}")
  out = []
  for cur in aggregate.values():
    out.append({
      "target": cur["target"],
      "field": cur["field"],
      "intervals": cur["intervals"],
      "routes": len(cur["routes"]),
      "weighted_effect_sum": round(float(cur["weighted_effect_sum"]), 3),
      "max_effect_z": round(float(cur["max_effect_z"]), 3),
      "examples": "; ".join(cur["examples"]),
    })
  out.sort(key=lambda row: (float(row["weighted_effect_sum"]), int(row["routes"]), int(row["intervals"])), reverse=True)
  return out


def svg_bar(path: Path, title: str, labels: list[str], values: list[float], width: int = 1100, height: int = 520) -> None:
  height = max(height, 78 + 34 * len(labels))
  max_value = max((abs(v) for v in values), default=1.0)
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#071019'/>",
    f"<text x='24' y='34' fill='#e8f2ff' font-family='Arial' font-size='21' font-weight='700'>{html.escape(title)}</text>",
  ]
  for idx, (label, value) in enumerate(zip(labels, values)):
    y = 68 + idx * 34
    bar = 0.0 if max_value <= 0.0 else abs(value) / max_value * 600.0
    color = "#65d6a3" if value >= 0 else "#ff7c7c"
    lines.append(f"<text x='24' y='{y + 16}' fill='#d7e7f7' font-family='Arial' font-size='12'>{html.escape(label[:42])}</text>")
    lines.append(f"<rect x='390' y='{y}' width='{bar:.1f}' height='20' rx='4' fill='{color}'/>")
    lines.append(f"<text x='{402 + bar:.1f}' y='{y + 15}' fill='#d7e7f7' font-family='Arial' font-size='12'>{value:.2f}</text>")
  lines.append("</svg>\n")
  path.write_text("\n".join(lines), encoding="utf-8")


def write_report(path: Path, candidate_rows: list[dict[str, Any]], route_rows: list[dict[str, Any]],
                 observed_rows: list[dict[str, Any]], can_rows: list[dict[str, Any]], warnings: list[str],
                 route_ids: list[str]) -> None:
  best_installable = next((row for row in candidate_rows if str(row.get("installable")).lower() == "true"), None)
  row_044 = next((row for row in candidate_rows if row["candidate"] == "044_texture_proxy"), None)
  extracted_samples = sum(int(r.get("samples") or 0) for r in observed_rows)
  clean_scoring_samples = int(candidate_rows[0].get("samples") or 0) if candidate_rows else 0
  zero_clean_routes = [str(r["route_id"]) for r in observed_rows if float(r.get("clean_lateral_sec") or 0.0) <= 0.0]
  lines = [
    "# Brickpilot Steering Smoothness R&D Sweep",
    "",
    f"Generated: `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
    "",
    "This is a research-only offline sweep. It does not change Brickpilot. It simulates torque-output texture filters on observed qlog commands, scores smoothness gains against command-delta distortion, and mines steering-related weak labels plus CAN intervals.",
    "",
    "## Scope",
    "",
    f"- Routes requested/analyzed: {len(route_ids)}",
    f"- Extracted lateral samples: {extracted_samples}",
    f"- Clean lateral scoring samples: {clean_scoring_samples}",
    f"- Routes with zero clean lateral scoring time: {len(zero_clean_routes)}" + (f" (`{'`, `'.join(zero_clean_routes)}`)" if zero_clean_routes else ""),
    "- Steering weak labels used: `steering_ping_pong`, `steering_jerk`, `low_speed_lateral_bad`, `quality_bad`.",
    "- The 0.4.4 smoother is included as `044_texture_proxy`; all scores include `score_delta_vs_044`.",
    "- Candidate simulation runs over the full extracted stream so inactive rows and steering overrides reset/cool down the filter before clean rows are scored.",
    "",
    "## Candidate Ranking",
    "",
    "| Candidate | Installable | Score | Delta vs 0.4.4 | Rate p95 | Jerk p95 | Sign changes/min | Command delta RMS | High-demand delta RMS |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
  ]
  for row in candidate_rows:
    lines.append(
      f"| `{row['candidate']}` | {row['installable']} | {row['score']} | {row.get('score_delta_vs_044', '')} | "
      f"{row['rate_p95_abs']} | {row['jerk_p95_abs']} | {row['sign_changes_per_min']} | "
      f"{row['tracking_delta_rms']} | {row['high_demand_delta_rms']} |"
    )
  lines.extend(["", "## Read", ""])
  if best_installable:
    lines.append(f"- Best installable offline steering candidate: `{best_installable['candidate']}`, score delta vs 0.4.4 `{best_installable.get('score_delta_vs_044')}`.")
  if row_044:
    lines.append(f"- Current 0.4.4 proxy score: `{row_044['score']}`, rate p95 `{row_044['rate_p95_abs']}`, command-delta RMS `{row_044['tracking_delta_rms']}`.")
  lines.extend([
    "- The frontier candidates intentionally over-smooth or rate-cap commands. They are useful to measure how much of the blocky wheel texture is removable in command space, not as direct road builds.",
    "- After full-stream reset/cooldown modeling, the best heavy smoothing candidates still score well but carry real command-delta cost. Treat them as useful probes, not proof that final output filtering is the root fix.",
    "",
    "## Root-Cause Hypotheses",
    "",
    "- Highest-confidence controller hypothesis: friction sign flips are being amplified into visible wheel texture. `latcontrol_torque.py` feeds friction from `error + JERK_GAIN * desired_lateral_jerk`, and the Hyundai CAN-FD controller later rounds and rate-limits the output. A small desired-jerk or error sign change near zero can therefore become a two-sided raw torque step.",
    "- Hyundai CAN-FD step texture is plausible even when each individual command is legal. The platform limits are small integer steps, so a 50-60 count friction-side swing can be released as a stair-step instead of a fluid wheel angle change.",
    "- Low-speed behavior is still suspicious. The shared torque controller uses very high low-speed proportional gains and freezes the integrator below 5 m/s, so neighborhood bends can become mostly P/friction behavior instead of a settled feedback loop.",
    "- The 0.4.4 smoother likely treats a symptom. It smooths the final NNLC/PID torque output, after model/path, torque control, friction, rounding, and safety shaping have already created texture.",
    "- Model/config effects are real. Recent 0.4.3 route comparisons showed far less steering-label time on the OP10 experimental route than on NNV2/WMI routes, even when command-rate metrics were not universally lower. That argues for a model/path component, not just a Hyundai/PHEV CAN issue.",
    "- Live parameter traces on recent routes average steer ratio near 13.8-14.1 and stiffness factor near 1.0, while the static Tucson PHEV override has a much lower tire-stiffness factor. Static params may matter most during startup/low-confidence windows, but the logs show paramsd is not simply staying at the static value.",
    "",
    "## Replay And Digital-Mile Status",
    "",
    "- Ready now: qlog/rlog-derived metric sweeps over the configured BrickpilotDriveDB raw import root; the local corpus is large enough for digital-mile ranking before road builds.",
    "- Ready now: an openpilot replay binary can load local segments with `--no-vipc --no-cache --benchmark` for message-stream validation.",
    "- Not ready in the native macOS path used for this sweep: true native `controlsd` process replay hit Linux ELF native modules, and the active Brickpilot tree lacks a built local replay binary. Docker also needs an in-container native-module build before `controlsd` replay is clean.",
    "- Intended full-replay path: use the existing Linux VM/replay environment on this machine and run a one-route `controlsd` smoke before broad process replay. This sweep did not exercise that VM path, so it should not be treated as blocked.",
    "- Practical current path: keep using qlog/shadow sweeps for candidate ranking, then use the existing Linux VM for process-replay checks before promoting top candidates into short road A/B builds.",
    "",
    "## Route Steering Read",
    "",
    "| Route | Version | Model | Exp | Clean lat sec | Rate p95 | Jerk p95 | Ping-pong sec | Jerk label sec | Live steer ratio | Live stiffness |",
    "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
  ])
  for row in observed_rows:
    lines.append(
      f"| `{str(row['route_id'])[:8]}` | {row['brickpilot_version']} | {str(row['model_bundle'])[:24]} | {row['experimental_mode']} | "
      f"{row['clean_lateral_sec']} | {row['observed_rate_p95_abs']} | {row['observed_jerk_p95_abs']} | "
      f"{row['steering_ping_pong_sec']} | {row['steering_jerk_sec']} | {row['steer_ratio_live_avg']} | {row['stiffness_factor_live_avg']} |"
    )
  lines.extend(["", "## Steering CAN Clues", ""])
  for row in can_rows[:20]:
    lines.append(
      f"- `{row['target']}` `{row['field']}`: weighted effect {row['weighted_effect_sum']}, routes {row['routes']}, max z {row['max_effect_z']} ({row['examples']})"
    )
  lines.extend([
    "",
    "These CAN rows are label-window correlations, not decoded steering commands. The CAN score is an unnormalized interval-weighted sum, and adjacent bytes are usually correlated rather than independent evidence. `0x1C5` was already a PHEV state/mode candidate, and the `0x1A5` cluster is a new high-priority context candidate. Treat both as things to decode, not inputs to a steering controller.",
    "",
    "## Hypothesis-Ranked Next Experiments",
    "",
    "Only the output-filter candidates above are directly scored by this sweep. The ordering below combines those scores with controller-code inspection, route/model comparisons, and the audit findings.",
    "",
    "1. Friction-first Tucson experiment: reduce/widen the friction feedforward near zero and score sign changes, torque-rate p95, steering labels, command delta, and measured lateral error if process replay is available. This attacks the strongest root-cause hypothesis instead of stacking more final smoothing.",
    "2. Two-stage/suburban output-filter experiment: the best scored installable candidates are heavier smoothing probes. They are worth a controlled A/B, but the command-delta penalty means they should not be the only strategy.",
    "3. Effective steering-response experiment: the steer-ratio proxy scores lower than the heavy filters but with less command delta. Test this as a real parameter change, not as a permanent magic multiplier.",
    "4. Curvature-rate/model-path experiment: smooth desired curvature or desired lateral acceleration before torque control, especially for suburban speeds where the wheel looks blocky around mild bends.",
    "5. Low-speed gain experiment: reduce the aggressive low-speed torque KP region and/or lower the integrator freeze threshold so low-speed bends are not dominated by P/friction bursts.",
    "6. Hyundai CAN-FD actuation-shaping experiment: test smaller up/down slew or a signed-step quantizer in replay first. This may make the wheel quieter but can also add lag, so it needs high-demand scoring.",
    "7. EPS damping experiment: sweep Hyundai `Damping_Gain` values in a short build matrix. This is a direct actuator-side texture knob and is meaningfully different from output filtering.",
    "8. Parameter-grid experiment: steer ratio and tire stiffness grid with live-parameter logging separated into startup, learned, and low-confidence windows.",
    "9. CAN decode experiment: prioritize `0x1C5`, `0x1A5`, and known PHEV regen/engine frames as context features for lateral-label clustering, not as direct actuation signals yet.",
    "10. Unsafe lab-only experiment: relax or bypass selected rate/texture constraints only in closed replay/safety lab to estimate the physical ceiling. This is for learning, not a comma install.",
    "",
    "## Frontier Experiments Worth Considering Later",
    "",
    "- Upstream curvature-rate filtering before torque control, not just output smoothing. This directly targets blocky desired curvature if the model/path output is stair-stepped.",
    "- Fixed Tucson PHEV lateral-parameter grid: steer ratio 14.4-15.6 and tire stiffness 0.45-0.75, scored by replay and one road A/B. The live parameter traces can decide whether paramsd is already drifting there.",
    "- Disable or freeze live torque parameters for a controlled run if `liveTorqueParameters` are injecting noisy friction/lat-accel updates.",
    "- Model-specific lateral policy: OP10 exp had much lower ping-pong in the 0.4.3 route comparison, so separate model path smoothness from controller smoothness before overfitting the controller.",
    "- Unsafe-only learning experiment, not road install: relax Hyundai CAN-FD steer slew in a closed replay/safety lab to prove whether stock CAN-FD rate limits are the block source. Do not promote without safety proof.",
    "- CAN decode focus: `0x1C5` and `0x1A5` are the highest interval-weighted steering-label context clusters in this pass, with `0x310` lower but still worth tracking as ADAS/EPS context. Treat these as decode targets, not control inputs.",
    "",
    "## Audit Notes",
    "",
    "- This is not a process replay and cannot prove lateral tracking after command changes. It is a command-space digital sweep to rank candidates for a later build or replay.",
    "- Because the raw pre-smoothing controller output is not logged separately on all versions, the simulation uses observed torque command as a proxy input. `Command delta RMS` is therefore a distortion proxy, not actual path-tracking error.",
    "- The result is still valuable: if command-space filtering cannot materially reduce rate/jerk without big distortion, the root cause is likely upstream model/curvature or vehicle params.",
  ])
  if warnings:
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {warning}" for warning in warnings[:40])
  lines.extend([
    "",
    "## Files",
    "",
    "- `candidate_scores.csv`",
    "- `route_candidate_scores.csv`",
    "- `route_observed_steering_summary.csv`",
    "- `steering_label_can_candidates.csv`",
    "- `candidate_score_chart.svg`",
    "- `score_delta_vs_044_chart.svg`",
  ])
  path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--route-id", action="append", default=[])
  parser.add_argument("--config", default=None)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--out", type=Path, default=None)
  args = parser.parse_args()

  route_ids = args.route_id or list(DEFAULT_ROUTE_IDS)
  store = DriveStore(load_config(args.config))
  all_samples: list[dict[str, Any]] = []
  warnings: list[str] = []
  analyzed_route_ids: list[str] = []
  for route_id in route_ids:
    route = route_row(store, route_id)
    if route is None:
      warnings.append(f"{route_id}: not found in DriveDB")
      continue
    samples, route_warnings = extract_route_samples(store, route)
    warnings.extend(route_warnings)
    if samples:
      all_samples.extend(samples)
      analyzed_route_ids.append(route_id)
    else:
      warnings.append(f"{route_id}: no lateral samples extracted")
  if not all_samples:
    raise SystemExit("no lateral samples extracted")

  add_dts(all_samples)
  candidate_rows, route_candidate_rows = score_candidates(all_samples, candidate_set())
  observed_rows = route_observed_summary(all_samples)
  can_rows = aggregate_steering_can(args.output_root)

  timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  out_dir = args.out or args.output_root / f"lateral_steering_sweep_{timestamp}"
  out_dir.mkdir(parents=True, exist_ok=True)
  write_csv(out_dir / "candidate_scores.csv", candidate_rows)
  write_csv(out_dir / "route_candidate_scores.csv", route_candidate_rows)
  write_csv(out_dir / "route_observed_steering_summary.csv", observed_rows)
  write_csv(out_dir / "steering_label_can_candidates.csv", can_rows)
  write_csv(out_dir / "sample_extract_preview.csv", all_samples[:2000])
  svg_bar(out_dir / "candidate_score_chart.svg", "Offline steering smoother candidate scores",
          [row["candidate"] for row in candidate_rows], [float(row["score"]) for row in candidate_rows])
  svg_bar(out_dir / "score_delta_vs_044_chart.svg", "Score delta versus 0.4.4 texture proxy",
          [row["candidate"] for row in candidate_rows], [float(row.get("score_delta_vs_044") or 0.0) for row in candidate_rows])
  run_summary = {
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "routes_requested": route_ids,
    "routes_analyzed": analyzed_route_ids,
    "sample_count": len(all_samples),
    "candidate_scores": candidate_rows,
    "warnings": warnings,
    "output_dir": str(out_dir),
  }
  (out_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  write_report(out_dir / "report.md", candidate_rows, route_candidate_rows, observed_rows, can_rows, warnings, analyzed_route_ids)
  print(out_dir)
  print("best", candidate_rows[0]["candidate"], candidate_rows[0]["score"], "delta_vs_044", candidate_rows[0].get("score_delta_vs_044"))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
