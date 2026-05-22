#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROUTES = (
  "0000020c--59eadd34bc",  # 0.5.6 Alpha Long OFF nnv2 native reference
  "0000020f--2ecc9b6e08",  # 0.5.6 Alpha Long ON nnv2
  "00000213--ab5b813126",  # 0.5.7 Alpha Long ON WMI reviewed
  "00000207--a8c307d230",  # 0.5.6 Alpha Long ON reviewed
  "00000209--f9ffcb0581",  # 0.5.6 Alpha Long OFF reviewed/native reference
)

GOOD_LABELS = {
  "good_rolling_follow",
  "rolling_follow_good",
  "pacing_good",
  "good_pacing",
  "smooth_close_gap",
  "follow_distance_good",
  "native_like_pacing",
  "good_resume",
}

BAD_LABELS = {
  "pacing_bad",
  "pacing_bursty",
  "follow_distance_too_far",
  "overbraked_rolling_traffic",
  "unnecessary_braking",
  "driver_gas_after_brake",
  "driver_brake_intervention",
  "missed_stop",
  "lead_brake_bad",
  "braking_bad",
  "bad_resume",
  "surged_to_close_gap",
}

BLOCK_REASON_NAMES = {
  0: "none",
  1: "not_eligible",
  2: "driver_override",
  3: "high_lat",
  4: "stop_priority",
  5: "brake_blend",
  6: "urgent_ttc",
  7: "soft_regen_decel",
  8: "deadband",
  9: "entry_hold",
  10: "rate_limit_zero",
  11: "hard_suppressor",
}


def read_csv(path: Path) -> list[dict[str, str]]:
  if not path.exists():
    return []
  with path.open(newline="", encoding="utf-8") as f:
    return list(csv.DictReader(f))


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


def as_float(value: Any, default: float = 0.0) -> float:
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return default
  return ret if math.isfinite(ret) else default


def as_int(value: Any, default: int = 0) -> int:
  try:
    return int(float(value))
  except (TypeError, ValueError):
    return default


def as_bool(value: Any) -> bool:
  return str(value).strip().lower() in {"1", "1.0", "true", "yes"}


def time_of(row: dict[str, Any]) -> float:
  return as_float(row.get("route_time_sec", row.get("t_sec", 0.0)))


@dataclass(frozen=True)
class Candidate:
  name: str
  description: str
  max_pos: float = 0.10
  max_neg: float = 0.16
  gap_gain: float = 0.012
  pos_vrel_gain: float = 0.025
  neg_vrel_gain: float = 0.040
  pos_deadband_extra: float = 4.0
  neg_deadband_extra: float = 1.5
  min_raw_delta: float = 0.030
  entry_hold: float = 0.35
  ramp_up: float = 0.15
  ramp_down: float = 0.25
  release_ramp: float = 0.35
  time_gap_scale: float = 1.0
  deadband_scale: float = 1.0
  installable: bool = False


CANDIDATES = (
  Candidate("058_installable_microdelta", "0.5.8 live candidate: native-style rolling-lead micro pacing.", installable=True),
  Candidate("058_gentler_positive", "Same gates, half positive catch-up pressure.", max_pos=0.07, gap_gain=0.009, pos_vrel_gain=0.018),
  Candidate("058_tighter_native_gap", "Tests a slightly tighter native-like following target.", time_gap_scale=0.88, deadband_scale=0.90),
  Candidate("058_looser_guarded_gap", "Tests whether looser gaps avoid overbrake/pacing penalties.", time_gap_scale=1.12, deadband_scale=1.10),
  Candidate("058_stronger_shadow_decel", "Shadow-only stronger close/closing correction.", max_neg=0.22, neg_vrel_gain=0.055),
)


def lead_profile(v_ego: float, v_lead: float, candidate: Candidate) -> tuple[float, float]:
  ref = max(0.0, min(max(v_ego, v_lead), 28.0))
  base = interp(ref, (0.0, 5.0, 12.0, 20.0, 28.0), (4.0, 5.0, 6.0, 8.0, 10.0))
  t_gap = interp(ref, (0.0, 5.0, 12.0, 20.0, 28.0), (0.90, 1.05, 1.15, 1.35, 1.55)) * candidate.time_gap_scale
  deadband = interp(ref, (0.0, 10.0, 25.0), (1.5, 2.2, 3.5)) * candidate.deadband_scale
  return max(5.0, min(60.0, base + t_gap * ref)), deadband


def interp(x: float, xs: tuple[float, ...], ys: tuple[float, ...]) -> float:
  if x <= xs[0]:
    return ys[0]
  if x >= xs[-1]:
    return ys[-1]
  for idx in range(1, len(xs)):
    if x <= xs[idx]:
      span = xs[idx] - xs[idx - 1]
      frac = 0.0 if span == 0.0 else (x - xs[idx - 1]) / span
      return ys[idx - 1] + frac * (ys[idx] - ys[idx - 1])
  return ys[-1]


def rate_limit(target: float, previous: float, dt: float, candidate: Candidate) -> tuple[float, bool]:
  dt = max(0.0, min(dt, 0.20))
  if abs(target) < 1e-6:
    step_up = candidate.release_ramp * dt
    step_down = candidate.release_ramp * dt
  else:
    step_up = candidate.ramp_up * dt
    step_down = candidate.ramp_down * dt
  if target > previous + step_up:
    return previous + step_up, True
  if target < previous - step_down:
    return previous - step_down, True
  return target, False


@dataclass
class SimState:
  delta: float = 0.0
  mode: int = 0
  mode_age: float = 0.0


def simulate_row(row: dict[str, str], prev: SimState, dt: float, candidate: Candidate) -> tuple[dict[str, Any], SimState]:
  t = time_of(row)
  d_rel = as_float(row.get("leadDistance", row.get("stopLeadDistance")))
  v_rel = as_float(row.get("leadPacingVRel", row.get("stopLeadVRel")))
  lead_status = as_bool(row.get("leadStatus")) or as_bool(row.get("hasLead"))
  v_lead = as_float(row.get("leadAbsSpeed"))
  v_ego = as_float(row.get("vEgo"))
  if lead_status and v_ego <= 0.0 and v_lead > 0.0:
    v_ego = max(0.0, v_lead - v_rel)
  a_ego = as_float(row.get("aEgo"))
  a_target = as_float(row.get("longitudinalAssistATarget", row.get("aTarget")))
  if lead_status and v_lead <= 0.0:
    v_lead = max(0.0, v_ego + v_rel)
  ttc = as_float(row.get("stopTtc"))
  lateral = as_float(row.get("longitudinalLateralDemand"))
  speed_deficit = as_float(row.get("speedDeficit"))
  stop_active = as_bool(row.get("stopActive"))
  stop_mode = as_int(row.get("stopMode"))
  final_stop_allowed = as_bool(row.get("finalStopAllowed"))
  steering_guard = as_bool(row.get("steeringGuardSuppressed"))
  long_active = as_bool(row.get("longActive"))
  standstill = as_bool(row.get("standstill")) or as_bool(row.get("nearStandstill")) or as_bool(row.get("cruiseStandstill"))

  target_gap, deadband = lead_profile(v_ego, v_lead, candidate) if lead_status and d_rel > 0.0 else (0.0, 0.0)
  gap_error = d_rel - target_gap if target_gap > 0.0 else 0.0
  driver_override = False
  high_lat = lateral > 0.75 or steering_guard
  stop_priority = stop_active or final_stop_allowed or stop_mode in {2, 3, 4} or (ttc > 0.0 and ttc <= 3.2)
  # Pre-0.5.8 prelims do not yet expose raw CAN brake blend fields consistently.
  # Treat recorded friction/hold brake state as a hard brake-blend context.
  brake_state = as_int(row.get("stopBrakeState"))
  brake_blend = brake_state in {3, 4}
  regen_soft = brake_state == 1
  regen_hard = brake_state == 2
  eligible = bool(long_active and lead_status and
                  6.0 <= d_rel <= 70.0 and
                  2.0 <= v_ego <= 28.0 and
                  v_lead >= 2.0 and not standstill)

  block_reason = 0
  raw_delta = 0.0
  mode = 0
  if not eligible:
    block_reason = 1
  elif driver_override:
    block_reason = 2
  elif high_lat:
    block_reason = 3
  elif stop_priority:
    block_reason = 4
  elif brake_blend:
    block_reason = 5
  else:
    mode = 4
    too_far = max(0.0, gap_error - deadband)
    too_close = max(0.0, -gap_error - deadband)
    too_far_action = max(0.0, too_far - candidate.pos_deadband_extra)
    too_close_action = max(0.0, too_close - candidate.neg_deadband_extra)
    lead_pulling = max(0.0, v_rel)
    closing = max(0.0, -v_rel - 0.45)
    positive_ok = bool(too_far_action > 0.0 and v_rel > -0.20 and
                       (ttc == 0.0 or ttc > 8.0) and v_lead > 3.0 and
                       (speed_deficit >= 0.5 or v_rel > 0.1) and not regen_hard)
    coast_or_neg_ok = bool(v_lead > 2.0 and d_rel > 7.0 and (ttc == 0.0 or ttc > 3.2))
    if positive_ok:
      raw_delta = min(candidate.max_pos, candidate.gap_gain * too_far_action + candidate.pos_vrel_gain * lead_pulling)
      if regen_soft:
        raw_delta = 0.0 if a_ego < -0.12 else raw_delta * 0.5
        if raw_delta == 0.0:
          block_reason = 7
      mode = 2 if raw_delta > 0.01 else 1
    elif coast_or_neg_ok and a_target > 0.0 and (v_rel < -0.55 or gap_error < -(deadband + candidate.neg_deadband_extra)):
      raw_delta = -min(max(0.0, a_target), 0.08)
      mode = 4 if raw_delta < -0.01 else 1
    elif coast_or_neg_ok and (too_close_action > 0.0 or closing > 0.0):
      raw_delta = -min(candidate.max_neg, candidate.gap_gain * too_close_action + candidate.neg_vrel_gain * closing)
      mode = 3 if raw_delta < -0.01 else 1
    if v_ego > 18.0 and raw_delta != 0.0:
      raw_delta *= max(0.0, min(1.0, (28.0 - v_ego) / 10.0))
    if abs(raw_delta) < candidate.min_raw_delta:
      raw_delta = 0.0
    if block_reason == 0 and abs(raw_delta) <= 0.001:
      block_reason = 8

  mode_age = prev.mode_age + dt if mode != 0 and mode == prev.mode else (dt if mode != 0 else 0.0)
  entry_ready = abs(prev.delta) > 0.01 or mode_age >= candidate.entry_hold
  if not entry_ready and abs(raw_delta) > 0.001:
    raw_delta = 0.0
    if block_reason == 0:
      block_reason = 9
  limited_delta, jerk_limited = rate_limit(raw_delta, prev.delta, dt, candidate)
  live = bool(eligible and block_reason == 0 and entry_ready and abs(limited_delta) > 0.01)
  live_delta = limited_delta if live else 0.0
  if not live and block_reason == 0 and abs(raw_delta) > 0.001:
    block_reason = 10
  if live:
    block_reason = 0

  return {
    "route_time_sec": t,
    "eligible": eligible,
    "mode": mode,
    "target_gap": target_gap,
    "deadband": deadband,
    "gap_error": gap_error,
    "v_rel": v_rel,
    "v_lead": v_lead,
    "ttc": ttc,
    "raw_delta": raw_delta,
    "rate_limited_delta": limited_delta,
    "live_delta": live_delta,
    "jerk_limited": jerk_limited,
    "block_reason": block_reason,
    "stop_priority": stop_priority,
    "brake_blend": brake_blend,
    "regen_soft": regen_soft,
    "high_lat": high_lat,
    "prior_057_mode": as_int(row.get("leadPacingMode")),
    "prior_057_delta": as_float(row.get("leadPacingAssistDelta")),
  }, SimState(delta=limited_delta, mode=mode, mode_age=mode_age)


def simulate_route(rows: list[dict[str, str]], candidate: Candidate) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  prev = SimState()
  last_t = 0.0
  for idx, row in enumerate(rows):
    t = time_of(row)
    dt = 0.05 if idx == 0 else max(0.01, min(0.20, t - last_t))
    sim, prev = simulate_row(row, prev, dt, candidate)
    out.append(sim)
    last_t = t
  return out


def discover_prelim(route_id: str, roots: list[Path]) -> Path | None:
  candidates: list[Path] = []
  for root in roots:
    if not root.exists():
      continue
    candidates.extend(p for p in root.rglob(f"*{route_id}*") if p.is_dir() and (p / "brickpilot_shadow_samples.csv").exists())
  if not candidates:
    return None
  return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def labels_for(prelim_dir: Path) -> list[dict[str, Any]]:
  rows = []
  for row in read_csv(prelim_dir / "test_route_predictions.csv"):
    target = row.get("target", "")
    if target in GOOD_LABELS or target in BAD_LABELS:
      rows.append({
        "target": target,
        "start": as_float(row.get("start_sec")),
        "end": as_float(row.get("end_sec")),
        "peak": as_float(row.get("peak_score")),
      })
  return rows


def overlap_seconds(samples: list[dict[str, Any]], labels: list[dict[str, Any]], predicate) -> dict[str, float]:
  ret: dict[str, float] = {label["target"]: 0.0 for label in labels}
  if not samples or not labels:
    return ret
  times = [as_float(row["route_time_sec"]) for row in samples]
  for idx, sample in enumerate(samples):
    if not predicate(sample):
      continue
    t = times[idx]
    dt = 0.05 if idx + 1 >= len(times) else max(0.01, min(0.20, times[idx + 1] - t))
    for label in labels:
      if label["start"] <= t <= label["end"]:
        ret[label["target"]] = ret.get(label["target"], 0.0) + dt
  return ret


def event_windows(route_id: str, candidate: Candidate, samples: list[dict[str, Any]], labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
  windows: list[dict[str, Any]] = []
  active: list[dict[str, Any]] = []
  for sample in samples:
    if abs(sample["live_delta"]) > 0.001:
      active.append(sample)
    elif active:
      windows.append(window_row(route_id, candidate, active, labels))
      active = []
  if active:
    windows.append(window_row(route_id, candidate, active, labels))
  return windows


def window_row(route_id: str, candidate: Candidate, samples: list[dict[str, Any]], labels: list[dict[str, Any]]) -> dict[str, Any]:
  start = samples[0]["route_time_sec"]
  end = samples[-1]["route_time_sec"]
  nearby = sorted({label["target"] for label in labels if label["start"] <= end + 3.0 and label["end"] >= start - 3.0})
  return {
    "route_id": route_id,
    "candidate": candidate.name,
    "start_sec": round(start, 2),
    "end_sec": round(end, 2),
    "duration_sec": round(max(0.0, end - start), 2),
    "mode": Counter(sample["mode"] for sample in samples).most_common(1)[0][0],
    "mean_live_delta": round(sum(sample["live_delta"] for sample in samples) / len(samples), 4),
    "max_live_delta": round(max(sample["live_delta"] for sample in samples), 4),
    "min_live_delta": round(min(sample["live_delta"] for sample in samples), 4),
    "mean_gap_error": round(sum(sample["gap_error"] for sample in samples) / len(samples), 3),
    "mean_v_rel": round(sum(sample["v_rel"] for sample in samples) / len(samples), 3),
    "nearby_labels": ",".join(nearby),
  }


def summarize(route_id: str, candidate: Candidate, samples: list[dict[str, Any]], labels: list[dict[str, Any]]) -> dict[str, Any]:
  total = len(samples)
  rolling = [s for s in samples if s["eligible"]]
  active = [s for s in samples if abs(s["live_delta"]) > 0.001]
  abs_deltas = sorted(abs(s["live_delta"]) for s in active)
  active_frac = len(active) / len(rolling) if rolling else 0.0
  p95 = abs_deltas[int(0.95 * (len(abs_deltas) - 1))] if abs_deltas else 0.0
  label_overlap = overlap_seconds(samples, labels, lambda s: abs(s["live_delta"]) > 0.001)
  good_sec = sum(sec for label, sec in label_overlap.items() if label in GOOD_LABELS)
  bad_sec = sum(sec for label, sec in label_overlap.items() if label in BAD_LABELS)
  prior_mode_zero_delta = [s for s in samples if s["prior_057_mode"] != 0 and abs(s["prior_057_delta"]) <= 0.001 and s["eligible"]]
  fixed_zero_delta = [s for s in prior_mode_zero_delta if abs(s["live_delta"]) > 0.001]
  mode_switches = 0
  previous_mode = 0
  for sample in samples:
    mode = sample["mode"] if sample["eligible"] else 0
    if mode != 0 and previous_mode != 0 and mode != previous_mode:
      mode_switches += 1
    if mode != 0:
      previous_mode = mode
    elif previous_mode != 0:
      previous_mode = 0
  route_duration = total * 0.05
  return {
    "route_id": route_id,
    "candidate": candidate.name,
    "installable": candidate.installable,
    "samples": total,
    "rolling_eligible_samples": len(rolling),
    "active_samples": len(active),
    "active_frac_of_eligible": round(active_frac, 4),
    "active_sec": round(len(active) * 0.05, 2),
    "max_positive_delta": round(max((s["live_delta"] for s in samples), default=0.0), 4),
    "max_negative_delta": round(min((s["live_delta"] for s in samples), default=0.0), 4),
    "p95_abs_delta": round(p95, 4),
    "good_overlap_sec": round(good_sec, 2),
    "bad_overlap_sec": round(bad_sec, 2),
    "prior_mode_zero_delta_samples": len(prior_mode_zero_delta),
    "prior_mode_zero_delta_fixed_samples": len(fixed_zero_delta),
    "mode_switches": mode_switches,
    "mode_switches_per_2s": round(mode_switches / max(1.0, route_duration / 2.0), 4),
    "score": round(good_sec - 1.6 * bad_sec + 0.01 * len(fixed_zero_delta) - 0.02 * max(0, len(active) - int(0.18 * max(1, len(rolling)))), 3),
  }


def write_report(out_dir: Path, summary_rows: list[dict[str, Any]], event_rows: list[dict[str, Any]], missing: list[str]) -> None:
  installable = [row for row in summary_rows if str(row.get("installable")).lower() == "true" or row.get("installable") is True]
  lines = [
    "# Lead Pacing Sweep 0.5.8",
    "",
    "## Scope",
    "This sweep preflights the 0.5.8 native rolling-lead micro-pacing policy against the latest Alpha Long ON/OFF and 0.5.7 pacing artifacts. It is not a stronger braking sweep.",
    "",
    "## Missing Routes",
  ]
  lines.extend([f"- `{route_id}`" for route_id in missing] or ["- none"])
  lines.extend(["", "## Installable Candidate"])
  for row in installable:
    lines.append(f"- `{row['route_id']}` active fraction {row['active_frac_of_eligible']}, max +{row['max_positive_delta']}, min {row['max_negative_delta']}, p95 abs {row['p95_abs_delta']}, fixed 0.5.7 zero-delta samples {row['prior_mode_zero_delta_fixed_samples']}/{row['prior_mode_zero_delta_samples']}.")
  lines.extend([
    "",
    "## Preflight Read",
    "- Pass target: nonzero live pacing on roughly 5-18% of rolling eligible samples where route context supports pacing.",
    "- Pass target: no live positive deltas in active stop/final-stop/urgent TTC/brake-blend/high-lateral contexts.",
    "- 0.5.8 should specifically avoid the 0.5.7 failure where `leadPacingMode` was nonzero but the applied delta stayed zero in valid far rolling-lead contexts.",
    "",
    "## Event Windows",
  ])
  for row in event_rows[:30]:
    lines.append(f"- `{row['route_id']}` `{row['candidate']}` {row['start_sec']}-{row['end_sec']}s delta {row['min_live_delta']}..{row['max_live_delta']} labels `{row['nearby_labels']}`")
  lines.append("")
  (out_dir / "lead_pacing_sweep_058_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
  parser = argparse.ArgumentParser(description="Preflight Brickpilot 0.5.8 rolling-lead micro-pacing candidates from prelim shadow exports.")
  parser.add_argument("--analysis-root", type=Path, default=Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", "/Users/brick/BrickpilotDriveDB/analysis_exports")))
  parser.add_argument("--export-dir", type=Path, action="append", default=[])
  parser.add_argument("--route", action="append", default=[])
  parser.add_argument("--output-dir", type=Path)
  args = parser.parse_args()

  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  out_dir = args.output_dir or (args.analysis_root / f"lead_pacing_sweep_058_{stamp}")
  out_dir.mkdir(parents=True, exist_ok=True)
  roots = args.export_dir or [args.analysis_root]
  route_ids = args.route or list(DEFAULT_ROUTES)
  summary_rows: list[dict[str, Any]] = []
  gate_rows: list[dict[str, Any]] = []
  label_rows: list[dict[str, Any]] = []
  event_rows: list[dict[str, Any]] = []
  missing: list[str] = []

  for route_id in route_ids:
    prelim = discover_prelim(route_id, roots)
    if prelim is None:
      missing.append(route_id)
      continue
    raw_rows = read_csv(prelim / "brickpilot_shadow_samples.csv")
    labels = labels_for(prelim)
    for candidate in CANDIDATES:
      samples = simulate_route(raw_rows, candidate)
      summary_rows.append(summarize(route_id, candidate, samples, labels))
      reason_counts = Counter(BLOCK_REASON_NAMES.get(sample["block_reason"], str(sample["block_reason"])) for sample in samples)
      for reason, count in sorted(reason_counts.items()):
        gate_rows.append({"route_id": route_id, "candidate": candidate.name, "block_reason": reason, "samples": count, "frac": round(count / max(1, len(samples)), 4)})
      overlaps = overlap_seconds(samples, labels, lambda s: abs(s["live_delta"]) > 0.001)
      for label, seconds in sorted(overlaps.items()):
        if seconds > 0.0:
          label_rows.append({"route_id": route_id, "candidate": candidate.name, "label": label, "overlap_sec": round(seconds, 2), "connotation": "good" if label in GOOD_LABELS else "bad"})
      event_rows.extend(event_windows(route_id, candidate, samples, labels))

  write_csv(out_dir / "lead_pacing_sweep_058_route_summary.csv", summary_rows)
  write_csv(out_dir / "lead_pacing_sweep_058_gate_crosstab.csv", gate_rows)
  write_csv(out_dir / "lead_pacing_sweep_058_label_overlap.csv", label_rows)
  write_csv(out_dir / "lead_pacing_sweep_058_event_cards.csv", event_rows)
  write_report(out_dir, summary_rows, event_rows, missing)
  print(out_dir)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
