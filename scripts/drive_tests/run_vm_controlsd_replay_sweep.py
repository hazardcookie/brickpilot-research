#!/usr/bin/env python3
"""Run Brickpilot controlsd process replay inside the Linux openpilot VM image.

This script is intended to be executed from inside the Docker/Linux replay
environment with the Brickpilot repo mounted at /workspace/brickpilot and
DriveDB mounted at /workspace/BrickpilotDriveDB.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import statistics
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpilot.tools.lib.logreader import LogReader
from selfdrive.test.process_replay.process_replay import get_custom_params_from_lr, get_process_config, replay_process


DEFAULT_INPUT_ROOT = Path("/workspace/BrickpilotDriveDB/imports/raw/from_comma")
DEFAULT_OUTPUT_ROOT = Path("/workspace/BrickpilotDriveDB/analysis_exports")
DEFAULT_ROUTE_IDS = (
  "000001b4--6e74738cbb",
  "000001b7--5f24a935ac",
  "000001b9--2e9510764e",
  "000001bc--4a526eeff9",
  "000001c0--116fd8c7ce",
)
DEFAULT_EXPERIMENTAL_ROUTES = {
  "000001b9--2e9510764e",
  "000001bc--4a526eeff9",
  "000001c0--116fd8c7ce",
}
TUCSON_NN_MODEL_PATH = "/workspace/brickpilot/sunnypilot/neural_network_data/neural_network_lateral_control/HYUNDAI_TUCSON_4TH_GEN.json"
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
MAX_RATE_GAP_SEC = 0.55
CONTROLSD_EXTRA_INPUTS = {
  "carStateSP",
  "longitudinalPlanSP",
  "radarState",
  "selfdriveStateSP",
}


@dataclass
class SegmentCandidate:
  route_id: str
  segment_index: int
  path: Path
  logged_score: float
  logged_samples: int
  logged_active_samples: int
  logged_rate_p95_abs: float


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


def lateral_state(controls_state: Any) -> tuple[str, Any]:
  lcs = safe(controls_state, "lateralControlState")
  try:
    which = lcs.which()
  except Exception:
    return "", None
  return str(which), safe(lcs, str(which))


def read_messages(path: Path, slice_sec: float | None = None) -> list[Any]:
  messages = list(LogReader(str(path)))
  if slice_sec is None or slice_sec <= 0.0 or not messages:
    return messages
  start = int(messages[0].logMonoTime)
  end = start + int(slice_sec * 1e9)
  return [msg for msg in messages if int(msg.logMonoTime) <= end]


def controls_series(messages: list[Any], source: str, route_id: str, segment_index: int) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  mono0 = min((int(msg.logMonoTime) for msg in messages), default=0)
  for idx, msg in enumerate(messages):
    if msg.which() != "controlsState":
      continue
    typ, state = lateral_state(msg.controlsState)
    if typ != "torqueState":
      continue
    output = as_float(safe(state, "output"))
    if output is None:
      continue
    t_sec = (int(msg.logMonoTime) - mono0) / 1e9 if mono0 else 0.0
    rows.append({
      "source": source,
      "route_id": route_id,
      "segment_index": segment_index,
      "idx": len(rows),
      "t_sec": round(t_sec, 6),
      "active": bool(safe(state, "active")),
      "saturated": bool(safe(state, "saturated")),
      "torque_output": output,
      "desired_lateral_accel": as_float(safe(state, "desiredLateralAccel")),
      "actual_lateral_accel": as_float(safe(state, "actualLateralAccel")),
      "lateral_error": as_float(safe(state, "error")),
    })
  return rows


def car_control_series(messages: list[Any], source: str, route_id: str, segment_index: int) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  mono0 = min((int(msg.logMonoTime) for msg in messages), default=0)
  for msg in messages:
    if msg.which() != "carControl":
      continue
    cc = msg.carControl
    actuators = cc.actuators
    rows.append({
      "source": source,
      "route_id": route_id,
      "segment_index": segment_index,
      "idx": len(rows),
      "t_sec": round(((int(msg.logMonoTime) - mono0) / 1e9) if mono0 else 0.0, 6),
      "lat_active": bool(cc.latActive),
      "long_active": bool(cc.longActive),
      "actuator_torque": as_float(safe(actuators, "torque")),
      "actuator_accel": as_float(safe(actuators, "accel")),
      "actuator_curvature": as_float(safe(actuators, "curvature")),
    })
  return rows


def derivative(values: list[tuple[float, float]]) -> tuple[list[float], list[float], int]:
  rates: list[float] = []
  jerks: list[float] = []
  sign_changes = 0
  prev: tuple[float, float] | None = None
  prev_rate: float | None = None
  prev_sign = 0
  for t, value in values:
    sign = 1 if value > 1e-6 else -1 if value < -1e-6 else 0
    if prev is not None:
      dt = t - prev[0]
      if 0.0 < dt <= MAX_RATE_GAP_SEC:
        rate = (value - prev[1]) / dt
        rates.append(rate)
        if prev_rate is not None:
          jerks.append((rate - prev_rate) / dt)
        prev_rate = rate
        if sign and prev_sign and sign != prev_sign:
          sign_changes += 1
    if sign:
      prev_sign = sign
    prev = (t, value)
  return rates, jerks, sign_changes


def percentile(values: list[float], q: float) -> float:
  if not values:
    return 0.0
  ordered = sorted(values)
  idx = min(len(ordered) - 1, max(0, round((q / 100.0) * (len(ordered) - 1))))
  return ordered[idx]


def rms(values: list[float]) -> float:
  return math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0


def metric_summary(rows: list[dict[str, Any]], field: str = "torque_output", active_only: bool = True) -> dict[str, Any]:
  selected = [row for row in rows if (not active_only or row.get("active", row.get("lat_active", False)))]
  values = [(float(row["t_sec"]), float(row[field])) for row in selected if as_float(row.get(field)) is not None]
  raw_values = [value for _, value in values]
  rates, jerks, sign_changes = derivative(values)
  duration = 0.0
  if len(values) >= 2:
    duration = max(0.0, values[-1][0] - values[0][0])
  return {
    "samples": len(values),
    "duration_sec": round(duration, 3),
    "rms": round(rms(raw_values), 5),
    "p95_abs": round(percentile([abs(value) for value in raw_values], 95), 5),
    "max_abs": round(max((abs(value) for value in raw_values), default=0.0), 5),
    "rate_rms": round(rms(rates), 5),
    "rate_p95_abs": round(percentile([abs(value) for value in rates], 95), 5),
    "jerk_rms": round(rms(jerks), 5),
    "jerk_p95_abs": round(percentile([abs(value) for value in jerks], 95), 5),
    "sign_changes_per_min": round((sign_changes * 60.0 / duration) if duration > 1e-6 else 0.0, 5),
    "saturated_pct": round(100.0 * sum(1 for row in selected if row.get("saturated")) / max(1, len(selected)), 3),
  }


def paired_delta_metrics(logged: list[dict[str, Any]], replay: list[dict[str, Any]], field: str) -> dict[str, Any]:
  count = min(len(logged), len(replay))
  deltas: list[float] = []
  for idx in range(count):
    left = as_float(logged[idx].get(field))
    right = as_float(replay[idx].get(field))
    if left is None or right is None:
      continue
    deltas.append(right - left)
  return {
    f"{field}_paired_samples": len(deltas),
    f"{field}_delta_mean": round(statistics.mean(deltas), 6) if deltas else 0.0,
    f"{field}_delta_rms": round(rms(deltas), 6),
    f"{field}_delta_p95_abs": round(percentile([abs(value) for value in deltas], 95), 6),
    f"{field}_delta_max_abs": round(max((abs(value) for value in deltas), default=0.0), 6),
  }


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


def redact(text: str) -> str:
  return VIN_RE.sub("<VIN_REDACTED>", text or "")


def segment_paths(input_root: Path, route_id: str) -> list[tuple[int, Path]]:
  seg_root = input_root / route_id / "segments"
  out: list[tuple[int, Path]] = []
  for path in sorted(seg_root.glob(f"{route_id}--*/rlog.zst")):
    try:
      seg = int(path.parent.name.rsplit("--", 1)[-1])
    except ValueError:
      continue
    out.append((seg, path))
  return sorted(out)


def score_segment(route_id: str, seg: int, path: Path, slice_sec: float | None) -> SegmentCandidate:
  try:
    messages = read_messages(path, slice_sec=slice_sec)
    rows = controls_series(messages, "logged", route_id, seg)
    active = [row for row in rows if row.get("active")]
    metrics = metric_summary(rows)
    score = float(metrics["duration_sec"]) * (1.0 + float(metrics["rate_p95_abs"]))
    return SegmentCandidate(route_id, seg, path, score, len(rows), len(active), float(metrics["rate_p95_abs"]))
  except Exception:
    return SegmentCandidate(route_id, seg, path, -1.0, 0, 0, 0.0)


def select_segments(route_ids: list[str], input_root: Path, max_segments_per_route: int, slice_sec: float | None) -> list[SegmentCandidate]:
  selected: list[SegmentCandidate] = []
  for route_id in route_ids:
    scored = [score_segment(route_id, seg, path, slice_sec) for seg, path in segment_paths(input_root, route_id)]
    scored = [row for row in scored if row.logged_samples > 0 and row.logged_active_samples > 0]
    scored.sort(key=lambda row: (row.logged_score, row.logged_active_samples), reverse=True)
    selected.extend(scored[:max_segments_per_route])
  return sorted(selected, key=lambda row: (row.route_id, row.segment_index))


def replay_segment(candidate: SegmentCandidate, args: argparse.Namespace, out_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
  started = time.time()
  messages = read_messages(candidate.path, slice_sec=args.slice_sec)
  logged_controls = controls_series(messages, "logged", candidate.route_id, candidate.segment_index)
  logged_car_control = car_control_series(messages, "logged", candidate.route_id, candidate.segment_index)
  metric: dict[str, Any] = {
    "route_id": candidate.route_id,
    "segment_index": candidate.segment_index,
    "input_messages": len(messages),
    "logged_controls_samples": len(logged_controls),
    "logged_car_control_samples": len(logged_car_control),
    "experimental_mode": candidate.route_id in args.experimental_route,
  }
  try:
    car_params = next(msg.carParams for msg in messages if msg.which() == "carParams")
    car_params_sp = next(msg.carParamsSP.as_builder() for msg in messages if msg.which() == "carParamsSP")
    car_params_sp.neuralNetworkLateralControl.model.path = TUCSON_NN_MODEL_PATH
    car_params_sp.neuralNetworkLateralControl.model.name = "HYUNDAI_TUCSON_4TH_GEN"

    custom_params = get_custom_params_from_lr(messages)
    custom_params.update({
      "CarParams": car_params.as_builder().to_bytes(),
      "CarParamsSP": car_params_sp.to_bytes(),
      "OpenpilotEnabledToggle": True,
      "AlphaLongitudinalEnabled": True,
      "EnforceTorqueControl": True,
      "NeuralNetworkLateralControl": False,
      "ExperimentalMode": candidate.route_id in args.experimental_route,
      "DisengageOnAccelerator": True,
    })

    cfg = get_process_config("controlsd")
    cfg.init_callback = None
    cfg.pubs = sorted(set(cfg.pubs) | CONTROLSD_EXTRA_INPUTS)
    captured: dict[str, dict[str, str]] = {}
    replayed_messages = replay_process(cfg, messages, custom_params=custom_params, captured_output_store=captured, disable_progress=True)
    for proc, data in captured.items():
      for stream, text in data.items():
        if text:
          capture_dir = out_dir / "captured_output"
          capture_dir.mkdir(exist_ok=True)
          (capture_dir / f"{candidate.route_id}--{candidate.segment_index}_{proc}_{stream}.redacted.txt").write_text(redact(text), encoding="utf-8")

    replay_controls = controls_series(replayed_messages, "replay_0_4_4", candidate.route_id, candidate.segment_index)
    replay_car_control = car_control_series(replayed_messages, "replay_0_4_4", candidate.route_id, candidate.segment_index)
    logged_metrics = metric_summary(logged_controls)
    replay_metrics = metric_summary(replay_controls)
    logged_accel_metrics = metric_summary(logged_car_control, field="actuator_accel", active_only=False)
    replay_accel_metrics = metric_summary(replay_car_control, field="actuator_accel", active_only=False)
    metric.update({
      "ok": True,
      "elapsed_sec": round(time.time() - started, 3),
      "output_messages": len(replayed_messages),
      "output_counts": json.dumps(Counter(msg.which() for msg in replayed_messages), sort_keys=True),
      "replay_controls_samples": len(replay_controls),
      "replay_car_control_samples": len(replay_car_control),
      "logged_torque_rate_p95_abs": logged_metrics["rate_p95_abs"],
      "replay_torque_rate_p95_abs": replay_metrics["rate_p95_abs"],
      "torque_rate_p95_delta": round(float(replay_metrics["rate_p95_abs"]) - float(logged_metrics["rate_p95_abs"]), 5),
      "logged_torque_jerk_p95_abs": logged_metrics["jerk_p95_abs"],
      "replay_torque_jerk_p95_abs": replay_metrics["jerk_p95_abs"],
      "torque_jerk_p95_delta": round(float(replay_metrics["jerk_p95_abs"]) - float(logged_metrics["jerk_p95_abs"]), 5),
      "logged_sign_changes_per_min": logged_metrics["sign_changes_per_min"],
      "replay_sign_changes_per_min": replay_metrics["sign_changes_per_min"],
      "sign_changes_delta": round(float(replay_metrics["sign_changes_per_min"]) - float(logged_metrics["sign_changes_per_min"]), 5),
      "logged_saturated_pct": logged_metrics["saturated_pct"],
      "replay_saturated_pct": replay_metrics["saturated_pct"],
      "logged_accel_rate_p95_abs": logged_accel_metrics["rate_p95_abs"],
      "replay_accel_rate_p95_abs": replay_accel_metrics["rate_p95_abs"],
    })
    metric.update(paired_delta_metrics(logged_controls, replay_controls, "torque_output"))
    metric.update(paired_delta_metrics(logged_controls, replay_controls, "desired_lateral_accel"))
    metric.update(paired_delta_metrics(logged_car_control, replay_car_control, "actuator_accel"))
    return metric, logged_controls + replay_controls, logged_car_control + replay_car_control
  except Exception as exc:
    metric.update({
      "ok": False,
      "elapsed_sec": round(time.time() - started, 3),
      "error": repr(exc),
      "traceback": traceback.format_exc(),
    })
    return metric, logged_controls, logged_car_control


def aggregate_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  ok_rows = [row for row in rows if row.get("ok")]
  if not ok_rows:
    return []
  numeric_fields = [
    "logged_torque_rate_p95_abs",
    "replay_torque_rate_p95_abs",
    "torque_rate_p95_delta",
    "logged_torque_jerk_p95_abs",
    "replay_torque_jerk_p95_abs",
    "torque_jerk_p95_delta",
    "logged_sign_changes_per_min",
    "replay_sign_changes_per_min",
    "sign_changes_delta",
    "torque_output_delta_rms",
    "torque_output_delta_p95_abs",
    "desired_lateral_accel_delta_rms",
    "actuator_accel_delta_rms",
  ]
  aggregate: dict[str, Any] = {
    "segments": len(ok_rows),
    "routes": len({row["route_id"] for row in ok_rows}),
    "input_messages": sum(int(row.get("input_messages") or 0) for row in ok_rows),
    "elapsed_sec": round(sum(float(row.get("elapsed_sec") or 0.0) for row in ok_rows), 3),
  }
  for field in numeric_fields:
    values = [float(row[field]) for row in ok_rows if as_float(row.get(field)) is not None]
    aggregate[f"{field}_mean"] = round(statistics.mean(values), 6) if values else 0.0
    aggregate[f"{field}_median"] = round(statistics.median(values), 6) if values else 0.0
  return [aggregate]


def route_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  route_ids = sorted({str(row["route_id"]) for row in rows if row.get("ok")})
  numeric_fields = [
    "logged_torque_rate_p95_abs",
    "replay_torque_rate_p95_abs",
    "torque_rate_p95_delta",
    "torque_jerk_p95_delta",
    "sign_changes_delta",
    "torque_output_delta_rms",
    "desired_lateral_accel_delta_rms",
    "actuator_accel_delta_rms",
  ]
  for route_id in route_ids:
    route_rows = [row for row in rows if row.get("ok") and str(row["route_id"]) == route_id]
    route_out: dict[str, Any] = {
      "route_id": route_id,
      "segments": len(route_rows),
      "experimental_mode": bool(route_rows[0].get("experimental_mode")) if route_rows else False,
    }
    for field in numeric_fields:
      values = [float(row[field]) for row in route_rows if as_float(row.get(field)) is not None]
      route_out[f"{field}_mean"] = round(statistics.mean(values), 6) if values else 0.0
      route_out[f"{field}_median"] = round(statistics.median(values), 6) if values else 0.0
    out.append(route_out)
  return out


def svg_bar(path: Path, title: str, labels: list[str], values: list[float], width: int = 1120, height: int = 520) -> None:
  height = max(height, 86 + 30 * len(labels))
  max_value = max((abs(value) for value in values), default=1.0)
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#081018'/>",
    f"<text x='24' y='38' fill='#e8f2ff' font-family='Arial' font-size='22' font-weight='700'>{html.escape(title)}</text>",
  ]
  zero_x = 530
  lines.append(f"<line x1='{zero_x}' y1='58' x2='{zero_x}' y2='{height - 24}' stroke='#355167' stroke-width='1'/>")
  for idx, (label, value) in enumerate(zip(labels, values)):
    y = 70 + idx * 30
    bar = 0.0 if max_value <= 0.0 else abs(value) / max_value * 430.0
    color = "#6ee7a8" if value <= 0 else "#ff8a7a"
    x = zero_x - bar if value < 0 else zero_x
    text_x = x - 70 if value < 0 else x + bar + 10
    lines.append(f"<text x='24' y='{y + 15}' fill='#d7e7f7' font-family='Arial' font-size='12'>{html.escape(label[:58])}</text>")
    lines.append(f"<rect x='{x:.1f}' y='{y}' width='{bar:.1f}' height='18' rx='3' fill='{color}'/>")
    lines.append(f"<text x='{text_x:.1f}' y='{y + 14}' fill='#d7e7f7' font-family='Arial' font-size='12'>{value:.4f}</text>")
  lines.append("</svg>\n")
  path.write_text("\n".join(lines), encoding="utf-8")


def write_report(path: Path, rows: list[dict[str, Any]], aggregate: list[dict[str, Any]],
                 routes: list[dict[str, Any]], args: argparse.Namespace) -> None:
  ok_rows = [row for row in rows if row.get("ok")]
  fail_rows = [row for row in rows if not row.get("ok")]
  agg = aggregate[0] if aggregate else {}
  worst_rate = sorted(ok_rows, key=lambda row: float(row.get("torque_rate_p95_delta") or 0.0), reverse=True)[:5]
  best_rate = sorted(ok_rows, key=lambda row: float(row.get("torque_rate_p95_delta") or 0.0))[:5]
  lines = [
    "# Brickpilot VM controlsd Process Replay Sweep",
    "",
    f"Generated: `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
    "",
    "This run used the Linux `openpilot-arm64` Docker VM image, not the native macOS log proxy path. It executed real `controlsd` process replay against local rlogs.",
    "",
    "## Replay Setup",
    "",
    f"- Brickpilot repo inside VM: `/workspace/brickpilot`",
    f"- Input root: `{args.input_root}`",
    f"- Routes requested: {len(args.route_id)}",
    f"- Segments replayed successfully: {len(ok_rows)}",
    f"- Failed segments: {len(fail_rows)}",
    f"- Max segments per route: {args.max_segments_per_route}",
    f"- Slice seconds: `{args.slice_sec or 'full segment'}`",
    "- Replay fix applied: route `CarParamsSP.neuralNetworkLateralControl.model.path` was rewritten from the on-device `/data/openpilot/...` path to the VM-mounted `/workspace/brickpilot/...` path.",
    f"- Replay config patch applied: added current Brickpilot sunnypilot inputs `{', '.join(sorted(CONTROLSD_EXTRA_INPUTS))}` to the stock process-replay `controlsd` config.",
    "- Route settings applied for this pass: `EnforceTorqueControl=true`, `NeuralNetworkLateralControl=false`, `AlphaLongitudinalEnabled=true`; experimental mode follows the supplied route list.",
    "",
    "## Aggregate",
    "",
  ]
  if agg:
    lines.extend([
      f"- Replayed routes: {agg.get('routes')}",
      f"- Replayed segments: {agg.get('segments')}",
      f"- Input messages processed: {agg.get('input_messages')}",
      f"- Total replay wall time: {agg.get('elapsed_sec')} sec",
      f"- Mean logged torque-rate p95: {agg.get('logged_torque_rate_p95_abs_mean')}",
      f"- Mean replay torque-rate p95: {agg.get('replay_torque_rate_p95_abs_mean')}",
      f"- Mean replay-minus-logged torque-rate p95 delta: {agg.get('torque_rate_p95_delta_mean')}",
      f"- Mean torque output delta RMS: {agg.get('torque_output_delta_rms_mean')}",
      f"- Mean desired lateral accel delta RMS: {agg.get('desired_lateral_accel_delta_rms_mean')}",
      f"- Mean actuator accel delta RMS: {agg.get('actuator_accel_delta_rms_mean')}",
    ])
  lines.extend([
    "",
    "## Route Read",
    "",
    "| Route | Segments | Exp | Rate delta mean | Jerk delta mean | Sign-change delta mean | Torque delta RMS mean | Desired lat accel delta RMS mean |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
  ])
  for row in routes:
    lines.append(
      f"| `{row['route_id'][:8]}` | {row['segments']} | {int(bool(row['experimental_mode']))} | "
      f"{row['torque_rate_p95_delta_mean']} | {row['torque_jerk_p95_delta_mean']} | "
      f"{row['sign_changes_delta_mean']} | {row['torque_output_delta_rms_mean']} | "
      f"{row['desired_lateral_accel_delta_rms_mean']} |"
    )
  lines.extend([
    "",
    "## Segment Results",
    "",
    "| Route | Seg | Exp | Logged rate p95 | Replay rate p95 | Delta | Torque delta RMS | Desired lat accel delta RMS | Accel delta RMS |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
  ])
  for row in ok_rows:
    lines.append(
      f"| `{row['route_id'][:8]}` | {row['segment_index']} | {int(bool(row['experimental_mode']))} | "
      f"{row['logged_torque_rate_p95_abs']} | {row['replay_torque_rate_p95_abs']} | {row['torque_rate_p95_delta']} | "
      f"{row.get('torque_output_delta_rms')} | {row.get('desired_lateral_accel_delta_rms')} | {row.get('actuator_accel_delta_rms')} |"
    )
  if worst_rate:
    lines.extend(["", "## Highest replay rate increases", ""])
    for row in worst_rate:
      lines.append(f"- `{row['route_id']}--{row['segment_index']}` delta `{row['torque_rate_p95_delta']}`, torque delta RMS `{row.get('torque_output_delta_rms')}`")
  if best_rate:
    lines.extend(["", "## Highest replay rate reductions", ""])
    for row in best_rate:
      lines.append(f"- `{row['route_id']}--{row['segment_index']}` delta `{row['torque_rate_p95_delta']}`, torque delta RMS `{row.get('torque_output_delta_rms')}`")
  lines.extend([
    "",
    "## Read",
    "",
    "- A negative torque-rate delta means current staging `controlsd` replayed smoother than the originally logged route segment on the same input stream. A positive delta means the current replay is more abrupt in command space.",
    "- Torque output deltas are expected because this compares current staging code to prior route logs. The useful signal is whether those deltas reduce command rate/jerk without large desired-lateral-accel or actuator-accel movement.",
    "- This is real process replay now. It still does not prove road feel by itself because model outputs, vehicle response, and safety hooks are replay inputs/outputs rather than the physical car.",
    "",
    "## Files",
    "",
    "- `segment_metrics.csv`",
    "- `aggregate_metrics.csv`",
    "- `route_metrics.csv`",
    "- `controlsd_series.csv`",
    "- `car_control_series.csv`",
    "- `run_manifest.json`",
    "- `torque_rate_delta_chart.svg`",
  ])
  if fail_rows:
    lines.extend(["", "## Failures", ""])
    for row in fail_rows:
      lines.append(f"- `{row['route_id']}--{row['segment_index']}`: `{row.get('error')}`")
  path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--route-id", action="append", default=[])
  parser.add_argument("--experimental-route", action="append", default=[])
  parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--out", type=Path, default=None)
  parser.add_argument("--max-segments-per-route", type=int, default=2)
  parser.add_argument("--slice-sec", type=float, default=0.0)
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  route_ids = list(dict.fromkeys(args.route_id or list(DEFAULT_ROUTE_IDS)))
  args.route_id = route_ids
  args.experimental_route = set(args.experimental_route or list(DEFAULT_EXPERIMENTAL_ROUTES))
  timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  out_dir = args.out or args.output_root / f"vm_controlsd_replay_steering_{timestamp}"
  out_dir.mkdir(parents=True, exist_ok=True)

  selected = select_segments(route_ids, args.input_root, args.max_segments_per_route, args.slice_sec)
  metrics: list[dict[str, Any]] = []
  control_rows: list[dict[str, Any]] = []
  car_control_rows: list[dict[str, Any]] = []
  for idx, candidate in enumerate(selected, start=1):
    print(f"[{idx}/{len(selected)}] replay {candidate.route_id}--{candidate.segment_index}", flush=True)
    metric, controls, car_controls = replay_segment(candidate, args, out_dir)
    metrics.append({key: value for key, value in metric.items() if key != "traceback"})
    if not metric.get("ok"):
      failure_dir = out_dir / "failures"
      failure_dir.mkdir(exist_ok=True)
      (failure_dir / f"{candidate.route_id}--{candidate.segment_index}.traceback.txt").write_text(
        redact(str(metric.get("traceback") or "")), encoding="utf-8"
      )
    control_rows.extend(controls)
    car_control_rows.extend(car_controls)

  aggregate = aggregate_metrics(metrics)
  routes = route_metrics(metrics)
  write_csv(out_dir / "segment_metrics.csv", metrics)
  write_csv(out_dir / "aggregate_metrics.csv", aggregate)
  write_csv(out_dir / "route_metrics.csv", routes)
  write_csv(out_dir / "controlsd_series.csv", control_rows)
  write_csv(out_dir / "car_control_series.csv", car_control_rows)
  labels = [f"{row['route_id'][:8]}--{row['segment_index']}" for row in metrics if row.get("ok")]
  values = [float(row.get("torque_rate_p95_delta") or 0.0) for row in metrics if row.get("ok")]
  svg_bar(out_dir / "torque_rate_delta_chart.svg", "VM process replay torque-rate p95 delta", labels, values)
  manifest = {
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "input_root": str(args.input_root),
    "output_dir": str(out_dir),
    "route_ids": route_ids,
    "experimental_routes": sorted(args.experimental_route),
    "max_segments_per_route": args.max_segments_per_route,
    "slice_sec": args.slice_sec,
    "selected_segments": [candidate.__dict__ | {"path": str(candidate.path)} for candidate in selected],
    "ok_segments": sum(1 for row in metrics if row.get("ok")),
    "failed_segments": sum(1 for row in metrics if not row.get("ok")),
  }
  (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
  write_report(out_dir / "report.md", metrics, aggregate, routes, args)
  latest = args.output_root / "vm_controlsd_replay_steering_latest"
  if latest.exists() or latest.is_symlink():
    latest.unlink()
  latest_target = out_dir.name if out_dir.parent == args.output_root else out_dir
  latest.symlink_to(latest_target, target_is_directory=True)
  print(out_dir)
  if aggregate:
    print(json.dumps(aggregate[0], indent=2, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
