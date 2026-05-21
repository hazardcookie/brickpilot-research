#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_ROOT = SCRIPT_DIR.parents[1]
if str(TOOLS_ROOT) not in sys.path:
  sys.path.insert(0, str(TOOLS_ROOT))

from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore
from scripts.drive_tests.train_voice_labeler_0325 import ALL_ROUTES, TEST_ROUTE, VALIDATION_ROUTES

DEFAULT_BASELINE_EXPORT = Path("__latest__")
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
TARGET_BUSES = (0, 1, 2, 128, 129, 130)
TARGET_ADDRS = (0x06F, 0x0FA, 0x0E0, 0x0BA, 0x065, 0x10A, 0x120, 0x1C5, 0x1A5, 0x310)
KEY_LABEL_TARGETS = {
  "group_stationary_state",
  "ready_park_no_pedals",
  "drive_brake_held",
  "drive_creep_no_gas",
  "auto_hold_on",
  "auto_hold_active",
  "auto_hold_release",
  "group_phev_regen",
  "phev_regen_coast",
  "phev_regen_hard",
  "phev_regen_light",
  "phev_regen_none",
  "group_phev_state",
  "ice_engine_on",
  "braking_bad",
  "braking_late",
  "braking_early",
  "braking_absent",
  "lead_brake_bad",
  "lead_brake_good",
  "lead_present_context",
  "brake_hard",
  "driver_brake",
  "driver_no_brake",
  "driver_brake_intervention",
  "stop_complete",
  "stop_go_bad",
  "stop_creep_fail",
  "stop_hold_fail",
  "accel_too_lazy",
  "accel_target_gap",
  "resume_bad",
  "resume_lazy",
  "human_intervention_gas",
  "human_intervention_brake",
  "unnecessary_braking",
  "regen_light_coast",
  "low_speed_lateral_bad",
  "quality_good",
}


@dataclass
class Stats:
  n: int = 0
  total: float = 0.0
  total2: float = 0.0
  min_value: float | None = None
  max_value: float | None = None

  def add(self, value: float) -> None:
    if not math.isfinite(value):
      return
    self.n += 1
    self.total += value
    self.total2 += value * value
    self.min_value = value if self.min_value is None else min(self.min_value, value)
    self.max_value = value if self.max_value is None else max(self.max_value, value)

  @property
  def mean(self) -> float:
    return self.total / self.n if self.n else 0.0

  @property
  def variance(self) -> float:
    if self.n <= 1:
      return 0.0
    return max(self.total2 / self.n - self.mean * self.mean, 0.0)

  @property
  def std(self) -> float:
    return math.sqrt(self.variance)


def s8(value: int) -> int:
  return value - 256 if value >= 128 else value


def u8(payload: bytes, idx: int) -> int | None:
  return payload[idx] if len(payload) > idx else None


def s8_at(payload: bytes, idx: int) -> int | None:
  raw = u8(payload, idx)
  return None if raw is None else s8(raw)


def s16_le(payload: bytes, idx: int) -> int | None:
  if len(payload) <= idx + 1:
    return None
  return int.from_bytes(payload[idx:idx + 2], "little", signed=True)


def add_field(out: dict[str, float], name: str, value: int | float | None) -> None:
  if value is not None:
    out[name] = float(value)


def decode_candidate_fields(bus: int, address: int, payload: bytes) -> dict[str, float]:
  out: dict[str, float] = {}
  suffix = f"bus{bus}"
  if address == 0x06F:
    for idx in range(min(len(payload), 8)):
      add_field(out, f"f06f_b{idx}_u8_{suffix}", u8(payload, idx))
    add_field(out, f"f06f_b4_s8_{suffix}", s8_at(payload, 4))
  elif address == 0x0FA:
    add_field(out, f"fa_b4_u8_{suffix}", u8(payload, 4))
    add_field(out, f"fa_b4_s8_{suffix}", s8_at(payload, 4))
    add_field(out, f"fa_b6_u8_{suffix}", u8(payload, 6))
    add_field(out, f"fa_b7_u8_{suffix}", u8(payload, 7))
  elif address == 0x0E0:
    add_field(out, f"e0_b7_u8_{suffix}", u8(payload, 7))
    add_field(out, f"e0_s16_08_le_{suffix}", s16_le(payload, 8))
    add_field(out, f"e0_s16_10_le_{suffix}", s16_le(payload, 10))
    add_field(out, f"e0_s16_16_le_{suffix}", s16_le(payload, 16))
    add_field(out, f"e0_b16_u8_{suffix}", u8(payload, 16))
    add_field(out, f"e0_b17_u8_{suffix}", u8(payload, 17))
  elif address == 0x0BA:
    add_field(out, f"ba_b9_u8_{suffix}", u8(payload, 9))
    add_field(out, f"ba_b11_s8_{suffix}", s8_at(payload, 11))
    add_field(out, f"ba_b11_u8_{suffix}", u8(payload, 11))
    add_field(out, f"ba_b14_u8_{suffix}", u8(payload, 14))
  elif address == 0x065:
    add_field(out, f"brake065_b3_u8_{suffix}", u8(payload, 3))
    add_field(out, f"brake065_b9_u8_{suffix}", u8(payload, 9))
    add_field(out, f"brake065_b10_u8_{suffix}", u8(payload, 10))
    add_field(out, f"brake065_b11_u8_{suffix}", u8(payload, 11))
    add_field(out, f"brake065_b12_u8_{suffix}", u8(payload, 12))
    add_field(out, f"brake065_b14_u8_{suffix}", u8(payload, 14))
  elif address == 0x10A:
    add_field(out, f"a10_b5_u8_{suffix}", u8(payload, 5))
    add_field(out, f"a10_b10_u8_{suffix}", u8(payload, 10))
    add_field(out, f"a10_b16_u8_{suffix}", u8(payload, 16))
    add_field(out, f"a10_b18_u8_{suffix}", u8(payload, 18))
  elif address == 0x120:
    add_field(out, f"a120_b0_u8_{suffix}", u8(payload, 0))
    add_field(out, f"a120_b3_u8_{suffix}", u8(payload, 3))
  elif address == 0x1C5:
    add_field(out, f"c5_b5_u8_{suffix}", u8(payload, 5))
    add_field(out, f"c5_b14_u8_{suffix}", u8(payload, 14))
  elif address == 0x1A5:
    add_field(out, f"a5_b14_u8_{suffix}", u8(payload, 14))
    add_field(out, f"a5_b15_u8_{suffix}", u8(payload, 15))
    add_field(out, f"a5_b16_u8_{suffix}", u8(payload, 16))
    add_field(out, f"a5_b17_u8_{suffix}", u8(payload, 17))
  elif address == 0x310:
    add_field(out, f"adas310_b10_u8_{suffix}", u8(payload, 10))
    add_field(out, f"adas310_b17_u8_{suffix}", u8(payload, 17))
    add_field(out, f"adas310_b18_u8_{suffix}", u8(payload, 18))
  return out


def rows_to_dicts(cursor) -> list[dict[str, Any]]:
  return [dict(row) for row in cursor.fetchall()]


def fetch_route_ids(store: DriveStore) -> dict[str, str]:
  placeholders = ",".join("?" for _ in ALL_ROUTES)
  rows = rows_to_dicts(store.execute(
    f"SELECT id, route_id FROM routes WHERE route_id IN ({placeholders})",
    tuple(ALL_ROUTES),
  ))
  return {row["route_id"]: row["id"] for row in rows}


def fetch_frames(store: DriveStore, route_uuids: dict[str, str]) -> list[dict[str, Any]]:
  if not route_uuids:
    return []
  placeholders = ",".join("?" for _ in route_uuids)
  addr_placeholders = ",".join("?" for _ in TARGET_ADDRS)
  bus_placeholders = ",".join("?" for _ in TARGET_BUSES)
  uuid_to_route = {uuid: route_id for route_id, uuid in route_uuids.items()}
  rows = rows_to_dicts(store.execute(
    f"""SELECT route_uuid, t_sec, bus, address, data_hex
        FROM can_frames_sampled
        WHERE route_uuid IN ({placeholders})
          AND address IN ({addr_placeholders})
          AND bus IN ({bus_placeholders})
        ORDER BY route_uuid, t_sec, bus, address""",
    (*route_uuids.values(), *TARGET_ADDRS, *TARGET_BUSES),
  ))
  for row in rows:
    row["route_id"] = uuid_to_route[row["route_uuid"]]
    row["payload"] = bytes.fromhex(str(row["data_hex"] or ""))
  return rows


def read_csv(path: Path) -> list[dict[str, str]]:
  with path.open(newline="") as f:
    return list(csv.DictReader(f))


def resolve_baseline_export(path: Path, output_root: Path) -> Path:
  if str(path) != "__latest__":
    return path
  candidates = sorted(output_root.glob("ml_040_beta_voice_labeler_*"))
  if not candidates:
    candidates = sorted(output_root.glob("ml_0325_voice_labeler_*"))
  if not candidates:
    raise RuntimeError(f"No voice labeler export found under {output_root}")
  return candidates[-1]


def read_groups(path: Path) -> dict[str, set[str]]:
  if not path.exists():
    return {}
  data = json.loads(path.read_text())
  return {name: set(labels) for name, labels in data.get("groups", {}).items()}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fieldnames: list[str] = []
  for row in rows:
    for key in row:
      if key not in fieldnames:
        fieldnames.append(key)
  with path.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)


def merged(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
  out: list[tuple[float, float]] = []
  for start, end in sorted(intervals):
    if not out or start > out[-1][1]:
      out.append((start, end))
    else:
      out[-1] = (out[-1][0], max(out[-1][1], end))
  return out


def contains(intervals: list[tuple[float, float]], t: float) -> bool:
  return any(start <= t <= end for start, end in intervals)


def build_label_intervals(corpus_rows: list[dict[str, str]], groups: dict[str, set[str]], pad_sec: float) -> dict[str, dict[str, list[tuple[float, float]]]]:
  by_target: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
  label_to_groups: dict[str, list[str]] = defaultdict(list)
  for group, labels in groups.items():
    for label in labels:
      label_to_groups[label].append(group)

  for row in corpus_rows:
    route_id = row["route_id"]
    if route_id not in VALIDATION_ROUTES:
      continue
    label = row["canonical_label"]
    start = max(float(row["start_sec"]) - pad_sec, 0.0)
    end = max(float(row["end_sec"]) + pad_sec, start)
    targets = {label, *label_to_groups.get(label, [])}
    for target in targets:
      by_target[target][route_id].append((start, end))

  return {
    target: {route_id: merged(intervals) for route_id, intervals in by_route.items()}
    for target, by_route in by_target.items()
  }


def decoded_field_rows(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  for frame in frames:
    fields = decode_candidate_fields(int(frame["bus"]), int(frame["address"]), frame["payload"])
    for field, value in fields.items():
      rows.append({
        "route_id": frame["route_id"],
        "t_sec": float(frame["t_sec"]),
        "bus": int(frame["bus"]),
        "address_hex": f"0x{int(frame['address']):03x}",
        "field": field,
        "value": value,
      })
  return rows


def route_summary(field_rows: list[dict[str, Any]], frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
  stats: dict[tuple[str, str], Stats] = defaultdict(Stats)
  for row in field_rows:
    stats[(row["route_id"], row["field"])].add(float(row["value"]))

  fa_by_route_time: dict[tuple[str, float], dict[int, int]] = defaultdict(dict)
  for frame in frames:
    if int(frame["address"]) != 0x0FA:
      continue
    value = u8(frame["payload"], 4)
    if value is not None:
      fa_by_route_time[(frame["route_id"], float(frame["t_sec"]))][int(frame["bus"])] = value

  mirror_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"paired": 0, "match": 0, "diff": 0})
  for (route_id, _), by_bus in fa_by_route_time.items():
    if 0 in by_bus and 130 in by_bus:
      mirror_counts[route_id]["paired"] += 1
      if by_bus[0] == by_bus[130]:
        mirror_counts[route_id]["match"] += 1
      else:
        mirror_counts[route_id]["diff"] += 1

  out: list[dict[str, Any]] = []
  for (route_id, field), stat in sorted(stats.items()):
    mirror = mirror_counts.get(route_id, {})
    out.append({
      "route_id": route_id,
      "field": field,
      "count": stat.n,
      "mean": round(stat.mean, 6),
      "std": round(stat.std, 6),
      "min": stat.min_value,
      "max": stat.max_value,
      "fa_b4_bus0_bus130_pairs": mirror.get("paired", 0),
      "fa_b4_bus0_bus130_match_frac": round(mirror.get("match", 0) / mirror["paired"], 6) if mirror.get("paired") else "",
      "fa_b4_bus0_bus130_diff_count": mirror.get("diff", 0),
    })
  return out


def label_effects(field_rows: list[dict[str, Any]], intervals: dict[str, dict[str, list[tuple[float, float]]]]) -> list[dict[str, Any]]:
  targets = sorted(
    (set(intervals) & KEY_LABEL_TARGETS)
    | {t for t in intervals if t.startswith(("group_phev", "group_stationary", "group_longitudinal", "group_lateral"))}
  )
  out: list[dict[str, Any]] = []
  for target in targets:
    by_route = intervals[target]
    pos: dict[str, Stats] = defaultdict(Stats)
    bg: dict[str, Stats] = defaultdict(Stats)
    for row in field_rows:
      route_id = row["route_id"]
      if route_id not in VALIDATION_ROUTES:
        continue
      stat = pos if contains(by_route.get(route_id, []), float(row["t_sec"])) else bg
      stat[row["field"]].add(float(row["value"]))

    for field in set(pos) | set(bg):
      pos_stat = pos[field]
      bg_stat = bg[field]
      if pos_stat.n < 3 or bg_stat.n < 10:
        continue
      pooled = math.sqrt(max((pos_stat.variance + bg_stat.variance) / 2.0, 1e-9))
      effect = (pos_stat.mean - bg_stat.mean) / pooled
      out.append({
        "target": target,
        "field": field,
        "pos_count": pos_stat.n,
        "background_count": bg_stat.n,
        "pos_mean": round(pos_stat.mean, 6),
        "background_mean": round(bg_stat.mean, 6),
        "effect": round(effect, 6),
        "abs_effect": round(abs(effect), 6),
        "interpretation_status": "decoded_candidate_correlation_not_control_semantics",
      })
  out.sort(key=lambda row: (float(row["abs_effect"]), row["target"], row["field"]), reverse=True)
  return out


def summarize_interval_values(field_rows: list[dict[str, Any]], test_predictions: list[dict[str, str]]) -> list[dict[str, Any]]:
  by_route_field: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
  for row in field_rows:
    by_route_field[(row["route_id"], row["field"])].append((float(row["t_sec"]), float(row["value"])))

  key_fields = [
    "f06f_b4_s8_bus0",
    "f06f_b4_s8_bus130",
    "f06f_b4_u8_bus0",
    "f06f_b4_u8_bus130",
    "fa_b4_s8_bus0",
    "fa_b4_s8_bus130",
    "e0_s16_08_le_bus0",
    "e0_s16_10_le_bus0",
    "e0_s16_16_le_bus0",
    "ba_b11_s8_bus0",
    "brake065_b9_u8_bus0",
    "brake065_b10_u8_bus0",
    "a10_b10_u8_bus0",
    "a10_b18_u8_bus0",
    "a120_b3_u8_bus0",
    "c5_b5_u8_bus0",
    "a5_b14_u8_bus0",
    "a5_b15_u8_bus0",
    "a5_b16_u8_bus0",
    "a5_b17_u8_bus0",
    "adas310_b17_u8_bus1",
    "adas310_b18_u8_bus1",
  ]
  out: list[dict[str, Any]] = []
  for pred in test_predictions:
    route_id = pred["route_id"]
    start = float(pred["start_sec"])
    end = float(pred["end_sec"])
    row: dict[str, Any] = {
      "rank": pred.get("rank", ""),
      "target": pred["target"],
      "route_id": route_id,
      "start_sec": start,
      "end_sec": end,
      "peak_score": pred.get("peak_score", ""),
      "reason": pred.get("reason", ""),
    }
    for field in key_fields:
      values = [value for t, value in by_route_field.get((route_id, field), []) if start <= t <= end]
      if not values:
        continue
      stat = Stats()
      for value in values:
        stat.add(value)
      row[f"{field}_count"] = stat.n
      row[f"{field}_mean"] = round(stat.mean, 6)
      row[f"{field}_min"] = stat.min_value
      row[f"{field}_max"] = stat.max_value
    out.append(row)
  return out


def svg_bar_chart(path: Path, title: str, labels: list[str], values: list[float], width: int = 1100, height: int = 560) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  if not labels:
    path.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n")
    return
  margin_left, margin_right, margin_top, margin_bottom = 280, 40, 48, 40
  plot_w = width - margin_left - margin_right
  row_h = max(18, int((height - margin_top - margin_bottom) / len(labels)))
  height = margin_top + margin_bottom + row_h * len(labels)
  max_abs = max(abs(v) for v in values) or 1.0
  zero_x = margin_left + plot_w / 2
  scale = (plot_w / 2) / max_abs
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#fbfbf8'/>",
    f"<text x='{margin_left}' y='28' font-family='Arial' font-size='20' font-weight='700' fill='#1c2430'>{title}</text>",
    f"<line x1='{zero_x:.1f}' x2='{zero_x:.1f}' y1='{margin_top - 8}' y2='{height - margin_bottom + 8}' stroke='#6b7280' stroke-width='1'/>",
  ]
  for idx, (label, value) in enumerate(zip(labels, values)):
    y = margin_top + idx * row_h
    bar_w = abs(value) * scale
    x = zero_x if value >= 0 else zero_x - bar_w
    fill = "#2a9d8f" if value >= 0 else "#d45d5d"
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines.append(f"<text x='12' y='{y + row_h * 0.65:.1f}' font-family='Arial' font-size='12' fill='#26313f'>{safe_label}</text>")
    lines.append(f"<rect x='{x:.1f}' y='{y + 3}' width='{bar_w:.1f}' height='{max(row_h - 6, 8)}' rx='3' fill='{fill}'/>")
    lines.append(f"<text x='{x + bar_w + 6 if value >= 0 else x - 52:.1f}' y='{y + row_h * 0.65:.1f}' font-family='Arial' font-size='12' fill='#26313f'>{value:.2f}</text>")
  lines.append("</svg>")
  path.write_text("\n".join(lines) + "\n")


def svg_trace(path: Path, title: str, series: dict[str, list[tuple[float, float]]], width: int = 1100, height: int = 420) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  points = [pt for vals in series.values() for pt in vals]
  if not points:
    path.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n")
    return
  margin_left, margin_right, margin_top, margin_bottom = 64, 32, 48, 44
  xs = [p[0] for p in points]
  ys = [p[1] for p in points]
  min_x, max_x = min(xs), max(xs)
  min_y, max_y = min(ys), max(ys)
  if min_y == max_y:
    min_y -= 1
    max_y += 1
  plot_w = width - margin_left - margin_right
  plot_h = height - margin_top - margin_bottom

  def xp(x: float) -> float:
    return margin_left + (x - min_x) / max(max_x - min_x, 1e-9) * plot_w

  def yp(y: float) -> float:
    return margin_top + (max_y - y) / max(max_y - min_y, 1e-9) * plot_h

  colors = ["#2a9d8f", "#d45d5d", "#457b9d", "#7d5fff"]
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#fbfbf8'/>",
    f"<text x='{margin_left}' y='28' font-family='Arial' font-size='20' font-weight='700' fill='#1c2430'>{title}</text>",
    f"<line x1='{margin_left}' x2='{width - margin_right}' y1='{height - margin_bottom}' y2='{height - margin_bottom}' stroke='#6b7280'/>",
    f"<line x1='{margin_left}' x2='{margin_left}' y1='{margin_top}' y2='{height - margin_bottom}' stroke='#6b7280'/>",
    f"<text x='{margin_left}' y='{height - 12}' font-family='Arial' font-size='12' fill='#4b5563'>route seconds</text>",
    f"<text x='10' y='{margin_top - 12}' font-family='Arial' font-size='12' fill='#4b5563'>signed byte</text>",
  ]
  for idx, (name, vals) in enumerate(series.items()):
    color = colors[idx % len(colors)]
    vals = sorted(vals)
    poly = " ".join(f"{xp(x):.1f},{yp(y):.1f}" for x, y in vals)
    safe_name = name.replace("&", "&amp;")
    lines.append(f"<polyline points='{poly}' fill='none' stroke='{color}' stroke-width='2'/>")
    lines.append(f"<circle cx='{width - margin_right - 150}' cy='{margin_top + idx * 20}' r='5' fill='{color}'/>")
    lines.append(f"<text x='{width - margin_right - 138}' y='{margin_top + idx * 20 + 4}' font-family='Arial' font-size='12' fill='#26313f'>{safe_name}</text>")
  lines.append("</svg>")
  path.write_text("\n".join(lines) + "\n")


def report_text(out_dir: Path, label_rows: list[dict[str, Any]], route_rows: list[dict[str, Any]],
                interval_rows: list[dict[str, Any]], baseline_export: Path) -> str:
  top = label_rows[:15]
  fa_rows = [r for r in route_rows if str(r["field"]).startswith("fa_b4_s8_bus")]
  lines = [
    "# Brickpilot 0.4.0-beta PHEV CAN Candidate Experiment",
    "",
    "## Scope",
    "",
    f"- Baseline ML export: `{baseline_export}`",
    f"- Validation routes: {', '.join(VALIDATION_ROUTES)}",
    f"- Held-out test route: `{TEST_ROUTE}`",
    "- These are decoded candidate correlations for logger design and review; they are not promoted to control semantics.",
    "",
    "## Main Findings",
    "",
    "- `0x0FA.b4` is now treated as the top read-only regen/charge candidate and is decoded as both unsigned and signed int8.",
    "- `0x06F` is included for the new stationary/creep/auto-hold route because the prior pass kept it as a stop/creep candidate.",
    "- Bus 0 and bus 130 mirror checks are reported separately because the current labels repeatedly found the same byte on both sources.",
    "- `0x0E0` is decoded as signed little-endian 16-bit candidates at byte offsets 8, 10, and 16.",
    "- `0x10A`/`0x120` remain engine-state candidates; `0x1C5` and `0x1A5` remain selected-mode or energy-flow-state candidates; `0x310` is kept ADAS/gating-scoped.",
    "",
    "## Strongest Label Effects",
    "",
    "| target | field | pos mean | background mean | effect |",
    "|---|---|---:|---:|---:|",
  ]
  for row in top:
    lines.append(f"| `{row['target']}` | `{row['field']}` | {row['pos_mean']} | {row['background_mean']} | {row['effect']} |")
  lines += [
    "",
    "## FA Signed Route Summary",
    "",
    "| route | field | count | mean | min | max | bus0/bus130 match |",
    "|---|---|---:|---:|---:|---:|---:|",
  ]
  for row in fa_rows:
    lines.append(
      f"| `{row['route_id']}` | `{row['field']}` | {row['count']} | {row['mean']} | {row['min']} | {row['max']} | {row['fa_b4_bus0_bus130_match_frac']} |"
    )
  lines += [
    "",
    "## Held-Out Test Route",
    "",
    f"- Candidate interval summaries written for {len(interval_rows)} predicted intervals.",
    "- The next review pass should focus on intervals where high `fa_b4_s8` magnitude coincides with bad-braking predictions but no driver brake press.",
    "",
    "## Outputs",
    "",
    f"- `{out_dir / 'decoded_candidate_samples.csv'}`",
    f"- `{out_dir / 'route_candidate_summary.csv'}`",
    f"- `{out_dir / 'label_candidate_effects.csv'}`",
    f"- `{out_dir / 'test_route_interval_candidate_summary.csv'}`",
    f"- `{out_dir / 'charts' / 'top_label_effects.svg'}`",
    f"- `{out_dir / 'charts' / 'test_fa_b4_trace.svg'}`",
    f"- `{out_dir / 'run_summary.json'}`",
  ]
  return "\n".join(lines) + "\n"


def main() -> int:
  parser = argparse.ArgumentParser(description="Run 0.4.0-beta PHEV CAN candidate experiments over scoped label-validation data.")
  parser.add_argument("--config", default=None)
  parser.add_argument("--baseline-export", type=Path, default=DEFAULT_BASELINE_EXPORT)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--label-pad-sec", type=float, default=3.0)
  parser.add_argument("--output-dir", type=Path, default=None)
  args = parser.parse_args()

  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  baseline_export = resolve_baseline_export(args.baseline_export, args.output_root)
  out_dir = args.output_dir or (args.output_root / f"phev_can_candidate_experiments_040_beta_{stamp}")
  out_dir.mkdir(parents=True, exist_ok=True)

  store = DriveStore(load_config(args.config))
  route_uuids = fetch_route_ids(store)
  missing = sorted(set(ALL_ROUTES) - set(route_uuids))
  if missing:
    raise RuntimeError(f"Missing routes in drive DB: {missing}")

  frames = fetch_frames(store, route_uuids)
  field_rows = decoded_field_rows(frames)
  corpus_rows = read_csv(baseline_export / "voice_label_corpus.csv")
  groups = read_groups(baseline_export / "voice_label_taxonomy.json")
  intervals = build_label_intervals(corpus_rows, groups, args.label_pad_sec)
  route_rows = route_summary(field_rows, frames)
  effect_rows = label_effects(field_rows, intervals)
  test_predictions = read_csv(baseline_export / "test_route_predictions.csv")
  interval_rows = summarize_interval_values(field_rows, test_predictions)

  write_csv(out_dir / "decoded_candidate_samples.csv", field_rows)
  write_csv(out_dir / "route_candidate_summary.csv", route_rows)
  write_csv(out_dir / "label_candidate_effects.csv", effect_rows)
  write_csv(out_dir / "test_route_interval_candidate_summary.csv", interval_rows)

  top_effects = effect_rows[:20]
  svg_bar_chart(
    out_dir / "charts" / "top_label_effects.svg",
    "Top decoded candidate label effects",
    [f"{r['target']} / {r['field']}" for r in top_effects],
    [float(r["effect"]) for r in top_effects],
  )

  test_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
  for row in field_rows:
    if row["route_id"] != TEST_ROUTE or row["field"] not in {"fa_b4_s8_bus0", "fa_b4_s8_bus130"}:
      continue
    test_series[row["field"]].append((float(row["t_sec"]), float(row["value"])))
  svg_trace(out_dir / "charts" / "test_fa_b4_trace.svg", "Held-out test route 0x0FA.b4 signed trace", dict(test_series))

  summary = {
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "baseline_export": str(baseline_export),
    "routes": ALL_ROUTES,
    "frame_rows": len(frames),
    "decoded_field_rows": len(field_rows),
    "route_summary_rows": len(route_rows),
    "label_effect_rows": len(effect_rows),
    "test_interval_rows": len(interval_rows),
    "top_effects": effect_rows[:20],
  }
  (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
  (out_dir / "report.md").write_text(report_text(out_dir, effect_rows, route_rows, interval_rows, baseline_export))
  print(out_dir)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
