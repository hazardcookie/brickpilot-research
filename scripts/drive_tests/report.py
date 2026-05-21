#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any


def rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
  conn.row_factory = sqlite3.Row
  return [dict(row) for row in conn.execute(query, params).fetchall()]


def fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
  if value is None:
    return "-"
  if isinstance(value, bool):
    return "yes" if value else "no"
  try:
    value = float(value)
  except (TypeError, ValueError):
    return str(value)
  return f"{value:.{digits}f}{suffix}"


def fmt_duration(seconds: Any) -> str:
  if seconds is None:
    return "-"
  seconds = float(seconds)
  minutes = seconds / 60.0
  if minutes < 60:
    return f"{minutes:.1f} min"
  return f"{minutes / 60.0:.1f} hr"


def fmt_distance(meters: Any) -> str:
  if meters is None:
    return "-"
  return f"{float(meters) / 1609.344:.2f} mi"


def markdown_table(headers: list[str], table_rows: list[list[Any]]) -> str:
  out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
  for row in table_rows:
    out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
  return "\n".join(out)


def compact_settings(settings_json: str | None) -> str:
  if not settings_json:
    return "-"
  try:
    settings = json.loads(settings_json)
  except json.JSONDecodeError:
    return settings_json
  if not settings:
    return "-"
  return ", ".join(f"{k}={v}" for k, v in settings.items())


def per_10_min(seconds: Any, duration: Any) -> float | None:
  if seconds is None or duration is None or float(duration) <= 0:
    return None
  return float(seconds) / float(duration) * 600.0


def best_row(metric_rows: list[dict[str, Any]], key: str, lower_is_better: bool = True,
             require_key: str | None = None) -> dict[str, Any] | None:
  candidates = []
  for row in metric_rows:
    if row.get(key) is None:
      continue
    if require_key is not None and (row.get(require_key) or 0) <= 0:
      continue
    candidates.append(row)
  if not candidates:
    return None
  return sorted(candidates, key=lambda r: r[key], reverse=not lower_is_better)[0]


def route_metrics(conn: sqlite3.Connection, scope: str = "post_warmup") -> list[dict[str, Any]]:
  return rows(conn, """
    SELECT m.*, r.route_id, r.settings_json, r.notes, r.available_segments, r.missing_segments, r.qlog_segments,
           r.total_duration_sec, r.estimated_distance_m, r.weight_in_overall_score, r.car_params_json
      FROM route_metrics m
      JOIN routes r ON r.label = m.route_label
     WHERE m.scope=?
     ORDER BY r.weight_in_overall_score DESC, m.model
  """, (scope,))


def median_stop_go_by_route(conn: sqlite3.Connection) -> dict[str, float | None]:
  ret = {}
  for route in rows(conn, "SELECT label FROM routes ORDER BY label"):
    vals = [r["time_to_5mph_sec"] for r in rows(conn, """
      SELECT time_to_5mph_sec FROM stop_go_events
       WHERE route_label=? AND post_warmup=1 AND time_to_5mph_sec IS NOT NULL
    """, (route["label"],))]
    ret[route["label"]] = median(vals) if vals else None
  return ret


def comparison_table(conn: sqlite3.Connection) -> str:
  stop_go = median_stop_go_by_route(conn)
  table = []
  for row in route_metrics(conn, "post_warmup"):
    pinned_per_10 = per_10_min(row.get("pinned_output_time_sec"), row.get("duration_sec"))
    table.append([
      row["model"],
      fmt(row.get("lateral_error_rms"), 4),
      fmt(row.get("lateral_error_p95_abs"), 4),
      fmt(row.get("lateral_error_max_abs"), 4),
      fmt(pinned_per_10, 2, "s"),
      fmt(row.get("longest_pinned_burst_sec"), 2, "s"),
      fmt(row.get("steering_pressed_pct"), 2, "%"),
      fmt(row.get("angle_offset_deg_avg"), 3),
      fmt(row.get("live_torque_use_params_pct"), 1, "%"),
      fmt(row.get("no_lead_avg_speed_deficit_mph"), 2, " mph"),
      fmt(row.get("no_lead_p95_speed_deficit_mph"), 2, " mph"),
      fmt(row.get("lazy_accel_time_sec"), 1, "s"),
      fmt(stop_go.get(row["route_label"]), 2, "s"),
    ])
  return markdown_table([
    "model", "clean lateral RMS", "p95 lat err", "max clean lat err", "pinned/10m",
    "longest pinned", "steerPressed", "avg angleOffset", "useParams",
    "no-lead avg deficit", "no-lead p95 deficit", "lazy accel", "SNG med 5mph",
  ], table)


def route_catalog_table(conn: sqlite3.Connection) -> str:
  table = []
  warning_counts = {r["route_label"]: r["cnt"] for r in rows(conn, "SELECT route_label, COUNT(*) AS cnt FROM warnings GROUP BY route_label")}
  for route in rows(conn, "SELECT * FROM routes ORDER BY label"):
    table.append([
      route["model"],
      route["route_id"],
      compact_settings(route["settings_json"]),
      route["available_segments"] or "-",
      fmt_duration(route["total_duration_sec"]),
      fmt_distance(route["estimated_distance_m"]),
      warning_counts.get(route["label"], 0),
      route["missing_segments"] or "-",
      route["qlog_segments"] or "-",
    ])
  return markdown_table(["model", "route ID", "settings", "available segments", "duration", "distance", "warnings", "missing", "qlog"], table)


def segment_tables(conn: sqlite3.Connection) -> str:
  parts = []
  for route in rows(conn, "SELECT label, model FROM routes ORDER BY label"):
    segs = rows(conn, """
      SELECT s.segment_index, s.log_type, s.status, s.duration_sec, s.distance_m, s.warning_count,
             m.lateral_error_rms, m.lateral_error_p95_abs, m.pinned_output_time_sec,
             m.no_lead_avg_speed_deficit_mph, m.lazy_accel_time_sec
        FROM segments s
        LEFT JOIN segment_metrics m
          ON m.route_label=s.route_label AND m.segment_index=s.segment_index AND m.scope='all'
       WHERE s.route_label=?
       ORDER BY s.segment_index
    """, (route["label"],))
    table = []
    for seg in segs:
      table.append([
        seg["segment_index"], seg["log_type"], seg["status"], fmt_duration(seg["duration_sec"]),
        fmt_distance(seg["distance_m"]), fmt(seg["lateral_error_rms"], 4),
        fmt(seg["lateral_error_p95_abs"], 4), fmt(seg["pinned_output_time_sec"], 2, "s"),
        fmt(seg["no_lead_avg_speed_deficit_mph"], 2, " mph"), fmt(seg["lazy_accel_time_sec"], 1, "s"),
        seg["warning_count"],
      ])
    parts.append(f"### {route['model']}\n" + markdown_table([
      "seg", "log", "status", "duration", "distance", "RMS", "p95", "pinned", "no-lead deficit", "lazy", "warnings",
    ], table))
  return "\n\n".join(parts)


def lateral_regime_table(conn: sqlite3.Connection) -> str:
  rows_ = rows(conn, """
    SELECT model, regime_name, duration_sec, clean_lateral_sample_count, lateral_error_rms,
           lateral_error_p95_abs, pinned_output_time_sec, longest_pinned_burst_sec
      FROM regime_metrics
     WHERE scope='post_warmup' AND segment_index IS NULL AND regime_type='speed'
       AND regime_name IN ('neighborhood', 'backroad', 'highway')
     ORDER BY regime_name, model
  """)
  table = []
  for row in rows_:
    table.append([
      row["regime_name"], row["model"], fmt_duration(row["duration_sec"]),
      row["clean_lateral_sample_count"], fmt(row["lateral_error_rms"], 4),
      fmt(row["lateral_error_p95_abs"], 4), fmt(row["pinned_output_time_sec"], 2, "s"),
      fmt(row["longest_pinned_burst_sec"], 2, "s"),
    ])
  return markdown_table(["regime", "model", "duration", "clean samples", "RMS", "p95", "pinned", "longest burst"], table)


def pinned_summary(conn: sqlite3.Connection) -> str:
  table = []
  for row in rows(conn, """
    SELECT route_label, model, COUNT(*) AS bursts, SUM(duration_sec) AS total_sec,
           MAX(duration_sec) AS longest_sec, MAX(max_abs_lateral_error) AS max_err
      FROM pinned_bursts
     WHERE post_warmup=1
     GROUP BY route_label, model
     ORDER BY total_sec DESC
  """):
    table.append([row["model"], row["bursts"], fmt(row["total_sec"], 2, "s"), fmt(row["longest_sec"], 2, "s"), fmt(row["max_err"], 4)])
  if not table:
    return "No post-warmup pinned bursts were detected."
  return markdown_table(["model", "bursts", "total pinned", "longest", "max abs error"], table)


def longitudinal_event_summary(conn: sqlite3.Connection) -> str:
  catchups = rows(conn, """
    SELECT model, COUNT(*) AS events, SUM(CASE WHEN lazy_flag=1 THEN 1 ELSE 0 END) AS lazy_events,
           AVG(duration_sec) AS avg_duration, AVG(max_speed_deficit_mph) AS avg_max_deficit,
           AVG(mean_accel_cmd) AS avg_accel_cmd, AVG(mean_actual_a_ego) AS avg_actual
      FROM acceleration_events
     WHERE post_warmup=1
     GROUP BY model
     ORDER BY avg_max_deficit DESC
  """)
  launches = rows(conn, """
    SELECT model, COUNT(*) AS events, AVG(time_to_5mph_sec) AS avg_to_5,
           AVG(mean_accel_cmd) AS avg_accel_cmd, AVG(mean_actual_a_ego) AS avg_actual
      FROM stop_go_events
     WHERE post_warmup=1
     GROUP BY model
     ORDER BY avg_to_5
  """)
  parts = []
  if catchups:
    parts.append(markdown_table(["model", "catch-up events", "lazy events", "avg duration", "avg max deficit", "mean cmd", "mean actual"], [
      [r["model"], r["events"], r["lazy_events"], fmt(r["avg_duration"], 2, "s"), fmt(r["avg_max_deficit"], 2, " mph"), fmt(r["avg_accel_cmd"], 2), fmt(r["avg_actual"], 2)]
      for r in catchups
    ]))
  else:
    parts.append("No post-warmup acceleration catch-up events were detected.")

  if launches:
    parts.append(markdown_table(["model", "stop-go events", "avg time to 5 mph", "mean cmd", "mean actual"], [
      [r["model"], r["events"], fmt(r["avg_to_5"], 2, "s"), fmt(r["avg_accel_cmd"], 2), fmt(r["avg_actual"], 2)]
      for r in launches
    ]))
  else:
    parts.append("No post-warmup stop-and-go launches were detected.")
  return "\n\n".join(parts)


def acceleration_diagnosis(conn: sqlite3.Connection) -> str:
  lazy = rows(conn, """
    SELECT model, start_route_time_sec, duration_sec, max_speed_deficit_mph, mean_accel_cmd, max_accel_cmd, mean_actual_a_ego, end_reason
      FROM acceleration_events
     WHERE post_warmup=1
     ORDER BY lazy_flag DESC, max_speed_deficit_mph DESC
     LIMIT 8
  """)
  if not lazy:
    return "There were no clear post-warmup catch-up events to diagnose."
  notes = []
  for e in lazy:
    if e["mean_accel_cmd"] is not None and e["mean_accel_cmd"] < 0.5:
      diagnosis = "planner/controller requested low accel"
    elif e["max_accel_cmd"] is not None and e["max_accel_cmd"] >= 1.0 and e["mean_actual_a_ego"] is not None and e["mean_actual_a_ego"] < 0.4:
      diagnosis = "actual acceleration lagged a reasonable command"
    else:
      diagnosis = "mixed or inconclusive"
    notes.append(f"- {e['model']} at {fmt(e['start_route_time_sec'], 1)}s: max deficit {fmt(e['max_speed_deficit_mph'], 1, ' mph')}, mean cmd {fmt(e['mean_accel_cmd'], 2)}, mean actual {fmt(e['mean_actual_a_ego'], 2)} -> {diagnosis} ({e['end_reason']}).")
  return "\n".join(notes)


def appendix(conn: sqlite3.Connection) -> str:
  top_lat = rows(conn, """
    SELECT model, rank, segment_index, route_time_sec, speed_mph, speed_regime, turn_regime,
           abs_lateral_error, torque_output, steering_angle_deg
      FROM lateral_events
     WHERE rank <= 10
     ORDER BY model, rank
  """)
  top_accel = rows(conn, """
    SELECT model, event_index, segment_index, start_route_time_sec, duration_sec,
           max_speed_deficit_mph, mean_accel_cmd, max_accel_cmd, mean_actual_a_ego, lazy_flag, end_reason
      FROM acceleration_events
     ORDER BY max_speed_deficit_mph DESC
     LIMIT 20
  """)
  warnings = rows(conn, "SELECT * FROM warnings ORDER BY route_label, segment_index, message LIMIT 200")
  parts = ["### Top lateral events"]
  parts.append(markdown_table(["model", "rank", "seg", "route sec", "mph", "speed regime", "turn", "abs err", "output", "steer angle"], [
    [r["model"], r["rank"], r["segment_index"], fmt(r["route_time_sec"], 1), fmt(r["speed_mph"], 1), r["speed_regime"],
     r["turn_regime"], fmt(r["abs_lateral_error"], 4), fmt(r["torque_output"], 3), fmt(r["steering_angle_deg"], 1)]
    for r in top_lat
  ]) if top_lat else "No lateral events were recorded.")
  parts.append("### Top acceleration-lag events")
  parts.append(markdown_table(["model", "event", "seg", "start sec", "duration", "max deficit", "mean cmd", "max cmd", "mean actual", "lazy", "end"], [
    [r["model"], r["event_index"], r["segment_index"], fmt(r["start_route_time_sec"], 1), fmt(r["duration_sec"], 2, "s"),
     fmt(r["max_speed_deficit_mph"], 2, " mph"), fmt(r["mean_accel_cmd"], 2), fmt(r["max_accel_cmd"], 2),
     fmt(r["mean_actual_a_ego"], 2), "yes" if r["lazy_flag"] else "no", r["end_reason"]]
    for r in top_accel
  ]) if top_accel else "No acceleration events were recorded.")
  parts.append("### Warnings and missing segments")
  parts.append(markdown_table(["route", "model", "seg", "severity", "message"], [
    [r["route_label"], r["model"], r["segment_index"], r["severity"], str(r["message"]).replace("|", "\\|")]
    for r in warnings
  ]) if warnings else "No warnings were recorded.")
  return "\n\n".join(parts)


def executive_summary(conn: sqlite3.Connection) -> str:
  metric_rows = [r for r in route_metrics(conn, "post_warmup") if (r.get("weight_in_overall_score") or 0) > 0]
  best_steering = best_row(metric_rows, "lateral_error_rms", True, "clean_lateral_sample_count")
  best_accel = best_row(metric_rows, "no_lead_avg_speed_deficit_mph", True, "no_lead_relaxed_time_sec")
  best_pinned = best_row(metric_rows, "pinned_output_time_sec", True, "clean_lateral_sample_count")
  best_rms = best_steering
  best_deficit = best_accel
  warnings = rows(conn, "SELECT COUNT(*) AS cnt FROM warnings")[0]["cnt"]
  qlogs = [r for r in rows(conn, "SELECT model, qlog_segments FROM routes WHERE qlog_segments IS NOT NULL AND qlog_segments != ''")]
  missing = [r for r in rows(conn, "SELECT model, missing_segments FROM routes WHERE missing_segments IS NOT NULL AND missing_segments != ''")]

  lines = []
  lines.append(f"- Steering smoothness: {best_steering['model']} has the lowest post-warmup clean lateral RMS ({fmt(best_steering['lateral_error_rms'], 4)}) among routes with clean samples." if best_steering else "- Steering smoothness: inconclusive; clean lateral samples were unavailable.")
  lines.append(f"- Acceleration / set-speed catch-up: {best_accel['model']} has the lowest post-warmup no-lead average speed deficit ({fmt(best_accel['no_lead_avg_speed_deficit_mph'], 2, ' mph')})." if best_accel else "- Acceleration / set-speed catch-up: inconclusive; no-lead set-speed samples were unavailable.")
  lines.append(f"- Fewest pinned lateral events: {best_pinned['model']} has the lowest post-warmup pinned output time ({fmt(best_pinned['pinned_output_time_sec'], 2, 's')})." if best_pinned else "- Fewest pinned lateral events: inconclusive.")
  lines.append(f"- Lowest clean lateral RMS: {best_rms['model']}." if best_rms else "- Lowest clean lateral RMS: unavailable.")
  lines.append(f"- Lowest no-lead speed deficit: {best_deficit['model']}." if best_deficit else "- Lowest no-lead speed deficit: unavailable.")
  if warnings or qlogs or missing:
    uncertainty = [f"{warnings} warning(s) recorded"]
    if qlogs:
      uncertainty.append("qlog fallback used for " + ", ".join(f"{r['model']} segs {r['qlog_segments']}" for r in qlogs))
    if missing:
      uncertainty.append("missing segments for " + ", ".join(f"{r['model']} segs {r['missing_segments']}" for r in missing))
    lines.append("- Uncertainty: " + "; ".join(uncertainty) + ".")
  lines.append("- Comparability note: North Nevada v2 is weighted lower in the catalog because it includes an intentional aggressive low-speed neighborhood turn; use regime and pinned-burst tables before treating it as a normal-driving model comparison.")
  return "\n".join(lines)


def car_params_summary(conn: sqlite3.Connection) -> str:
  table = []
  for route in rows(conn, "SELECT model, car_params_json FROM routes ORDER BY label"):
    try:
      cp = json.loads(route["car_params_json"] or "{}")
    except json.JSONDecodeError:
      cp = {}
    fw = cp.get("carFwSummary") or {}
    table.append([
      route["model"],
      cp.get("brand", "-"),
      cp.get("carFingerprint", "-"),
      cp.get("fingerprintSource", "-"),
      fmt(cp.get("mass"), 1),
      fmt(cp.get("wheelbase"), 3),
      fmt(cp.get("steerRatio"), 2),
      cp.get("lateralTuningType", "-"),
      cp.get("redactedCarVin", "-"),
      ",".join(fw.get("firmware_markers") or []) or "-",
    ])
  return markdown_table(["model", "brand", "fingerprint", "source", "mass", "wheelbase", "steerRatio", "lat tune", "VIN", "FW markers"], table)


def generate_report(db_path: Path, out_path: Path) -> None:
  conn = sqlite3.connect(db_path)
  try:
    parts = [
      "# Drive-Test Analysis Report",
      "Generated from local comma/sunnypilot logs. VINs are redacted and car firmware is summarized only at a high level.",
      "## Executive Summary",
      executive_summary(conn),
      "## Route Catalog",
      route_catalog_table(conn),
      "## High-Level Comparison",
      comparison_table(conn),
      "## CarParams Summary",
      car_params_summary(conn),
      "## Segment-Level Tables",
      segment_tables(conn),
      "## Lateral Analysis",
      "Post-warmup lateral metrics are split by speed regime so the older North Nevada stress turn does not dominate normal-driving comparisons.",
      lateral_regime_table(conn),
      "### Pinned Burst Summary",
      pinned_summary(conn),
      "## Longitudinal Analysis",
      "Catch-up events focus on clean longActive, no-lead-relaxed samples with at least a 5 mph set-speed deficit. Stop-go events start from standstill or below 0.5 mph and end above 5 mph.",
      longitudinal_event_summary(conn),
      "### Acceleration Diagnosis",
      acceleration_diagnosis(conn),
      "## Recommended Next Tests",
      "- Repeat the same route with the best two models from this report.\n- Keep SCC-V and SCC-M off for a clean acceleration comparison.\n- Test SCC-V on separately after the clean comparison.\n- Include at least one boring/no-intervention highway route and one neighborhood route.\n- Keep the YAML catalog updated with exact settings and route notes before each run.",
      "## Appendix",
      appendix(conn),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n\n".join(parts) + "\n")
  finally:
    conn.close()


def main() -> int:
  parser = argparse.ArgumentParser(description="Generate drive-test Markdown report from results.sqlite")
  parser.add_argument("--db", type=Path, required=True)
  parser.add_argument("--out", type=Path, required=True)
  args = parser.parse_args()
  generate_report(args.db, args.out)
  print(f"Wrote {args.out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
