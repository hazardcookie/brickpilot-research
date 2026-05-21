#!/usr/bin/env python3
"""Offline Brickpilot longitudinal policy sweep over prelim route exports."""

from __future__ import annotations

import argparse
import csv
import html
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
DEFAULT_ROUTE_IDS = (
  "000001ac--e1e3616975",
  "000001b0--90d579ad38",
  "000001b4--6e74738cbb",
  "000001b7--5f24a935ac",
  "000001b9--2e9510764e",
  "000001bc--4a526eeff9",
  "000001c0--116fd8c7ce",
)
MPH_TO_MS = 0.44704


SUPPRESSORS = {
  0: "NOT_TUCSON_CANFD",
  1: "INVALID",
  2: "LONG_INACTIVE",
  3: "PLANNER_NOT_POSITIVE",
  4: "ALLOW_THROTTLE_FALSE",
  5: "SHOULD_STOP",
  6: "STOP_CREEP_BAND",
  7: "DRIVER_OVERRIDE",
  8: "STEERING_OVERRIDE",
  9: "LEAD_PRESENT_OR_LIMITING",
  10: "HIGH_LATERAL_DEMAND",
  11: "NO_CATCHUP_DEMAND",
  12: "ACCEL_LAG_TOO_SMALL",
  13: "FCW_OR_MODEL_BRAKE",
  14: "NOT_CRUISE_SOURCE",
  15: "OPENPILOT_LONG_REQUIRED",
  16: "RADAR_MODEL_MISMATCH",
  17: "SUNNYPILOT_PLAN_INVALID",
  18: "DEC_OR_SCC_ACTIVE",
  19: "HIGH_PREDICTED_LATERAL_DEMAND",
  20: "PHEV_REGEN_OR_BRAKE",
  21: "PHEV_STATIONARY_OR_AUTO_HOLD",
}

SOFT_GATE_BITS = {
  3,   # PLANNER_NOT_POSITIVE
  11,  # NO_CATCHUP_DEMAND
  12,  # ACCEL_LAG_TOO_SMALL
}

NONCRUISE_BIT = 14
LATERAL_BIT = 10
BASE_HARD_BITS = set(SUPPRESSORS) - SOFT_GATE_BITS - {NONCRUISE_BIT, LATERAL_BIT}
SAMPLE_DT_SEC = 0.10

GOOD_LABEL_WEIGHTS = {
  "accel_too_lazy": 2.2,
  "accel_target_gap": 2.4,
  "human_intervention_gas": 0.9,
  "speed_target_gap": 1.6,
  "setting_speed": 0.35,
}
BAD_LABEL_WEIGHTS = {
  "human_intervention_brake": 4.0,
  "braking_bad": 3.4,
  "brake_hard": 2.2,
  "phev_regen_hard": 1.8,
  "phev_regen_coast": 1.4,
  "stop_late": 3.2,
  "manual_takeover": 3.0,
}


@dataclass(frozen=True)
class Candidate:
  name: str
  description: str
  min_deficit_mph: float
  min_speed_mph: float
  min_accel_lag: float
  delta_cap: float
  planner_floor_accel: float
  max_lateral: float
  soft_curve_lateral: float
  curve_decay: float
  deficit_coeff: float
  lag_coeff: float
  ramp_min_speed_mph: float
  ramp_min_deficit_mph: float
  ramp_bonus: float
  ramp_delta_cap: float
  allow_noncruise: bool = False
  noncruise_min_deficit_mph: float = 6.0
  noncruise_min_speed_mph: float = 18.0
  noncruise_delta_cap: float = 0.70
  soften_lateral_bit: bool = False
  installable: bool = True


@dataclass
class Sample:
  route_id: str
  route_label: str
  t: float
  suppressors: int
  speed_deficit: float
  accel_lag: float
  lateral: float
  a_target: float
  active_actual: bool
  delta_actual: float
  long_active: bool
  has_lead: bool
  lazy_candidate: bool
  labels_good: float = 0.0
  labels_bad: float = 0.0


def parse_float(value: str | None, default: float = 0.0) -> float:
  try:
    ret = float(value or "")
  except ValueError:
    return default
  if not math.isfinite(ret):
    return default
  return ret


def parse_bool(value: str | None) -> bool:
  return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
  if not path.exists():
    return []
  with path.open(newline="") as f:
    return list(csv.DictReader(f))


def latest_prelim_dirs(route_ids: tuple[str, ...] | list[str], root: Path) -> list[Path]:
  dirs: list[Path] = []
  for route_id in route_ids:
    matches = sorted(root.glob(f"prelim_*_{route_id}_*"))
    if matches:
      dirs.append(matches[-1])
  return dirs


def route_id_from_dir(path: Path) -> str:
  parts = path.name.split("_")
  for part in parts:
    if "--" in part:
      return part
  return path.name


def route_label_from_dir(path: Path) -> str:
  route_id = route_id_from_dir(path)
  prefix = path.name.split(route_id)[0].strip("_")
  return prefix.replace("prelim_", "").replace("_", " ") or route_id


def interval_label_weights(prediction_rows: list[dict[str, str]]) -> list[tuple[float, float, float, float]]:
  intervals: list[tuple[float, float, float, float]] = []
  for row in prediction_rows:
    target = (row.get("target") or "").strip()
    start = parse_float(row.get("start_sec"), -1.0)
    end = parse_float(row.get("end_sec"), -1.0)
    score = max(0.0, min(1.0, parse_float(row.get("peak_score"), 0.0)))
    if start < 0.0 or end <= start or not target:
      continue
    good = GOOD_LABEL_WEIGHTS.get(target, 0.0) * score
    bad = BAD_LABEL_WEIGHTS.get(target, 0.0) * score
    if good or bad:
      intervals.append((start, end, good, bad))
  return intervals


def attach_label_weights(samples: list[Sample], intervals: list[tuple[float, float, float, float]]) -> None:
  if not intervals:
    return
  intervals = sorted(intervals, key=lambda row: row[0])
  active: list[tuple[float, float, float, float]] = []
  idx = 0
  for sample in sorted(samples, key=lambda s: s.t):
    while idx < len(intervals) and intervals[idx][0] <= sample.t:
      active.append(intervals[idx])
      idx += 1
    active = [interval for interval in active if interval[1] >= sample.t]
    for start, end, good, bad in active:
      if start <= sample.t <= end:
        sample.labels_good += good
        sample.labels_bad += bad


def read_samples(export_dir: Path) -> list[Sample]:
  route_id = route_id_from_dir(export_dir)
  route_label = route_label_from_dir(export_dir)
  samples: list[Sample] = []
  for row in read_csv_rows(export_dir / "brickpilot_shadow_samples.csv"):
    t = parse_float(row.get("route_time_sec"), -1.0)
    if t < 0.0:
      continue
    samples.append(Sample(
      route_id=route_id,
      route_label=route_label,
      t=t,
      suppressors=int(parse_float(row.get("longitudinalAssistSuppressors"), 0.0)),
      speed_deficit=max(0.0, parse_float(row.get("speedDeficit"), 0.0)),
      accel_lag=max(0.0, parse_float(row.get("longitudinalAccelLag"), 0.0)),
      lateral=abs(parse_float(row.get("longitudinalLateralDemand"), 0.0)),
      a_target=parse_float(row.get("longitudinalAssistATarget"), 0.0),
      active_actual=parse_bool(row.get("longitudinalAssistActive")),
      delta_actual=max(0.0, parse_float(row.get("longitudinalAssistDelta"), 0.0)),
      long_active=parse_bool(row.get("longActive")),
      has_lead=parse_bool(row.get("hasLead")),
      lazy_candidate=parse_bool(row.get("lazyCandidate")),
    ))
  attach_label_weights(samples, interval_label_weights(read_csv_rows(export_dir / "test_route_predictions.csv")))
  return samples


def has_bit(mask: int, bit: int) -> bool:
  return bool(mask & (1 << bit))


def hard_suppressor_bits(candidate: Candidate, sample: Sample, speed_deficit_mph: float) -> set[int]:
  hard = set(BASE_HARD_BITS)
  if not candidate.allow_noncruise or speed_deficit_mph < candidate.noncruise_min_deficit_mph:
    hard.add(NONCRUISE_BIT)
  if not candidate.soften_lateral_bit or sample.lateral > candidate.max_lateral:
    hard.add(LATERAL_BIT)
  return {bit for bit in hard if has_bit(sample.suppressors, bit)}


def candidate_delta(candidate: Candidate, sample: Sample) -> tuple[bool, float, bool, set[int]]:
  speed_deficit_mph = sample.speed_deficit / MPH_TO_MS
  speed_gate_mph = candidate.noncruise_min_speed_mph if has_bit(sample.suppressors, NONCRUISE_BIT) else candidate.min_speed_mph
  hard_bits = hard_suppressor_bits(candidate, sample, speed_deficit_mph)
  if hard_bits or speed_deficit_mph < candidate.min_deficit_mph or sample.lateral > candidate.max_lateral:
    return False, 0.0, False, hard_bits
  if has_bit(sample.suppressors, NONCRUISE_BIT):
    if not candidate.allow_noncruise or speed_deficit_mph < candidate.noncruise_min_deficit_mph:
      return False, 0.0, False, hard_bits
    active_cap = min(candidate.delta_cap, candidate.noncruise_delta_cap)
  else:
    active_cap = candidate.delta_cap
  if sample.speed_deficit < candidate.min_deficit_mph * MPH_TO_MS:
    return False, 0.0, False, hard_bits
  # The qlog shadow samples do not include vEgo directly. Use route-level speed
  # deficit plus longActive/suppressor shape as a policy proxy, and keep the
  # implementation-side vEgo gate as the source of truth.
  if not sample.long_active and has_bit(sample.suppressors, 2):
    return False, 0.0, False, hard_bits
  planner_floor = candidate.planner_floor_accel if sample.a_target < 0.25 and speed_deficit_mph >= candidate.min_deficit_mph + 1.0 else sample.a_target
  effective_lag = max(sample.accel_lag, planner_floor - sample.a_target)
  ramp = bool(speed_deficit_mph >= candidate.ramp_min_deficit_mph)
  cap = min(candidate.ramp_delta_cap if ramp else active_cap, active_cap if not ramp else candidate.ramp_delta_cap)
  deficit_over = max(0.0, speed_deficit_mph - candidate.min_deficit_mph)
  lag_over = max(0.0, effective_lag - candidate.min_accel_lag)
  delta = max(0.145,
              candidate.deficit_coeff * deficit_over +
              candidate.lag_coeff * min(lag_over, 1.0) +
              (candidate.ramp_bonus if ramp else 0.0))
  delta = min(cap, delta)
  if sample.lateral > candidate.soft_curve_lateral:
    delta *= candidate.curve_decay
  if has_bit(sample.suppressors, NONCRUISE_BIT):
    delta = min(delta, candidate.noncruise_delta_cap)
  return delta > 0.0, delta, ramp, hard_bits


def score_candidates(samples: list[Sample], candidates: list[Candidate]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  rows: list[dict[str, Any]] = []
  route_rows: list[dict[str, Any]] = []
  route_ids = sorted({sample.route_id for sample in samples})
  for candidate in candidates:
    totals = {
      "samples": len(samples),
      "active": 0,
      "ramp": 0,
      "noncruise_active": 0,
      "delta_sum": 0.0,
      "delta_max": 0.0,
      "good_seconds": 0.0,
      "bad_seconds": 0.0,
      "deficit_weight": 0.0,
      "hard_overlap": 0,
    }
    per_route: dict[str, dict[str, Any]] = {route_id: dict(totals, samples=0, route_label="") for route_id in route_ids}
    for sample in samples:
      active, delta, ramp, hard_bits = candidate_delta(candidate, sample)
      route_totals = per_route[sample.route_id]
      route_totals["samples"] += 1
      route_totals["route_label"] = sample.route_label
      if hard_bits:
        totals["hard_overlap"] += 1
        route_totals["hard_overlap"] += 1
      if not active:
        continue
      speed_deficit_mph = sample.speed_deficit / MPH_TO_MS
      good_weight = 1.0 + sample.labels_good + (0.7 if sample.lazy_candidate else 0.0) + max(0.0, speed_deficit_mph - 2.0) * 0.08
      bad_weight = sample.labels_bad + (1.0 if has_bit(sample.suppressors, 20) else 0.0) + (1.0 if has_bit(sample.suppressors, 7) else 0.0)
      for target in (totals, route_totals):
        target["active"] += 1
        target["ramp"] += int(ramp)
        target["noncruise_active"] += int(has_bit(sample.suppressors, NONCRUISE_BIT))
        target["delta_sum"] += delta
        target["delta_max"] = max(target["delta_max"], delta)
        target["good_seconds"] += good_weight * SAMPLE_DT_SEC
        target["bad_seconds"] += bad_weight * SAMPLE_DT_SEC
        target["deficit_weight"] += max(0.0, speed_deficit_mph - 2.0) * delta * SAMPLE_DT_SEC

    active_frac = totals["active"] / totals["samples"] if totals["samples"] else 0.0
    avg_delta = totals["delta_sum"] / totals["active"] if totals["active"] else 0.0
    score = totals["good_seconds"] + totals["deficit_weight"] - totals["bad_seconds"] * 2.7 - active_frac * 8.0
    rows.append({
      "candidate": candidate.name,
      "installable": candidate.installable,
      "description": candidate.description,
      "score": round(score, 3),
      "active_frac": round(active_frac, 4),
      "active_sec": round(totals["active"] * SAMPLE_DT_SEC, 1),
      "noncruise_active_sec": round(totals["noncruise_active"] * SAMPLE_DT_SEC, 1),
      "ramp_active_sec": round(totals["ramp"] * SAMPLE_DT_SEC, 1),
      "avg_delta": round(avg_delta, 3),
      "max_delta": round(totals["delta_max"], 3),
      "good_weight_sec": round(totals["good_seconds"], 1),
      "bad_weight_sec": round(totals["bad_seconds"], 1),
      "deficit_weight": round(totals["deficit_weight"], 1),
      "samples": totals["samples"],
    })
    for route_id, route_totals in per_route.items():
      samples_count = route_totals["samples"]
      route_active_frac = route_totals["active"] / samples_count if samples_count else 0.0
      route_avg_delta = route_totals["delta_sum"] / route_totals["active"] if route_totals["active"] else 0.0
      route_score = route_totals["good_seconds"] + route_totals["deficit_weight"] - route_totals["bad_seconds"] * 2.7 - route_active_frac * 2.0
      route_rows.append({
        "candidate": candidate.name,
        "installable": candidate.installable,
        "route_id": route_id,
        "route_label": route_totals["route_label"],
        "score": round(route_score, 3),
        "active_frac": round(route_active_frac, 4),
        "active_sec": round(route_totals["active"] * SAMPLE_DT_SEC, 1),
        "noncruise_active_sec": round(route_totals["noncruise_active"] * SAMPLE_DT_SEC, 1),
        "ramp_active_sec": round(route_totals["ramp"] * SAMPLE_DT_SEC, 1),
        "avg_delta": round(route_avg_delta, 3),
        "max_delta": round(route_totals["delta_max"], 3),
        "good_weight_sec": round(route_totals["good_seconds"], 1),
        "bad_weight_sec": round(route_totals["bad_seconds"], 1),
        "deficit_weight": round(route_totals["deficit_weight"], 1),
        "samples": samples_count,
      })
  rows.sort(key=lambda row: float(row["score"]), reverse=True)
  route_rows.sort(key=lambda row: (str(row["candidate"]), str(row["route_id"])))
  return rows, route_rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
  if not rows:
    path.write_text("")
    return
  with path.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


def svg_bar_chart(path: Path, rows: list[dict[str, Any]], title: str, key: str) -> None:
  top = rows[:10]
  width = 1100
  height = max(280, 80 + 44 * len(top))
  max_val = max((abs(float(row[key])) for row in top), default=1.0)
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#071019'/>",
    f"<text x='26' y='34' fill='#e8f2ff' font-family='Arial' font-size='22' font-weight='700'>{html.escape(title)}</text>",
  ]
  for i, row in enumerate(top):
    y = 70 + i * 44
    val = float(row[key])
    bar = 0 if max_val <= 0 else abs(val) / max_val * 650
    color = "#66d9a8" if val >= 0 else "#ff7676"
    lines.append(f"<text x='26' y='{y + 17}' fill='#d7e7f7' font-family='Arial' font-size='13'>{html.escape(str(row['candidate']))}</text>")
    lines.append(f"<rect x='330' y='{y}' width='{bar:.1f}' height='24' rx='4' fill='{color}'/>")
    lines.append(f"<text x='{342 + bar:.1f}' y='{y + 17}' fill='#d7e7f7' font-family='Arial' font-size='13'>{val:.2f}</text>")
  lines.append("</svg>\n")
  path.write_text("\n".join(lines))


def write_report(path: Path, rows: list[dict[str, Any]], route_rows: list[dict[str, Any]], export_dirs: list[Path]) -> None:
  top = rows[:5]
  installable_top = next((row for row in rows if str(row.get("installable")).lower() == "true"), None)
  lines = [
    "# Brickpilot Longitudinal Candidate Sweep",
    "",
    f"Generated: `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
    "",
    "This is an offline qlog/shadow-sample policy sweep. It is not a physics replay, but it exercises the same suppressor, speed-deficit, accel-lag, and label-proximity signals across the logged 0.4.x routes so candidates can be ranked before touching the on-car build.",
    "",
    "## Routes",
  ]
  lines.extend(f"- `{route_id_from_dir(path)}` from `{path.name}`" for path in export_dirs)
  lines.extend(["", "## Top Candidates", "", "| Candidate | Installable | Score | Active | Non-cruise active | Ramp active | Avg delta | Max delta | Bad weight |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
  for row in top:
    lines.append(
      f"| `{row['candidate']}` | {row['installable']} | {row['score']} | {float(row['active_frac']) * 100:.1f}% | "
      f"{row['noncruise_active_sec']}s | {row['ramp_active_sec']}s | {row['avg_delta']} | {row['max_delta']} | {row['bad_weight_sec']} |"
    )
  lines.extend(["", "## Read", ""])
  if installable_top:
    winner = installable_top
    lines.append(f"- Best installable policy: `{winner['candidate']}`. It finds {winner['active_sec']}s of candidate assist, with {winner['noncruise_active_sec']}s in non-cruise/experimental-source contexts and a max simulated delta of {winner['max_delta']}.")
  if top and str(top[0].get("installable")).lower() != "true":
    lines.append(f"- `{top[0]['candidate']}` is retained as a ceiling/upper-bound policy, not a direct build candidate.")
  lines.append("- The score rewards active overlap with lazy/target-gap labels and large speed deficit, then penalizes brake, hard-regen, and human-brake label overlap.")
  lines.append("- Hard vetoes remain hard in the promoted candidates: invalid vehicle, long inactive, stop/creep, driver override, lead, FCW/model brake, radar/model mismatch, DEC/SCC/map turning risk, PHEV regen/brake, and auto-hold/stationary.")
  lines.extend(["", "## Candidate Descriptions", ""])
  for row in rows:
    lines.append(f"- `{row['candidate']}`: {row['description']}")
  lines.extend(["", "## Files", "", "- `candidate_scores.csv`", "- `route_candidate_summary.csv`", "- `candidate_score_chart.svg`", ""])
  path.write_text("\n".join(lines))


def candidate_set() -> list[Candidate]:
  return [
    Candidate(
      name="044_cruise_wide_floor",
      description="No-exp cruise catch-up with low lag gate, lower deficit threshold, planner floor, and wider lateral allowance.",
      min_deficit_mph=2.0,
      min_speed_mph=7.0,
      min_accel_lag=0.05,
      delta_cap=0.95,
      planner_floor_accel=0.70,
      max_lateral=0.95,
      soft_curve_lateral=0.55,
      curve_decay=0.86,
      deficit_coeff=0.040,
      lag_coeff=0.210,
      ramp_min_speed_mph=25.0,
      ramp_min_deficit_mph=4.0,
      ramp_bonus=0.22,
      ramp_delta_cap=1.05,
      soften_lateral_bit=True,
    ),
    Candidate(
      name="044_cruise_ramp_plus",
      description="Aggressive no-exp route: 4 mph ramp trigger, 1.08 cap, larger deficit coefficient.",
      min_deficit_mph=2.0,
      min_speed_mph=7.0,
      min_accel_lag=0.05,
      delta_cap=0.98,
      planner_floor_accel=0.72,
      max_lateral=0.88,
      soft_curve_lateral=0.50,
      curve_decay=0.84,
      deficit_coeff=0.046,
      lag_coeff=0.220,
      ramp_min_speed_mph=24.0,
      ramp_min_deficit_mph=4.0,
      ramp_bonus=0.26,
      ramp_delta_cap=1.08,
      soften_lateral_bit=True,
    ),
    Candidate(
      name="044_exp_bridge_clean",
      description="Adds an experimental-source catch-up bridge only when speed deficit is large and hard vetoes are clear.",
      min_deficit_mph=2.5,
      min_speed_mph=7.0,
      min_accel_lag=0.05,
      delta_cap=0.92,
      planner_floor_accel=0.68,
      max_lateral=0.80,
      soft_curve_lateral=0.48,
      curve_decay=0.82,
      deficit_coeff=0.038,
      lag_coeff=0.200,
      ramp_min_speed_mph=25.0,
      ramp_min_deficit_mph=5.0,
      ramp_bonus=0.18,
      ramp_delta_cap=1.00,
      allow_noncruise=True,
      noncruise_min_deficit_mph=6.0,
      noncruise_min_speed_mph=18.0,
      noncruise_delta_cap=0.70,
      soften_lateral_bit=True,
    ),
    Candidate(
      name="044_exp_bridge_strong",
      description="Stronger experimental-source bridge for high-deficit merges, with non-cruise cap at 0.82.",
      min_deficit_mph=2.0,
      min_speed_mph=7.0,
      min_accel_lag=0.05,
      delta_cap=0.98,
      planner_floor_accel=0.72,
      max_lateral=0.90,
      soft_curve_lateral=0.55,
      curve_decay=0.84,
      deficit_coeff=0.043,
      lag_coeff=0.220,
      ramp_min_speed_mph=24.0,
      ramp_min_deficit_mph=4.5,
      ramp_bonus=0.23,
      ramp_delta_cap=1.08,
      allow_noncruise=True,
      noncruise_min_deficit_mph=5.0,
      noncruise_min_speed_mph=16.0,
      noncruise_delta_cap=0.82,
      soften_lateral_bit=True,
    ),
    Candidate(
      name="044_yolo_wide",
      description="Upper-bound research candidate: very broad clean catch-up activation, useful as a ceiling but not directly installable.",
      min_deficit_mph=1.5,
      min_speed_mph=7.0,
      min_accel_lag=0.0,
      delta_cap=1.05,
      planner_floor_accel=0.78,
      max_lateral=1.05,
      soft_curve_lateral=0.65,
      curve_decay=0.82,
      deficit_coeff=0.050,
      lag_coeff=0.240,
      ramp_min_speed_mph=20.0,
      ramp_min_deficit_mph=3.5,
      ramp_bonus=0.28,
      ramp_delta_cap=1.16,
      allow_noncruise=True,
      noncruise_min_deficit_mph=4.0,
      noncruise_min_speed_mph=14.0,
      noncruise_delta_cap=0.92,
      soften_lateral_bit=True,
      installable=False,
    ),
  ]


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--export-dir", action="append", type=Path, default=[], help="Prelim export directory. May be repeated.")
  parser.add_argument("--route-id", action="append", default=[], help="Route id to discover from the output root.")
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--out", type=Path, default=None)
  args = parser.parse_args()

  route_ids = tuple(args.route_id) if args.route_id else DEFAULT_ROUTE_IDS
  export_dirs = args.export_dir or latest_prelim_dirs(route_ids, args.output_root)
  if not export_dirs:
    raise SystemExit("no prelim export directories found")

  samples: list[Sample] = []
  for export_dir in export_dirs:
    route_samples = read_samples(export_dir)
    if route_samples:
      samples.extend(route_samples)
  if not samples:
    raise SystemExit("no shadow samples found")

  timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  out_dir = args.out or args.output_root / f"longitudinal_candidate_sweep_{timestamp}"
  out_dir.mkdir(parents=True, exist_ok=True)

  rows, route_rows = score_candidates(samples, candidate_set())
  write_csv(out_dir / "candidate_scores.csv", rows)
  write_csv(out_dir / "route_candidate_summary.csv", route_rows)
  svg_bar_chart(out_dir / "candidate_score_chart.svg", rows, "Brickpilot offline longitudinal candidate scores", "score")
  write_report(out_dir / "report.md", rows, route_rows, export_dirs)
  best_installable = next((row for row in rows if str(row.get("installable")).lower() == "true"), rows[0])
  print(out_dir)
  print("best_installable", best_installable["candidate"], best_installable["score"])
  print("best_ceiling", rows[0]["candidate"], rows[0]["score"])
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
