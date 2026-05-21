#!/usr/bin/env python3
from __future__ import annotations

import math
import re
from collections import defaultdict
from statistics import median
from typing import Any, Callable

import numpy as np


MPH_TO_MS = 0.44704
MS_TO_MPH = 1.0 / MPH_TO_MS

SPEED_REGIMES = (
  ("stopped", None, 1.0),
  ("low_neighborhood", 1.0, 20.0),
  ("neighborhood", 20.0, 35.0),
  ("backroad", 35.0, 55.0),
  ("highway", 55.0, None),
)

TURN_REGIMES = (
  ("straightish", None, 0.25),
  ("mild_curve", 0.25, 0.75),
  ("medium_curve", 0.75, 1.25),
  ("hard_curve", 1.25, None),
)

DEFAULT_THRESHOLDS = {
  "pinned_output_abs": 0.98,
  "pinned_burst_gap_sec": 0.20,
  "lead_relaxed_drel_m": 50.0,
  "lazy_deficit_mph": 5.0,
  "lazy_accel_cmd_mps2": 0.5,
  "lazy_min_duration_sec": 3.0,
  "accel_event_start_deficit_mph": 5.0,
  "accel_event_end_deficit_mph": 2.0,
  "accel_event_min_speed_mph": 3.0,
  "accel_event_min_duration_sec": 1.0,
  "stop_go_start_mph": 0.5,
  "stop_go_end_mph": 5.0,
  "max_metric_gap_sec": 1.0,
  "max_rate_gap_sec": 0.5,
}

RELEVANT_TIME_KINDS = {
  "carState",
  "carControl",
  "controlsState",
  "selfdriveState",
  "liveParameters",
  "liveTorqueParameters",
  "longitudinalPlan",
  "radarState",
}

METRIC_COLUMNS = [
  ("duration_sec", "REAL"),
  ("distance_m", "REAL"),
  ("lat_active_pct", "REAL"),
  ("selfdrive_active_pct", "REAL"),
  ("steering_pressed_pct", "REAL"),
  ("gas_pressed_pct", "REAL"),
  ("brake_pressed_pct", "REAL"),
  ("clean_lateral_sample_count", "INTEGER"),
  ("strict_clean_lateral_sample_count", "INTEGER"),
  ("clean_lateral_time_sec", "REAL"),
  ("lateral_error_rms", "REAL"),
  ("lateral_error_mean_abs", "REAL"),
  ("lateral_error_p95_abs", "REAL"),
  ("lateral_error_p99_abs", "REAL"),
  ("lateral_error_max_abs", "REAL"),
  ("desired_lateral_accel_max_abs", "REAL"),
  ("actual_lateral_accel_max_abs", "REAL"),
  ("torque_output_max_abs", "REAL"),
  ("carcontrol_torque_max_abs", "REAL"),
  ("pinned_output_time_sec", "REAL"),
  ("pinned_output_count", "INTEGER"),
  ("pinned_output_pct_clean", "REAL"),
  ("pinned_burst_count", "INTEGER"),
  ("longest_pinned_burst_sec", "REAL"),
  ("saturation_pct", "REAL"),
  ("steering_angle_max_abs", "REAL"),
  ("steering_torque_max_abs", "REAL"),
  ("steering_torque_eps_max_abs", "REAL"),
  ("output_rate_rms", "REAL"),
  ("output_rate_p95_abs", "REAL"),
  ("output_sign_changes_per_min", "REAL"),
  ("lateral_error_sign_changes_per_min", "REAL"),
  ("angle_offset_deg_avg", "REAL"),
  ("angle_offset_deg_median", "REAL"),
  ("angle_offset_deg_min", "REAL"),
  ("angle_offset_deg_max", "REAL"),
  ("angle_offset_average_deg_avg", "REAL"),
  ("steer_ratio_avg", "REAL"),
  ("steer_ratio_median", "REAL"),
  ("steer_ratio_min", "REAL"),
  ("steer_ratio_max", "REAL"),
  ("stiffness_factor_avg", "REAL"),
  ("angle_offset_drift_deg", "REAL"),
  ("live_torque_cal_perc_max", "REAL"),
  ("live_torque_live_valid_pct", "REAL"),
  ("live_torque_use_params_pct", "REAL"),
  ("live_torque_lat_accel_factor_filtered_avg", "REAL"),
  ("live_torque_friction_coefficient_filtered_avg", "REAL"),
  ("long_active_pct", "REAL"),
  ("clean_longitudinal_sample_count", "INTEGER"),
  ("clean_longitudinal_time_sec", "REAL"),
  ("accel_cmd_avg", "REAL"),
  ("accel_cmd_p95", "REAL"),
  ("accel_cmd_max", "REAL"),
  ("actual_accel_avg", "REAL"),
  ("actual_accel_p95", "REAL"),
  ("no_lead_avg_speed_deficit_mps", "REAL"),
  ("no_lead_p95_speed_deficit_mps", "REAL"),
  ("no_lead_avg_speed_deficit_mph", "REAL"),
  ("no_lead_p95_speed_deficit_mph", "REAL"),
  ("deficit_gt_3mph_time_sec", "REAL"),
  ("deficit_gt_5mph_time_sec", "REAL"),
  ("deficit_gt_8mph_time_sec", "REAL"),
  ("lazy_accel_time_sec", "REAL"),
  ("lead_limited_time_sec", "REAL"),
  ("no_lead_strict_time_sec", "REAL"),
  ("no_lead_relaxed_time_sec", "REAL"),
  ("set_speed_samples", "INTEGER"),
  ("set_speed_uncertain_samples", "INTEGER"),
]

METRIC_NAMES = [name for name, _ in METRIC_COLUMNS]


def merged_thresholds(overrides: dict[str, Any] | None = None) -> dict[str, float]:
  ret = DEFAULT_THRESHOLDS.copy()
  if overrides:
    ret.update({k: v for k, v in overrides.items() if v is not None})
  return ret


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


def safe_which(obj: Any, default: str | None = None) -> str | None:
  if obj is None:
    return default
  try:
    return obj.which()
  except Exception:
    return default


def as_float(value: Any, default: float | None = None) -> float | None:
  if value is None:
    return default
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


def enum_name(value: Any) -> str | None:
  if value is None:
    return None
  try:
    return str(value)
  except Exception:
    return None


def finite_values(values: list[Any]) -> list[float]:
  ret = []
  for value in values:
    value = as_float(value)
    if value is not None:
      ret.append(value)
  return ret


def pct(numerator: float, denominator: float) -> float | None:
  if denominator <= 0:
    return None
  return 100.0 * numerator / denominator


def avg(values: list[Any]) -> float | None:
  vals = finite_values(values)
  return float(np.mean(vals)) if vals else None


def percentile(values: list[Any], q: float) -> float | None:
  vals = finite_values(values)
  return float(np.percentile(vals, q)) if vals else None


def max_abs(values: list[Any]) -> float | None:
  vals = finite_values(values)
  return max((abs(v) for v in vals), default=None)


def redact_vin(vin: Any) -> str | None:
  if vin is None:
    return None
  vin = str(vin).strip()
  if not vin:
    return None
  return vin[:3] + ("*" * 12)


def summarize_car_fw(car_fw: Any) -> dict[str, Any]:
  ecu_counts: dict[str, int] = defaultdict(int)
  buses: set[int] = set()
  brands: set[str] = set()
  markers: set[str] = set()
  count = 0

  for fw in list(car_fw or []):
    count += 1
    ecu_counts[enum_name(safe(fw, "ecu")) or "unknown"] += 1
    bus = safe(fw, "bus")
    if bus is not None:
      try:
        buses.add(int(bus))
      except (TypeError, ValueError):
        pass
    brand = safe(fw, "brand")
    if brand:
      brands.add(str(brand))

    fw_version = safe(fw, "fwVersion")
    if fw_version is not None:
      try:
        text = bytes(fw_version).decode("latin-1", "ignore")
      except Exception:
        text = str(fw_version)
      for marker in re.findall(r"\b(?:NX4[A-Z0-9]*|TUCSON|HYUNDAI)\b", text.upper()):
        markers.add(marker)

  return {
    "entry_count": count,
    "ecu_counts": dict(sorted(ecu_counts.items())),
    "buses": sorted(buses),
    "brands": sorted(brands),
    "firmware_markers": sorted(markers),
  }


def extract_car_params(cp: Any) -> dict[str, Any]:
  if cp is None:
    return {}

  lat_tuning = safe(cp, "lateralTuning")
  lat_type = safe_which(lat_tuning)
  lat_tune = safe(lat_tuning, lat_type) if lat_type else None

  safety_configs = []
  for cfg in list(safe(cp, "safetyConfigs", []) or []):
    safety_configs.append({
      "safetyModel": enum_name(safe(cfg, "safetyModel")),
      "safetyParam": safe(cfg, "safetyParam"),
    })

  return {
    "brand": safe(cp, "brand"),
    "carFingerprint": safe(cp, "carFingerprint"),
    "fingerprintSource": enum_name(safe(cp, "fingerprintSource")),
    "mass": as_float(safe(cp, "mass")),
    "wheelbase": as_float(safe(cp, "wheelbase")),
    "steerRatio": as_float(safe(cp, "steerRatio")),
    "steerActuatorDelay": as_float(safe(cp, "steerActuatorDelay")),
    "steerLimitTimer": as_float(safe(cp, "steerLimitTimer")),
    "minSteerSpeed": as_float(safe(cp, "minSteerSpeed")),
    "safetyConfigs": safety_configs,
    "lateralTuningType": lat_type,
    "torque": {
      "latAccelFactor": as_float(safe(lat_tune, "latAccelFactor")),
      "friction": as_float(safe(lat_tune, "friction")),
      "latAccelOffset": as_float(safe(lat_tune, "latAccelOffset")),
    } if lat_type == "torque" else {},
    "openpilotLongitudinalControl": safe(cp, "openpilotLongitudinalControl"),
    "enableBsm": safe(cp, "enableBsm"),
    "networkLocation": enum_name(safe(cp, "networkLocation")),
    "flags": safe(cp, "flags"),
    "alternativeExperience": safe(cp, "alternativeExperience"),
    "redactedCarVin": redact_vin(safe(cp, "carVin")),
    "carFwSummary": summarize_car_fw(safe(cp, "carFw", [])),
  }


def speed_regime(v_ego_mps: Any) -> str:
  mph = (as_float(v_ego_mps, 0.0) or 0.0) * MS_TO_MPH
  for name, low, high in SPEED_REGIMES:
    if (low is None or mph >= low) and (high is None or mph < high):
      return name
  return "unknown"


def turn_regime(desired_lateral_accel: Any) -> str:
  val = abs(as_float(desired_lateral_accel, 0.0) or 0.0)
  for name, low, high in TURN_REGIMES:
    if (low is None or val >= low) and (high is None or val < high):
      return name
  return "unknown"


def sample_matches_speed_regime(name: str) -> Callable[[dict[str, Any]], bool]:
  return lambda s: speed_regime(s.get("v_ego_mps")) == name


def sample_matches_turn_regime(name: str) -> Callable[[dict[str, Any]], bool]:
  return lambda s: turn_regime(s.get("desired_lateral_accel")) == name


def pick_set_speed(car_control: Any, car_state: Any) -> tuple[float | None, str | None, bool]:
  candidates = [
    (safe(car_control, "hudControl.setSpeed"), "carControl.hudControl.setSpeed", False),
    (safe(car_state, "cruiseState.speed"), "carState.cruiseState.speed", False),
    (safe(car_state, "vCruise"), "carState.vCruise", True),
    (safe(car_state, "vCruiseCluster"), "carState.vCruiseCluster", True),
  ]
  for value, source, uncertain in candidates:
    value = as_float(value)
    if value is None or value <= 0:
      continue
    if value < 70.0:
      return value, source, uncertain
    if uncertain and value < 180.0:
      return value / 3.6, source + "_kph_guess", True
  return None, None, False


def no_lead_confidence(sample: dict[str, Any], thresholds: dict[str, float]) -> float:
  if sample.get("no_lead_strict"):
    return 1.0
  if sample.get("no_lead_relaxed"):
    return 0.75
  return 0.0


def extract_lateral_state(controls_state: Any) -> tuple[str | None, Any]:
  lcs = safe(controls_state, "lateralControlState")
  lateral_type = safe_which(lcs)
  return lateral_type, safe(lcs, lateral_type) if lateral_type else None


def make_common_sample(segment_index: int, segment_time: float, route_time: float,
                       car_state: Any, car_control: Any, controls_state: Any,
                       selfdrive_state: Any, radar_state: Any,
                       longitudinal_plan: Any, thresholds: dict[str, float]) -> dict[str, Any]:
  lateral_type, lateral_state = extract_lateral_state(controls_state)
  v_ego = as_float(safe(car_state, "vEgo"))
  a_ego = as_float(safe(car_state, "aEgo"))
  steering_pressed = as_bool(safe(car_state, "steeringPressed"))
  gas_pressed = as_bool(safe(car_state, "gasPressed"))
  brake_pressed = as_bool(safe(car_state, "brakePressed"))
  lat_active = as_bool(safe(car_control, "latActive")) or as_bool(safe(lateral_state, "active"))
  long_active = as_bool(safe(car_control, "longActive"))
  selfdrive_active = as_bool(safe(selfdrive_state, "active")) or as_bool(safe(car_control, "enabled"))
  enabled = as_bool(safe(selfdrive_state, "enabled")) or as_bool(safe(car_control, "enabled")) or selfdrive_active
  torque_active = as_bool(safe(lateral_state, "active")) if lateral_type == "torqueState" else False
  output = as_float(safe(lateral_state, "output"))
  desired_lat = as_float(safe(lateral_state, "desiredLateralAccel"))
  actual_lat = as_float(safe(lateral_state, "actualLateralAccel"))
  lat_error = as_float(safe(lateral_state, "error"))
  saturated = as_bool(safe(lateral_state, "saturated"))
  set_speed, set_speed_source, set_speed_uncertain = pick_set_speed(car_control, car_state)

  lead_one = safe(radar_state, "leadOne")
  lead_status = as_bool(safe(lead_one, "status"))
  d_rel = as_float(safe(lead_one, "dRel"))
  lead_limit_m = thresholds["lead_relaxed_drel_m"]
  no_lead_strict = not lead_status
  no_lead_relaxed = (not lead_status) or (d_rel is not None and d_rel > lead_limit_m)
  lead_limited = lead_status and d_rel is not None and d_rel <= lead_limit_m
  speed_deficit = set_speed - v_ego if set_speed is not None and v_ego is not None else None

  clean_lateral = lat_active and torque_active and not steering_pressed
  strict_clean_lateral = clean_lateral and not gas_pressed and not brake_pressed
  clean_longitudinal = long_active and not gas_pressed and not brake_pressed
  lazy_candidate = (
    clean_longitudinal and no_lead_relaxed and speed_deficit is not None and
    speed_deficit * MS_TO_MPH > thresholds["lazy_deficit_mph"] and
    (as_float(safe(car_control, "actuators.accel"), 0.0) or 0.0) < thresholds["lazy_accel_cmd_mps2"]
  )

  return {
    "segment_index": segment_index,
    "segment_time_sec": segment_time,
    "route_time_sec": route_time,
    "v_ego_mps": v_ego,
    "speed_mph": v_ego * MS_TO_MPH if v_ego is not None else None,
    "a_ego_mps2": a_ego,
    "standstill": as_bool(safe(car_state, "standstill")),
    "steering_pressed": steering_pressed,
    "gas_pressed": gas_pressed,
    "brake_pressed": brake_pressed,
    "lat_active": lat_active,
    "long_active": long_active,
    "selfdrive_active": selfdrive_active,
    "enabled": enabled,
    "lateral_control_type": lateral_type,
    "torque_state_active": torque_active,
    "lateral_error": lat_error,
    "lateral_error_abs": abs(lat_error) if lat_error is not None else None,
    "actual_lateral_accel": actual_lat,
    "desired_lateral_accel": desired_lat,
    "torque_output": output,
    "torque_output_abs": abs(output) if output is not None else None,
    "lateral_saturated": saturated,
    "carcontrol_torque": as_float(safe(car_control, "actuators.torque")),
    "steering_angle_deg": as_float(safe(car_state, "steeringAngleDeg")),
    "steering_torque": as_float(safe(car_state, "steeringTorque")),
    "steering_torque_eps": as_float(safe(car_state, "steeringTorqueEps")),
    "clean_lateral": clean_lateral,
    "strict_clean_lateral": strict_clean_lateral,
    "pinned": clean_lateral and output is not None and abs(output) >= thresholds["pinned_output_abs"],
    "accel_cmd": as_float(safe(car_control, "actuators.accel")),
    "carcontrol_long_control_state": enum_name(safe(car_control, "actuators.longControlState")),
    "controls_long_control_state": enum_name(safe(controls_state, "longControlState")),
    "set_speed_mps": set_speed,
    "set_speed_source": set_speed_source,
    "set_speed_units_uncertain": set_speed_uncertain,
    "speed_deficit_mps": speed_deficit,
    "speed_deficit_mph": speed_deficit * MS_TO_MPH if speed_deficit is not None else None,
    "lead_status": lead_status,
    "lead_d_rel_m": d_rel,
    "lead_v_rel_mps": as_float(safe(lead_one, "vRel")),
    "no_lead_strict": no_lead_strict,
    "no_lead_relaxed": no_lead_relaxed,
    "lead_limited": lead_limited,
    "clean_longitudinal": clean_longitudinal,
    "lazy_candidate": lazy_candidate,
    "longitudinal_plan_a_target": as_float(safe(longitudinal_plan, "aTarget")),
    "longitudinal_plan_source": enum_name(safe(longitudinal_plan, "longitudinalPlanSource")),
    "speed_regime": speed_regime(v_ego),
    "turn_regime": turn_regime(desired_lat),
  }


def make_live_parameters_sample(segment_index: int, segment_time: float, route_time: float, lp: Any) -> dict[str, Any]:
  return {
    "segment_index": segment_index,
    "segment_time_sec": segment_time,
    "route_time_sec": route_time,
    "angle_offset_deg": as_float(safe(lp, "angleOffsetDeg")),
    "angle_offset_average_deg": as_float(safe(lp, "angleOffsetAverageDeg")),
    "steer_ratio": as_float(safe(lp, "steerRatio")),
    "stiffness_factor": as_float(safe(lp, "stiffnessFactor")),
  }


def make_live_torque_sample(segment_index: int, segment_time: float, route_time: float, ltp: Any) -> dict[str, Any]:
  return {
    "segment_index": segment_index,
    "segment_time_sec": segment_time,
    "route_time_sec": route_time,
    "live_valid": as_bool(safe(ltp, "liveValid")),
    "lat_accel_factor_filtered": as_float(safe(ltp, "latAccelFactorFiltered")),
    "friction_coefficient_filtered": as_float(safe(ltp, "frictionCoefficientFiltered")),
    "use_params": as_bool(safe(ltp, "useParams")),
    "cal_perc": as_float(safe(ltp, "calPerc")),
  }


def process_segment_messages(messages: Any, segment_index: int, log_type: str,
                             thresholds: dict[str, float]) -> dict[str, Any]:
  car_state = car_control = controls_state = selfdrive_state = None
  live_parameters = live_torque_parameters = longitudinal_plan = radar_state = None
  car_params_summary: dict[str, Any] | None = None
  warnings: list[str] = []
  warned: set[str] = set()
  msg_count = 0
  first_t: float | None = None
  last_t: float | None = None

  base_samples: list[dict[str, Any]] = []
  lat_samples: list[dict[str, Any]] = []
  long_samples: list[dict[str, Any]] = []
  live_parameter_samples: list[dict[str, Any]] = []
  live_torque_samples: list[dict[str, Any]] = []

  def warn_once(key: str, message: str) -> None:
    if key not in warned:
      warnings.append(message)
      warned.add(key)

  for msg in messages:
    try:
      kind = msg.which()
    except Exception as exc:
      warn_once("bad_union", f"segment {segment_index}: skipped unreadable union message: {exc!r}")
      continue

    msg_count += 1
    mono = as_float(safe(msg, "logMonoTime"))
    if mono is None:
      continue
    t = mono / 1e9
    if kind == "carParams" and car_params_summary is None:
      car_params_summary = extract_car_params(safe(msg, "carParams"))
      continue

    if kind not in RELEVANT_TIME_KINDS:
      continue

    if first_t is None:
      first_t = t
    last_t = t
    segment_time = t - first_t
    route_time = segment_index * 60.0 + segment_time

    if kind == "carState":
      car_state = safe(msg, "carState")
      if car_state is not None:
        base_samples.append(make_common_sample(segment_index, segment_time, route_time, car_state, car_control,
                                               controls_state, selfdrive_state, radar_state, longitudinal_plan, thresholds))
    elif kind == "carControl":
      car_control = safe(msg, "carControl")
      if car_state is not None:
        long_samples.append(make_common_sample(segment_index, segment_time, route_time, car_state, car_control,
                                               controls_state, selfdrive_state, radar_state, longitudinal_plan, thresholds))
    elif kind == "controlsState":
      controls_state = safe(msg, "controlsState")
      lateral_type, _ = extract_lateral_state(controls_state)
      if lateral_type != "torqueState":
        warn_once("non_torque_lateral", f"segment {segment_index}: controlsState lateralControlState is {lateral_type!r}, torque metrics may be missing")
      if car_state is not None:
        lat_samples.append(make_common_sample(segment_index, segment_time, route_time, car_state, car_control,
                                              controls_state, selfdrive_state, radar_state, longitudinal_plan, thresholds))
    elif kind == "selfdriveState":
      selfdrive_state = safe(msg, "selfdriveState")
    elif kind == "liveParameters":
      live_parameters = safe(msg, "liveParameters")
      live_parameter_samples.append(make_live_parameters_sample(segment_index, segment_time, route_time, live_parameters))
    elif kind == "liveTorqueParameters":
      live_torque_parameters = safe(msg, "liveTorqueParameters")
      live_torque_samples.append(make_live_torque_sample(segment_index, segment_time, route_time, live_torque_parameters))
    elif kind == "longitudinalPlan":
      longitudinal_plan = safe(msg, "longitudinalPlan")
    elif kind == "radarState":
      radar_state = safe(msg, "radarState")

  if not lat_samples:
    warn_once("no_lateral_samples", f"segment {segment_index}: no controlsState lateral samples found")
  if not long_samples:
    warn_once("no_longitudinal_samples", f"segment {segment_index}: no carControl longitudinal samples found")
  if not live_parameter_samples:
    warn_once("no_live_parameters", f"segment {segment_index}: no liveParameters messages found")
  if not live_torque_samples:
    warn_once("no_live_torque", f"segment {segment_index}: no liveTorqueParameters messages found")

  return {
    "segment_index": segment_index,
    "log_type": log_type,
    "message_count": msg_count,
    "duration_sec": max(0.0, (last_t - first_t)) if first_t is not None and last_t is not None else 0.0,
    "base_samples": base_samples,
    "lat_samples": lat_samples,
    "long_samples": long_samples,
    "live_parameter_samples": live_parameter_samples,
    "live_torque_samples": live_torque_samples,
    "car_params_summary": car_params_summary,
    "warnings": warnings,
  }


def with_dt(samples: list[dict[str, Any]], thresholds: dict[str, float]) -> list[tuple[dict[str, Any], float]]:
  if not samples:
    return []
  ordered = sorted(samples, key=lambda s: s.get("route_time_sec") or 0.0)
  ret = []
  max_gap = thresholds["max_metric_gap_sec"]
  for idx, sample in enumerate(ordered):
    if idx + 1 >= len(ordered):
      dt = 0.0
    else:
      t0 = as_float(sample.get("route_time_sec"), 0.0) or 0.0
      t1 = as_float(ordered[idx + 1].get("route_time_sec"), t0) or t0
      dt = t1 - t0
      if dt <= 0.0 or dt > max_gap:
        dt = 0.0
    ret.append((sample, dt))
  return ret


def apply_scope_and_filter(weighted: list[tuple[dict[str, Any], float]], scope: str, warmup_skip_sec: float,
                           sample_filter: Callable[[dict[str, Any]], bool] | None = None) -> list[tuple[dict[str, Any], float]]:
  ret = []
  for sample, dt in weighted:
    if scope == "post_warmup" and (as_float(sample.get("route_time_sec"), 0.0) or 0.0) < warmup_skip_sec:
      continue
    if sample_filter is not None and not sample_filter(sample):
      continue
    ret.append((sample, dt))
  return ret


def bool_time(weighted: list[tuple[dict[str, Any], float]], key: str) -> float:
  return sum(dt for sample, dt in weighted if sample.get(key))


def weighted_values(weighted: list[tuple[dict[str, Any], float]], key: str,
                    predicate: Callable[[dict[str, Any]], bool] | None = None) -> list[float]:
  vals = []
  for sample, _ in weighted:
    if predicate is not None and not predicate(sample):
      continue
    value = as_float(sample.get(key))
    if value is not None:
      vals.append(value)
  return vals


def weighted_duration(weighted: list[tuple[dict[str, Any], float]],
                      predicate: Callable[[dict[str, Any]], bool] | None = None) -> float:
  return sum(dt for sample, dt in weighted if predicate is None or predicate(sample))


def group_true_durations(weighted: list[tuple[dict[str, Any], float]], predicate: Callable[[dict[str, Any]], bool],
                         max_gap_sec: float) -> list[float]:
  durations = []
  cur = 0.0
  prev_t: float | None = None
  for sample, dt in weighted:
    t = as_float(sample.get("route_time_sec"), 0.0) or 0.0
    if predicate(sample):
      if prev_t is not None and t - prev_t > max_gap_sec and cur > 0.0:
        durations.append(cur)
        cur = 0.0
      cur += dt
      prev_t = t
    elif cur > 0.0:
      durations.append(cur)
      cur = 0.0
      prev_t = None
  if cur > 0.0:
    durations.append(cur)
  return durations


def derivative_stats(weighted: list[tuple[dict[str, Any], float]], key: str, thresholds: dict[str, float]) -> tuple[float | None, float | None]:
  rates = []
  prev_sample = None
  prev_value = None
  max_gap = thresholds["max_rate_gap_sec"]
  for sample, _ in weighted:
    value = as_float(sample.get(key))
    if value is None:
      continue
    if prev_sample is not None and prev_value is not None:
      dt = (as_float(sample.get("route_time_sec"), 0.0) or 0.0) - (as_float(prev_sample.get("route_time_sec"), 0.0) or 0.0)
      if 0.0 < dt <= max_gap:
        rates.append((value - prev_value) / dt)
    prev_sample = sample
    prev_value = value
  if not rates:
    return None, None
  return float(math.sqrt(np.mean(np.square(rates)))), float(np.percentile(np.abs(rates), 95))


def sign_changes_per_min(weighted: list[tuple[dict[str, Any], float]], key: str, active_time: float,
                         thresholds: dict[str, float]) -> float | None:
  if active_time <= 0:
    return None
  prev_sign = 0
  prev_t: float | None = None
  changes = 0
  for sample, _ in weighted:
    value = as_float(sample.get(key))
    if value is None:
      continue
    sign = 1 if value > 1e-6 else -1 if value < -1e-6 else 0
    t = as_float(sample.get("route_time_sec"), 0.0) or 0.0
    if sign == 0:
      continue
    if prev_sign != 0 and sign != prev_sign and prev_t is not None and 0.0 < t - prev_t <= thresholds["max_rate_gap_sec"]:
      changes += 1
    prev_sign = sign
    prev_t = t
  return changes * 60.0 / active_time


def aggregate_metrics(base_samples: list[dict[str, Any]], lat_samples: list[dict[str, Any]],
                      long_samples: list[dict[str, Any]], live_parameter_samples: list[dict[str, Any]],
                      live_torque_samples: list[dict[str, Any]], warmup_skip_sec: float, scope: str,
                      thresholds: dict[str, float],
                      sample_filter: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
  base_w = apply_scope_and_filter(with_dt(base_samples, thresholds), scope, warmup_skip_sec, sample_filter)
  lat_w = apply_scope_and_filter(with_dt(lat_samples, thresholds), scope, warmup_skip_sec, sample_filter)
  long_w = apply_scope_and_filter(with_dt(long_samples, thresholds), scope, warmup_skip_sec, sample_filter)

  live_samples = live_parameter_samples
  torque_samples = live_torque_samples
  if scope == "post_warmup":
    live_samples = [s for s in live_samples if (as_float(s.get("route_time_sec"), 0.0) or 0.0) >= warmup_skip_sec]
    torque_samples = [s for s in torque_samples if (as_float(s.get("route_time_sec"), 0.0) or 0.0) >= warmup_skip_sec]
  if sample_filter is not None:
    live_samples = []
    torque_samples = []

  duration = sum(dt for _, dt in base_w)
  lat_duration = sum(dt for _, dt in lat_w)
  long_duration = sum(dt for _, dt in long_w)
  distance_m = sum((as_float(sample.get("v_ego_mps"), 0.0) or 0.0) * dt for sample, dt in base_w)

  clean_lat = [(s, dt) for s, dt in lat_w if s.get("clean_lateral")]
  strict_clean_lat = [(s, dt) for s, dt in lat_w if s.get("strict_clean_lateral")]
  clean_lat_time = sum(dt for _, dt in clean_lat)
  lateral_errors = [abs(v) for v in weighted_values(clean_lat, "lateral_error")]
  output_rate_rms, output_rate_p95 = derivative_stats(clean_lat, "torque_output", thresholds)
  pinned = [(s, dt) for s, dt in clean_lat if s.get("pinned")]
  pinned_time = sum(dt for _, dt in pinned)
  pinned_bursts = detect_pinned_bursts([s for s, _ in clean_lat], thresholds)
  saturation_time = bool_time(clean_lat, "lateral_saturated")

  clean_long = [(s, dt) for s, dt in long_w if s.get("clean_longitudinal")]
  clean_long_time = sum(dt for _, dt in clean_long)
  no_lead_strict = [(s, dt) for s, dt in clean_long if s.get("no_lead_strict")]
  no_lead_relaxed = [(s, dt) for s, dt in clean_long if s.get("no_lead_relaxed")]
  lead_limited = [(s, dt) for s, dt in clean_long if s.get("lead_limited")]
  no_lead_deficit = [v for v in weighted_values(no_lead_relaxed, "speed_deficit_mps") if v is not None]
  lazy_durations = group_true_durations(long_w, lambda s: bool(s.get("lazy_candidate")),
                                        thresholds["max_rate_gap_sec"])
  lazy_time = sum(d for d in lazy_durations if d >= thresholds["lazy_min_duration_sec"])

  angle_offsets = finite_values([s.get("angle_offset_deg") for s in live_samples])
  steer_ratios = finite_values([s.get("steer_ratio") for s in live_samples])
  first_90 = finite_values([s.get("angle_offset_deg") for s in live_parameter_samples if (as_float(s.get("route_time_sec"), 0.0) or 0.0) < warmup_skip_sec])
  after_90 = finite_values([s.get("angle_offset_deg") for s in live_parameter_samples if (as_float(s.get("route_time_sec"), 0.0) or 0.0) >= warmup_skip_sec])

  metrics = {
    "duration_sec": duration,
    "distance_m": distance_m,
    "lat_active_pct": pct(bool_time(base_w, "lat_active"), duration),
    "selfdrive_active_pct": pct(bool_time(base_w, "selfdrive_active"), duration),
    "steering_pressed_pct": pct(bool_time(base_w, "steering_pressed"), duration),
    "gas_pressed_pct": pct(bool_time(base_w, "gas_pressed"), duration),
    "brake_pressed_pct": pct(bool_time(base_w, "brake_pressed"), duration),
    "clean_lateral_sample_count": len(clean_lat),
    "strict_clean_lateral_sample_count": len(strict_clean_lat),
    "clean_lateral_time_sec": clean_lat_time,
    "lateral_error_rms": float(math.sqrt(np.mean(np.square(lateral_errors)))) if lateral_errors else None,
    "lateral_error_mean_abs": float(np.mean(lateral_errors)) if lateral_errors else None,
    "lateral_error_p95_abs": float(np.percentile(lateral_errors, 95)) if lateral_errors else None,
    "lateral_error_p99_abs": float(np.percentile(lateral_errors, 99)) if lateral_errors else None,
    "lateral_error_max_abs": max(lateral_errors) if lateral_errors else None,
    "desired_lateral_accel_max_abs": max_abs(weighted_values(clean_lat, "desired_lateral_accel")),
    "actual_lateral_accel_max_abs": max_abs(weighted_values(clean_lat, "actual_lateral_accel")),
    "torque_output_max_abs": max_abs(weighted_values(clean_lat, "torque_output")),
    "carcontrol_torque_max_abs": max_abs(weighted_values(clean_lat, "carcontrol_torque")),
    "pinned_output_time_sec": pinned_time,
    "pinned_output_count": len(pinned),
    "pinned_output_pct_clean": pct(pinned_time, clean_lat_time),
    "pinned_burst_count": len(pinned_bursts),
    "longest_pinned_burst_sec": max((b["duration_sec"] for b in pinned_bursts), default=0.0),
    "saturation_pct": pct(saturation_time, clean_lat_time),
    "steering_angle_max_abs": max_abs(weighted_values(clean_lat, "steering_angle_deg")),
    "steering_torque_max_abs": max_abs(weighted_values(clean_lat, "steering_torque")),
    "steering_torque_eps_max_abs": max_abs(weighted_values(clean_lat, "steering_torque_eps")),
    "output_rate_rms": output_rate_rms,
    "output_rate_p95_abs": output_rate_p95,
    "output_sign_changes_per_min": sign_changes_per_min(clean_lat, "torque_output", clean_lat_time, thresholds),
    "lateral_error_sign_changes_per_min": sign_changes_per_min(clean_lat, "lateral_error", clean_lat_time, thresholds),
    "angle_offset_deg_avg": float(np.mean(angle_offsets)) if angle_offsets else None,
    "angle_offset_deg_median": float(np.median(angle_offsets)) if angle_offsets else None,
    "angle_offset_deg_min": min(angle_offsets) if angle_offsets else None,
    "angle_offset_deg_max": max(angle_offsets) if angle_offsets else None,
    "angle_offset_average_deg_avg": avg([s.get("angle_offset_average_deg") for s in live_samples]),
    "steer_ratio_avg": float(np.mean(steer_ratios)) if steer_ratios else None,
    "steer_ratio_median": float(np.median(steer_ratios)) if steer_ratios else None,
    "steer_ratio_min": min(steer_ratios) if steer_ratios else None,
    "steer_ratio_max": max(steer_ratios) if steer_ratios else None,
    "stiffness_factor_avg": avg([s.get("stiffness_factor") for s in live_samples]),
    "angle_offset_drift_deg": (float(np.mean(after_90) - np.mean(first_90)) if first_90 and after_90 else None),
    "live_torque_cal_perc_max": max(finite_values([s.get("cal_perc") for s in torque_samples]), default=None),
    "live_torque_live_valid_pct": pct(sum(1 for s in torque_samples if s.get("live_valid")), len(torque_samples)),
    "live_torque_use_params_pct": pct(sum(1 for s in torque_samples if s.get("use_params")), len(torque_samples)),
    "live_torque_lat_accel_factor_filtered_avg": avg([s.get("lat_accel_factor_filtered") for s in torque_samples]),
    "live_torque_friction_coefficient_filtered_avg": avg([s.get("friction_coefficient_filtered") for s in torque_samples]),
    "long_active_pct": pct(bool_time(long_w, "long_active"), long_duration),
    "clean_longitudinal_sample_count": len(clean_long),
    "clean_longitudinal_time_sec": clean_long_time,
    "accel_cmd_avg": avg(weighted_values(clean_long, "accel_cmd")),
    "accel_cmd_p95": percentile(weighted_values(clean_long, "accel_cmd"), 95),
    "accel_cmd_max": max(finite_values(weighted_values(clean_long, "accel_cmd")), default=None),
    "actual_accel_avg": avg(weighted_values(clean_long, "a_ego_mps2")),
    "actual_accel_p95": percentile(weighted_values(clean_long, "a_ego_mps2"), 95),
    "no_lead_avg_speed_deficit_mps": avg(no_lead_deficit),
    "no_lead_p95_speed_deficit_mps": percentile(no_lead_deficit, 95),
    "no_lead_avg_speed_deficit_mph": (avg(no_lead_deficit) * MS_TO_MPH if avg(no_lead_deficit) is not None else None),
    "no_lead_p95_speed_deficit_mph": (percentile(no_lead_deficit, 95) * MS_TO_MPH if percentile(no_lead_deficit, 95) is not None else None),
    "deficit_gt_3mph_time_sec": weighted_duration(no_lead_relaxed, lambda s: (as_float(s.get("speed_deficit_mph"), -999.0) or -999.0) > 3.0),
    "deficit_gt_5mph_time_sec": weighted_duration(no_lead_relaxed, lambda s: (as_float(s.get("speed_deficit_mph"), -999.0) or -999.0) > 5.0),
    "deficit_gt_8mph_time_sec": weighted_duration(no_lead_relaxed, lambda s: (as_float(s.get("speed_deficit_mph"), -999.0) or -999.0) > 8.0),
    "lazy_accel_time_sec": lazy_time,
    "lead_limited_time_sec": sum(dt for _, dt in lead_limited),
    "no_lead_strict_time_sec": sum(dt for _, dt in no_lead_strict),
    "no_lead_relaxed_time_sec": sum(dt for _, dt in no_lead_relaxed),
    "set_speed_samples": sum(1 for s, _ in clean_long if s.get("set_speed_mps") is not None),
    "set_speed_uncertain_samples": sum(1 for s, _ in clean_long if s.get("set_speed_units_uncertain")),
  }

  return {name: metrics.get(name) for name in METRIC_NAMES}


def detect_pinned_bursts(lat_samples: list[dict[str, Any]], thresholds: dict[str, float]) -> list[dict[str, Any]]:
  weighted = with_dt(sorted(lat_samples, key=lambda s: s.get("route_time_sec") or 0.0), thresholds)
  pinned = [(s, dt) for s, dt in weighted if s.get("clean_lateral") and s.get("pinned")]
  bursts = []
  current: list[tuple[dict[str, Any], float]] = []
  prev_t: float | None = None
  gap = thresholds["pinned_burst_gap_sec"]

  def flush() -> None:
    if not current:
      return
    samples = [s for s, _ in current]
    duration = sum(dt for _, dt in current)
    start = samples[0]
    end = samples[-1]
    if duration <= 0.0:
      duration = max(0.0, (as_float(end.get("route_time_sec"), 0.0) or 0.0) - (as_float(start.get("route_time_sec"), 0.0) or 0.0))
    speeds = finite_values([s.get("speed_mph") for s in samples])
    outputs = finite_values([s.get("torque_output") for s in samples])
    steer_angles = finite_values([s.get("steering_angle_deg") for s in samples])
    desired = finite_values([s.get("desired_lateral_accel") for s in samples])
    actual = finite_values([s.get("actual_lateral_accel") for s in samples])
    bursts.append({
      "segment_index": start.get("segment_index"),
      "start_segment_time_sec": start.get("segment_time_sec"),
      "end_segment_time_sec": end.get("segment_time_sec"),
      "start_route_time_sec": start.get("route_time_sec"),
      "end_route_time_sec": end.get("route_time_sec"),
      "duration_sec": duration,
      "avg_mph": float(np.mean(speeds)) if speeds else None,
      "max_abs_lateral_error": max_abs([s.get("lateral_error") for s in samples]),
      "avg_output_sign": float(np.mean([1 if o > 0 else -1 if o < 0 else 0 for o in outputs])) if outputs else None,
      "steering_angle_min_deg": min(steer_angles) if steer_angles else None,
      "steering_angle_max_deg": max(steer_angles) if steer_angles else None,
      "desired_lateral_accel_min": min(desired) if desired else None,
      "desired_lateral_accel_max": max(desired) if desired else None,
      "actual_lateral_accel_min": min(actual) if actual else None,
      "actual_lateral_accel_max": max(actual) if actual else None,
      "gas_pressed": any(s.get("gas_pressed") for s in samples),
      "brake_pressed": any(s.get("brake_pressed") for s in samples),
      "steering_pressed": any(s.get("steering_pressed") for s in samples),
      "sample_count": len(samples),
      "post_warmup": False,
    })

  for sample, dt in pinned:
    t = as_float(sample.get("route_time_sec"), 0.0) or 0.0
    if current and prev_t is not None and t - prev_t > gap:
      flush()
      current = []
    current.append((sample, dt))
    prev_t = t
  flush()
  return bursts


def top_lateral_events(lat_samples: list[dict[str, Any]], top_n: int = 50) -> list[dict[str, Any]]:
  candidates = [s for s in lat_samples if s.get("clean_lateral") and as_float(s.get("lateral_error_abs")) is not None]
  candidates.sort(key=lambda s: as_float(s.get("lateral_error_abs"), 0.0) or 0.0, reverse=True)
  events = []
  for rank, sample in enumerate(candidates[:top_n], start=1):
    events.append({
      "rank": rank,
      "segment_index": sample.get("segment_index"),
      "segment_time_sec": sample.get("segment_time_sec"),
      "route_time_sec": sample.get("route_time_sec"),
      "speed_mph": sample.get("speed_mph"),
      "speed_regime": sample.get("speed_regime"),
      "turn_regime": sample.get("turn_regime"),
      "lateral_error": sample.get("lateral_error"),
      "abs_lateral_error": sample.get("lateral_error_abs"),
      "desired_lateral_accel": sample.get("desired_lateral_accel"),
      "actual_lateral_accel": sample.get("actual_lateral_accel"),
      "torque_output": sample.get("torque_output"),
      "carcontrol_torque": sample.get("carcontrol_torque"),
      "steering_angle_deg": sample.get("steering_angle_deg"),
      "steering_torque": sample.get("steering_torque"),
      "steering_torque_eps": sample.get("steering_torque_eps"),
      "gas_pressed": sample.get("gas_pressed"),
      "brake_pressed": sample.get("brake_pressed"),
      "steering_pressed": sample.get("steering_pressed"),
      "post_warmup": False,
    })
  return events


def detect_acceleration_events(long_samples: list[dict[str, Any]], thresholds: dict[str, float],
                               warmup_skip_sec: float) -> list[dict[str, Any]]:
  weighted = with_dt(long_samples, thresholds)
  events = []
  current: list[tuple[dict[str, Any], float]] = []
  min_duration = thresholds["accel_event_min_duration_sec"]

  def start_condition(sample: dict[str, Any]) -> bool:
    return (
      sample.get("clean_longitudinal") and sample.get("no_lead_relaxed") and
      (as_float(sample.get("speed_deficit_mph"), -999.0) or -999.0) >= thresholds["accel_event_start_deficit_mph"] and
      (as_float(sample.get("speed_mph"), 0.0) or 0.0) > thresholds["accel_event_min_speed_mph"]
    )

  def end_reason(sample: dict[str, Any]) -> str | None:
    deficit = as_float(sample.get("speed_deficit_mph"))
    if deficit is not None and deficit <= thresholds["accel_event_end_deficit_mph"]:
      return "within_2mph"
    if sample.get("lead_limited"):
      return "lead_within_50m"
    if sample.get("gas_pressed") or sample.get("brake_pressed"):
      return "driver_gas_or_brake"
    if not sample.get("long_active"):
      return "long_inactive"
    return None

  def flush(reason: str) -> None:
    if not current:
      return
    samples = [s for s, _ in current]
    start = samples[0]
    end = samples[-1]
    duration = max(sum(dt for _, dt in current), (as_float(end.get("route_time_sec"), 0.0) or 0.0) - (as_float(start.get("route_time_sec"), 0.0) or 0.0))
    if duration < min_duration:
      return
    deficits = finite_values([s.get("speed_deficit_mph") for s in samples])
    accel_cmds = finite_values([s.get("accel_cmd") for s in samples])
    actual_accels = finite_values([s.get("a_ego_mps2") for s in samples])
    lazy_durations = group_true_durations(current, lambda s: bool(s.get("lazy_candidate")), thresholds["max_rate_gap_sec"])
    achieved = reason == "within_2mph"
    conf_weights = [dt for _, dt in current]
    conf_vals = [no_lead_confidence(s, thresholds) for s, _ in current]
    conf = float(np.average(conf_vals, weights=conf_weights)) if conf_vals and sum(conf_weights) > 0 else None
    events.append({
      "segment_index": start.get("segment_index"),
      "start_segment_time_sec": start.get("segment_time_sec"),
      "end_segment_time_sec": end.get("segment_time_sec"),
      "start_route_time_sec": start.get("route_time_sec"),
      "end_route_time_sec": end.get("route_time_sec"),
      "duration_sec": duration,
      "start_speed_mph": start.get("speed_mph"),
      "target_set_speed_mph": (start.get("set_speed_mps") * MS_TO_MPH if start.get("set_speed_mps") is not None else None),
      "max_speed_deficit_mph": max(deficits) if deficits else None,
      "final_speed_deficit_mph": end.get("speed_deficit_mph"),
      "time_to_within_2mph_sec": duration if achieved else None,
      "mean_accel_cmd": float(np.mean(accel_cmds)) if accel_cmds else None,
      "max_accel_cmd": max(accel_cmds) if accel_cmds else None,
      "mean_actual_a_ego": float(np.mean(actual_accels)) if actual_accels else None,
      "max_actual_a_ego": max(actual_accels) if actual_accels else None,
      "no_lead_confidence": conf,
      "lazy_flag": any(d >= thresholds["lazy_min_duration_sec"] for d in lazy_durations),
      "end_reason": reason,
      "achieved_within_2mph": achieved,
      "post_warmup": (as_float(start.get("route_time_sec"), 0.0) or 0.0) >= warmup_skip_sec,
    })

  for sample, dt in weighted:
    if not current:
      if start_condition(sample):
        current = [(sample, dt)]
      continue
    current.append((sample, dt))
    reason = end_reason(sample)
    if reason is not None:
      flush(reason)
      current = []
  if current:
    flush("segment_end")
  return events


def detect_stop_go_events(long_samples: list[dict[str, Any]], thresholds: dict[str, float],
                          warmup_skip_sec: float) -> list[dict[str, Any]]:
  weighted = with_dt(long_samples, thresholds)
  events = []
  current: list[tuple[dict[str, Any], float]] = []

  def start_condition(sample: dict[str, Any]) -> bool:
    speed = as_float(sample.get("speed_mph"), 0.0) or 0.0
    return (speed < thresholds["stop_go_start_mph"] or sample.get("standstill")) and (sample.get("long_active") or sample.get("enabled"))

  def flush(incomplete: bool) -> None:
    if not current:
      return
    samples = [s for s, _ in current]
    start = samples[0]
    end = samples[-1]
    duration = max(sum(dt for _, dt in current), (as_float(end.get("route_time_sec"), 0.0) or 0.0) - (as_float(start.get("route_time_sec"), 0.0) or 0.0))
    accel_cmds = finite_values([s.get("accel_cmd") for s in samples])
    actual_accels = finite_values([s.get("a_ego_mps2") for s in samples])

    def time_to(threshold_mph: float) -> float | None:
      t0 = as_float(start.get("route_time_sec"), 0.0) or 0.0
      for s in samples:
        if (as_float(s.get("speed_mph"), 0.0) or 0.0) >= threshold_mph:
          return (as_float(s.get("route_time_sec"), t0) or t0) - t0
      return None

    moving_time = time_to(thresholds["stop_go_start_mph"])
    events.append({
      "segment_index": start.get("segment_index"),
      "start_segment_time_sec": start.get("segment_time_sec"),
      "end_segment_time_sec": end.get("segment_time_sec"),
      "start_route_time_sec": start.get("route_time_sec"),
      "end_route_time_sec": end.get("route_time_sec"),
      "duration_sec": duration,
      "stopped_duration_sec": moving_time,
      "time_to_1mph_sec": time_to(1.0),
      "time_to_3mph_sec": time_to(3.0),
      "time_to_5mph_sec": time_to(5.0),
      "mean_accel_cmd": float(np.mean(accel_cmds)) if accel_cmds else None,
      "max_accel_cmd": max(accel_cmds) if accel_cmds else None,
      "mean_actual_a_ego": float(np.mean(actual_accels)) if actual_accels else None,
      "max_actual_a_ego": max(actual_accels) if actual_accels else None,
      "lead_status_at_start": start.get("lead_status"),
      "lead_d_rel_m_at_start": start.get("lead_d_rel_m"),
      "driver_gas_or_brake": any(s.get("gas_pressed") or s.get("brake_pressed") for s in samples),
      "incomplete": incomplete,
      "notes": "driver gas/brake occurred" if any(s.get("gas_pressed") or s.get("brake_pressed") for s in samples) else "",
      "post_warmup": (as_float(start.get("route_time_sec"), 0.0) or 0.0) >= warmup_skip_sec,
    })

  for sample, dt in weighted:
    if not current:
      if start_condition(sample):
        current = [(sample, dt)]
      continue
    current.append((sample, dt))
    if (as_float(sample.get("speed_mph"), 0.0) or 0.0) > thresholds["stop_go_end_mph"]:
      flush(False)
      current = []
  if current:
    flush(True)
  return events


def metric_defaults() -> dict[str, Any]:
  return {name: None for name in METRIC_NAMES}
