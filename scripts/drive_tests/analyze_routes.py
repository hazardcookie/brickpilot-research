#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])).expanduser()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
DATA_ROOT = Path(os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB")).expanduser()
for root in (TOOLS_ROOT, REPO_ROOT):
  if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from openpilot.tools.lib.logreader import LogReader, LogsUnavailable, ReadMode
from openpilot.tools.lib.route import Route

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
  sys.path.insert(0, str(SCRIPT_DIR))

import metrics


SCOPES = ("all", "post_warmup")


def sql_columns(column_defs: list[tuple[str, str]]) -> str:
  return ",\n      ".join(f"{name} {sql_type}" for name, sql_type in column_defs)


def metric_column_names() -> list[str]:
  return [name for name, _ in metrics.METRIC_COLUMNS]


def init_db(db_path: Path) -> sqlite3.Connection:
  db_path.parent.mkdir(parents=True, exist_ok=True)
  if db_path.exists():
    db_path.unlink()
  conn = sqlite3.connect(db_path)
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA synchronous=NORMAL")
  metric_cols = sql_columns(metrics.METRIC_COLUMNS)
  conn.executescript(f"""
    CREATE TABLE routes (
      label TEXT PRIMARY KEY,
      model TEXT,
      route_id TEXT,
      warmup_skip_sec REAL,
      weight_in_overall_score REAL,
      notes TEXT,
      settings_json TEXT,
      car_params_json TEXT,
      redacted_car_vin TEXT,
      available_segments TEXT,
      missing_segments TEXT,
      qlog_segments TEXT,
      total_duration_sec REAL,
      estimated_distance_m REAL
    );

    CREATE TABLE segments (
      route_label TEXT,
      model TEXT,
      route_id TEXT,
      segment_index INTEGER,
      log_type TEXT,
      status TEXT,
      message_count INTEGER,
      duration_sec REAL,
      distance_m REAL,
      warning_count INTEGER,
      PRIMARY KEY(route_label, segment_index)
    );

    CREATE TABLE route_metrics (
      route_label TEXT,
      model TEXT,
      scope TEXT,
      {metric_cols},
      PRIMARY KEY(route_label, scope)
    );

    CREATE TABLE segment_metrics (
      route_label TEXT,
      model TEXT,
      segment_index INTEGER,
      log_type TEXT,
      scope TEXT,
      {metric_cols},
      PRIMARY KEY(route_label, segment_index, scope)
    );

    CREATE TABLE regime_metrics (
      route_label TEXT,
      model TEXT,
      segment_index INTEGER,
      scope TEXT,
      regime_type TEXT,
      regime_name TEXT,
      {metric_cols}
    );

    CREATE TABLE lateral_events (
      route_label TEXT,
      model TEXT,
      rank INTEGER,
      segment_index INTEGER,
      segment_time_sec REAL,
      route_time_sec REAL,
      speed_mph REAL,
      speed_regime TEXT,
      turn_regime TEXT,
      lateral_error REAL,
      abs_lateral_error REAL,
      desired_lateral_accel REAL,
      actual_lateral_accel REAL,
      torque_output REAL,
      carcontrol_torque REAL,
      steering_angle_deg REAL,
      steering_torque REAL,
      steering_torque_eps REAL,
      gas_pressed INTEGER,
      brake_pressed INTEGER,
      steering_pressed INTEGER,
      post_warmup INTEGER
    );

    CREATE TABLE pinned_bursts (
      route_label TEXT,
      model TEXT,
      burst_index INTEGER,
      segment_index INTEGER,
      start_segment_time_sec REAL,
      end_segment_time_sec REAL,
      start_route_time_sec REAL,
      end_route_time_sec REAL,
      duration_sec REAL,
      avg_mph REAL,
      max_abs_lateral_error REAL,
      avg_output_sign REAL,
      steering_angle_min_deg REAL,
      steering_angle_max_deg REAL,
      desired_lateral_accel_min REAL,
      desired_lateral_accel_max REAL,
      actual_lateral_accel_min REAL,
      actual_lateral_accel_max REAL,
      gas_pressed INTEGER,
      brake_pressed INTEGER,
      steering_pressed INTEGER,
      sample_count INTEGER,
      post_warmup INTEGER
    );

    CREATE TABLE acceleration_events (
      route_label TEXT,
      model TEXT,
      event_index INTEGER,
      segment_index INTEGER,
      start_segment_time_sec REAL,
      end_segment_time_sec REAL,
      start_route_time_sec REAL,
      end_route_time_sec REAL,
      duration_sec REAL,
      start_speed_mph REAL,
      target_set_speed_mph REAL,
      max_speed_deficit_mph REAL,
      final_speed_deficit_mph REAL,
      time_to_within_2mph_sec REAL,
      mean_accel_cmd REAL,
      max_accel_cmd REAL,
      mean_actual_a_ego REAL,
      max_actual_a_ego REAL,
      no_lead_confidence REAL,
      lazy_flag INTEGER,
      end_reason TEXT,
      achieved_within_2mph INTEGER,
      post_warmup INTEGER
    );

    CREATE TABLE stop_go_events (
      route_label TEXT,
      model TEXT,
      event_index INTEGER,
      segment_index INTEGER,
      start_segment_time_sec REAL,
      end_segment_time_sec REAL,
      start_route_time_sec REAL,
      end_route_time_sec REAL,
      duration_sec REAL,
      stopped_duration_sec REAL,
      time_to_1mph_sec REAL,
      time_to_3mph_sec REAL,
      time_to_5mph_sec REAL,
      mean_accel_cmd REAL,
      max_accel_cmd REAL,
      mean_actual_a_ego REAL,
      max_actual_a_ego REAL,
      lead_status_at_start INTEGER,
      lead_d_rel_m_at_start REAL,
      driver_gas_or_brake INTEGER,
      incomplete INTEGER,
      notes TEXT,
      post_warmup INTEGER
    );

    CREATE TABLE warnings (
      route_label TEXT,
      model TEXT,
      segment_index INTEGER,
      severity TEXT,
      message TEXT
    );

    CREATE TABLE lateral_timeseries (
      route_label TEXT,
      model TEXT,
      segment_index INTEGER,
      log_type TEXT,
      segment_time_sec REAL,
      route_time_sec REAL,
      post_warmup INTEGER,
      v_ego_mps REAL,
      speed_mph REAL,
      lateral_error REAL,
      desired_lateral_accel REAL,
      actual_lateral_accel REAL,
      torque_output REAL,
      carcontrol_torque REAL,
      steering_angle_deg REAL,
      gas_pressed INTEGER,
      brake_pressed INTEGER,
      steering_pressed INTEGER,
      clean_lateral INTEGER,
      pinned INTEGER
    );

    CREATE TABLE longitudinal_timeseries (
      route_label TEXT,
      model TEXT,
      segment_index INTEGER,
      log_type TEXT,
      segment_time_sec REAL,
      route_time_sec REAL,
      post_warmup INTEGER,
      v_ego_mps REAL,
      speed_mph REAL,
      a_ego_mps2 REAL,
      accel_cmd REAL,
      set_speed_mps REAL,
      set_speed_source TEXT,
      set_speed_units_uncertain INTEGER,
      speed_deficit_mps REAL,
      speed_deficit_mph REAL,
      long_active INTEGER,
      clean_longitudinal INTEGER,
      no_lead_strict INTEGER,
      no_lead_relaxed INTEGER,
      lead_limited INTEGER,
      lead_status INTEGER,
      lead_d_rel_m REAL,
      gas_pressed INTEGER,
      brake_pressed INTEGER,
      lazy_candidate INTEGER
    );

    CREATE TABLE live_parameters_timeseries (
      route_label TEXT,
      model TEXT,
      segment_index INTEGER,
      log_type TEXT,
      segment_time_sec REAL,
      route_time_sec REAL,
      post_warmup INTEGER,
      angle_offset_deg REAL,
      angle_offset_average_deg REAL,
      steer_ratio REAL,
      stiffness_factor REAL
    );
  """)
  return conn


def load_catalog(path: Path) -> dict[str, Any]:
  with path.open() as f:
    catalog = yaml.safe_load(f) or {}
  catalog.setdefault("defaults", {})
  catalog.setdefault("routes", [])
  return catalog


def route_thresholds(catalog: dict[str, Any], route: dict[str, Any]) -> dict[str, float]:
  thresholds = {}
  thresholds.update((catalog.get("defaults") or {}).get("thresholds") or {})
  thresholds.update(route.get("thresholds") or {})
  return metrics.merged_thresholds(thresholds)


def load_route_paths(route_id: str) -> tuple[list[str | None], list[str | None], str | None]:
  try:
    route = Route(route_id)
    return route.log_paths(), route.qlog_paths(), None
  except Exception as exc:
    return [], [], f"{type(exc).__name__}: {exc}"


def load_local_route_paths(route: dict[str, Any], max_segments: int) -> tuple[list[str | None], list[str | None]]:
  """Return local rlog/qlog paths for copied comma segments, when catalog provides them.

  Historical logdrive catalogs include ``local_segments_dir`` so offline analysis can
  run after /logdrive copies without relying on comma's route file API or network.
  """
  local_segments_dir = route.get("local_segments_dir")
  route_id = route.get("route_id")
  if not local_segments_dir or not route_id:
    return [], []

  root = Path(str(local_segments_dir))
  if not root.exists():
    return [], []

  log_paths: list[str | None] = [None] * max_segments
  qlog_paths: list[str | None] = [None] * max_segments
  for seg_dir in sorted(root.glob(f"{route_id}--*")):
    if not seg_dir.is_dir():
      continue
    try:
      segment_index = int(seg_dir.name.rsplit("--", 1)[-1])
    except ValueError:
      continue
    if not 0 <= segment_index < max_segments:
      continue
    rlog = seg_dir / "rlog.zst"
    qlog = seg_dir / "qlog.zst"
    if rlog.exists() and rlog.stat().st_size > 0:
      log_paths[segment_index] = str(rlog)
    if qlog.exists() and qlog.stat().st_size > 0:
      qlog_paths[segment_index] = str(qlog)
  return log_paths, qlog_paths


def fetch_segment_from_paths(log_paths: list[str | None], qlog_paths: list[str | None],
                             segment_index: int, allow_qlog_fallback: bool) -> tuple[Any | None, str | None, str | None]:
  if segment_index < len(log_paths) and log_paths[segment_index]:
    return LogReader(log_paths[segment_index], sort_by_time=True, only_union_types=True), "rlog", None
  if allow_qlog_fallback and segment_index < len(qlog_paths) and qlog_paths[segment_index]:
    return LogReader(qlog_paths[segment_index], sort_by_time=True, only_union_types=True), "qlog", None
  return None, None, "segment not listed by comma route files API"


def fetch_segment(route_id: str, segment_index: int, allow_qlog_fallback: bool) -> tuple[Any | None, str | None, str | None]:
  rlog_identifier = f"{route_id}/{segment_index}/r"
  try:
    return LogReader(rlog_identifier, default_mode=ReadMode.RLOG, sort_by_time=True, only_union_types=True), "rlog", None
  except (LogsUnavailable, FileNotFoundError, AssertionError, Exception) as exc:
    rlog_error = f"{type(exc).__name__}: {exc}"

  if not allow_qlog_fallback:
    return None, None, rlog_error

  auto_identifier = f"{route_id}/{segment_index}/a"
  try:
    lr = LogReader(auto_identifier, default_mode=ReadMode.AUTO, sort_by_time=True, only_union_types=True)
    identifiers = getattr(lr, "logreader_identifiers", [])
    log_type = "qlog" if any("qlog" in str(identifier) for identifier in identifiers) else "rlog"
    return lr, log_type, None
  except (LogsUnavailable, FileNotFoundError, AssertionError, Exception) as exc:
    return None, None, f"{rlog_error}; qlog fallback failed: {type(exc).__name__}: {exc}"


def insert_metrics(conn: sqlite3.Connection, table: str, prefix: dict[str, Any], row: dict[str, Any]) -> None:
  cols = list(prefix.keys()) + metric_column_names()
  values = [prefix.get(c) for c in prefix.keys()] + [row.get(name) for name in metric_column_names()]
  placeholders = ",".join("?" for _ in cols)
  conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", values)


def insert_many_dicts(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
  if not rows:
    return
  cols = list(rows[0].keys())
  placeholders = ",".join("?" for _ in cols)
  conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                   [[row.get(c) for c in cols] for row in rows])


def bool_int(value: Any) -> int:
  return 1 if bool(value) else 0


def store_timeseries(conn: sqlite3.Connection, route: dict[str, Any], segment: dict[str, Any],
                     warmup_skip_sec: float) -> None:
  label = route["label"]
  model = route["model"]
  log_type = segment["log_type"]

  lateral_rows = []
  for sample in segment["lat_samples"]:
    lateral_rows.append({
      "route_label": label,
      "model": model,
      "segment_index": sample.get("segment_index"),
      "log_type": log_type,
      "segment_time_sec": sample.get("segment_time_sec"),
      "route_time_sec": sample.get("route_time_sec"),
      "post_warmup": bool_int((sample.get("route_time_sec") or 0.0) >= warmup_skip_sec),
      "v_ego_mps": sample.get("v_ego_mps"),
      "speed_mph": sample.get("speed_mph"),
      "lateral_error": sample.get("lateral_error"),
      "desired_lateral_accel": sample.get("desired_lateral_accel"),
      "actual_lateral_accel": sample.get("actual_lateral_accel"),
      "torque_output": sample.get("torque_output"),
      "carcontrol_torque": sample.get("carcontrol_torque"),
      "steering_angle_deg": sample.get("steering_angle_deg"),
      "gas_pressed": bool_int(sample.get("gas_pressed")),
      "brake_pressed": bool_int(sample.get("brake_pressed")),
      "steering_pressed": bool_int(sample.get("steering_pressed")),
      "clean_lateral": bool_int(sample.get("clean_lateral")),
      "pinned": bool_int(sample.get("pinned")),
    })
  insert_many_dicts(conn, "lateral_timeseries", lateral_rows)

  long_rows = []
  for sample in segment["long_samples"]:
    long_rows.append({
      "route_label": label,
      "model": model,
      "segment_index": sample.get("segment_index"),
      "log_type": log_type,
      "segment_time_sec": sample.get("segment_time_sec"),
      "route_time_sec": sample.get("route_time_sec"),
      "post_warmup": bool_int((sample.get("route_time_sec") or 0.0) >= warmup_skip_sec),
      "v_ego_mps": sample.get("v_ego_mps"),
      "speed_mph": sample.get("speed_mph"),
      "a_ego_mps2": sample.get("a_ego_mps2"),
      "accel_cmd": sample.get("accel_cmd"),
      "set_speed_mps": sample.get("set_speed_mps"),
      "set_speed_source": sample.get("set_speed_source"),
      "set_speed_units_uncertain": bool_int(sample.get("set_speed_units_uncertain")),
      "speed_deficit_mps": sample.get("speed_deficit_mps"),
      "speed_deficit_mph": sample.get("speed_deficit_mph"),
      "long_active": bool_int(sample.get("long_active")),
      "clean_longitudinal": bool_int(sample.get("clean_longitudinal")),
      "no_lead_strict": bool_int(sample.get("no_lead_strict")),
      "no_lead_relaxed": bool_int(sample.get("no_lead_relaxed")),
      "lead_limited": bool_int(sample.get("lead_limited")),
      "lead_status": bool_int(sample.get("lead_status")),
      "lead_d_rel_m": sample.get("lead_d_rel_m"),
      "gas_pressed": bool_int(sample.get("gas_pressed")),
      "brake_pressed": bool_int(sample.get("brake_pressed")),
      "lazy_candidate": bool_int(sample.get("lazy_candidate")),
    })
  insert_many_dicts(conn, "longitudinal_timeseries", long_rows)

  live_rows = []
  for sample in segment["live_parameter_samples"]:
    live_rows.append({
      "route_label": label,
      "model": model,
      "segment_index": sample.get("segment_index"),
      "log_type": log_type,
      "segment_time_sec": sample.get("segment_time_sec"),
      "route_time_sec": sample.get("route_time_sec"),
      "post_warmup": bool_int((sample.get("route_time_sec") or 0.0) >= warmup_skip_sec),
      "angle_offset_deg": sample.get("angle_offset_deg"),
      "angle_offset_average_deg": sample.get("angle_offset_average_deg"),
      "steer_ratio": sample.get("steer_ratio"),
      "stiffness_factor": sample.get("stiffness_factor"),
    })
  insert_many_dicts(conn, "live_parameters_timeseries", live_rows)


def store_events(conn: sqlite3.Connection, route: dict[str, Any], segment: dict[str, Any],
                 thresholds: dict[str, float], warmup_skip_sec: float) -> tuple[int, int, int]:
  label = route["label"]
  model = route["model"]

  pinned_rows = []
  for burst in metrics.detect_pinned_bursts(segment["lat_samples"], thresholds):
    burst["post_warmup"] = bool_int((burst.get("start_route_time_sec") or 0.0) >= warmup_skip_sec)
    pinned_rows.append({
      "route_label": label,
      "model": model,
      "burst_index": None,
      **{k: bool_int(v) if k in ("gas_pressed", "brake_pressed", "steering_pressed", "post_warmup") else v for k, v in burst.items()},
    })
  insert_many_dicts(conn, "pinned_bursts", pinned_rows)

  acceleration_rows = []
  for event in metrics.detect_acceleration_events(segment["long_samples"], thresholds, warmup_skip_sec):
    acceleration_rows.append({
      "route_label": label,
      "model": model,
      "event_index": None,
      **{k: bool_int(v) if k in ("lazy_flag", "achieved_within_2mph", "post_warmup") else v for k, v in event.items()},
    })
  insert_many_dicts(conn, "acceleration_events", acceleration_rows)

  stop_go_rows = []
  for event in metrics.detect_stop_go_events(segment["long_samples"], thresholds, warmup_skip_sec):
    stop_go_rows.append({
      "route_label": label,
      "model": model,
      "event_index": None,
      **{k: bool_int(v) if k in ("lead_status_at_start", "driver_gas_or_brake", "incomplete", "post_warmup") else v for k, v in event.items()},
    })
  insert_many_dicts(conn, "stop_go_events", stop_go_rows)
  return len(pinned_rows), len(acceleration_rows), len(stop_go_rows)


def renumber_events(conn: sqlite3.Connection, table: str, index_col: str) -> None:
  rows = conn.execute(f"SELECT rowid, route_label FROM {table} ORDER BY route_label, start_route_time_sec, rowid").fetchall()
  counters: dict[str, int] = {}
  for rowid, label in rows:
    counters[label] = counters.get(label, 0) + 1
    conn.execute(f"UPDATE {table} SET {index_col}=? WHERE rowid=?", (counters[label], rowid))


def store_top_lateral_events(conn: sqlite3.Connection, route: dict[str, Any], lat_samples: list[dict[str, Any]],
                             warmup_skip_sec: float) -> None:
  rows = []
  for event in metrics.top_lateral_events(lat_samples, 50):
    event["post_warmup"] = bool_int((event.get("route_time_sec") or 0.0) >= warmup_skip_sec)
    rows.append({
      "route_label": route["label"],
      "model": route["model"],
      **{k: bool_int(v) if k in ("gas_pressed", "brake_pressed", "steering_pressed", "post_warmup") else v for k, v in event.items()},
    })
  insert_many_dicts(conn, "lateral_events", rows)


def write_csv(conn: sqlite3.Connection, query: str, out_path: Path) -> None:
  out_path.parent.mkdir(parents=True, exist_ok=True)
  cur = conn.execute(query)
  with out_path.open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([desc[0] for desc in cur.description])
    writer.writerows(cur.fetchall())


def export_csvs(conn: sqlite3.Connection, out_dir: Path) -> None:
  write_csv(conn, "SELECT * FROM route_metrics ORDER BY route_label, scope", out_dir / "summary.csv")
  write_csv(conn, "SELECT * FROM segment_metrics ORDER BY route_label, segment_index, scope", out_dir / "segment_summary.csv")
  write_csv(conn, "SELECT * FROM lateral_events ORDER BY route_label, rank", out_dir / "lateral_events.csv")
  write_csv(conn, "SELECT * FROM pinned_bursts ORDER BY route_label, burst_index", out_dir / "pinned_bursts.csv")
  write_csv(conn, "SELECT * FROM acceleration_events ORDER BY route_label, event_index", out_dir / "acceleration_events.csv")
  write_csv(conn, "SELECT * FROM stop_go_events ORDER BY route_label, event_index", out_dir / "stop_go_events.csv")


def route_row(route: dict[str, Any], car_params: dict[str, Any] | None,
              available: list[int], missing: list[int], qlog_segments: list[int],
              route_metrics_row: dict[str, Any]) -> dict[str, Any]:
  car_params = car_params or {}
  return {
    "label": route["label"],
    "model": route["model"],
    "route_id": route["route_id"],
    "warmup_skip_sec": route.get("warmup_skip_sec"),
    "weight_in_overall_score": route.get("weight_in_overall_score", 1.0),
    "notes": route.get("notes", ""),
    "settings_json": json.dumps(route.get("settings", {}), sort_keys=True),
    "car_params_json": json.dumps(car_params, sort_keys=True),
    "redacted_car_vin": car_params.get("redactedCarVin"),
    "available_segments": ",".join(str(s) for s in available),
    "missing_segments": ",".join(str(s) for s in missing),
    "qlog_segments": ",".join(str(s) for s in qlog_segments),
    "total_duration_sec": route_metrics_row.get("duration_sec"),
    "estimated_distance_m": route_metrics_row.get("distance_m"),
  }


def analyze_route(conn: sqlite3.Connection, catalog: dict[str, Any], route: dict[str, Any],
                  max_segments: int, allow_qlog_fallback: bool) -> None:
  defaults = catalog.get("defaults") or {}
  warmup_skip_sec = float(route.get("warmup_skip_sec", defaults.get("warmup_skip_sec", 90)))
  route["warmup_skip_sec"] = warmup_skip_sec
  route.setdefault("weight_in_overall_score", 1.0)
  thresholds = route_thresholds(catalog, route)
  missing_stop_after = int(route.get("missing_stop_after", defaults.get("missing_stop_after", 5)))
  label = route["label"]
  model = route["model"]
  route_id = route["route_id"]
  print(f"Analyzing {label} ({model})", flush=True)

  available: list[int] = []
  missing: list[int] = []
  qlog_segments: list[int] = []
  consecutive_missing = 0
  all_base: list[dict[str, Any]] = []
  all_lat: list[dict[str, Any]] = []
  all_long: list[dict[str, Any]] = []
  all_live: list[dict[str, Any]] = []
  all_torque: list[dict[str, Any]] = []
  car_params: dict[str, Any] | None = None

  log_paths, qlog_paths = load_local_route_paths(route, max_segments)
  route_path_error = None
  if not (log_paths or qlog_paths):
    log_paths, qlog_paths, route_path_error = load_route_paths(route_id)
  if route_path_error:
    conn.execute("INSERT INTO warnings VALUES (?,?,?,?,?)", (label, model, None, "warning", f"route file listing failed, falling back to per-segment LogReader discovery: {route_path_error}"))
    conn.commit()

  for segment_index in range(max_segments):
    if log_paths or qlog_paths:
      lr, log_type, error = fetch_segment_from_paths(log_paths, qlog_paths, segment_index, allow_qlog_fallback)
    else:
      lr, log_type, error = fetch_segment(route_id, segment_index, allow_qlog_fallback)
    if lr is None:
      missing.append(segment_index)
      consecutive_missing += 1
      conn.execute("INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (label, model, route_id, segment_index, "missing", "missing", 0, 0.0, 0.0, 1))
      conn.execute("INSERT INTO warnings VALUES (?,?,?,?,?)", (label, model, segment_index, "warning", f"missing rlog segment {segment_index}: {error}"))
      conn.commit()
      print(f"  seg {segment_index:02d}: missing", flush=True)
      if consecutive_missing >= missing_stop_after:
        print(f"  stopping discovery after {consecutive_missing} consecutive missing segments", flush=True)
        break
      continue

    consecutive_missing = 0
    available.append(segment_index)
    if log_type == "qlog":
      qlog_segments.append(segment_index)
      conn.execute("INSERT INTO warnings VALUES (?,?,?,?,?)", (label, model, segment_index, "warning", "rlog missing; used qlog fallback with lower-fidelity metrics"))

    segment = metrics.process_segment_messages(lr, segment_index, log_type or "rlog", thresholds)
    if segment["car_params_summary"] and car_params is None:
      car_params = segment["car_params_summary"]

    all_base.extend(segment["base_samples"])
    all_lat.extend(segment["lat_samples"])
    all_long.extend(segment["long_samples"])
    all_live.extend(segment["live_parameter_samples"])
    all_torque.extend(segment["live_torque_samples"])

    segment_distance = metrics.aggregate_metrics(segment["base_samples"], segment["lat_samples"], segment["long_samples"],
                                                 segment["live_parameter_samples"], segment["live_torque_samples"],
                                                 warmup_skip_sec, "all", thresholds)["distance_m"]
    conn.execute("INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (label, model, route_id, segment_index, log_type, "ok", segment["message_count"], segment["duration_sec"],
                  segment_distance, len(segment["warnings"])))
    for warning in segment["warnings"]:
      conn.execute("INSERT INTO warnings VALUES (?,?,?,?,?)", (label, model, segment_index, "warning", warning))

    for scope in SCOPES:
      row = metrics.aggregate_metrics(segment["base_samples"], segment["lat_samples"], segment["long_samples"],
                                      segment["live_parameter_samples"], segment["live_torque_samples"],
                                      warmup_skip_sec, scope, thresholds)
      insert_metrics(conn, "segment_metrics", {
        "route_label": label,
        "model": model,
        "segment_index": segment_index,
        "log_type": log_type,
        "scope": scope,
      }, row)

    store_timeseries(conn, route, segment, warmup_skip_sec)
    store_events(conn, route, segment, thresholds, warmup_skip_sec)
    print(f"  seg {segment_index:02d}: {log_type}, {segment['duration_sec']:.1f}s, {segment['message_count']} msgs", flush=True)
    conn.commit()

  if not available:
    conn.execute("INSERT INTO warnings VALUES (?,?,?,?,?)", (label, model, None, "error", "no available rlog/qlog segments found"))

  route_metric_rows: dict[str, dict[str, Any]] = {}
  for scope in SCOPES:
    row = metrics.aggregate_metrics(all_base, all_lat, all_long, all_live, all_torque, warmup_skip_sec, scope, thresholds)
    route_metric_rows[scope] = row
    insert_metrics(conn, "route_metrics", {"route_label": label, "model": model, "scope": scope}, row)

    for regime_name, _, _ in metrics.SPEED_REGIMES:
      regime_row = metrics.aggregate_metrics(all_base, all_lat, all_long, all_live, all_torque, warmup_skip_sec, scope,
                                             thresholds, metrics.sample_matches_speed_regime(regime_name))
      insert_metrics(conn, "regime_metrics", {
        "route_label": label,
        "model": model,
        "segment_index": None,
        "scope": scope,
        "regime_type": "speed",
        "regime_name": regime_name,
      }, regime_row)
    for regime_name, _, _ in metrics.TURN_REGIMES:
      regime_row = metrics.aggregate_metrics(all_base, all_lat, all_long, all_live, all_torque, warmup_skip_sec, scope,
                                             thresholds, metrics.sample_matches_turn_regime(regime_name))
      insert_metrics(conn, "regime_metrics", {
        "route_label": label,
        "model": model,
        "segment_index": None,
        "scope": scope,
        "regime_type": "turn_demand",
        "regime_name": regime_name,
      }, regime_row)

  store_top_lateral_events(conn, route, all_lat, warmup_skip_sec)

  row = route_row(route, car_params, available, missing, qlog_segments, route_metric_rows.get("all", {}))
  conn.execute(f"INSERT INTO routes ({','.join(row.keys())}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
  conn.commit()


def concise_summary(conn: sqlite3.Connection) -> str:
  rows = conn.execute("""
    SELECT r.route_label, r.model, rt.weight_in_overall_score,
           r.lateral_error_rms, r.pinned_output_time_sec, r.clean_lateral_time_sec,
           r.no_lead_avg_speed_deficit_mph, r.lazy_accel_time_sec
      FROM route_metrics r
      JOIN routes rt ON rt.label = r.route_label
     WHERE r.scope='post_warmup'
       AND rt.weight_in_overall_score > 0
  """).fetchall()
  if not rows:
    return "No post-warmup metrics were available."

  def best_by(index: int, require_positive_time_index: int | None = None) -> tuple[str, str] | None:
    candidates = []
    for row in rows:
      val = row[index]
      if val is None:
        continue
      if require_positive_time_index is not None and (row[require_positive_time_index] or 0) <= 0:
        continue
      candidates.append((val, row[1]))
    if not candidates:
      return None
    val, model = min(candidates, key=lambda item: item[0])
    return model, f"{val:.3f}"

  steering = best_by(3, 5)
  accel = best_by(6)
  scores = []
  for row in rows:
    values = [row[3], row[4], row[6], row[7]]
    if all(v is not None for v in values):
      scores.append((sum(float(v) for v in values), row[1]))
  balanced = min(scores, key=lambda item: item[0])[1] if scores else None
  return "\n".join([
    f"Likely best steering smoothness: {steering[0]} (RMS {steering[1]})" if steering else "Likely best steering smoothness: unavailable",
    f"Likely best acceleration / set-speed catch-up: {accel[0]} (avg deficit {accel[1]} mph)" if accel else "Likely best acceleration / set-speed catch-up: unavailable",
    f"Likely best balanced behavior: {balanced}" if balanced else "Likely best balanced behavior: unavailable",
  ])


def main() -> int:
  parser = argparse.ArgumentParser(description="Analyze comma/sunnypilot routes for lateral and longitudinal drive-test metrics")
  parser.add_argument("--catalog", type=Path, default=SCRIPT_DIR / "route_catalog.yaml")
  parser.add_argument("--out", type=Path, default=DATA_ROOT / "analysis_exports" / "drive_tests")
  parser.add_argument("--max-segments", type=int, default=80)
  parser.add_argument("--qlog-fallback", action=argparse.BooleanOptionalAction, default=True,
                      help="Use LogReader /a fallback when rlogs are missing, marking lower-fidelity qlog-derived metrics")
  parser.add_argument("--skip-report", action="store_true")
  parser.add_argument("--skip-plots", action="store_true")
  args = parser.parse_args()

  catalog = load_catalog(args.catalog)
  args.out.mkdir(parents=True, exist_ok=True)
  conn = init_db(args.out / "results.sqlite")
  try:
    for route in catalog.get("routes", []):
      analyze_route(conn, catalog, route, args.max_segments, args.qlog_fallback)

    renumber_events(conn, "pinned_bursts", "burst_index")
    renumber_events(conn, "acceleration_events", "event_index")
    renumber_events(conn, "stop_go_events", "event_index")
    conn.commit()
    export_csvs(conn, args.out)
    if not args.skip_report:
      import report as report_module
      report_module.generate_report(args.out / "results.sqlite", args.out / "report.md")
    if not args.skip_plots:
      import plots as plots_module
      plots_module.generate_plots(args.out / "results.sqlite", args.out / "plots")
    print("\nAnalysis complete.")
    print(concise_summary(conn))
    print(f"Outputs: {args.out}")
  finally:
    conn.close()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
