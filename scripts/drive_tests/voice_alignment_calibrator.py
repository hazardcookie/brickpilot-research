#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
ANCHOR_WINDOW_SEC = 6.0
ROUTE_OFFSET_MIN_ANCHORS = 2
DIRECT_ALIGNMENT_MIN_CONFIDENCE = 0.70
ROUTE_OFFSET_APPLY_MIN_CONFIDENCE = 0.85
ROUTE_OFFSET_APPLY_MAX_MAD_SEC = 0.75


@dataclass(frozen=True)
class TelemetryEdge:
  route_id: str
  family: str
  t_sec: float
  edge: str
  value: float


@dataclass(frozen=True)
class AlignmentSuggestion:
  route_id: str
  bookmark_id: int
  raw_text: str
  original_t_sec: float
  anchor_family: str
  edge_t_sec: float | None
  offset_sec: float | None
  confidence: float
  alignment_source: str
  reason: str


@dataclass(frozen=True)
class RouteOffset:
  route_id: str
  offset_sec: float
  confidence: float
  anchor_count: int
  mad_sec: float


def safe_float(value: Any, default: float = 0.0) -> float:
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return default
  return ret if math.isfinite(ret) else default


def safe_int(value: Any, default: int = 0) -> int:
  try:
    return int(value)
  except (TypeError, ValueError):
    return default


def as_bool(value: Any) -> bool:
  if isinstance(value, bool):
    return value
  if isinstance(value, (int, float)):
    return bool(value)
  return str(value).strip().lower() in {"1", "1.0", "true", "yes", "y", "on"}


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


def json_safe(value: Any) -> Any:
  if isinstance(value, dict):
    return {str(k): json_safe(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [json_safe(v) for v in value]
  if isinstance(value, (AlignmentSuggestion, TelemetryEdge, RouteOffset)):
    return json_safe(value.__dict__)
  if value is None or isinstance(value, (str, int, bool)):
    return value
  if isinstance(value, float):
    return value if math.isfinite(value) else None
  if isinstance(value, (date, datetime)):
    return value.isoformat()
  return str(value)


def classify_anchor_text(text: str) -> str | None:
  s = " ".join(text.lower().replace("_", " ").replace("-", " ").split())
  if not s:
    return None

  brake_words = ("brake", "braking", "break", "breaking")
  gas_words = ("gas", "accelerator", "throttle")
  steer_words = ("manual steering", "driver steering", "steering override", "steer override", "human steering")
  manual_words = ("manual", "driver", "human", "intervention", "pressed", "pressing", "pedal", "override", "needed")

  if any(phrase in s for phrase in steer_words):
    return "steer"
  if any(word in s for word in brake_words) and any(word in s for word in manual_words):
    return "brake"
  if any(phrase in s for phrase in ("driver brake", "manual brake", "human brake", "brake pedal", "on the brake")):
    return "brake"
  if any(word in s for word in gas_words) and any(word in s for word in manual_words):
    return "gas"
  if any(phrase in s for phrase in ("driver gas", "manual gas", "human gas", "on the gas", "gas pedal")):
    return "gas"
  return None


def edge_confidence(offset_sec: float, edge_count_nearby: int = 1) -> tuple[float, str]:
  abs_offset = abs(offset_sec)
  if abs_offset <= 1.25:
    base, reason = 0.95, "nearest edge within 1.25s"
  elif abs_offset <= 2.5:
    base, reason = 0.78, "nearest edge within 2.5s"
  elif abs_offset <= ANCHOR_WINDOW_SEC:
    base, reason = 0.48, "nearest edge within search window"
  else:
    return 0.0, "outside search window"
  if edge_count_nearby > 1:
    base *= 0.85
    reason += "; multiple nearby edges"
  return round(base, 3), reason


def detect_edges(route_id: str, samples: list[dict[str, Any]]) -> list[TelemetryEdge]:
  edges: list[TelemetryEdge] = []
  prev = {"gas": False, "brake": False, "steer": False}
  for row in sorted(samples, key=lambda x: safe_float(x.get("t_sec", x.get("t", 0.0)))):
    t = safe_float(row.get("t_sec", row.get("t", 0.0)))
    current = {
      "gas": as_bool(row.get("gas_pressed")),
      "brake": as_bool(row.get("brake_pressed")),
      "steer": as_bool(row.get("steering_pressed")),
    }
    for family, active in current.items():
      if active and not prev.get(family, False):
        edges.append(TelemetryEdge(route_id=route_id, family=family, t_sec=t, edge="rising", value=1.0))
      elif not active and prev.get(family, False):
        edges.append(TelemetryEdge(route_id=route_id, family=family, t_sec=t, edge="falling", value=0.0))
    prev = current
  return edges


def nearest_edge(route_id: str, bookmark_id: int, text: str, t_sec: float, family: str,
                 edges: list[TelemetryEdge], window_sec: float = ANCHOR_WINDOW_SEC) -> AlignmentSuggestion:
  candidates = [edge for edge in edges if edge.family == family and edge.edge == "rising" and abs(edge.t_sec - t_sec) <= window_sec]
  if not candidates:
    return AlignmentSuggestion(
      route_id=route_id,
      bookmark_id=bookmark_id,
      raw_text=text,
      original_t_sec=t_sec,
      anchor_family=family,
      edge_t_sec=None,
      offset_sec=None,
      confidence=0.0,
      alignment_source="none",
      reason="no matching telemetry rising edge in search window",
    )
  best = min(candidates, key=lambda edge: abs(edge.t_sec - t_sec))
  nearby = sum(1 for edge in candidates if abs(edge.t_sec - best.t_sec) <= 1.5)
  offset = best.t_sec - t_sec
  confidence, reason = edge_confidence(offset, nearby)
  return AlignmentSuggestion(
    route_id=route_id,
    bookmark_id=bookmark_id,
    raw_text=text,
    original_t_sec=t_sec,
    anchor_family=family,
    edge_t_sec=round(best.t_sec, 3),
    offset_sec=round(offset, 3),
    confidence=confidence,
    alignment_source="telemetry_edge" if confidence > 0.0 else "none",
    reason=reason,
  )


def route_offset_from_suggestions(route_id: str, suggestions: list[AlignmentSuggestion]) -> RouteOffset | None:
  offsets = [s.offset_sec for s in suggestions if s.offset_sec is not None and s.confidence >= DIRECT_ALIGNMENT_MIN_CONFIDENCE]
  if len(offsets) < ROUTE_OFFSET_MIN_ANCHORS:
    return None
  med = float(median(offsets))
  deviations = [abs(x - med) for x in offsets]
  mad = float(median(deviations)) if deviations else 0.0
  confidence = max(0.0, min(0.98, 0.96 - 0.24 * mad))
  return RouteOffset(route_id=route_id, offset_sec=round(med, 3),
                     confidence=round(confidence, 3), anchor_count=len(offsets), mad_sec=round(mad, 3))


def route_offset_is_applicable(route_offset: RouteOffset | None) -> bool:
  if route_offset is None:
    return False
  return (
    route_offset.confidence >= ROUTE_OFFSET_APPLY_MIN_CONFIDENCE
    and route_offset.mad_sec <= ROUTE_OFFSET_APPLY_MAX_MAD_SEC
  )


def calibrate_route(route_id: str, bookmarks: list[dict[str, Any]],
                    samples: list[dict[str, Any]], window_sec: float = ANCHOR_WINDOW_SEC) -> tuple[list[TelemetryEdge], list[AlignmentSuggestion], RouteOffset | None]:
  edges = detect_edges(route_id, samples)
  suggestions: list[AlignmentSuggestion] = []
  for row in bookmarks:
    text = str(row.get("text") or row.get("label") or row.get("reason") or "").strip()
    family = classify_anchor_text(text)
    if family is None:
      continue
    suggestions.append(nearest_edge(route_id, safe_int(row.get("id")), text, safe_float(row.get("t_sec", row.get("t", 0.0))),
                                    family, edges, window_sec))
  return edges, suggestions, route_offset_from_suggestions(route_id, suggestions)


def route_row(store: DriveStore, route_id_or_uuid: str) -> dict[str, Any]:
  row = store.one(
    """SELECT id, route_id, route_label, brickpilot_version, model_bundle, drive_type,
              started_at, ended_at, duration_sec
       FROM routes
       WHERE id::text=? OR route_id=?
       ORDER BY updated_at DESC LIMIT 1""",
    (route_id_or_uuid, route_id_or_uuid),
  )
  if not row:
    raise RuntimeError(f"route not found: {route_id_or_uuid}")
  return dict(row)


def fetch_route_bookmarks(store: DriveStore, route_uuid: str) -> list[dict[str, Any]]:
  return [dict(row) for row in store.execute(
    """SELECT id, t_sec, end_sec, text, tags, source, metadata_jsonb
       FROM bookmarks
       WHERE route_uuid=? AND deleted_at IS NULL
       ORDER BY t_sec, id""",
    (route_uuid,),
  ).fetchall()]


def fetch_route_samples(store: DriveStore, route_uuid: str) -> list[dict[str, Any]]:
  rows = store.execute(
    """SELECT t_sec, speed_mph, set_speed_mph, a_ego_mps2, gas_pressed,
              brake_pressed, lead_status, lead_d_rel_m, lead_v_rel_mps, raw_jsonb
       FROM route_samples
       WHERE route_uuid=?
       ORDER BY t_sec""",
    (route_uuid,),
  ).fetchall()
  out: list[dict[str, Any]] = []
  for row in rows:
    rec = dict(row)
    raw: dict[str, Any] = {}
    try:
      raw = json.loads(rec.get("raw_jsonb") or "{}")
    except Exception:
      raw = {}
    if "steering_pressed" not in rec:
      rec["steering_pressed"] = bool(raw.get("steeringPressed") or raw.get("steering_pressed"))
    out.append(rec)
  return out


def suggestion_rows(suggestions: list[AlignmentSuggestion], route_offset: RouteOffset | None) -> list[dict[str, Any]]:
  route_shift = route_offset.offset_sec if route_offset else 0.0
  route_conf = route_offset.confidence if route_offset else 0.0
  route_applicable = route_offset_is_applicable(route_offset)
  rows: list[dict[str, Any]] = []
  for s in suggestions:
    direct = s.confidence >= DIRECT_ALIGNMENT_MIN_CONFIDENCE and s.offset_sec is not None
    applied_offset = s.offset_sec if direct else (route_shift if route_applicable else None)
    source = s.alignment_source if direct else ("route_offset" if applied_offset is not None else "none")
    rows.append({
      "route_id": s.route_id,
      "bookmark_id": s.bookmark_id,
      "anchor_family": s.anchor_family,
      "raw_text": s.raw_text,
      "original_t_sec": s.original_t_sec,
      "edge_t_sec": "" if s.edge_t_sec is None else s.edge_t_sec,
      "offset_sec": "" if s.offset_sec is None else s.offset_sec,
      "confidence": s.confidence,
      "alignment_source": s.alignment_source,
      "applied_offset_sec": "" if applied_offset is None else round(float(applied_offset), 3),
      "applied_source": source,
      "aligned_t_sec": "" if applied_offset is None else round(s.original_t_sec + float(applied_offset), 3),
      "reason": s.reason,
    })
  return rows


def edge_rows(edges: list[TelemetryEdge]) -> list[dict[str, Any]]:
  return [edge.__dict__ for edge in edges]


def summary_rows(route: dict[str, Any], edges: list[TelemetryEdge],
                 suggestions: list[AlignmentSuggestion], route_offset: RouteOffset | None) -> list[dict[str, Any]]:
  counts = Counter(edge.family for edge in edges if edge.edge == "rising")
  matched = [s for s in suggestions if s.confidence >= DIRECT_ALIGNMENT_MIN_CONFIDENCE]
  return [{
    "route_id": route.get("route_id"),
    "route_uuid": route.get("id"),
    "brickpilot_version": route.get("brickpilot_version"),
    "drive_type": route.get("drive_type"),
    "duration_sec": route.get("duration_sec"),
    "anchor_suggestions": len(suggestions),
    "high_confidence_anchors": len(matched),
    "route_offset_sec": "" if route_offset is None else route_offset.offset_sec,
    "route_offset_confidence": "" if route_offset is None else route_offset.confidence,
    "route_offset_anchor_count": "" if route_offset is None else route_offset.anchor_count,
    "route_offset_mad_sec": "" if route_offset is None else route_offset.mad_sec,
    "route_offset_applicable": route_offset_is_applicable(route_offset),
    "brake_edges": counts.get("brake", 0),
    "gas_edges": counts.get("gas", 0),
    "steer_edges": counts.get("steer", 0),
  }]


def write_report(out_dir: Path, route: dict[str, Any], edges: list[TelemetryEdge],
                 suggestions: list[AlignmentSuggestion], route_offset: RouteOffset | None) -> None:
  high = [s for s in suggestions if s.confidence >= DIRECT_ALIGNMENT_MIN_CONFIDENCE]
  lines = [
    f"# Voice Alignment Calibration: {route.get('route_id')}",
    "",
    "## Summary",
    f"- Anchor suggestions: {len(suggestions)}",
    f"- High-confidence anchors: {len(high)}",
    f"- Telemetry edges: {len(edges)}",
  ]
  if route_offset is not None:
    applicability = "applicable" if route_offset_is_applicable(route_offset) else "reported only; anchors too noisy for global shifting"
    lines.append(f"- Route offset candidate: {route_offset.offset_sec:+.3f}s, confidence {route_offset.confidence:.3f}, anchors {route_offset.anchor_count}, MAD {route_offset.mad_sec:.3f}s ({applicability})")
  else:
    lines.append("- Route offset candidate: unavailable; not enough high-confidence anchors")
  lines.extend([
    "",
    "## Notes",
    "- This does not mutate bookmarks or labels.",
    "- Direct telemetry-edge snaps are only suggested for explicit manual/driver gas, brake, or steering phrases.",
    "- Subjective labels can use the route offset only after enough high-confidence anchors exist and the anchor spread is low.",
    "",
    "## Top Suggestions",
  ])
  for s in sorted(suggestions, key=lambda x: x.confidence, reverse=True)[:20]:
    edge = "none" if s.edge_t_sec is None else f"{s.edge_t_sec:.3f}s"
    off = "none" if s.offset_sec is None else f"{s.offset_sec:+.3f}s"
    lines.append(f"- bookmark {s.bookmark_id} `{s.anchor_family}` t={s.original_t_sec:.3f}s edge={edge} offset={off} confidence={s.confidence:.3f}: {s.raw_text[:100]}")
  lines.append("")
  (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_route(store: DriveStore, route_id_or_uuid: str, out_dir: Path | None = None,
              window_sec: float = ANCHOR_WINDOW_SEC) -> dict[str, Any]:
  route = route_row(store, route_id_or_uuid)
  route_uuid = str(route["id"])
  route_id = str(route["route_id"])
  bookmarks = fetch_route_bookmarks(store, route_uuid)
  samples = fetch_route_samples(store, route_uuid)
  edges, suggestions, route_offset = calibrate_route(route_id, bookmarks, samples, window_sec)
  if out_dir is None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_route = route_id.replace("/", "_")
    out_dir = DEFAULT_OUTPUT_ROOT / f"voice_alignment_{safe_route}_{stamp}"
  out_dir.mkdir(parents=True, exist_ok=True)
  write_csv(out_dir / "telemetry_edges.csv", edge_rows(edges))
  write_csv(out_dir / "alignment_suggestions.csv", suggestion_rows(suggestions, route_offset))
  write_csv(out_dir / "route_alignment_summary.csv", summary_rows(route, edges, suggestions, route_offset))
  payload = {
    "route": route,
    "route_offset": route_offset,
    "suggestions": suggestions,
    "edge_count": len(edges),
    "output_dir": str(out_dir),
  }
  (out_dir / "run_summary.json").write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
  write_report(out_dir, route, edges, suggestions, route_offset)
  return json_safe(payload)


def main() -> int:
  parser = argparse.ArgumentParser(description="Calibrate voice bookmark timing against hard telemetry edges.")
  parser.add_argument("route_id", help="DriveDB route_id or route UUID")
  parser.add_argument("--config", default=None)
  parser.add_argument("--output-dir", type=Path)
  parser.add_argument("--window-sec", type=float, default=ANCHOR_WINDOW_SEC)
  args = parser.parse_args()
  store = DriveStore(load_config(args.config))
  summary = run_route(store, args.route_id, args.output_dir, args.window_sec)
  print(summary["output_dir"])
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
