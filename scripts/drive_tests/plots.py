#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", str(Path(os.environ.get("TMPDIR", "/tmp")) / "drive_tests_mplconfig"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, Circle


MS_TO_MPH = 2.2369362920544


MODEL_COLORS = {
  "OP 10 v3": "#2f6f8f",
  "Tomb Raider v16": "#7b4f9f",
  "WMI v12": "#b66a2f",
  "North Nevada v2": "#5b7f45",
}

MODEL_ORDER = ["OP 10 v3", "Tomb Raider v16", "WMI v12", "North Nevada v2"]


def dict_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
  conn.row_factory = sqlite3.Row
  return [dict(row) for row in conn.execute(query, params).fetchall()]


def save_bar(rows: list[dict[str, Any]], value_key: str, title: str, ylabel: str, out_path: Path,
             transform=None) -> None:
  labels = [r["model"] for r in rows]
  values = []
  for r in rows:
    val = r.get(value_key)
    if transform is not None:
      val = transform(r)
    values.append(0.0 if val is None else float(val))
  fig, ax = plt.subplots(figsize=(10, 5))
  ax.bar(labels, values, color="#3f7f93")
  ax.set_title(title)
  ax.set_ylabel(ylabel)
  ax.grid(axis="y", alpha=0.25)
  ax.tick_params(axis="x", rotation=20)
  fig.tight_layout()
  fig.savefig(out_path, dpi=150)
  plt.close(fig)


def safe_float(value: Any, default: float | None = None) -> float | None:
  try:
    if value is None:
      return default
    return float(value)
  except (TypeError, ValueError):
    return default


def fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
  value = safe_float(value)
  if value is None:
    return "-"
  return f"{value:.{digits}f}{suffix}"


def wrap_text(text: str, width: int) -> str:
  words = text.split()
  lines = []
  cur = []
  for word in words:
    if sum(len(w) for w in cur) + len(cur) + len(word) > width and cur:
      lines.append(" ".join(cur))
      cur = [word]
    else:
      cur.append(word)
  if cur:
    lines.append(" ".join(cur))
  return "\n".join(lines)


def lower_is_better_score(value: float | None, values: list[float]) -> float:
  if value is None or not values:
    return 0.0
  lo = min(values)
  hi = max(values)
  if hi <= lo:
    return 1.0
  return 1.0 - ((value - lo) / (hi - lo))


def add_card(fig, x: float, y: float, w: float, h: float, color: str) -> None:
  fig.patches.append(FancyBboxPatch(
    (x, y), w, h,
    boxstyle="round,pad=0.006,rounding_size=0.012",
    transform=fig.transFigure,
    linewidth=1.1,
    edgecolor="#d6d1c8",
    facecolor="#fffdf8",
    zorder=1,
  ))
  fig.patches.append(Rectangle(
    (x, y + h - 0.008), w, 0.008,
    transform=fig.transFigure,
    linewidth=0,
    facecolor=color,
    zorder=2,
  ))


def add_metric_bar(fig, x: float, y: float, w: float, label: str, value_text: str,
                   score: float, color: str) -> None:
  fig.text(x, y + 0.014, label, fontsize=9, color="#5b5f63", fontweight="bold", zorder=5)
  fig.text(x + w, y + 0.014, value_text, fontsize=9, color="#262b2f", ha="right", zorder=5)
  fig.patches.append(Rectangle((x, y), w, 0.008, transform=fig.transFigure,
                               linewidth=0, facecolor="#ece7de", zorder=3))
  fig.patches.append(Rectangle((x, y), w * max(0.0, min(score, 1.0)), 0.008,
                               transform=fig.transFigure, linewidth=0, facecolor=color, zorder=4))


def add_road_glyph(fig, x: float, y: float, color: str, score: float) -> None:
  fig.patches.append(Rectangle((x + 0.020, y), 0.010, 0.060, transform=fig.transFigure,
                               linewidth=0, facecolor="#d8d2c6", zorder=3))
  fig.patches.append(Rectangle((x + 0.065, y), 0.010, 0.060, transform=fig.transFigure,
                               linewidth=0, facecolor="#d8d2c6", zorder=3))
  fig.patches.append(Rectangle((x + 0.040, y + 0.006), 0.014, 0.012, transform=fig.transFigure,
                               linewidth=0, facecolor=color, zorder=4))
  fig.patches.append(Rectangle((x + 0.037, y + 0.018), 0.020, 0.024, transform=fig.transFigure,
                               linewidth=0, facecolor=color, alpha=0.85, zorder=4))
  fig.patches.append(Circle((x + 0.039, y + 0.006), 0.004, transform=fig.transFigure,
                            linewidth=0, facecolor="#262b2f", zorder=5))
  fig.patches.append(Circle((x + 0.056, y + 0.006), 0.004, transform=fig.transFigure,
                            linewidth=0, facecolor="#262b2f", zorder=5))
  fig.patches.append(Circle((x + 0.039, y + 0.043), 0.004, transform=fig.transFigure,
                            linewidth=0, facecolor="#262b2f", zorder=5))
  fig.patches.append(Circle((x + 0.056, y + 0.043), 0.004, transform=fig.transFigure,
                            linewidth=0, facecolor="#262b2f", zorder=5))
  fig.text(x + 0.092, y + 0.020, f"{score:.0f}", fontsize=21, fontweight="bold",
           color=color, va="center", zorder=5)
  fig.text(x + 0.092, y + 0.002, "balance\nscore", fontsize=7, color="#77736b",
           va="bottom", zorder=5)


def infographic_plot(conn: sqlite3.Connection, out_dir: Path) -> None:
  metric_rows = dict_rows(conn, """
    SELECT m.*, r.weight_in_overall_score, r.available_segments, r.qlog_segments,
           r.missing_segments, r.total_duration_sec, r.estimated_distance_m, r.notes
      FROM route_metrics m
      JOIN routes r ON r.label = m.route_label
     WHERE m.scope='post_warmup'
  """)
  if not metric_rows:
    return

  by_model = {r["model"]: r for r in metric_rows}
  ordered = [by_model[m] for m in MODEL_ORDER if m in by_model]
  ordered.extend([r for r in metric_rows if r["model"] not in MODEL_ORDER])

  rms_vals = [safe_float(r["lateral_error_rms"]) for r in ordered if safe_float(r["lateral_error_rms"]) is not None]
  pinned_per_10_vals = [
    (safe_float(r["pinned_output_time_sec"], 0.0) or 0.0) / (safe_float(r["duration_sec"], 0.0) or 1.0) * 600.0
    for r in ordered if safe_float(r["duration_sec"], 0.0)
  ]
  deficit_vals = [safe_float(r["no_lead_avg_speed_deficit_mph"]) for r in ordered if safe_float(r["no_lead_avg_speed_deficit_mph"]) is not None]
  lazy_vals = [safe_float(r["lazy_accel_time_sec"]) for r in ordered if safe_float(r["lazy_accel_time_sec"]) is not None]

  best_rms = min(ordered, key=lambda r: safe_float(r["lateral_error_rms"], 999.0))
  best_pinned = min(ordered, key=lambda r: safe_float(r["pinned_output_time_sec"], 999.0))
  best_deficit = min(ordered, key=lambda r: safe_float(r["no_lead_avg_speed_deficit_mph"], 999.0))

  row_count = max(1, (len(ordered) + 1) // 2)
  card_h = 22.5
  row_gap = 4.0
  cards_bottom = 8.0
  card_area_h = row_count * card_h + (row_count - 1) * row_gap
  cards_top = cards_bottom + card_area_h
  total_h = cards_top + 55.0

  fig, ax = plt.subplots(figsize=(16, max(11.2, total_h / 10.0)), facecolor="#f6f3ec")
  ax.set_xlim(0, 100)
  ax.set_ylim(0, total_h)
  ax.axis("off")

  def rounded(x: float, y: float, w: float, h: float, face: str, edge: str = "none",
              lw: float = 1.0, radius: float = 0.8, z: int = 1) -> None:
    ax.add_patch(FancyBboxPatch(
      (x, y), w, h,
      boxstyle=f"round,pad=0.25,rounding_size={radius}",
      linewidth=lw,
      edgecolor=edge,
      facecolor=face,
      zorder=z,
    ))

  def hbar(x: float, y: float, w: float, label: str, value: str, score: float, color: str) -> None:
    ax.text(x, y + 2.0, label, fontsize=8.5, color="#5e6469", fontweight="bold", va="bottom")
    ax.text(x + w, y + 2.0, value, fontsize=8.5, color="#30363a", ha="right", va="bottom")
    ax.add_patch(Rectangle((x, y), w, 1.3, linewidth=0, facecolor="#e9e3d8", zorder=2))
    ax.add_patch(Rectangle((x, y), max(0.0, min(score, 1.0)) * w, 1.3,
                           linewidth=0, facecolor=color, zorder=3))

  def mini_car(cx: float, cy: float, color: str) -> None:
    ax.add_patch(Rectangle((cx - 3.2, cy - 5.0), 1.0, 10.0, linewidth=0, facecolor="#d9d2c6"))
    ax.add_patch(Rectangle((cx + 2.2, cy - 5.0), 1.0, 10.0, linewidth=0, facecolor="#d9d2c6"))
    ax.add_patch(Rectangle((cx - 1.3, cy - 3.4), 2.6, 6.8, linewidth=0, facecolor=color, alpha=0.9))
    for dx in (-1.8, 1.8):
      for dy in (-4.1, 4.1):
        ax.add_patch(Circle((cx + dx, cy + dy), 0.48, linewidth=0, facecolor="#252b30"))

  title_y = total_h - 5.0
  subtitle_y = total_h - 11.0
  hero_y = total_h - 30.0
  chips_y = hero_y - 12.0
  next_y = chips_y - 10.0

  ax.text(4, title_y, "sunnypilot drive-test model shootout", fontsize=29, fontweight="bold",
          color="#202427", va="top")
  ax.text(4, subtitle_y, "2022 Hyundai Tucson PHEV / HYUNDAI_TUCSON_4TH_GEN / NX4PHEV markers",
          fontsize=11.5, color="#61686f", va="top")
  ax.text(94, title_y - 0.7, "RUN NOW", fontsize=8.5, color="#697078", ha="right", fontweight="bold")
  ax.text(94, subtitle_y - 1.3, "OP 10 v3", fontsize=22, color=MODEL_COLORS["OP 10 v3"],
          ha="right", fontweight="bold")

  rounded(4, hero_y, 92, 8.7, "#202427", radius=1.0)
  ax.text(6.4, hero_y + 5.2, "Decision", fontsize=9.5, color="#a7b4ba", fontweight="bold", va="center")
  ax.text(6.4, hero_y + 2.1, "Run OP 10 v3 now: best balance, lowest RMS, only 8.0s lazy accel.",
          fontsize=12.6, color="#fffdf8", va="center")
  ax.plot([65, 65], [hero_y + 1.0, hero_y + 6.4], color="#3a4044", linewidth=1.2)
  ax.text(68.0, hero_y + 5.2, "Next isolated test", fontsize=9.5, color="#e0b577", fontweight="bold", va="center")
  ax.text(68.0, hero_y + 2.1, "OP 10 v3 + SCC-V ON", fontsize=14.5, color="#fffdf8", fontweight="bold", va="center")

  model_notes = {
    "OP 10 v3": "Best balance. Lowest clean lateral RMS, low pinned time, and far less lazy acceleration than Tomb Raider or WMI.",
    "Tomb Raider v16": "Least pinned time and low steering intervention, but speed catch-up was clearly weaker.",
    "WMI v12": "Best average speed deficit by a hair, but much more pinned output and the most lazy-accel time.",
    "North Nevada v2": "Stress/reference route. Includes intentional low-speed tight turn and qlog fallback near the end.",
  }

  chip_data = [
    ("lowest RMS", best_rms["model"], fmt(best_rms["lateral_error_rms"], 4)),
    ("fewest pinned", best_pinned["model"], fmt(best_pinned["pinned_output_time_sec"], 2, "s")),
    ("best speed catch-up", best_deficit["model"], fmt(best_deficit["no_lead_avg_speed_deficit_mph"], 2, " mph")),
  ]
  for i, (label, model, value) in enumerate(chip_data):
    x = 4 + i * 31
    rounded(x, chips_y, 28, 6.3, "#e9e4da", radius=0.9)
    ax.text(x + 2, chips_y + 4.0, label.upper(), fontsize=8.0, color="#6b6f73", fontweight="bold", va="center")
    ax.text(x + 2, chips_y + 1.5, f"{model}  {value}", fontsize=11.5,
            color=MODEL_COLORS.get(model, "#202427"), fontweight="bold", va="center")

  rounded(4, next_y, 92, 5.8, "#fffaf0", "#d1c5b7", radius=0.9)
  ax.text(6.4, next_y + 3.4, "Next test setup", fontsize=8.8, color="#8b6a2e", fontweight="bold", va="center")
  ax.text(6.4, next_y + 1.3,
          "Keep OP 10 v3 and all current settings unchanged except SCC-V: ON. This isolates set-speed catch-up without changing lateral tuning.",
          fontsize=10.4, color="#272b2f", va="center")

  for idx, row in enumerate(ordered):
    col = idx % 2
    grid_row = idx // 2
    x = 4 if col == 0 else 52
    y = cards_top - (grid_row + 1) * card_h - grid_row * row_gap
    w, h = 44, card_h
    model = row["model"]
    color = MODEL_COLORS.get(model, "#60656b")
    rounded(x, y, w, h, "#fffdf8", "#d6d1c8", radius=1.0)
    ax.add_patch(Rectangle((x + 0.8, y + h - 1.4), w - 1.6, 1.0, linewidth=0, facecolor=color, zorder=3))
    title_size = 15.0 if len(model) < 16 else 14.0
    ax.text(x + 2.4, y + h - 4.0, model, fontsize=title_size, fontweight="bold", color="#202427", va="top")

    duration_min = (safe_float(row["duration_sec"], 0.0) or 0.0) / 60.0
    distance_mi = (safe_float(row["distance_m"], 0.0) or 0.0) / 1609.344
    qlog = row["qlog_segments"] if row["qlog_segments"] else "none"
    ax.text(x + 2.4, y + h - 7.0, f"{duration_min:.1f} min / {distance_mi:.2f} mi / qlog: {qlog}",
            fontsize=8.2, color="#747a80", va="top")

    rms = safe_float(row["lateral_error_rms"])
    pinned_per_10 = ((safe_float(row["pinned_output_time_sec"], 0.0) or 0.0) /
                     (safe_float(row["duration_sec"], 0.0) or 1.0) * 600.0)
    deficit = safe_float(row["no_lead_avg_speed_deficit_mph"])
    lazy = safe_float(row["lazy_accel_time_sec"])
    scores = [
      lower_is_better_score(rms, rms_vals),
      lower_is_better_score(pinned_per_10, pinned_per_10_vals),
      lower_is_better_score(deficit, deficit_vals),
      lower_is_better_score(lazy, lazy_vals),
    ]
    balance = sum(scores) / len(scores) * 100.0

    mini_car(x + 7.3, y + 10.0, color)
    ax.text(x + 13.0, y + 11.1, f"{balance:.0f}", fontsize=17.0, color=color,
            fontweight="bold", va="center")
    ax.text(x + 13.2, y + 8.4, "balance\nscore", fontsize=6.4, color="#77736b", va="center")
    hbar(x + 23.0, y + 16.2, 18.0, "clean lateral RMS", fmt(rms, 4), scores[0], color)
    hbar(x + 23.0, y + 12.7, 18.0, "pinned / 10 min", fmt(pinned_per_10, 2, "s"), scores[1], color)
    hbar(x + 23.0, y + 9.2, 18.0, "no-lead deficit", fmt(deficit, 2, " mph"), scores[2], color)
    hbar(x + 23.0, y + 5.7, 18.0, "lazy accel", fmt(lazy, 1, "s"), scores[3], color)
    ax.add_patch(Rectangle((x + 1.4, y + 0.8), w - 2.8, 3.6, linewidth=0,
                           facecolor="#f5f1e9", zorder=2))
    ax.text(x + 2.2, y + 2.45, wrap_text(model_notes.get(model, ""), 78),
            fontsize=7.3, color="#3d4246", va="center", zorder=4)

  ax.text(4, 2.2,
          "Bars are normalized within each metric, so longer is better. North Nevada is a stress/reference drive, not a fair normal-route model score.",
          fontsize=7.8, color="#77736b")

  out_path = out_dir / "model_comparison_scoreboard.png"
  fig.savefig(out_path, dpi=180, bbox_inches="tight")
  fig.savefig(out_dir / "model_comparison_infographic.png", dpi=180, bbox_inches="tight")
  plt.close(fig)


def comparison_plots(conn: sqlite3.Connection, out_dir: Path) -> None:
  routes = dict_rows(conn, """
    SELECT * FROM route_metrics
     WHERE scope='post_warmup'
     ORDER BY model
  """)
  save_bar(routes, "lateral_error_rms", "Clean Lateral RMS by Model", "RMS lateral error", out_dir / "comparison_lateral_rms_by_model.png")
  save_bar(routes, "pinned_output_time_sec", "Pinned Output Time by Model", "Pinned time per 10 min (s)",
           out_dir / "comparison_pinned_time_by_model.png",
           transform=lambda r: (r["pinned_output_time_sec"] or 0.0) / r["duration_sec"] * 600.0 if r.get("duration_sec") else None)
  save_bar(routes, "no_lead_avg_speed_deficit_mph", "No-Lead Speed Deficit by Model", "Avg deficit (mph)",
           out_dir / "comparison_longitudinal_speed_deficit_by_model.png")
  save_bar(routes, "lazy_accel_time_sec", "Lazy Acceleration Time by Model", "Lazy accel time (s)",
           out_dir / "comparison_lazy_accel_time_by_model.png")
  save_bar(routes, "steering_pressed_pct", "Steering Pressed by Model", "Steering pressed (%)",
           out_dir / "comparison_steering_pressed_pct_by_model.png")


def route_speed_plots(conn: sqlite3.Connection, out_dir: Path) -> None:
  for route in dict_rows(conn, "SELECT label, model FROM routes ORDER BY label"):
    data = dict_rows(conn, """
      SELECT route_time_sec, v_ego_mps, set_speed_mps, speed_deficit_mph
        FROM longitudinal_timeseries
       WHERE route_label=?
       ORDER BY route_time_sec
    """, (route["label"],))
    if not data:
      continue
    t = [r["route_time_sec"] for r in data]
    speed = [(r["v_ego_mps"] or 0.0) * MS_TO_MPH for r in data]
    set_speed = [r["set_speed_mps"] * MS_TO_MPH if r["set_speed_mps"] is not None else None for r in data]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(t, speed, label="vEgo", linewidth=1.0)
    ax.plot(t, set_speed, label="set speed", linewidth=1.0)
    ax.set_title(f"{route['model']} Speed vs Set Speed")
    ax.set_xlabel("Route time (s)")
    ax.set_ylabel("Speed (mph)")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / f"{route['label']}_speed_vs_set_speed.png", dpi=150)
    plt.close(fig)


def route_lateral_plots(conn: sqlite3.Connection, out_dir: Path) -> None:
  for route in dict_rows(conn, "SELECT label, model FROM routes ORDER BY label"):
    data = dict_rows(conn, """
      SELECT route_time_sec, lateral_error, torque_output, clean_lateral, pinned
        FROM lateral_timeseries
       WHERE route_label=?
       ORDER BY route_time_sec
    """, (route["label"],))
    if not data:
      continue
    t = [r["route_time_sec"] for r in data]
    err = [r["lateral_error"] for r in data]
    output = [r["torque_output"] for r in data]
    pinned_t = [r["route_time_sec"] for r in data if r["pinned"]]
    pinned_y = [r["torque_output"] for r in data if r["pinned"]]

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(t, err, label="lateral error", linewidth=1.0, color="#2f5f8f")
    ax.set_xlabel("Route time (s)")
    ax.set_ylabel("Lateral error")
    ax.grid(alpha=0.25)
    ax2 = ax.twinx()
    ax2.plot(t, output, label="torque output", linewidth=0.8, color="#b65f3a", alpha=0.8)
    ax2.scatter(pinned_t, pinned_y, s=8, color="#d62728", label="pinned")
    ax2.set_ylabel("Torque output")
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, loc="upper right")
    ax.set_title(f"{route['model']} Lateral Error / Output")
    fig.tight_layout()
    fig.savefig(out_dir / f"{route['label']}_lateral_error_output.png", dpi=150)
    plt.close(fig)


def route_angle_offset_plots(conn: sqlite3.Connection, out_dir: Path) -> None:
  for route in dict_rows(conn, "SELECT label, model FROM routes ORDER BY label"):
    data = dict_rows(conn, """
      SELECT route_time_sec, angle_offset_deg, angle_offset_average_deg
        FROM live_parameters_timeseries
       WHERE route_label=?
       ORDER BY route_time_sec
    """, (route["label"],))
    if not data:
      continue
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot([r["route_time_sec"] for r in data], [r["angle_offset_deg"] for r in data], label="angleOffsetDeg")
    ax.plot([r["route_time_sec"] for r in data], [r["angle_offset_average_deg"] for r in data], label="angleOffsetAverageDeg")
    ax.set_title(f"{route['model']} Angle Offset")
    ax.set_xlabel("Route time (s)")
    ax.set_ylabel("deg")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / f"{route['label']}_angleOffsetDeg.png", dpi=150)
    plt.close(fig)


def pinned_detail_plots(conn: sqlite3.Connection, out_dir: Path) -> None:
  for route in dict_rows(conn, "SELECT label, model FROM routes ORDER BY label"):
    bursts = dict_rows(conn, """
      SELECT * FROM pinned_bursts
       WHERE route_label=?
       ORDER BY duration_sec DESC, max_abs_lateral_error DESC
       LIMIT 3
    """, (route["label"],))
    for burst in bursts:
      start = max(0.0, (burst["start_route_time_sec"] or 0.0) - 2.0)
      end = (burst["end_route_time_sec"] or burst["start_route_time_sec"] or 0.0) + 2.0
      data = dict_rows(conn, """
        SELECT route_time_sec, desired_lateral_accel, actual_lateral_accel, torque_output,
               steering_angle_deg, speed_mph, gas_pressed, brake_pressed, steering_pressed
          FROM lateral_timeseries
         WHERE route_label=? AND route_time_sec BETWEEN ? AND ?
         ORDER BY route_time_sec
      """, (route["label"], start, end))
      if not data:
        continue
      t = [r["route_time_sec"] for r in data]
      fig, axes = plt.subplots(4, 1, figsize=(12, 9), sharex=True)
      axes[0].plot(t, [r["desired_lateral_accel"] for r in data], label="desired")
      axes[0].plot(t, [r["actual_lateral_accel"] for r in data], label="actual")
      axes[0].set_ylabel("lat accel")
      axes[0].legend()
      axes[1].plot(t, [r["torque_output"] for r in data], color="#b65f3a")
      axes[1].axhline(0.98, color="#d62728", linestyle="--", linewidth=0.8)
      axes[1].axhline(-0.98, color="#d62728", linestyle="--", linewidth=0.8)
      axes[1].set_ylabel("output")
      axes[2].plot(t, [r["steering_angle_deg"] for r in data], color="#4d7f45")
      axes[2].set_ylabel("steer angle")
      axes[3].plot(t, [r["speed_mph"] for r in data], color="#2f5f8f", label="speed")
      for marker_key, color, label in (("gas_pressed", "#2ca02c", "gas"), ("brake_pressed", "#d62728", "brake"), ("steering_pressed", "#9467bd", "steering")):
        mt = [r["route_time_sec"] for r in data if r[marker_key]]
        if mt:
          axes[3].scatter(mt, [0.0] * len(mt), s=16, label=label, color=color)
      axes[3].set_ylabel("mph")
      axes[3].set_xlabel("Route time (s)")
      axes[3].legend(loc="upper right")
      for ax in axes:
        ax.axvspan(burst["start_route_time_sec"], burst["end_route_time_sec"], color="#d62728", alpha=0.12)
        ax.grid(alpha=0.25)
      fig.suptitle(f"{route['model']} pinned burst {burst['burst_index']}")
      fig.tight_layout()
      fig.savefig(out_dir / f"{route['label']}_pinned_burst_{burst['burst_index']}.png", dpi=150)
      plt.close(fig)


def generate_plots(db_path: Path, out_dir: Path) -> None:
  out_dir.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(db_path)
  try:
    comparison_plots(conn, out_dir)
    route_speed_plots(conn, out_dir)
    route_lateral_plots(conn, out_dir)
    route_angle_offset_plots(conn, out_dir)
    pinned_detail_plots(conn, out_dir)
    infographic_plot(conn, out_dir)
  finally:
    conn.close()


def main() -> int:
  parser = argparse.ArgumentParser(description="Generate drive-test plots from results.sqlite")
  parser.add_argument("--db", type=Path, required=True)
  parser.add_argument("--out", type=Path, required=True)
  args = parser.parse_args()
  generate_plots(args.db, args.out)
  print(f"Wrote plots to {args.out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
