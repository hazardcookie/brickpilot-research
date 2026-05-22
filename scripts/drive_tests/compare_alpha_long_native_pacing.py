#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any


SUMMARY_FIELDS = (
  "longitudinalAssistActive",
  "longitudinalAssistShadowCandidate",
  "longitudinalAssistDelta",
  "speedDeficit",
  "accelCmd",
  "stopActive",
  "stopShadowCandidate",
  "stopRequiredDecel",
  "stopPlannerDebt",
  "stopControllerDebt",
  "stopBrakeDebt",
  "stopAssistDelta",
  "stopDistanceBuffer",
  "leadPacingAssistDelta",
)

LABELS_OF_INTEREST = (
  "pacing_good",
  "pacing_bad",
  "pacing_bursty",
  "rolling_follow_good",
  "follow_distance_too_far",
  "overbraked_rolling_traffic",
  "too_early_brake",
  "unnecessary_braking",
  "driver_gas_after_brake",
  "driver_brake_intervention",
  "lead_brake_good",
  "lead_brake_bad",
  "missed_stop",
  "stop_complete",
  "stop_creep_fail",
  "stop_hold_fail",
)


def read_csv(path: Path) -> list[dict[str, str]]:
  if not path.exists():
    return []
  with path.open(newline="", encoding="utf-8") as f:
    return list(csv.DictReader(f))


def as_float(value: Any, default: float = 0.0) -> float:
  try:
    return float(value)
  except (TypeError, ValueError):
    return default


def summary_by_field(prelim_dir: Path) -> dict[str, dict[str, str]]:
  return {row.get("field", ""): row for row in read_csv(prelim_dir / "brickpilot_shadow_summary.csv")}


def label_seconds(prelim_dir: Path) -> dict[str, float]:
  return {row.get("target", ""): as_float(row.get("seconds")) for row in read_csv(prelim_dir / "prediction_label_summary.csv")}


def crosstab(path: Path) -> dict[str, float]:
  return {row.get("value", ""): as_float(row.get("frac")) for row in read_csv(path)}


def coverage(prelim_dir: Path) -> dict[str, str]:
  rows = read_csv(prelim_dir / "stop_stack_events" / "label_coverage_summary.csv")
  return rows[0] if rows else {}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
  if not rows:
    path.write_text("", encoding="utf-8")
    return
  fields: list[str] = []
  for row in rows:
    for key in row:
      if key not in fields:
        fields.append(key)
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)


def build_rows(on_dir: Path, off_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  on_summary = summary_by_field(on_dir)
  off_summary = summary_by_field(off_dir)
  metric_rows: list[dict[str, Any]] = []
  for field in SUMMARY_FIELDS:
    on = on_summary.get(field, {})
    off = off_summary.get(field, {})
    metric_rows.append({
      "metric": field,
      "alpha_on_mean": on.get("mean", ""),
      "alpha_on_nonzero_frac": on.get("nonzero_frac", ""),
      "alpha_on_min": on.get("min", ""),
      "alpha_on_max": on.get("max", ""),
      "alpha_off_mean": off.get("mean", ""),
      "alpha_off_nonzero_frac": off.get("nonzero_frac", ""),
      "alpha_off_min": off.get("min", ""),
      "alpha_off_max": off.get("max", ""),
    })

  on_labels = label_seconds(on_dir)
  off_labels = label_seconds(off_dir)
  label_rows = [{
    "label": label,
    "alpha_on_predicted_seconds": on_labels.get(label, 0.0),
    "alpha_off_predicted_seconds": off_labels.get(label, 0.0),
  } for label in LABELS_OF_INTEREST]
  return metric_rows, label_rows


def write_report(out_dir: Path, on_dir: Path, off_dir: Path, metric_rows: list[dict[str, Any]],
                 label_rows: list[dict[str, Any]]) -> None:
  on_cov = coverage(on_dir)
  off_cov = coverage(off_dir)
  on_reason = crosstab(on_dir / "stop_stack_events" / "active_stop_assist_by_reason.csv")
  on_mode = crosstab(on_dir / "stop_stack_events" / "active_stop_assist_by_stop_mode.csv")
  off_reason = crosstab(off_dir / "stop_stack_events" / "active_stop_assist_by_reason.csv")

  def metric(field: str, key: str) -> str:
    for row in metric_rows:
      if row["metric"] == field:
        return str(row.get(key, ""))
    return ""

  lines = [
    "# Alpha Long ON/OFF Native Pacing Comparison",
    "",
    "## Routes",
    f"- Alpha Long ON prelim: `{on_dir}`",
    f"- Alpha Long OFF prelim: `{off_dir}`",
    f"- Alpha ON reviewed labels/bookmarks: {on_cov.get('reviewed_atomic_label_count', '')}/{on_cov.get('voice_bookmark_count', '')}",
    f"- Alpha OFF reviewed labels/bookmarks: {off_cov.get('reviewed_atomic_label_count', '')}/{off_cov.get('voice_bookmark_count', '')}",
    "",
    "## Control Shape",
    f"- Alpha ON live assist nonzero fraction: {metric('longitudinalAssistActive', 'alpha_on_nonzero_frac')}",
    f"- Alpha OFF live assist nonzero fraction: {metric('longitudinalAssistActive', 'alpha_off_nonzero_frac')}",
    f"- Alpha ON stop assist nonzero fraction: {metric('stopActive', 'alpha_on_nonzero_frac')}",
    f"- Alpha OFF stop assist nonzero fraction: {metric('stopActive', 'alpha_off_nonzero_frac')}",
    f"- Alpha ON max stop assist delta: {metric('stopAssistDelta', 'alpha_on_min')}",
    f"- Alpha OFF max stop assist delta: {metric('stopAssistDelta', 'alpha_off_min')}",
    "",
    "## Active Alpha ON Stop Assist Mix",
  ]
  if on_reason:
    for key, frac in sorted(on_reason.items()):
      lines.append(f"- reason `{key}`: {frac:.3f}")
  else:
    lines.append("- no active stop assist samples")
  lines.append("")
  lines.append("## Active Alpha ON Stop Mode Mix")
  if on_mode:
    for key, frac in sorted(on_mode.items()):
      lines.append(f"- mode `{key}`: {frac:.3f}")
  else:
    lines.append("- no active stop assist samples")
  lines.extend([
    "",
    "## Alpha OFF Reference",
    "- Alpha OFF has no Brickpilot live stop assist by design; treat it as a native/SCC pacing shape reference, not as Brickpilot control output.",
  ])
  if off_reason:
    lines.append("- Warning: active stop rows were present in OFF reports; inspect settings/trusted metadata.")
  lines.extend([
    "",
    "## Label Signal Of Interest",
  ])
  for row in label_rows:
    on_sec = float(row["alpha_on_predicted_seconds"])
    off_sec = float(row["alpha_off_predicted_seconds"])
    if on_sec > 0.0 or off_sec > 0.0:
      lines.append(f"- `{row['label']}`: Alpha ON {on_sec:.1f}s, Alpha OFF {off_sec:.1f}s")
  lines.extend([
    "",
    "## R&D Read",
    "- The native/OFF route remains the best available reference for low-speed lead pacing and rolling traffic.",
    "- The 0.5.7 build should mimic the native shape with small signed lead-pacing deltas, not more final-stop authority.",
    "- Success should be lower `follow_distance_too_far`, `pacing_bad`, `pacing_bursty`, `overbraked_rolling_traffic`, and `driver_gas_after_brake` without increasing missed stops or driver-brake interventions.",
    "",
  ])
  (out_dir / "alpha_long_native_pacing_comparison.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
  parser = argparse.ArgumentParser(description="Compare one Alpha Long ON prelim export against one Alpha Long OFF/native reference export.")
  parser.add_argument("--alpha-on", type=Path, required=True)
  parser.add_argument("--alpha-off", type=Path, required=True)
  parser.add_argument("--output-dir", type=Path, required=True)
  args = parser.parse_args()

  args.output_dir.mkdir(parents=True, exist_ok=True)
  metric_rows, label_rows = build_rows(args.alpha_on, args.alpha_off)
  write_csv(args.output_dir / "alpha_long_native_pacing_metrics.csv", metric_rows)
  write_csv(args.output_dir / "alpha_long_native_pacing_labels.csv", label_rows)
  write_report(args.output_dir, args.alpha_on, args.alpha_off, metric_rows, label_rows)
  print(args.output_dir)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
