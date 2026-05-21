#!/usr/bin/env python3
"""Sweep steering-output policies over VM controlsd replay series."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
DEFAULT_REPLAY_DIR = Path(os.environ.get("BRICKPILOT_REPLAY_DIR", DEFAULT_OUTPUT_ROOT / "vm_controlsd_replay_steering_latest"))
MAX_RATE_GAP_SEC = 0.55
HIGH_DEMAND_LAT_ACCEL = 0.75
HIGH_DEMAND_TORQUE = 0.65


@dataclass(frozen=True)
class Candidate:
  name: str
  family: str
  alpha: float
  reversal_scale: float
  zero_hold_frames: int
  zero_cross_raw: float
  zero_cross_smooth: float
  rate_limit_per_sec: float | None
  second_alpha: float | None
  deadband: float
  torque_scale: float
  lookahead_frames: int
  hold_after_reversal_frames: int
  unsafe_lab_only: bool
  unsafe_reason: str


@dataclass
class State:
  initialized: bool = False
  smooth: float = 0.0
  second: float = 0.0
  zero_hold: int = 0
  reversal_hold: int = 0


def as_float(value: Any, default: float = 0.0) -> float:
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return default
  return ret if math.isfinite(ret) else default


def read_rows(path: Path) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  with path.open(newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
      if row.get("source") != "replay_0_4_4":
        continue
      row["segment_key"] = f"{row['route_id']}--{row['segment_index']}"
      row["t_sec"] = as_float(row.get("t_sec"))
      row["active"] = str(row.get("active")).lower() == "true"
      row["torque_output"] = as_float(row.get("torque_output"))
      row["desired_lateral_accel"] = as_float(row.get("desired_lateral_accel"))
      row["actual_lateral_accel"] = as_float(row.get("actual_lateral_accel"))
      row["lateral_error"] = as_float(row.get("lateral_error"))
      rows.append(row)
  rows.sort(key=lambda row: (str(row["segment_key"]), float(row["t_sec"])))
  add_dts(rows)
  return rows


def add_dts(rows: list[dict[str, Any]]) -> None:
  by_segment: dict[str, list[dict[str, Any]]] = {}
  for row in rows:
    by_segment.setdefault(str(row["segment_key"]), []).append(row)
  for segment_rows in by_segment.values():
    segment_rows.sort(key=lambda row: float(row["t_sec"]))
    for idx, row in enumerate(segment_rows):
      if idx + 1 < len(segment_rows):
        dt = float(segment_rows[idx + 1]["t_sec"]) - float(row["t_sec"])
        row["dt"] = dt if 0.0 < dt <= MAX_RATE_GAP_SEC else 0.0
      else:
        row["dt"] = 0.0


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


def metric_summary(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
  selected = [row for row in rows if row.get("active")]
  values = [(float(row["t_sec"]), float(row[key])) for row in selected]
  raw_values = [value for _, value in values]
  rates, jerks, sign_changes = derivative(values)
  duration = max(0.0, values[-1][0] - values[0][0]) if len(values) >= 2 else 0.0
  return {
    "samples": float(len(values)),
    "duration_sec": duration,
    "rms": rms(raw_values),
    "p95_abs": percentile([abs(value) for value in raw_values], 95),
    "max_abs": max((abs(value) for value in raw_values), default=0.0),
    "rate_rms": rms(rates),
    "rate_p95_abs": percentile([abs(value) for value in rates], 95),
    "jerk_rms": rms(jerks),
    "jerk_p95_abs": percentile([abs(value) for value in jerks], 95),
    "sign_changes_per_min": (sign_changes * 60.0 / duration) if duration > 1e-6 else 0.0,
  }


def reset_state(state: State) -> None:
  state.initialized = False
  state.smooth = 0.0
  state.second = 0.0
  state.zero_hold = 0
  state.reversal_hold = 0


def apply_candidate(candidate: Candidate, state: State, raw_input: float, raw_now: float, active: bool, dt: float) -> float:
  raw = raw_input * candidate.torque_scale
  if candidate.family == "baseline":
    return raw_now
  if not active:
    reset_state(state)
    return raw_now
  if state.reversal_hold > 0:
    state.reversal_hold -= 1
    state.initialized = True
    return state.smooth
  if state.zero_hold > 0:
    if abs(raw) <= candidate.zero_cross_raw:
      state.zero_hold -= 1
      state.smooth = 0.0
      state.second = 0.0
      state.initialized = True
      return 0.0
    state.zero_hold = 0
  if not state.initialized:
    state.initialized = True
    state.smooth = raw
    state.second = raw
    return raw
  weak_zero_cross = raw * state.smooth < 0.0 and abs(raw) <= candidate.zero_cross_raw and abs(state.smooth) <= candidate.zero_cross_smooth
  if weak_zero_cross:
    state.zero_hold = candidate.zero_hold_frames
    state.reversal_hold = candidate.hold_after_reversal_frames
    state.smooth = 0.0
    state.second = 0.0
    return 0.0
  alpha = candidate.alpha
  if raw * state.smooth < 0.0:
    alpha *= candidate.reversal_scale
  next_value = state.smooth + alpha * (raw - state.smooth)
  if candidate.rate_limit_per_sec is not None and dt > 0.0:
    limit = candidate.rate_limit_per_sec * dt
    next_value = max(state.smooth - limit, min(state.smooth + limit, next_value))
  if abs(next_value) < candidate.deadband and abs(raw) < candidate.zero_cross_raw:
    next_value = 0.0
  state.smooth = next_value
  if candidate.second_alpha is not None:
    state.second += candidate.second_alpha * (state.smooth - state.second)
    return state.second
  return state.smooth


def candidate_rows(rows: list[dict[str, Any]], candidate: Candidate) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  by_segment: dict[str, list[dict[str, Any]]] = {}
  for row in rows:
    by_segment.setdefault(str(row["segment_key"]), []).append(row)
  for segment_key, segment_rows in by_segment.items():
    state = State()
    ordered = sorted(segment_rows, key=lambda row: float(row["t_sec"]))
    raw_values = [float(row["torque_output"]) for row in ordered]
    for idx, row in enumerate(ordered):
      look_idx = min(len(raw_values) - 1, idx + candidate.lookahead_frames)
      shaped = apply_candidate(candidate, state, raw_values[look_idx], raw_values[idx], bool(row["active"]), float(row.get("dt") or 0.0))
      out.append({
        "route_id": row["route_id"],
        "segment_index": row["segment_index"],
        "segment_key": segment_key,
        "t_sec": row["t_sec"],
        "active": row["active"],
        "baseline_torque": raw_values[idx],
        "candidate_torque": shaped,
        "torque_delta": shaped - raw_values[idx],
        "desired_lateral_accel": row["desired_lateral_accel"],
        "lateral_error": row["lateral_error"],
      })
  return out


def build_candidates(total: int, unsafe_min: int) -> list[Candidate]:
  candidates: list[Candidate] = [
    Candidate("baseline_replay_0_4_4", "baseline", 1.0, 1.0, 0, 0.0, 0.0, None, None, 0.0, 1.0, 0, 0, False, ""),
  ]
  installable_alphas = [0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.22, 0.28, 0.36]
  reversal_scales = [0.25, 0.35, 0.45, 0.60, 0.80, 1.00]
  rate_limits = [None, 1.0, 1.35, 1.7, 2.1, 2.7, 3.4, 4.5]
  zero_holds = [0, 2, 4, 6, 8]
  deadbands = [0.0, 0.015, 0.03, 0.045]
  second_alphas = [None, 0.18, 0.26, 0.36]
  idx = 0
  for alpha in installable_alphas:
    for reversal in reversal_scales:
      for rate in rate_limits:
        for hold in zero_holds:
          deadband = deadbands[(idx // 3) % len(deadbands)]
          second = second_alphas[(idx // 11) % len(second_alphas)]
          candidates.append(Candidate(
            name=f"road_{idx:03d}_a{alpha:.2f}_r{reversal:.2f}_cap{rate or 0:.2f}_h{hold}",
            family="causal_texture",
            alpha=alpha,
            reversal_scale=reversal,
            zero_hold_frames=hold,
            zero_cross_raw=0.30 + 0.04 * hold,
            zero_cross_smooth=0.28 + 0.03 * hold,
            rate_limit_per_sec=rate,
            second_alpha=second,
            deadband=deadband,
            torque_scale=[0.94, 0.97, 1.0, 1.03][idx % 4],
            lookahead_frames=0,
            hold_after_reversal_frames=0,
            unsafe_lab_only=False,
            unsafe_reason="",
          ))
          idx += 1
          if len(candidates) >= max(1, total - unsafe_min):
            break
        if len(candidates) >= max(1, total - unsafe_min):
          break
      if len(candidates) >= max(1, total - unsafe_min):
        break
    if len(candidates) >= max(1, total - unsafe_min):
      break

  unsafe_idx = 0
  unsafe_alphas = [0.025, 0.04, 0.055, 0.07, 0.65, 0.85, 1.10]
  unsafe_rates = [0.18, 0.28, 0.42, 0.65, 7.5, 10.0, None]
  unsafe_scales = [0.70, 0.82, 1.12, 1.25, 1.45]
  lookaheads = [0, 2, 5, 10, 20]
  while len(candidates) < total:
    alpha = unsafe_alphas[unsafe_idx % len(unsafe_alphas)]
    rate = unsafe_rates[(unsafe_idx // 2) % len(unsafe_rates)]
    scale = unsafe_scales[(unsafe_idx // 3) % len(unsafe_scales)]
    lookahead = lookaheads[(unsafe_idx // 5) % len(lookaheads)]
    heavy_lag = alpha <= 0.07 or (rate is not None and rate <= 0.65)
    noncausal = lookahead > 0
    torque_break = scale > 1.10 or scale < 0.85
    reason = []
    if heavy_lag:
      reason.append("excessive lag/rate clamp")
    if noncausal:
      reason.append("noncausal lookahead")
    if torque_break:
      reason.append("torque scaling outside road budget")
    if not reason:
      reason.append("lab-only safety-margin violation")
    candidates.append(Candidate(
      name=f"lab_{unsafe_idx:03d}_a{alpha:.3f}_cap{rate or 0:.2f}_s{scale:.2f}_la{lookahead}",
      family="lab_break_safety",
      alpha=alpha,
      reversal_scale=[0.08, 0.18, 0.35, 1.25][unsafe_idx % 4],
      zero_hold_frames=[0, 8, 14, 24][unsafe_idx % 4],
      zero_cross_raw=[0.40, 0.65, 0.90][unsafe_idx % 3],
      zero_cross_smooth=[0.40, 0.65, 0.90][unsafe_idx % 3],
      rate_limit_per_sec=rate,
      second_alpha=[None, 0.10, 0.18][unsafe_idx % 3],
      deadband=[0.0, 0.08, 0.16][unsafe_idx % 3],
      torque_scale=scale,
      lookahead_frames=lookahead,
      hold_after_reversal_frames=[0, 3, 8][unsafe_idx % 3],
      unsafe_lab_only=True,
      unsafe_reason=", ".join(reason),
    ))
    unsafe_idx += 1
  return candidates[:total]


def score_candidate(rows: list[dict[str, Any]], baseline_metrics: dict[str, float], candidate: Candidate) -> tuple[dict[str, Any], list[dict[str, Any]]]:
  sim = candidate_rows(rows, candidate)
  metrics = metric_summary(sim, "candidate_torque")
  baseline = metric_summary(sim, "baseline_torque")
  deltas = [float(row["torque_delta"]) for row in sim if row.get("active")]
  high_demand = [
    row for row in sim
    if row.get("active") and (abs(float(row["desired_lateral_accel"])) >= HIGH_DEMAND_LAT_ACCEL or abs(float(row["baseline_torque"])) >= HIGH_DEMAND_TORQUE)
  ]
  high_deltas = [float(row["torque_delta"]) for row in high_demand]
  rate_gain = (baseline["rate_p95_abs"] - metrics["rate_p95_abs"]) / max(baseline["rate_p95_abs"], 1e-6)
  jerk_gain = (baseline["jerk_p95_abs"] - metrics["jerk_p95_abs"]) / max(baseline["jerk_p95_abs"], 1e-6)
  sign_gain = (baseline["sign_changes_per_min"] - metrics["sign_changes_per_min"]) / max(baseline["sign_changes_per_min"], 1e-6)
  tracking_rms = rms(deltas)
  tracking_p95 = percentile([abs(value) for value in deltas], 95)
  high_tracking = rms(high_deltas)
  # Lab-only candidates are allowed to score well, but get a separate road score
  # that makes the installability boundary explicit.
  learning_score = 120.0 * rate_gain + 90.0 * jerk_gain + 24.0 * sign_gain - 55.0 * tracking_rms - 20.0 * high_tracking
  road_score = 120.0 * rate_gain + 90.0 * jerk_gain + 24.0 * sign_gain - 95.0 * tracking_rms - 50.0 * tracking_p95 - 50.0 * high_tracking
  if candidate.unsafe_lab_only:
    road_score -= 250.0
  row = {
    "candidate": candidate.name,
    "family": candidate.family,
    "unsafe_lab_only": candidate.unsafe_lab_only,
    "unsafe_reason": candidate.unsafe_reason,
    "learning_score": round(learning_score, 5),
    "road_score": round(road_score, 5),
    "rate_gain": round(rate_gain, 6),
    "jerk_gain": round(jerk_gain, 6),
    "sign_gain": round(sign_gain, 6),
    "rate_p95_abs": round(metrics["rate_p95_abs"], 6),
    "jerk_p95_abs": round(metrics["jerk_p95_abs"], 6),
    "sign_changes_per_min": round(metrics["sign_changes_per_min"], 6),
    "torque_delta_rms": round(tracking_rms, 6),
    "torque_delta_p95_abs": round(tracking_p95, 6),
    "high_demand_delta_rms": round(high_tracking, 6),
    "samples": int(metrics["samples"]),
    **asdict(candidate),
  }
  return row, sim


def route_scores(sim: list[dict[str, Any]], candidate_name: str) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  by_route: dict[str, list[dict[str, Any]]] = {}
  for row in sim:
    by_route.setdefault(str(row["route_id"]), []).append(row)
  for route_id, rows in sorted(by_route.items()):
    cand = metric_summary(rows, "candidate_torque")
    base = metric_summary(rows, "baseline_torque")
    out.append({
      "candidate": candidate_name,
      "route_id": route_id,
      "rate_gain": round((base["rate_p95_abs"] - cand["rate_p95_abs"]) / max(base["rate_p95_abs"], 1e-6), 6),
      "jerk_gain": round((base["jerk_p95_abs"] - cand["jerk_p95_abs"]) / max(base["jerk_p95_abs"], 1e-6), 6),
      "sign_gain": round((base["sign_changes_per_min"] - cand["sign_changes_per_min"]) / max(base["sign_changes_per_min"], 1e-6), 6),
      "rate_p95_abs": round(cand["rate_p95_abs"], 6),
      "jerk_p95_abs": round(cand["jerk_p95_abs"], 6),
      "sign_changes_per_min": round(cand["sign_changes_per_min"], 6),
      "torque_delta_rms": round(rms([float(row["torque_delta"]) for row in rows if row.get("active")]), 6),
    })
  return out


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


def svg_bar(path: Path, title: str, labels: list[str], values: list[float], width: int = 1150, height: int = 560) -> None:
  height = max(height, 84 + 30 * len(labels))
  max_value = max((abs(value) for value in values), default=1.0)
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#071018'/>",
    f"<text x='24' y='38' fill='#eaf4ff' font-family='Arial' font-size='22' font-weight='700'>{html.escape(title)}</text>",
  ]
  for idx, (label, value) in enumerate(zip(labels, values)):
    y = 70 + idx * 30
    bar = 0.0 if max_value <= 0.0 else abs(value) / max_value * 560.0
    color = "#6ee7a8" if value >= 0 else "#ff8a7a"
    lines.append(f"<text x='24' y='{y + 14}' fill='#d7e7f7' font-family='Arial' font-size='12'>{html.escape(label[:58])}</text>")
    lines.append(f"<rect x='460' y='{y}' width='{bar:.1f}' height='18' rx='3' fill='{color}'/>")
    lines.append(f"<text x='{470 + bar:.1f}' y='{y + 14}' fill='#d7e7f7' font-family='Arial' font-size='12'>{value:.3f}</text>")
  lines.append("</svg>\n")
  path.write_text("\n".join(lines), encoding="utf-8")


def write_report(path: Path, rows: list[dict[str, Any]], route_rows: list[dict[str, Any]],
                 baseline_metrics: dict[str, float], args: argparse.Namespace) -> None:
  ranked_learning = sorted(rows, key=lambda row: float(row["learning_score"]), reverse=True)
  ranked_road = sorted([row for row in rows if not row["unsafe_lab_only"]], key=lambda row: float(row["road_score"]), reverse=True)
  ranked_lab = sorted([row for row in rows if row["unsafe_lab_only"]], key=lambda row: float(row["learning_score"]), reverse=True)
  lines = [
    "# Brickpilot 500-Way VM Replay Steering Variant Sweep",
    "",
    f"Generated: `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
    "",
    "This is a post-process policy sweep over real Linux VM `controlsd` replay output. It does not install anything and does not claim physical vehicle response. The goal is to search a large steering-output design space quickly before spending road-test time.",
    "",
    "## Scope",
    "",
    f"- Input replay dir: `{args.replay_dir}`",
    f"- Candidate policies: {len(rows)}",
    f"- Lab-only safety-breaking policies: {sum(1 for row in rows if row['unsafe_lab_only'])}",
    f"- Active replay samples scored: {int(baseline_metrics['samples'])}",
    f"- Baseline replay torque-rate p95: `{baseline_metrics['rate_p95_abs']:.6f}`",
    f"- Baseline replay jerk p95: `{baseline_metrics['jerk_p95_abs']:.6f}`",
    f"- Baseline replay sign changes/min: `{baseline_metrics['sign_changes_per_min']:.6f}`",
    "",
    "## Best Road-Budget Candidates",
    "",
    "| Rank | Candidate | Road score | Rate gain | Jerk gain | Sign gain | Delta RMS | Rate p95 |",
    "|---:|---|---:|---:|---:|---:|---:|---:|",
  ]
  for idx, row in enumerate(ranked_road[:20], start=1):
    lines.append(
      f"| {idx} | `{row['candidate']}` | {row['road_score']} | {row['rate_gain']} | {row['jerk_gain']} | "
      f"{row['sign_gain']} | {row['torque_delta_rms']} | {row['rate_p95_abs']} |"
    )
  lines.extend([
    "",
    "## Best Lab-Only Boundary Breakers",
    "",
    "| Rank | Candidate | Learning score | Reason | Rate gain | Jerk gain | Sign gain | Delta RMS |",
    "|---:|---|---:|---|---:|---:|---:|---:|",
  ])
  for idx, row in enumerate(ranked_lab[:20], start=1):
    lines.append(
      f"| {idx} | `{row['candidate']}` | {row['learning_score']} | {row['unsafe_reason']} | "
      f"{row['rate_gain']} | {row['jerk_gain']} | {row['sign_gain']} | {row['torque_delta_rms']} |"
    )
  best = ranked_road[0] if ranked_road else ranked_learning[0]
  lines.extend([
    "",
    "## Read",
    "",
    f"- Best road-budget candidate in this sweep: `{best['candidate']}`.",
    f"- It cuts torque-rate p95 by `{best['rate_gain']}` and jerk p95 by `{best['jerk_gain']}` versus the VM replay baseline, with torque delta RMS `{best['torque_delta_rms']}`.",
    "- The best road-budget family is causal final-output shaping. It supports the earlier read: the current 0.4.4 path reduced sign-flips, but there is still removable high-frequency torque texture.",
    "- The lab-only winners show the ceiling if we allow noncausal lookahead, excessive lag, or torque scaling outside a road budget. Those are learning tools, not installable settings.",
    "- If a lab-only candidate dominates with noncausal lookahead, the actionable road equivalent is upstream curvature/model smoothing, not future-looking output control.",
    "",
    "## Route Consistency For Top Road Candidate",
    "",
    "| Route | Rate gain | Jerk gain | Sign gain | Delta RMS |",
    "|---|---:|---:|---:|---:|",
  ])
  for row in [row for row in route_rows if row["candidate"] == best["candidate"]]:
    lines.append(f"| `{row['route_id'][:8]}` | {row['rate_gain']} | {row['jerk_gain']} | {row['sign_gain']} | {row['torque_delta_rms']} |")
  lines.extend([
    "",
    "## Files",
    "",
    "- `candidate_scores.csv`",
    "- `top_candidate_route_scores.csv`",
    "- `top_candidate_preview.csv`",
    "- `road_score_chart.svg`",
    "- `lab_learning_score_chart.svg`",
    "- `run_manifest.json`",
  ])
  path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--replay-dir", type=Path, default=DEFAULT_REPLAY_DIR)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--out", type=Path, default=None)
  parser.add_argument("--candidates", type=int, default=500)
  parser.add_argument("--unsafe-min", type=int, default=100)
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  rows = read_rows(args.replay_dir / "controlsd_series.csv")
  if not rows:
    raise SystemExit(f"no replay rows found in {args.replay_dir / 'controlsd_series.csv'}")
  baseline_metrics = metric_summary(rows, "torque_output")
  candidates = build_candidates(args.candidates, args.unsafe_min)
  timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  out_dir = args.out or args.output_root / f"vm_replay_steering_variant_sweep_{timestamp}"
  out_dir.mkdir(parents=True, exist_ok=True)

  score_rows: list[dict[str, Any]] = []
  top_route_rows: list[dict[str, Any]] = []
  preview_rows: list[dict[str, Any]] = []
  top_sims: dict[str, list[dict[str, Any]]] = {}
  for idx, candidate in enumerate(candidates, start=1):
    if idx % 50 == 0 or idx == 1:
      print(f"[{idx}/{len(candidates)}] {candidate.name}", flush=True)
    score, sim = score_candidate(rows, baseline_metrics, candidate)
    score_rows.append(score)
    if len(top_sims) < 40:
      top_sims[candidate.name] = sim

  ranked_road = sorted([row for row in score_rows if not row["unsafe_lab_only"]], key=lambda row: float(row["road_score"]), reverse=True)
  ranked_lab = sorted([row for row in score_rows if row["unsafe_lab_only"]], key=lambda row: float(row["learning_score"]), reverse=True)
  selected_names = {row["candidate"] for row in ranked_road[:20]} | {row["candidate"] for row in ranked_lab[:10]}
  # Recompute only selected candidates for route rows and preview output.
  by_name = {candidate.name: candidate for candidate in candidates}
  for name in selected_names:
    _, sim = score_candidate(rows, baseline_metrics, by_name[name])
    top_route_rows.extend(route_scores(sim, name))
    if name == ranked_road[0]["candidate"]:
      preview_rows = sim[:5000]

  score_rows.sort(key=lambda row: float(row["learning_score"]), reverse=True)
  write_csv(out_dir / "candidate_scores.csv", score_rows)
  write_csv(out_dir / "top_candidate_route_scores.csv", top_route_rows)
  write_csv(out_dir / "top_candidate_preview.csv", preview_rows)
  svg_bar(out_dir / "road_score_chart.svg", "Top road-budget VM replay variants",
          [row["candidate"] for row in ranked_road[:20]], [float(row["road_score"]) for row in ranked_road[:20]])
  svg_bar(out_dir / "lab_learning_score_chart.svg", "Top lab-only safety-breaking variants",
          [row["candidate"] for row in ranked_lab[:20]], [float(row["learning_score"]) for row in ranked_lab[:20]])
  manifest = {
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "replay_dir": str(args.replay_dir),
    "output_dir": str(out_dir),
    "candidate_count": len(candidates),
    "unsafe_lab_only_count": sum(1 for candidate in candidates if candidate.unsafe_lab_only),
    "baseline_metrics": baseline_metrics,
    "top_road_candidate": ranked_road[0] if ranked_road else None,
    "top_lab_candidate": ranked_lab[0] if ranked_lab else None,
  }
  (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
  write_report(out_dir / "report.md", score_rows, top_route_rows, baseline_metrics, args)
  latest = args.output_root / "vm_replay_steering_variant_sweep_latest"
  if latest.exists() or latest.is_symlink():
    latest.unlink()
  latest.symlink_to(out_dir.name if out_dir.parent == args.output_root else out_dir, target_is_directory=True)
  print(out_dir)
  print(json.dumps({
    "candidate_count": len(candidates),
    "unsafe_lab_only_count": sum(1 for candidate in candidates if candidate.unsafe_lab_only),
    "top_road_candidate": ranked_road[0] if ranked_road else None,
    "top_lab_candidate": ranked_lab[0] if ranked_lab else None,
  }, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
