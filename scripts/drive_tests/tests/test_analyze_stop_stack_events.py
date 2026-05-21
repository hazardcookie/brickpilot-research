from __future__ import annotations

import csv
from pathlib import Path

from scripts.drive_tests.analyze_stop_stack_events import run


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fields: list[str] = []
  for row in rows:
    for key in row:
      if key not in fields:
        fields.append(key)
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fields)
    writer.writeheader()
    writer.writerows(rows)


def read_rows(path: Path) -> list[dict[str, str]]:
  with path.open(newline="", encoding="utf-8") as f:
    return list(csv.DictReader(f))


def test_stop_stack_event_cards_emit_reason_source_and_label_coverage(tmp_path: Path) -> None:
  prelim = tmp_path / "prelim"
  out = tmp_path / "events"
  write_rows(prelim / "brickpilot_shadow_samples.csv", [
    {
      "route_time_sec": 10.0,
      "stopActive": True,
      "stopShadowCandidate": True,
      "stopSource": 4,
      "stopAssistReason": 4,
      "stopDebtBucket": 5,
      "stopBrakeState": 1,
      "stopRequiredDecelValid": True,
      "stopSourceValid": True,
      "stopAssistDelta": -0.9,
      "stopBrakeDebt": 0.7,
      "stopPlannerDebt": 0.1,
      "stopControllerDebt": 0.4,
      "stopSourcePersistSec": 0.6,
      "stopMode": 2,
      "leadAbsSpeed": 0.2,
      "leadNearStoppedPersistSec": 0.6,
      "rollingLeadConfidence": 0.0,
      "finalStopAllowed": True,
      "finalStopBlockedReason": 0,
      "stopTtc": 2.0,
      "stopLeadDistance": 5.0,
      "stopLeadVRel": -0.5,
    },
    {
      "route_time_sec": 40.0,
      "stopActive": False,
      "stopShadowCandidate": True,
      "stopSource": 1,
      "stopAssistReason": 0,
      "stopDebtBucket": 1,
      "stopGeometryInvalidReason": 3,
      "stopRequiredDecelValid": False,
      "stopSourceValid": True,
      "stopMode": 1,
      "leadAbsSpeed": 2.4,
      "rollingLeadConfidence": 1.0,
      "finalStopAllowed": False,
      "finalStopBlockedReason": 5,
      "stopTtc": 12.0,
    },
  ])
  write_rows(prelim / "carstatesp_shadow_samples.csv", [
    {
      "route_time_sec": 10.0,
      "brickpilotPhevFaB4U8": 253,
      "brickpilotPhevFaB4S8": -3,
      "brickpilotPhevFaB7U8": 2,
      "brickpilotBrake065B3U8": 33,
      "brickpilotBrake065B11U8": 146,
      "brickpilotBrake065B12U8": 147,
    }
  ])
  write_rows(prelim / "test_route_predictions.csv", [
    {"target": "stop_creep_fail", "start_sec": 8.0, "end_sec": 12.0, "peak_score": 0.95}
  ])

  run(prelim, out, "test-route", 20.0, 8)

  cards = read_rows(out / "stop_event_cards.csv")
  assert cards
  assert cards[0]["reason_mode"] == "final_stop_commit"
  assert cards[0]["dominant_active_reason"] == "final_stop_commit"
  assert cards[0]["dominant_active_stop_mode"] == "final_stop_commit"
  assert "stop_creep_fail" in cards[0]["nearby_labels"]
  assert cards[0]["can_brickpilotBrake065B11U8_max"] == "146.0"
  assert cards[0]["leadNearStoppedPersistSec_max"] == "0.6"
  sources = read_rows(out / "active_stop_assist_by_source.csv")
  assert sources[0]["value"] == "creep_final_hold"
  modes = read_rows(out / "active_stop_assist_by_stop_mode.csv")
  assert modes[0]["value"] == "final_stop_commit"
  blocks = read_rows(out / "final_stop_blocked_reason_mix.csv")
  assert {row["value"] for row in blocks} == {"none", "rolling_lead"}
  coverage = read_rows(out / "label_coverage_summary.csv")
  assert coverage[0]["prediction_intervals"] == "1"
