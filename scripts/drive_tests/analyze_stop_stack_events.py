#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from pathlib import Path
from typing import Any


SOURCE_NAMES = {
  0: "none",
  1: "lead",
  2: "model",
  3: "should_stop",
  4: "creep_final_hold",
}

REASON_NAMES = {
  0: "none",
  1: "brake_debt",
  2: "planner_debt",
  3: "controller_underbrake",
  4: "final_stop_commit",
  5: "regen_light_not_enough",
}

STOP_MODE_NAMES = {
  0: "none",
  1: "rolling_follow",
  2: "final_stop_commit",
  3: "urgent_brake_recovery",
  4: "creep_hold",
}

LEAD_PACING_MODE_NAMES = {
  0: "none",
  1: "shadow",
  2: "accel",
  3: "decel",
  4: "coast",
}

FINAL_STOP_BLOCKED_REASON_NAMES = {
  0: "none",
  1: "no_final_source",
  2: "geometry_invalid",
  3: "planner_not_braking",
  4: "source_not_persistent",
  5: "rolling_lead",
  6: "gap_not_urgent",
  7: "standstill",
  8: "speed_range",
}

BUCKET_NAMES = {
  0: "none",
  1: "invalid_geometry",
  2: "lead_close_closing",
  3: "planner_late",
  4: "controller_underbrake",
  5: "creep_hold",
  6: "regen_light_context",
  7: "driver_override",
  8: "stationary_hold",
}

INVALID_NAMES = {
  0: "none",
  1: "no_source",
  2: "no_lead_geometry",
  3: "far_nonclosing_lead",
  4: "speed_range",
}

BRAKE_STATE_NAMES = {
  0: "none",
  1: "regen_light_coast",
  2: "regen_brake_blend",
  3: "friction_brake_candidate",
  4: "stationary_hold",
}

STOP_EVENT_LABELS = {
  "braking_bad",
  "braking_late",
  "braking_underdecel",
  "missed_stop",
  "stop_creep_fail",
  "stop_hold_fail",
  "driver_brake",
  "driver_brake_intervention",
  "good_lead_stop",
  "smooth_final_stop",
  "too_early_brake",
  "unnecessary_braking",
  "overbraked_rolling_traffic",
  "good_rolling_follow",
  "driver_gas_after_brake",
  "lead_rolling",
  "lead_stopped",
}

EVENT_CARD_FIELDS = (
  "vEgo",
  "aEgo",
  "accelCmd",
  "longitudinalAssistATarget",
  "longitudinalAssistAssistedATarget",
  "stopRequiredDecel",
  "stopPlannerDebt",
  "stopControllerDebt",
  "stopBrakeDebt",
  "stopTtc",
  "stopAssistDelta",
  "stopDistanceBuffer",
  "stopLeadDistance",
  "stopLeadVRel",
  "stopRequestedDecel",
  "stopActualDecel",
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
)

CAN_CARD_FIELDS = (
  "brickpilotPhevFaB4U8",
  "brickpilotPhevFaB4S8",
  "brickpilotPhevFaB7U8",
  "brickpilotPhevFaB7S8",
  "brickpilotBrake065B3U8",
  "brickpilotBrake065B9U8",
  "brickpilotBrake065B10U8",
  "brickpilotBrake065B11U8",
  "brickpilotBrake065B12U8",
  "brickpilotBrake065B14U8",
  "brickpilotPhevBaB14U8",
  "brickpilotPhevE0S16Byte16Le",
  "brickpilotAdas310B17U8",
  "brickpilotAdas310B18U8",
)


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
    writer = csv.DictWriter(f, fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
      writer.writerow(row)


def as_float(value: Any, default: float = 0.0) -> float:
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return default
  return ret if math.isfinite(ret) else default


def as_int(value: Any, default: int = 0) -> int:
  if isinstance(value, bool):
    return 1 if value else 0
  lowered = str(value).lower()
  if lowered in {"true", "yes"}:
    return 1
  if lowered in {"false", "no"}:
    return 0
  try:
    return int(float(value))
  except (TypeError, ValueError):
    return default


def as_bool(value: Any) -> bool:
  if isinstance(value, bool):
    return value
  return str(value).lower() in {"1", "1.0", "true", "yes"}


def time_of(row: dict[str, Any]) -> float:
  return as_float(row.get("route_time_sec", row.get("t_sec", row.get("time_sec", 0.0))))


def classify_context(row: dict[str, Any]) -> str:
  active = as_bool(row.get("stopActive"))
  source = as_int(row.get("stopSource"))
  required_valid = as_bool(row.get("stopRequiredDecelValid"))
  source_valid = as_bool(row.get("stopSourceValid"))
  invalid = as_int(row.get("stopGeometryInvalidReason"))
  ttc = as_float(row.get("stopTtc"))
  bucket = as_int(row.get("stopDebtBucket"))

  if active and not required_valid:
    return "true_invalid_geometry"
  if source == 0:
    return "no_stop_source" if active or as_bool(row.get("stopShadowCandidate")) else "not_stop_context"
  if invalid == 3:
    return "far_nonclosing_lead"
  if invalid == 4:
    return "speed_range_invalid"
  if invalid == 2:
    return "distance_invalid"
  if invalid == 1:
    return "no_stop_source"
  if not source_valid or not required_valid:
    return "source_invalid"
  if ttc >= 10.0 and source == 1 and not active:
    return "high_ttc_mild_context"
  if bucket in BUCKET_NAMES:
    return BUCKET_NAMES[bucket]
  return "valid_stop_context"


def crosstab(rows: list[dict[str, Any]], field: str, names: dict[int, str] | None = None,
             active_only: bool = False, active_field: str = "stopActive") -> list[dict[str, Any]]:
  selected = [r for r in rows if not active_only or as_bool(r.get(active_field))]
  counts: Counter[str] = Counter()
  for row in selected:
    raw = as_int(row.get(field))
    label = names.get(raw, str(raw)) if names is not None else str(row.get(field, ""))
    counts[label] += 1
  total = sum(counts.values()) or 1
  return [{"value": key, "samples": val, "frac": val / total} for key, val in sorted(counts.items())]


def context_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  counts = Counter(classify_context(row) for row in rows)
  total = sum(counts.values()) or 1
  return [{"context": key, "samples": val, "frac": val / total} for key, val in sorted(counts.items())]


def numeric_summary(rows: list[dict[str, Any]], field: str, prefix: str) -> dict[str, Any]:
  values = [as_float(row.get(field), math.nan) for row in rows]
  values = [v for v in values if math.isfinite(v)]
  if not values:
    return {}
  return {
    f"{prefix}{field}_mean": sum(values) / len(values),
    f"{prefix}{field}_min": min(values),
    f"{prefix}{field}_max": max(values),
  }


def bool_frac(rows: list[dict[str, Any]], field: str) -> float:
  if not rows:
    return 0.0
  return sum(1 for row in rows if as_bool(row.get(field))) / len(rows)


def mode_name(rows: list[dict[str, Any]], field: str, names: dict[int, str]) -> str:
  if not rows:
    return ""
  counts = Counter(as_int(row.get(field)) for row in rows)
  raw, _ = counts.most_common(1)[0]
  return names.get(raw, str(raw))


def nearby_labels(predictions: list[dict[str, Any]], start: float, end: float) -> str:
  labels: list[str] = []
  for pred in predictions:
    p_start = as_float(pred.get("start_sec"))
    p_end = as_float(pred.get("end_sec"))
    if p_end >= start and p_start <= end:
      label = str(pred.get("target", ""))
      if label:
        labels.append(label)
  return "|".join(sorted(set(labels))[:12])


def event_triggers(shadow_rows: list[dict[str, Any]], predictions: list[dict[str, Any]], max_cards: int) -> list[dict[str, Any]]:
  triggers: list[dict[str, Any]] = []
  for row in shadow_rows:
    t = time_of(row)
    if as_bool(row.get("stopActive")):
      triggers.append({"time_sec": t, "trigger": "active_stop_assist", "score": abs(as_float(row.get("stopAssistDelta")))})
    if as_float(row.get("stopPlannerDebt")) >= 0.5:
      triggers.append({"time_sec": t, "trigger": "planner_debt", "score": as_float(row.get("stopPlannerDebt"))})
    if as_float(row.get("stopControllerDebt")) >= 0.35:
      triggers.append({"time_sec": t, "trigger": "controller_underbrake", "score": as_float(row.get("stopControllerDebt"))})
    if as_int(row.get("stopDebtBucket")) == 5:
      triggers.append({"time_sec": t, "trigger": "creep_hold_bucket", "score": as_float(row.get("stopBrakeDebt"))})

  for pred in predictions:
    target = str(pred.get("target", ""))
    if target in STOP_EVENT_LABELS:
      triggers.append({
        "time_sec": (as_float(pred.get("start_sec")) + as_float(pred.get("end_sec"))) / 2.0,
        "trigger": f"label:{target}",
        "score": as_float(pred.get("peak_score"), 0.0),
      })

  triggers.sort(key=lambda row: float(row["score"]), reverse=True)
  selected: list[dict[str, Any]] = []
  for trig in triggers:
    t = float(trig["time_sec"])
    if any(abs(t - float(prev["time_sec"])) < 8.0 for prev in selected):
      continue
    selected.append(trig)
    if len(selected) >= max_cards:
      break
  selected.sort(key=lambda row: float(row["time_sec"]))
  return selected


def build_event_cards(shadow_rows: list[dict[str, Any]], carstate_rows: list[dict[str, Any]],
                      predictions: list[dict[str, Any]], route_id: str, window_sec: float,
                      max_cards: int) -> list[dict[str, Any]]:
  cards: list[dict[str, Any]] = []
  half_window = window_sec / 2.0
  for idx, trig in enumerate(event_triggers(shadow_rows, predictions, max_cards), start=1):
    center = float(trig["time_sec"])
    start = max(0.0, center - half_window)
    end = center + half_window
    shadow_bucket = [row for row in shadow_rows if start <= time_of(row) <= end]
    can_bucket = [row for row in carstate_rows if start <= time_of(row) <= end]
    card: dict[str, Any] = {
      "card_id": idx,
      "route_id": route_id,
      "start_sec": round(start, 3),
      "end_sec": round(end, 3),
      "trigger": trig["trigger"],
      "trigger_score": round(float(trig["score"]), 6),
      "shadow_samples": len(shadow_bucket),
      "carstatesp_samples": len(can_bucket),
      "nearby_labels": nearby_labels(predictions, start, end),
      "classification": classify_context(max(shadow_bucket, key=lambda row: as_float(row.get("stopBrakeDebt"), 0.0))) if shadow_bucket else "",
      "stop_active_frac": bool_frac(shadow_bucket, "stopActive"),
      "stop_shadow_frac": bool_frac(shadow_bucket, "stopShadowCandidate"),
      "required_decel_valid_frac": bool_frac(shadow_bucket, "stopRequiredDecelValid"),
      "source_valid_frac": bool_frac(shadow_bucket, "stopSourceValid"),
      "source_mode": mode_name(shadow_bucket, "stopSource", SOURCE_NAMES),
      "reason_mode": mode_name(shadow_bucket, "stopAssistReason", REASON_NAMES),
      "bucket_mode": mode_name(shadow_bucket, "stopDebtBucket", BUCKET_NAMES),
      "brake_state_mode": mode_name(shadow_bucket, "stopBrakeState", BRAKE_STATE_NAMES),
      "stop_mode": mode_name(shadow_bucket, "stopMode", STOP_MODE_NAMES),
      "lead_pacing_mode": mode_name(shadow_bucket, "leadPacingMode", LEAD_PACING_MODE_NAMES),
      "final_stop_blocked_reason_mode": mode_name(shadow_bucket, "finalStopBlockedReason", FINAL_STOP_BLOCKED_REASON_NAMES),
    }
    active_bucket = [row for row in shadow_bucket if as_bool(row.get("stopActive"))]
    longitudinal_active_bucket = [row for row in shadow_bucket if as_bool(row.get("longitudinalAssistActive"))]
    if active_bucket:
      card["dominant_active_reason"] = mode_name(active_bucket, "stopAssistReason", REASON_NAMES)
      card["dominant_active_stop_mode"] = mode_name(active_bucket, "stopMode", STOP_MODE_NAMES)
      card["dominant_active_source"] = mode_name(active_bucket, "stopSource", SOURCE_NAMES)
    else:
      card["dominant_active_reason"] = "none"
      card["dominant_active_stop_mode"] = "none"
      card["dominant_active_source"] = "none"
    if longitudinal_active_bucket:
      card["dominant_active_lead_pacing_mode"] = mode_name(longitudinal_active_bucket, "leadPacingMode", LEAD_PACING_MODE_NAMES)
    else:
      card["dominant_active_lead_pacing_mode"] = "none"
    for field in EVENT_CARD_FIELDS:
      card.update(numeric_summary(shadow_bucket, field, ""))
    for field in CAN_CARD_FIELDS:
      card.update(numeric_summary(can_bucket, field, "can_"))
    cards.append(card)
  return cards


def route_label_counts(prelim_dir: Path, route_id: str) -> tuple[int | None, int | None]:
  for row in read_csv(prelim_dir / "route_summary.csv"):
    if row.get("route_id") == route_id:
      return as_int(row.get("atomic_label_count")), as_int(row.get("voice_bookmark_count"))
  return None, None


def label_coverage(prelim_dir: Path, route_id: str, predictions: list[dict[str, Any]],
                   shadow_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  duration = max((time_of(row) for row in shadow_rows), default=0.0)
  labels = Counter(str(row.get("target", "")) for row in predictions if row.get("target"))
  stop_labels = sum(count for label, count in labels.items() if label in STOP_EVENT_LABELS)
  atomic_count, bookmark_count = route_label_counts(prelim_dir, route_id)
  if atomic_count == 0 and bookmark_count == 0:
    note = "no reviewed voice/manual labels for this route; treat as telemetry-only route"
  elif atomic_count is None:
    note = (
      "route label counts unavailable; prediction intervals are weak-labeler output, "
      "not reviewed UX attribution"
    )
  else:
    note = "reviewed labels present; still inspect event-card phase/context before promotion"
  return [{
    "route_duration_sec": round(duration, 3),
    "reviewed_atomic_label_count": "" if atomic_count is None else atomic_count,
    "voice_bookmark_count": "" if bookmark_count is None else bookmark_count,
    "prediction_intervals": len(predictions),
    "unique_prediction_labels": len(labels),
    "stop_event_prediction_intervals": stop_labels,
    "label_coverage_note": note,
  }]


def write_report(out_dir: Path, route_id: str, cards: list[dict[str, Any]], source_rows: list[dict[str, Any]],
                 reason_rows: list[dict[str, Any]], validity_rows: list[dict[str, Any]],
                 coverage_rows: list[dict[str, Any]], stop_mode_rows: list[dict[str, Any]],
                 blocked_reason_rows: list[dict[str, Any]],
                 lead_pacing_mode_rows: list[dict[str, Any]]) -> None:
  coverage = coverage_rows[0] if coverage_rows else {}
  lines = [
    f"# Stop Stack Event Cards: {route_id}",
    "",
    "## Coverage",
    f"- Route duration seen in shadow samples: {coverage.get('route_duration_sec', 0)} sec",
    f"- Prediction intervals available: {coverage.get('prediction_intervals', 0)}",
    f"- Label coverage note: {coverage.get('label_coverage_note', '')}",
    "",
    "## Active Stop Assist Source Mix",
  ]
  for row in source_rows:
    lines.append(f"- {row['value']}: {row['samples']} samples ({float(row['frac']):.3f})")
  lines.extend(["", "## Active Stop Assist Reason Mix"])
  for row in reason_rows:
    lines.append(f"- {row['value']}: {row['samples']} samples ({float(row['frac']):.3f})")
  lines.extend(["", "## Active Stop Mode Mix"])
  for row in stop_mode_rows:
    lines.append(f"- {row['value']}: {row['samples']} samples ({float(row['frac']):.3f})")
  lines.extend(["", "## Active Lead-Pacing Mode Mix"])
  for row in lead_pacing_mode_rows:
    lines.append(f"- {row['value']}: {row['samples']} samples ({float(row['frac']):.3f})")
  lines.extend(["", "## Final-Stop Blocked Reason Mix"])
  for row in blocked_reason_rows:
    lines.append(f"- {row['value']}: {row['samples']} samples ({float(row['frac']):.3f})")
  lines.extend(["", "## Required-Decel Validity Mix"])
  for row in validity_rows:
    lines.append(f"- {row['value']}: {row['samples']} samples ({float(row['frac']):.3f})")
  lines.extend(["", "## Top Event Cards"])
  for card in cards[:12]:
    lines.append(
      f"- {float(card['start_sec']):.1f}-{float(card['end_sec']):.1f}s "
      f"`{card['trigger']}` class={card.get('classification', '')} "
      f"source={card.get('source_mode', '')} reason={card.get('dominant_active_reason', card.get('reason_mode', ''))} "
      f"mode={card.get('dominant_active_stop_mode', card.get('stop_mode', ''))} "
      f"lead_pacing={card.get('dominant_active_lead_pacing_mode', card.get('lead_pacing_mode', ''))} "
      f"final_block={card.get('final_stop_blocked_reason_mode', '')} "
      f"labels={card.get('nearby_labels', '')}"
    )
  lines.append("")
  (out_dir / "stop_stack_event_report.md").write_text("\n".join(lines), encoding="utf-8")


def run(prelim_dir: Path, out_dir: Path, route_id: str, window_sec: float, max_cards: int) -> None:
  shadow_rows = read_csv(prelim_dir / "brickpilot_shadow_samples.csv")
  carstate_rows = read_csv(prelim_dir / "carstatesp_shadow_samples.csv")
  predictions = read_csv(prelim_dir / "test_route_predictions.csv") or read_csv(prelim_dir / "prediction_timeline.csv")
  out_dir.mkdir(parents=True, exist_ok=True)

  cards = build_event_cards(shadow_rows, carstate_rows, predictions, route_id, window_sec, max_cards)
  source_rows = crosstab(shadow_rows, "stopSource", SOURCE_NAMES, active_only=True)
  reason_rows = crosstab(shadow_rows, "stopAssistReason", REASON_NAMES, active_only=True)
  stop_mode_rows = crosstab(shadow_rows, "stopMode", STOP_MODE_NAMES, active_only=True)
  lead_pacing_mode_rows = crosstab(shadow_rows, "leadPacingMode", LEAD_PACING_MODE_NAMES,
                                   active_only=True, active_field="longitudinalAssistActive")
  blocked_reason_rows = crosstab(shadow_rows, "finalStopBlockedReason", FINAL_STOP_BLOCKED_REASON_NAMES, active_only=False)
  bucket_rows = crosstab(shadow_rows, "stopDebtBucket", BUCKET_NAMES, active_only=True)
  validity_rows = crosstab(shadow_rows, "stopRequiredDecelValid", {0: "invalid", 1: "valid"}, active_only=True)
  driver_rows = crosstab(shadow_rows, "stopDebtBucket", BUCKET_NAMES, active_only=True)
  context_rows = context_summary(shadow_rows)
  coverage_rows = label_coverage(prelim_dir, route_id, predictions, shadow_rows)

  write_csv(out_dir / "stop_event_cards.csv", cards)
  write_csv(out_dir / "active_stop_assist_by_source.csv", source_rows)
  write_csv(out_dir / "active_stop_assist_by_reason.csv", reason_rows)
  write_csv(out_dir / "active_stop_assist_by_stop_mode.csv", stop_mode_rows)
  write_csv(out_dir / "active_lead_pacing_by_mode.csv", lead_pacing_mode_rows)
  write_csv(out_dir / "final_stop_blocked_reason_mix.csv", blocked_reason_rows)
  write_csv(out_dir / "active_stop_assist_by_bucket.csv", bucket_rows)
  write_csv(out_dir / "active_stop_assist_by_required_decel_valid.csv", validity_rows)
  write_csv(out_dir / "active_stop_assist_by_driver_override.csv", [row for row in driver_rows if row["value"] == "driver_override"])
  write_csv(out_dir / "stop_context_bucket_summary.csv", context_rows)
  write_csv(out_dir / "label_coverage_summary.csv", coverage_rows)
  write_report(out_dir, route_id, cards, source_rows, reason_rows, validity_rows, coverage_rows,
               stop_mode_rows, blocked_reason_rows, lead_pacing_mode_rows)


def main() -> None:
  parser = argparse.ArgumentParser(description="Build Brickpilot stop-stack event cards from a prelim route analysis directory.")
  parser.add_argument("prelim_dir", type=Path)
  parser.add_argument("--route-id", default="")
  parser.add_argument("--output-dir", type=Path)
  parser.add_argument("--event-window-sec", type=float, default=20.0)
  parser.add_argument("--max-cards", type=int, default=24)
  args = parser.parse_args()
  route_id = args.route_id or args.prelim_dir.name
  out_dir = args.output_dir or args.prelim_dir / "stop_stack_events"
  run(args.prelim_dir, out_dir, route_id, args.event_window_sec, args.max_cards)
  print(f"wrote stop-stack event cards to {out_dir}")


if __name__ == "__main__":
  main()
