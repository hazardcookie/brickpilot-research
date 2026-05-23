#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_ROOT = SCRIPT_DIR.parents[1]
if str(TOOLS_ROOT) not in sys.path:
  sys.path.insert(0, str(TOOLS_ROOT))

from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore
from scripts.drive_tests import voice_alignment_calibrator as voice_align


LEGACY_0325_VALIDATION_ROUTES = (
  "0000018e--5d3d27b763",
  "00000199--d5f5711730",
  "00000197--a5c011c5f6",
  "00000191--bba94609ce",
  "0000018c--68c15f8f88",
  "00000189--6fb7c85de1",
)
VALIDATION_ROUTES_0330 = (
  "0000019e--73cbd7a8fe",
  "0000019f--1f7c2d72e3",
  "000001a0--9ddf29c36d",
  "000001a1--5e4afaa183",
  "000001a2--3ede8392f4",
)
VALIDATION_ROUTES = (*LEGACY_0325_VALIDATION_ROUTES, *VALIDATION_ROUTES_0330)
REVIEWED_TRAINING_ROUTES_04X = (
  "000001c4--500d1c408b",
  "000001c7--0d7f3cc715",
  "000001cb--82868f15fa",
  "000001cd--0220daf57d",
  "000001d3--8074b1f3f1",
  "000001d9--9609d9a67f",
  "000001df--dce7365be2",
  "000001e1--79b080e561",
)
REVIEWED_TRAINING_ROUTES_050 = (
  "000001e5--3c82eba9a4",
)
REVIEWED_TRAINING_ROUTES_051 = (
  "000001eb--51605e23a9",
)
REVIEWED_TRAINING_ROUTES_053 = (
  "000001f5--33fd956c5f",
)
REVIEWED_TRAINING_ROUTES_054 = (
  "000001fa--ab5dda8ae2",
  "000001fb--097115907b",
  "000001fe--3bc01df03a",
)
REVIEWED_TRAINING_ROUTES_055 = (
  "00000203--5d7d932abf",
)
REVIEWED_TRAINING_ROUTES_056 = (
  "00000207--a8c307d230",
  "00000209--f9ffcb0581",
)
REVIEWED_TRAINING_ROUTES_057 = (
  "00000213--ab5b813126",
)
REVIEWED_TRAINING_ROUTES = (
  *REVIEWED_TRAINING_ROUTES_04X,
  *REVIEWED_TRAINING_ROUTES_050,
  *REVIEWED_TRAINING_ROUTES_051,
  *REVIEWED_TRAINING_ROUTES_053,
  *REVIEWED_TRAINING_ROUTES_054,
  *REVIEWED_TRAINING_ROUTES_055,
  *REVIEWED_TRAINING_ROUTES_056,
  *REVIEWED_TRAINING_ROUTES_057,
)
TRAINING_ROUTES = (*VALIDATION_ROUTES, *REVIEWED_TRAINING_ROUTES)
TEST_ROUTE = "00000195--d936b2944f"
ALL_ROUTES = (*TRAINING_ROUTES, TEST_ROUTE)
HUMAN_VALIDATION_ROUTES = (
  "00000199--d5f5711730",
  "000001a2--3ede8392f4",
)
HUMAN_VALIDATION_ROUTE = HUMAN_VALIDATION_ROUTES[0]
STATIONARY_VALIDATION_ROUTE = "0000019e--73cbd7a8fe"
PHEV_MODE_VALIDATION_ROUTE = "0000019f--1f7c2d72e3"
REGEN_BRAKE_VALIDATION_ROUTE = "000001a0--9ddf29c36d"
ENGAGED_LOOP_VALIDATION_ROUTE = "000001a1--5e4afaa183"

DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
DEFAULT_BACKUP_DIR = Path(os.environ.get("BRICKPILOT_BACKUP_ROOT", Path.home() / "BrickpilotDriveDB" / "backups")) / "label_ml_040_beta_prework_20260517T163321Z"

CAN_ADDRESSES = (
  0x6F,
  0xE0,
  0xFA,
  0x1C5,
  0x1A5,
  0x310,
  0x265,
  0x418,
  0x60,
  0x65,
  0x105,
  0x10A,
  0x120,
  0xBA,
)
CAN_BUSES = (0, 1, 2, 128, 130)
MAX_CAN_BYTES = 24

NEGATIVE_LABELS = {
  "accel_too_lazy",
  "accel_target_gap",
  "braking_bad",
  "braking_late",
  "braking_early",
  "braking_absent",
  "missed_stop",
  "stop_creep_issue",
  "stop_creep_fail",
  "stop_hold_fail",
  "stop_go_bad",
  "lead_brake_bad",
  "resume_bad",
  "resume_lazy",
  "driver_brake_intervention",
  "unnecessary_braking",
  "human_intervention_gas",
  "human_intervention_brake",
  "human_intervention_steering",
  "follow_distance_bad",
  "follow_distance_too_far",
  "pacing_bad",
  "pacing_bursty",
  "overbraked_rolling_traffic",
  "steering_jerk",
  "steering_ping_pong",
  "steering_too_damped",
  "low_speed_lateral_bad",
  "quality_bad",
}

GROUPS = {
  "group_phev_regen": {
    "phev_regen_none",
    "phev_regen_light",
    "phev_regen_medium",
    "phev_regen_hard",
    "phev_regen_coast",
  },
  "group_phev_state": {
    "ev_light_on",
    "ev_light_off",
    "ice_engine_on",
    "ice_engine_off",
    "eco_mode",
    "electric_mode",
    "automatic_mode",
    "hybrid_mode",
    "sport_mode",
    "snow_mode",
    "smart_mode",
    "manual_mode",
    "power_meter_charge",
    "power_meter_eco",
    "power_meter_power",
  },
  "group_stationary_state": {
    "ready_park_no_pedals",
    "drive_brake_held",
    "drive_creep_no_gas",
    "auto_hold_on",
    "auto_hold_off",
    "auto_hold_active",
    "auto_hold_release",
    "parking_brake_engaged",
    "gear_neutral",
  },
  "group_brake_stop": {
    "brake_light",
    "brake_medium",
    "brake_hard",
    "brake_good",
    "driver_brake",
    "driver_no_brake",
    "driver_brake_intervention",
    "lead_present_context",
    "lead_brake_good",
    "lead_brake_bad",
    "rolling_follow_good",
    "stop_complete",
    "stop_go",
    "stop_go_bad",
    "stop_creep_good",
    "stop_creep_issue",
    "stop_creep_fail",
    "stop_hold_fail",
    "braking_absent",
    "stop_intent_context",
  },
  "group_longitudinal_bad": {
    "accel_too_lazy",
    "accel_target_gap",
    "braking_bad",
    "braking_late",
    "braking_early",
    "braking_absent",
    "missed_stop",
    "stop_creep_issue",
    "stop_creep_fail",
    "stop_hold_fail",
    "stop_go_bad",
    "lead_brake_bad",
    "resume_bad",
    "resume_lazy",
    "driver_brake_intervention",
    "unnecessary_braking",
    "human_intervention_gas",
    "human_intervention_brake",
    "follow_distance_bad",
    "follow_distance_too_far",
    "pacing_bad",
    "pacing_bursty",
    "overbraked_rolling_traffic",
  },
  "group_follow_profile": {
    "follow_profile_aggressive",
    "follow_profile_standard",
    "follow_distance_setting_1",
    "follow_distance_setting_2",
    "follow_distance_setting_3",
    "follow_distance_setting_4",
  },
  "group_lateral_bad": {
    "steering_jerk",
    "steering_ping_pong",
    "steering_too_damped",
    "human_intervention_steering",
    "low_speed_lateral_bad",
  },
  "group_drive_control": {
    "gear_reverse",
    "gear_drive",
    "gear_park",
    "gear_neutral",
    "cruise_button_press",
    "comma_engaged",
    "comma_disengaged",
    "comma_on_not_active",
    "set_speed",
    "turn_signal_left",
    "turn_signal_right",
    "turn_signal_generic",
  },
  "group_accessory_context": {
    "parking_sensor_on",
    "parking_sensor_off",
    "parking_sensor_mention",
    "parking_camera_on",
    "parking_camera_off",
    "hvac_ac_on",
    "hvac_ac_off",
    "phone_wireless_charger_on",
    "headlight_flash",
  },
  "group_quality_good": {
    "quality_good",
    "smooth_driving",
    "accel_good",
    "brake_good",
    "lead_brake_good",
    "steering_good",
    "lane_change_good",
    "follow_distance_good",
    "rolling_follow_good",
    "pacing_good",
    "human_driving",
  },
}

REVIEW_PRIORITY_LABELS = {
  *NEGATIVE_LABELS,
  "brake_light",
  "brake_medium",
  "brake_hard",
  "brake_good",
  "driver_brake",
  "driver_no_brake",
  "driver_gas",
  "driver_no_gas",
  "stop_complete",
  "stop_go",
  "braking_late",
  "braking_early",
  "human_intervention_gas",
  "human_intervention_brake",
  "driver_brake_intervention",
  "lead_brake_bad",
  "lead_brake_good",
  "rolling_follow_good",
  "lead_present_context",
  "resume_bad",
  "resume_lazy",
  "stop_go_bad",
  "stop_creep_fail",
  "stop_hold_fail",
  "braking_absent",
  "unnecessary_braking",
  "overbraked_rolling_traffic",
  "follow_distance_too_far",
  "pacing_good",
  "pacing_bad",
  "pacing_bursty",
  "phev_regen_none",
  "phev_regen_light",
  "phev_regen_medium",
  "phev_regen_hard",
  "phev_regen_coast",
  "regen_light_coast",
  "ev_light_on",
  "ev_light_off",
  "ice_engine_on",
  "ice_engine_off",
  "eco_mode",
  "electric_mode",
  "automatic_mode",
  "hybrid_mode",
  "sport_mode",
  "snow_mode",
  "smart_mode",
  "manual_mode",
  "power_meter_charge",
  "power_meter_eco",
  "power_meter_power",
  "ready_park_no_pedals",
  "drive_brake_held",
  "drive_creep_no_gas",
  "auto_hold_on",
  "auto_hold_active",
  "auto_hold_release",
  "turn_signal_left",
  "turn_signal_right",
  "follow_profile_aggressive",
  "follow_profile_standard",
  "follow_distance_setting_1",
  "follow_distance_setting_2",
  "follow_distance_setting_3",
  "follow_distance_setting_4",
}

CV_PRIORITY_TARGETS = (
  "steering_jerk",
  "steering_ping_pong",
  "steering_too_damped",
  "human_intervention_steering",
  "low_speed_lateral_bad",
  "group_lateral_bad",
  "accel_too_lazy",
  "accel_target_gap",
  "human_intervention_gas",
  "human_intervention_brake",
  "braking_bad",
  "brake_hard",
  "missed_stop",
  "follow_distance_bad",
  "follow_distance_too_far",
  "pacing_bad",
  "overbraked_rolling_traffic",
  "group_longitudinal_bad",
  "phev_regen_hard",
  "phev_regen_coast",
  "ice_engine_on",
  "ev_light_on",
  "ev_light_off",
  "power_meter_charge",
  "power_meter_power",
  "ready_park_no_pedals",
  "drive_brake_held",
  "auto_hold_active",
  "auto_hold_release",
  "group_stationary_state",
  "steering_good",
  "quality_good",
  "smooth_driving",
)

CV_FIELDNAMES = [
  "holdout_route",
  "target",
  "train_pos_windows",
  "eval_pos_windows",
  "eval_windows",
  "auc",
  "mean_pos_score",
  "mean_neg_score",
  "recall_at_2x_pos",
  "threshold",
]


@dataclass
class RouteInfo:
  uuid: str
  route_id: str
  route_label: str
  drive_type: str
  role: str
  brickpilot_version: str
  model_bundle: str
  duration_sec: float
  segment_count: int
  started_at: str
  ended_at: str


@dataclass
class LabelAtom:
  route_id: str
  source_bookmark_id: int
  session_id: str
  start_sec: float
  end_sec: float
  raw_text: str
  canonical_label: str
  family: str
  polarity: str
  confidence: float
  tags: list[str]


@dataclass
class Window:
  route_id: str
  role: str
  start_sec: float
  end_sec: float
  center_sec: float
  features: dict[str, float]
  labels: set[str] = field(default_factory=set)
  raw_label_refs: list[int] = field(default_factory=list)
  raw_texts: list[str] = field(default_factory=list)


@dataclass
class Model:
  target: str
  pos_count: int
  neg_count: int
  feature_stats: list[dict[str, float]]
  threshold: float


@dataclass
class RunningStat:
  count: int = 0
  total: float = 0.0
  min_value: float = math.inf
  max_value: float = -math.inf

  def add(self, value: float) -> None:
    self.count += 1
    self.total += value
    if value < self.min_value:
      self.min_value = value
    if value > self.max_value:
      self.max_value = value


def clean_text(text: str) -> str:
  s = text.lower().strip()
  s = re.sub(r"[\u2018\u2019]", "'", s)
  s = re.sub(r"[\u201c\u201d]", '"', s)
  s = s.replace("break", "brake")
  s = s.replace("breaking", "braking")
  s = re.sub(r"\blate present\b", "lead present", s)
  s = s.replace("cell too lazy", "accel too lazy")
  s = s.replace("sting light regen", "coasting light regen")
  s = s.replace("ultrasonic", "parking sensor")
  s = re.sub(r"\s+", " ", s)
  return s


def add_label(out: set[str], label: str) -> None:
  if label:
    out.add(label)


def explicit_good_steering(text: str) -> bool:
  return bool(re.search(
    r"\b(good|smooth)\s+(steer|steering|lateral|turn)\b",
    text,
  ))


def normalize_labels(text: str) -> set[str]:
  s = clean_text(text)
  labels: set[str] = set()

  if re.search(r"\b(high beam|headlight)\s+flash", s):
    add_label(labels, "headlight_flash")
  if "phone on wireless charger" in s or "wireless charger" in s:
    add_label(labels, "phone_wireless_charger_on")
  if re.search(r"\bac(\s+light)?\s+on\b", s):
    add_label(labels, "hvac_ac_on")
  if re.search(r"\bac(\s+light)?\s+off\b", s):
    add_label(labels, "hvac_ac_off")

  if "parking sensor" in s:
    if re.search(r"parking sensor[^.]*\bon\b", s):
      add_label(labels, "parking_sensor_on")
    if re.search(r"parking sensor[^.]*\boff\b", s):
      add_label(labels, "parking_sensor_off")
    if "parking_sensor_on" not in labels and "parking_sensor_off" not in labels:
      add_label(labels, "parking_sensor_mention")
  if "parking camera" in s:
    if "off" in s:
      add_label(labels, "parking_camera_off")
    elif "on" in s:
      add_label(labels, "parking_camera_on")
    else:
      add_label(labels, "parking_camera_on")

  if re.search(r"\breverse (engaged|gear|selected)|\bgear.*reverse\b|\bshift.*reverse\b", s):
    add_label(labels, "gear_reverse")
  if re.search(r"\breverse\b.*\bbrak|\bbrak.*\breverse\b", s):
    add_label(labels, "gear_reverse")
    add_label(labels, "driver_brake")
  if re.search(r"\bdrive (engaged|gear|selected)|\bgear.*drive\b|\bshift.*drive\b", s):
    add_label(labels, "gear_drive")
  if re.search(r"\bpark (engaged|gear|selected)|\bparking engaged\b|\bgear.*park\b", s):
    add_label(labels, "gear_park")
  if re.search(r"\bneutral (engaged|gear|selected)|\bgear.*neutral\b|\bshift.*neutral\b", s):
    add_label(labels, "gear_neutral")
  if re.search(r"\boff neutral drive\b|\bneutral drive\b", s):
    add_label(labels, "gear_neutral")
    add_label(labels, "gear_drive")
  if re.search(r"\bready,? park,? no pedals\b", s):
    add_label(labels, "ready_park_no_pedals")
    add_label(labels, "gear_park")
  if re.search(r"\bdrive brake held\b", s):
    add_label(labels, "drive_brake_held")
    add_label(labels, "gear_drive")
    add_label(labels, "driver_brake")
  if re.search(r"\bdrive creep( no gas)?\b", s):
    add_label(labels, "drive_creep_no_gas")
    add_label(labels, "gear_drive")
    if "no gas" in s:
      add_label(labels, "driver_no_gas")
  if "parking brake" in s:
    add_label(labels, "parking_brake_engaged")
  if "auto hold" in s:
    if "active" in s:
      add_label(labels, "auto_hold_active")
    if "release" in s:
      add_label(labels, "auto_hold_release")
    if "off" in s:
      add_label(labels, "auto_hold_off")
    if re.search(r"\bauto hold\b[^.]*\bon\b", s):
      add_label(labels, "auto_hold_on")
  if "cruise button" in s:
    add_label(labels, "cruise_button_press")
  if re.search(r"\bcruise\s+set\b", s):
    add_label(labels, "set_speed")
  if re.search(r"\bcruise\s+engag(?:e|ed|ing)\b", s):
    add_label(labels, "comma_engaged")
    add_label(labels, "set_speed")
  if re.search(r"\b(?:pressed\s+)?comma\s+engag(?:e|ed|ing)\b", s):
    add_label(labels, "comma_engaged")
  if "comma disengaged" in s or "disengaged" in s:
    add_label(labels, "comma_disengaged")
  if "comma on but not active" in s:
    add_label(labels, "comma_on_not_active")
  if re.search(r"\b(set speed|speed set|setting speed)\b", s):
    add_label(labels, "set_speed")

  if "turn signal" in s:
    if "left" in s:
      add_label(labels, "turn_signal_left")
    elif "right" in s:
      add_label(labels, "turn_signal_right")
    else:
      add_label(labels, "turn_signal_generic")

  if re.search(r"\b(ev light|ev mode|electric light)\b.*\bon\b", s):
    add_label(labels, "ev_light_on")
  if re.search(r"\b(ev light|ev mode|electric light)\b.*\boff\b", s):
    add_label(labels, "ev_light_off")
  if re.search(r"\b(engine|gas engine|ice)\b.*\b(on|kicked on|take over|took over|running)\b", s):
    add_label(labels, "ice_engine_on")
  if re.search(r"\b(engine|gas engine|ice)\b.*\b(off|shut off|stopped)\b", s):
    add_label(labels, "ice_engine_off")
  if "eco mode" in s:
    add_label(labels, "eco_mode")
  if "electric mode" in s:
    add_label(labels, "electric_mode")
  if "automatic mode" in s or "auto mode" in s or "automatic selected" in s or s == "automatic":
    add_label(labels, "automatic_mode")
  if "hybrid mode" in s:
    add_label(labels, "hybrid_mode")
  if "sport mode" in s:
    add_label(labels, "sport_mode")
  if "snow mode" in s:
    add_label(labels, "snow_mode")
  if "smart mode" in s:
    add_label(labels, "smart_mode")
  if "manual mode" in s:
    add_label(labels, "manual_mode")
  if re.search(r"\bcharge (section|meter|zone)|\bdip.*charge\b", s):
    add_label(labels, "power_meter_charge")
  if re.search(r"\beco (section|meter|zone)\b", s):
    add_label(labels, "power_meter_eco")
  if re.search(r"\bpower (section|meter|zone)\b", s):
    add_label(labels, "power_meter_power")

  if "regen" in s:
    if "no regen" in s:
      add_label(labels, "phev_regen_none")
    if re.search(r"\b(light|mild|small)\s+regen\b", s):
      add_label(labels, "phev_regen_light")
    if "medium regen" in s:
      add_label(labels, "phev_regen_medium")
    if re.search(r"\b(hard|heavy|full)\s+regen\b", s):
      add_label(labels, "phev_regen_hard")
    if "coasting" in s or "coast" in s:
      add_label(labels, "phev_regen_coast")
    if re.search(r"\bregen\s+brak", s) and not any(x in s for x in ("hard regen", "heavy regen", "medium regen", "light regen", "mild regen", "small regen")):
      add_label(labels, "phev_regen_light")
  if re.search(r"\bcoast(?:ing)?\b", s):
    add_label(labels, "phev_regen_coast")
    add_label(labels, "regen_light_coast")

  if re.search(r"\bfoot on brake\b|\bbrake pressed\b|\bpressing brake\b|\bhuman intervention brake\b|\bhuman brake intervention\b", s):
    add_label(labels, "driver_brake")
  if re.search(r"\bdriver\s+brake\b", s):
    add_label(labels, "driver_brake")
  if re.search(r"\bbrake\s+present\b", s):
    add_label(labels, "brake_light")
  if re.search(r"\bno foot on brake\b|\bfoot off brake\b", s):
    add_label(labels, "driver_no_brake")
  if re.search(r"\brelease\s+brake\b", s):
    add_label(labels, "driver_no_brake")
  if re.search(r"\bfoot on gas\b|\bgas pressed\b|\bpressing gas\b|\bhuman intervention gas\b|\bhuman gas intervention\b|\bmanual gas intervention\b", s):
    add_label(labels, "driver_gas")
  if re.search(r"\bno gas\b|\bfoot off gas\b", s):
    add_label(labels, "driver_no_gas")
  if re.search(r"\b(light|small)\s+brake\b", s):
    add_label(labels, "brake_light")
  if re.search(r"\bbrake\s+engaged\b", s):
    add_label(labels, "driver_brake")
    add_label(labels, "brake_light")
  if re.search(r"\bmedium\s+brake\b", s):
    add_label(labels, "brake_medium")
  if re.search(r"\b(hard|heavy|full)\s+brake\b", s):
    add_label(labels, "brake_hard")
  if re.search(r"\b(full|complete)\s+stop\b|\bstop complete\b", s):
    add_label(labels, "stop_complete")
  if re.search(r"\bgood\s+(full\s+)?stop\b|\bsmooth\s+(full\s+)?stop\b", s):
    add_label(labels, "brake_good")
    add_label(labels, "stop_complete")
  if re.search(r"\bgood\s+for\s+stop\b", s):
    add_label(labels, "brake_good")
  if "stop and go" in s or "stop-and-go" in s:
    add_label(labels, "stop_go")
    if re.search(r"\bbad\b|\btoo aggressive\b|\bdriver\s+brake\s+needed\b|\blazy\b", s):
      add_label(labels, "stop_go_bad")
    if "lazy" in s:
      add_label(labels, "resume_lazy")
    if re.search(r"\blate\s+.*resume|\bresume\s+late\b", s):
      add_label(labels, "resume_bad")
      add_label(labels, "resume_lazy")
  if "stop creep" in s or "creep issue" in s:
    add_label(labels, "stop_creep_issue")
  if "stop and creep good" in s:
    add_label(labels, "stop_creep_good")
  if re.search(r"\bgood\s+rolling\s+(stop|brake|follow)\b|\bsmooth\s+rolling\s+(stop|brake|follow)\b|\bgood\s+rolling\s+traffic\b", s):
    add_label(labels, "rolling_follow_good")
    add_label(labels, "brake_good")
  if re.search(r"\bgood\s+pacing\b|\bsmooth\s+pacing\b|\bpacing\s+good\b", s):
    add_label(labels, "pacing_good")
  if re.search(r"\bbad\s+pacing\b|\bpacing\s+bad\b|\bweird\s+pacing\b|\bstrange\s+pacing\b|\bbursty\b|\bsudden\s+burst", s):
    add_label(labels, "pacing_bad")
    add_label(labels, "quality_bad")
    if re.search(r"\bbursty\b|\bsudden\s+burst", s):
      add_label(labels, "pacing_bursty")
  if re.search(r"\b(good|smooth)\b.*\bbrak|\bbrak.*\b(good|smooth)\b", s):
    add_label(labels, "brake_good")

  if re.search(r"\blead(?: vehicle)?\b", s) and not re.search(r"\bno\s+lead(?:\s+present)?\b", s):
    add_label(labels, "lead_present_context")
    if re.search(r"\b(good|smooth)\b.*\bbrak|\bbrak.*\b(good|smooth)\b|\bgood\s+full\s+stop\b", s):
      add_label(labels, "lead_brake_good")
      add_label(labels, "brake_good")
    if re.search(r"\bbad(?:ly)?\b.*\bbrak|\bbrak.*\bbad(?:ly)?\b|\blate\s+brak", s):
      add_label(labels, "lead_brake_bad")
      add_label(labels, "braking_bad")
    if re.search(r"\blate\s+brak", s):
      add_label(labels, "braking_late")

  if re.search(r"\bdriver\s+brak(?:e|ing)\s+(needed|need)\b|\bbrake\s+driver\s+brak(?:e|ing)\s+(needed|need)\b", s):
    add_label(labels, "driver_brake")
    add_label(labels, "driver_brake_intervention")
    add_label(labels, "human_intervention_brake")
    add_label(labels, "braking_bad")
    if "lead_present_context" in labels:
      add_label(labels, "lead_brake_bad")

  if re.search(r"\bbad\s+resume\b|\bresume\s+bad\b", s):
    add_label(labels, "resume_bad")
  if re.search(r"\blazy\s+resume\b|\bresume\s+lazy\b|\bslow\s+resume\b|\bresume\s+slow\b", s):
    add_label(labels, "resume_lazy")
  if re.search(r"\bgood\s+resume\b|\bsmooth\s+resume\b|\bresume\s+good\b|\bresume\s+smooth\b", s):
    add_label(labels, "accel_good")
  if re.search(r"\bgood\s+distance\b.*\bstop\b|\bstop\b.*\bgood\s+distance\b", s):
    add_label(labels, "follow_distance_good")
    add_label(labels, "brake_good")

  if re.search(r"\btoo lazy\b|\blazy\b|\bslow.*set speed\b|\bslow.*target\b|\bbad\s+accel(?:eration)?\b|\baccel(?:eration)?\s+bad\b", s):
    add_label(labels, "accel_too_lazy")
  if re.search(r"\bslow catch[- ]?up\b|\bslow .*catch[- ]?up\b", s):
    add_label(labels, "accel_too_lazy")
    add_label(labels, "accel_target_gap")
    add_label(labels, "resume_lazy")
  if re.search(r"\blate\s+accel(?:eration)?\b", s):
    add_label(labels, "accel_too_lazy")
    add_label(labels, "accel_target_gap")
  if re.search(r"\btarget speed\b|\bget to set speed\b|\bspeed deficit\b", s):
    add_label(labels, "accel_target_gap")
  if re.search(r"\bbad\s+(lead\s+)?brak|\bbrak(?:e|ing)?\s+bad\b|\bmissed brak|\bfailed brak|\bbrake too late\b|\bstop too late\b|\blate stop\b", s):
    add_label(labels, "braking_bad")
  if re.search(r"\bbrake too late\b|\bstop too late\b|\blate stop\b|\bbad late stop\b", s):
    add_label(labels, "braking_late")
  if re.search(r"\bbrak(e|ing) too early\b|\bstopping too early\b|\bover[- ]?brak|\bover[- ]?committ?ing (to )?(brak|stop)|\btoo early\b|\bnot a full stop\b|\brolling kind of stop\b", s):
    add_label(labels, "braking_early")
  if re.search(r"\bover[- ]?brak|\bover[- ]?committ?ing (to )?(brak|stop)|\bmore of a roll\b|\brolling kind of stop\b|\bnot a full stop\b", s):
    add_label(labels, "unnecessary_braking")
    add_label(labels, "overbraked_rolling_traffic")
    add_label(labels, "quality_bad")
  if re.search(r"\bno brake\b|\bno braking\b", s):
    add_label(labels, "braking_absent")
    add_label(labels, "braking_bad")
  if re.search(r"\bmiss(?:ed)? stop\b|\bfailed stop\b|\bdid not stop\b|\bnot stopping\b", s):
    add_label(labels, "missed_stop")
  if "stop sign" in s or "traffic light" in s:
    add_label(labels, "traffic_control_context")
  if re.search(r"\bhuman intervention gas\b|\bhuman gas intervention\b|\bmanual gas intervention\b", s):
    add_label(labels, "human_intervention_gas")
  if re.search(r"\bhuman intervention brake\b|\bhuman brake intervention\b", s):
    add_label(labels, "human_intervention_brake")
  if re.search(r"\bbeen on (?:the )?gas\b|\bput on gas\b", s):
    add_label(labels, "driver_gas")
    add_label(labels, "human_intervention_gas")
  if "manual braking" in s:
    add_label(labels, "driver_brake")
    add_label(labels, "human_intervention_brake")
  if re.search(r"\b(good|smooth)\b.*\baccel|\baccel.*\b(good|smooth)\b", s):
    add_label(labels, "accel_good")
  if ("maintain speed" in s or "target speed" in s) and "good" in s:
    add_label(labels, "accel_good")

  if "ping pong" in s:
    add_label(labels, "steering_ping_pong")
  if re.search(r"\bjerk|jerky|jittery|twitchy", s) and re.search(r"\bsteer|wheel|lane|lateral", s):
    add_label(labels, "steering_jerk")
  if re.search(r"\bblocky\s+steer", s):
    add_label(labels, "steering_jerk")
  if re.search(r"\bstair\W*step|\bstair\W*stepp|\bstair-?step", s) or ("stair" in s and "step" in s):
    add_label(labels, "steering_jerk")
  if re.search(r"\bblocky\b|\bblocking\b|\bchoppy\b", s) and re.search(r"\bsteer|\bwheel|\breturn|\bentry|\bexit|\bturn", s):
    add_label(labels, "steering_jerk")
  if re.search(r"\bbad\s+steer|\bsteer(?:ing)?\s+bad", s):
    add_label(labels, "steering_jerk")
  if "steering override" in s or "manual steering override" in s:
    add_label(labels, "driver_steering")
    add_label(labels, "human_intervention_steering")
  if "steering too damp" in s or "too damp" in s:
    add_label(labels, "steering_too_damped")
  if "low speed lateral bad" in s:
    add_label(labels, "low_speed_lateral_bad")
  if re.search(r"\b(good|smooth)\b.*\b(steer|lateral|turn)|\b(steer|lateral|turn).*\b(good|smooth)\b", s):
    add_label(labels, "steering_good")
  if re.search(r"\bgood\s+lane\s+centering\b|\bsmooth\s+lane\s+centering\b", s):
    add_label(labels, "steering_good")
  if "steering_jerk" in labels and "blocky" in s and not explicit_good_steering(s):
    labels.discard("steering_good")
  if "lane change" in s and ("good" in s or "smooth" in s):
    add_label(labels, "lane_change_good")
  if "follow distance" in s and "good" in s:
    add_label(labels, "follow_distance_good")
  if re.search(r"\baggressive\s+follow\s+distance\b", s):
    add_label(labels, "follow_profile_aggressive")
  if re.search(r"\bstandard\s+follow\s+distance\b", s):
    add_label(labels, "follow_profile_standard")
  for idx, word in enumerate(("one", "two", "three", "four"), start=1):
    if re.search(rf"\bdistance\s+{word}\b|\bdistance\s+{idx}\b", s):
      add_label(labels, f"follow_distance_setting_{idx}")
  if "i don't like this distance" in s or "dont like this distance" in s:
    add_label(labels, "follow_distance_bad")
    add_label(labels, "quality_bad")
    if "distance four" in s or "distance 4" in s:
      add_label(labels, "follow_distance_too_far")
  if re.search(r"\bfollow distance\s+too\s+far\b|\btoo\s+far\s+follow distance\b|\bfollowing\s+too\s+far\b", s):
    add_label(labels, "follow_distance_too_far")
    add_label(labels, "follow_distance_bad")
    add_label(labels, "quality_bad")
  if re.search(r"\bbad follow distance\b|\bnot following\b", s):
    add_label(labels, "follow_distance_bad")
    add_label(labels, "quality_bad")
  if "human driving active" in s or re.search(r"\bhuman\b.*\b(good|smooth|active)\b", s):
    add_label(labels, "human_driving")
  if re.search(r"\bsmooth\b|\bnice\b|\bgood\b", s) and not re.search(r"\bnot good\b|\bbad\b", s):
    add_label(labels, "quality_good")
    if "smooth" in s:
      add_label(labels, "smooth_driving")
  if re.search(r"\bgood\s+coast|\bsmooth\s+coast", s):
    add_label(labels, "phev_regen_coast")
    add_label(labels, "regen_light_coast")
  if re.search(r"\bbad\b|\bscary\b|\bawful\b|\bterrible\b", s):
    add_label(labels, "quality_bad")
  if re.search(r"\bpower mode\b", s):
    add_label(labels, "power_meter_power")

  if not labels:
    add_label(labels, "narration_other")
  return labels


def label_family(label: str) -> str:
  if label.startswith("phev_") or label.startswith("regen_") or label in {
    "ev_light_on",
    "ev_light_off",
    "ice_engine_on",
    "ice_engine_off",
    "eco_mode",
    "electric_mode",
    "automatic_mode",
    "hybrid_mode",
    "sport_mode",
    "snow_mode",
    "smart_mode",
    "manual_mode",
    "power_meter_charge",
    "power_meter_eco",
    "power_meter_power",
  }:
    return "phev"
  if label in GROUPS["group_stationary_state"]:
    return "stationary"
  if (
    label.startswith(("brake", "braking", "lead_", "resume_", "accel"))
    or "stop" in label
    or "rolling_follow" in label
    or label.startswith("pacing_")
    or label.startswith("follow_profile_")
    or label.startswith("follow_distance_setting_")
    or label in {
    "driver_gas",
    "driver_brake",
    "driver_no_gas",
    "driver_no_brake",
    "driver_brake_intervention",
    "human_intervention_gas",
    "human_intervention_brake",
    "follow_distance_bad",
    "follow_distance_good",
    "follow_distance_too_far",
    "unnecessary_braking",
    "overbraked_rolling_traffic",
    }
  ):
    return "longitudinal"
  if label.startswith("steering") or label.startswith("turn_signal") or label in {"driver_steering", "human_intervention_steering", "low_speed_lateral_bad", "lane_change_good"}:
    return "lateral"
  if label.startswith("quality") or label in GROUPS["group_quality_good"]:
    return "quality"
  if label.startswith("gear") or label in {"cruise_button_press", "comma_engaged", "comma_disengaged", "comma_on_not_active", "set_speed"}:
    return "drive_control"
  if label in GROUPS["group_accessory_context"]:
    return "accessory"
  return "other"


def label_polarity(label: str) -> str:
  if label in NEGATIVE_LABELS:
    return "negative"
  if label in GROUPS["group_quality_good"]:
    return "positive"
  return "context"


def safe_float(value: Any, default: float = 0.0) -> float:
  try:
    if value is None:
      return default
    x = float(value)
    if not math.isfinite(x):
      return default
    return x
  except Exception:
    return default


def rows_as_dict(rows: list[Any]) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  for row in rows:
    d = dict(row)
    out.append(d)
  return out


def json_safe(value: Any) -> Any:
  if isinstance(value, (str, int, float, bool)) or value is None:
    return value
  if isinstance(value, dict):
    return {str(k): json_safe(v) for k, v in value.items()}
  if isinstance(value, (list, tuple, set)):
    return [json_safe(v) for v in value]
  return str(value)


def identity_counter(rows: list[dict[str, Any]], key: str) -> Counter[str]:
  out: Counter[str] = Counter()
  for row in rows:
    value = row.get(key)
    if value:
      out[str(value)] += 1
    else:
      payload = json.dumps(json_safe(row), sort_keys=True, separators=(",", ":"))
      out[hashlib.sha256(payload.encode("utf-8")).hexdigest()] += 1
  return out


def fetch_identity_rows(store: DriveStore, table: str, hash_col: str, route_uuids: list[str]) -> list[dict[str, Any]]:
  placeholders = ",".join("?" for _ in route_uuids)
  rows = store.execute(
    f"SELECT id, route_uuid, {hash_col} FROM {table} WHERE route_uuid IN ({placeholders}) ORDER BY id",
    tuple(route_uuids),
  ).fetchall()
  return rows_as_dict(rows)


def verify_voice_backup(backup_dir: Path, store: DriveStore, infos: dict[str, RouteInfo]) -> dict[str, Any]:
  manifest_path = backup_dir / "backup_manifest.json"
  export_path = backup_dir / "db_exports" / "routes_bookmarks_labels.json"
  if not export_path.exists():
    export_path = backup_dir / "db_exports" / "routes_bookmarks_labels_reviews.json"
  if not backup_dir.exists():
    raise RuntimeError(f"voice backup does not exist: {backup_dir}")
  if not manifest_path.exists() or not export_path.exists():
    raise RuntimeError(f"voice backup is missing backup_manifest.json or db_exports/routes_bookmarks_labels*.json: {backup_dir}")

  manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
  export = json.loads(export_path.read_text(encoding="utf-8"))
  backup_routes = sorted(str(row.get("route_id", "")) for row in export.get("routes", []) if not row.get("missing"))
  expected_routes = sorted(route_id for route_id in ALL_ROUTES if route_id not in REVIEWED_TRAINING_ROUTES)
  if backup_routes != expected_routes:
    raise RuntimeError(f"voice backup route set mismatch: expected {expected_routes}, got {backup_routes}")
  bad_versions = {
    str(row.get("route_id")): str(row.get("brickpilot_version") or "")
    for row in export.get("routes", [])
    if str(row.get("brickpilot_version") or "") not in {"0.3.25.0", "0.3.30.0"}
  }
  if bad_versions:
    raise RuntimeError(f"voice backup includes unexpected Brickpilot versions: {bad_versions}")

  session_ids = [str(row.get("session_id") or "") for row in export.get("sessions", []) if row.get("session_id")]
  if not session_ids:
    session_ids = []
    for row in export.get("bookmarks", []):
      metadata = row.get("metadata_jsonb") or {}
      if isinstance(metadata, str):
        try:
          metadata = json.loads(metadata)
        except Exception:
          metadata = {}
      session_id = str(metadata.get("session_id") or "")
      if session_id and session_id not in session_ids:
        session_ids.append(session_id)
  session_roots = (backup_dir / "sessions", backup_dir / "voice_sessions")
  missing_sessions = [
    session_id
    for session_id in session_ids
    if not any((root / session_id).exists() for root in session_roots)
  ]
  if missing_sessions:
    raise RuntimeError(f"voice backup is missing session directories: {missing_sessions}")

  route_uuids = [infos[route_id].uuid for route_id in expected_routes]
  current_bookmarks = fetch_identity_rows(store, "bookmarks", "bookmark_identity_hash", route_uuids)
  current_labels = fetch_identity_rows(store, "labels", "label_identity_hash", route_uuids)
  backup_bookmarks = list(export.get("bookmarks", []))
  backup_labels = list(export.get("labels", []))

  if identity_counter(current_bookmarks, "bookmark_identity_hash") != identity_counter(backup_bookmarks, "bookmark_identity_hash"):
    raise RuntimeError("voice backup bookmark identity set does not match current DB rows for the scoped routes")
  if identity_counter(current_labels, "label_identity_hash") != identity_counter(backup_labels, "label_identity_hash"):
    raise RuntimeError("voice backup label identity set does not match current DB rows for the scoped routes")

  expected_counts = {
    "route_count": len(expected_routes),
    "bookmark_rows": len(backup_bookmarks),
    "label_rows": len(backup_labels),
    "session_count": len(session_ids),
  }
  for key, value in expected_counts.items():
    if int(manifest.get(key, value)) != value:
      raise RuntimeError(f"voice backup manifest {key}={manifest.get(key)} does not match export count {value}")

  return {
    "backup_dir": str(backup_dir),
    "route_count": len(expected_routes),
    "bookmark_rows": len(backup_bookmarks),
    "label_rows": len(backup_labels),
    "session_count": len(session_ids),
    "session_file_count": int(manifest.get("session_file_count") or 0),
  }


def fetch_route_infos(store: DriveStore) -> dict[str, RouteInfo]:
  infos: dict[str, RouteInfo] = {}
  for route_id in ALL_ROUTES:
    row = store.one(
      """SELECT id, route_id, route_label, drive_type, brickpilot_version, model_bundle,
                duration_sec, segment_count, started_at, ended_at
         FROM routes WHERE route_id=? ORDER BY updated_at DESC LIMIT 1""",
      (route_id,),
    )
    if not row:
      raise RuntimeError(f"route not found in DB: {route_id}")
    role = "test"
    if route_id in VALIDATION_ROUTES:
      role = "human_validation" if route_id in HUMAN_VALIDATION_ROUTES else "label_validation"
      if route_id == STATIONARY_VALIDATION_ROUTE:
        role = "stationary_validation"
      elif route_id == PHEV_MODE_VALIDATION_ROUTE:
        role = "phev_mode_validation"
      elif route_id == REGEN_BRAKE_VALIDATION_ROUTE:
        role = "regen_brake_validation"
      elif route_id == ENGAGED_LOOP_VALIDATION_ROUTE:
        role = "engaged_loop_validation"
    elif route_id in REVIEWED_TRAINING_ROUTES:
      role = "reviewed_test_training"
    infos[route_id] = RouteInfo(
      uuid=str(row["id"]),
      route_id=str(row["route_id"]),
      route_label=str(row["route_label"] or row["route_id"]),
      drive_type=str(row["drive_type"] or ""),
      role=role,
      brickpilot_version=str(row["brickpilot_version"] or ""),
      model_bundle=str(row["model_bundle"] or ""),
      duration_sec=safe_float(row["duration_sec"]),
      segment_count=int(row["segment_count"] or 0),
      started_at=str(row["started_at"]),
      ended_at=str(row["ended_at"]),
    )
  return infos


def json_object(value: Any) -> dict[str, Any]:
  if isinstance(value, dict):
    return dict(value)
  if isinstance(value, str) and value:
    try:
      parsed = json.loads(value)
      return parsed if isinstance(parsed, dict) else {}
    except Exception:
      return {}
  return {}


def tag_list(value: Any) -> list[str]:
  if isinstance(value, list):
    return [str(x) for x in value]
  if isinstance(value, tuple):
    return [str(x) for x in value]
  if isinstance(value, str) and value:
    try:
      parsed = json.loads(value)
      if isinstance(parsed, list):
        return [str(x) for x in parsed]
    except Exception:
      return []
  return []


def aligned_voice_window(start_sec: float, end_sec: float, tags: list[str],
                         suggestion: voice_align.AlignmentSuggestion | None,
                         route_offset: voice_align.RouteOffset | None) -> tuple[float, float, list[str]]:
  applied_offset: float | None = None
  source = "none"
  confidence = 0.0
  if suggestion is not None and suggestion.offset_sec is not None and suggestion.confidence >= voice_align.DIRECT_ALIGNMENT_MIN_CONFIDENCE:
    applied_offset = float(suggestion.offset_sec)
    source = f"telemetry_edge_{suggestion.anchor_family}"
    confidence = suggestion.confidence
  elif voice_align.route_offset_is_applicable(route_offset):
    applied_offset = float(route_offset.offset_sec)
    source = "route_offset"
    confidence = route_offset.confidence

  if applied_offset is None:
    return start_sec, end_sec, tags

  out_tags = list(tags)
  out_tags.extend([
    "voice_timing_aligned",
    f"alignment_source:{source}",
    f"alignment_offset_sec:{applied_offset:.3f}",
    f"alignment_confidence:{confidence:.3f}",
  ])
  return max(0.0, start_sec + applied_offset), max(0.0, end_sec + applied_offset), out_tags


def fetch_voice_atoms(store: DriveStore, infos: dict[str, RouteInfo]) -> list[LabelAtom]:
  atoms: list[LabelAtom] = []
  for route_id in VALIDATION_ROUTES:
    info = infos[route_id]
    rows = store.execute(
      """SELECT id, t_sec, end_sec, text, tags, metadata_jsonb
         FROM bookmarks
         WHERE route_uuid=? AND deleted_at IS NULL AND source='voice_narration'
         ORDER BY t_sec, id""",
      (info.uuid,),
    ).fetchall()
    raw_rows = [dict(row) for row in rows]
    samples = voice_align.fetch_route_samples(store, info.uuid)
    _, alignment_suggestions, route_offset = voice_align.calibrate_route(route_id, raw_rows, samples)
    alignment_by_bookmark = {s.bookmark_id: s for s in alignment_suggestions}
    for row in rows:
      raw_text = str(row["text"] or "").strip()
      if not raw_text:
        continue
      metadata = json_object(row["metadata_jsonb"])
      session_id = str(metadata.get("session_id") or "")
      start_sec = safe_float(row["t_sec"])
      end_sec = safe_float(row["end_sec"], start_sec + 3.0)
      if end_sec < start_sec:
        start_sec, end_sec = end_sec, start_sec
      if end_sec - start_sec < 0.5:
        end_sec = start_sec + 1.0
      tags = tag_list(row["tags"])
      start_sec, end_sec, tags = aligned_voice_window(
        start_sec, end_sec, tags,
        alignment_by_bookmark.get(int(row["id"])),
        route_offset,
      )
      for canonical in sorted(normalize_labels(raw_text)):
        atoms.append(
          LabelAtom(
            route_id=route_id,
            source_bookmark_id=int(row["id"]),
            session_id=session_id,
            start_sec=start_sec,
            end_sec=end_sec,
            raw_text=raw_text,
            canonical_label=canonical,
            family=label_family(canonical),
            polarity=label_polarity(canonical),
            confidence=1.0,
            tags=tags,
          )
        )
  for route_id in REVIEWED_TRAINING_ROUTES:
    info = infos[route_id]
    rows = store.execute(
      """SELECT id, label, severity, start_sec, end_sec, notes, tags, metadata_jsonb
         FROM labels
         WHERE route_uuid=? AND deleted_at IS NULL
         ORDER BY start_sec, id""",
      (info.uuid,),
    ).fetchall()
    for row in rows:
      canonical = str(row["label"] or "").strip()
      if not canonical:
        continue
      metadata = json_object(row["metadata_jsonb"])
      source_metadata = json_object(metadata.get("source_bookmark_metadata"))
      raw_text = str(metadata.get("source_bookmark_text") or row["notes"] or canonical).strip()
      if raw_text.startswith("voice: "):
        raw_text = raw_text[7:].strip()
      start_sec = safe_float(row["start_sec"])
      end_sec = safe_float(row["end_sec"], start_sec + 3.0)
      if end_sec < start_sec:
        start_sec, end_sec = end_sec, start_sec
      if end_sec - start_sec < 0.5:
        end_sec = start_sec + 1.0
      source_bookmark_id = int(metadata.get("source_bookmark_id") or row["id"])
      session_id = str(source_metadata.get("session_id") or metadata.get("session_id") or "")
      tags = tag_list(row["tags"])
      if "reviewed_db_label" not in tags:
        tags.append("reviewed_db_label")
      atoms.append(
        LabelAtom(
          route_id=route_id,
          source_bookmark_id=source_bookmark_id,
          session_id=session_id,
          start_sec=start_sec,
          end_sec=end_sec,
          raw_text=raw_text,
          canonical_label=canonical,
          family=label_family(canonical),
          polarity=label_polarity(canonical),
          confidence=1.0,
          tags=tags,
        )
      )
  return atoms


def summarize_values(values: list[float], prefix: str, out: dict[str, float]) -> None:
  vals = [v for v in values if math.isfinite(v)]
  if not vals:
    out[f"{prefix}_count"] = 0.0
    return
  out[f"{prefix}_count"] = float(len(vals))
  out[f"{prefix}_mean"] = sum(vals) / len(vals)
  out[f"{prefix}_min"] = min(vals)
  out[f"{prefix}_max"] = max(vals)
  if len(vals) > 1:
    out[f"{prefix}_std"] = statistics.pstdev(vals)
  else:
    out[f"{prefix}_std"] = 0.0


def build_windows(
  store: DriveStore,
  infos: dict[str, RouteInfo],
  atoms: list[LabelAtom],
  window_sec: float,
  step_sec: float,
  skip_can: bool = False,
) -> list[Window]:
  atoms_by_route: dict[str, list[LabelAtom]] = defaultdict(list)
  for atom in atoms:
    atoms_by_route[atom.route_id].append(atom)

  windows: list[Window] = []
  windows_by_route: dict[str, list[Window]] = {}
  for route_id in ALL_ROUTES:
    info = infos[route_id]
    route_windows: list[Window] = []
    t = 0.0
    while t < max(1.0, info.duration_sec):
      end = min(info.duration_sec, t + window_sec)
      route_windows.append(
        Window(
          route_id=route_id,
          role=info.role,
          start_sec=round(t, 3),
          end_sec=round(end, 3),
          center_sec=round((t + end) / 2.0, 3),
          features={},
        )
      )
      t += step_sec
    windows_by_route[route_id] = route_windows
    windows.extend(route_windows)

  for route_id, route_windows in windows_by_route.items():
    info = infos[route_id]
    samples = rows_as_dict(
      store.execute(
        """SELECT t_sec, speed_mph, set_speed_mph, a_ego_mps2, gas_pressed,
                  brake_pressed, lead_status, lead_d_rel_m, lead_v_rel_mps
           FROM route_samples WHERE route_uuid=? ORDER BY t_sec""",
        (info.uuid,),
      ).fetchall()
    )
    sample_idx = 0
    for win in route_windows:
      while sample_idx < len(samples) and safe_float(samples[sample_idx]["t_sec"]) < win.start_sec:
        sample_idx += 1
      j = sample_idx
      bucket: list[dict[str, Any]] = []
      while j < len(samples) and safe_float(samples[j]["t_sec"]) <= win.end_sec:
        bucket.append(samples[j])
        j += 1
      feats = win.features
      feats["role_human_validation"] = 1.0 if info.role == "human_validation" else 0.0
      feats["role_label_validation"] = 1.0 if info.role.endswith("_validation") and info.role != "human_validation" else 0.0
      feats[f"role_{info.role}"] = 1.0
      feats["role_test"] = 1.0 if info.role == "test" else 0.0
      feats["time_norm"] = win.center_sec / max(1.0, info.duration_sec)
      speeds = [safe_float(s.get("speed_mph"), math.nan) for s in bucket]
      accels = [safe_float(s.get("a_ego_mps2"), math.nan) for s in bucket]
      set_speeds = [safe_float(s.get("set_speed_mph"), math.nan) for s in bucket]
      valid_set = [x for x in set_speeds if 5.0 <= x <= 95.0]
      deficits = []
      for s in bucket:
        sp = safe_float(s.get("speed_mph"), math.nan)
        ss = safe_float(s.get("set_speed_mph"), math.nan)
        if math.isfinite(sp) and math.isfinite(ss) and 5.0 <= ss <= 95.0:
          deficits.append(ss - sp)
      summarize_values(speeds, "speed_mph", feats)
      summarize_values(accels, "a_ego_mps2", feats)
      summarize_values(valid_set, "set_speed_mph_valid", feats)
      summarize_values(deficits, "speed_deficit_mph", feats)
      n = max(1, len(bucket))
      feats["gas_pressed_frac"] = sum(1 for s in bucket if s.get("gas_pressed")) / n
      feats["brake_pressed_frac"] = sum(1 for s in bucket if s.get("brake_pressed")) / n
      feats["lead_status_frac"] = sum(1 for s in bucket if s.get("lead_status")) / n
      feats["stopped_frac"] = sum(1 for x in speeds if math.isfinite(x) and x < 0.5) / n
      feats["low_speed_frac"] = sum(1 for x in speeds if math.isfinite(x) and x < 8.0) / n
      lead_ds = [safe_float(s.get("lead_d_rel_m"), math.nan) for s in bucket if s.get("lead_d_rel_m") is not None]
      lead_vs = [safe_float(s.get("lead_v_rel_mps"), math.nan) for s in bucket if s.get("lead_v_rel_mps") is not None]
      summarize_values(lead_ds, "lead_d_rel_m", feats)
      summarize_values(lead_vs, "lead_v_rel_mps", feats)

  if not skip_can:
    add_can_features(store, infos, windows_by_route, window_sec, step_sec)

  for win in windows:
    for atom in atoms_by_route.get(win.route_id, []):
      overlap = max(0.0, min(win.end_sec, atom.end_sec) - max(win.start_sec, atom.start_sec))
      atom_center = (atom.start_sec + atom.end_sec) / 2.0
      near = abs(win.center_sec - atom_center) <= (window_sec / 2.0)
      if overlap > 0.0 or near:
        win.labels.add(atom.canonical_label)
        win.raw_label_refs.append(atom.source_bookmark_id)
        if atom.raw_text not in win.raw_texts:
          win.raw_texts.append(atom.raw_text)
    for group, members in GROUPS.items():
      if win.labels.intersection(members):
        win.labels.add(group)
    if win.role == "human_validation" and not win.labels.intersection(NEGATIVE_LABELS):
      win.labels.add("quality_human_baseline")
  return windows


def add_can_features(
  store: DriveStore,
  infos: dict[str, RouteInfo],
  windows_by_route: dict[str, list[Window]],
  window_sec: float,
  step_sec: float,
) -> None:
  address_placeholders = ",".join("?" for _ in CAN_ADDRESSES)
  bus_placeholders = ",".join("?" for _ in CAN_BUSES)
  for route_id, route_windows in windows_by_route.items():
    info = infos[route_id]
    accum: list[dict[str, RunningStat]] = [dict() for _ in route_windows]
    rows = store.execute(
      f"""SELECT t_sec, bus, address, data_hex
          FROM can_frames_sampled
          WHERE route_uuid=? AND address IN ({address_placeholders}) AND bus IN ({bus_placeholders})
          ORDER BY t_sec""",
      (info.uuid, *CAN_ADDRESSES, *CAN_BUSES),
    ).fetchall()
    for row in rows:
      t_sec = safe_float(row["t_sec"], math.nan)
      if not math.isfinite(t_sec) or t_sec < -window_sec or t_sec > info.duration_sec + window_sec:
        continue
      try:
        payload = bytes.fromhex(str(row["data_hex"] or ""))
      except ValueError:
        continue
      if not payload:
        continue
      first = max(0, int(math.floor((t_sec - window_sec) / step_sec)))
      last = min(len(route_windows) - 1, int(math.floor(t_sec / step_sec)) + 1)
      bus = int(row["bus"])
      addr = int(row["address"])
      prefix = f"can_bus{bus}_addr{addr:03x}"
      for idx in range(first, last + 1):
        win = route_windows[idx]
        if win.start_sec <= t_sec <= win.end_sec:
          count_key = f"{prefix}_count"
          count_stat = accum[idx].get(count_key)
          if count_stat is None:
            count_stat = RunningStat()
            accum[idx][count_key] = count_stat
          count_stat.add(1.0)
          for bidx, bval in enumerate(payload[:MAX_CAN_BYTES]):
            key = f"{prefix}_b{bidx:02d}"
            stat = accum[idx].get(key)
            if stat is None:
              stat = RunningStat()
              accum[idx][key] = stat
            stat.add(float(bval))
    for idx, stats in enumerate(accum):
      feats = windows_by_route[route_id][idx].features
      for key, stat in stats.items():
        if key.endswith("_count"):
          feats[key] = float(stat.count)
          continue
        if stat.count <= 0:
          continue
        mean = stat.total / stat.count
        feats[f"{key}_mean"] = mean
        feats[f"{key}_range"] = stat.max_value - stat.min_value


def feature_names(windows: list[Window]) -> list[str]:
  names: set[str] = set()
  for win in windows:
    names.update(win.features.keys())
  return sorted(names)


def select_model_feature_names(windows: list[Window], max_can_features: int) -> list[str]:
  names = feature_names(windows)
  def allowed_non_can(name: str) -> bool:
    if name.startswith("can_"):
      return False
    if name.startswith("role_") or name == "time_norm":
      return False
    if name.endswith("_count"):
      return False
    return True

  non_can = [name for name in names if allowed_non_can(name)]
  can_ranked: list[tuple[float, int, str]] = []
  for name in names:
    if not name.startswith("can_"):
      continue
    vals = [feature_value(win, name) for win in windows]
    non_zero = sum(1 for v in vals if abs(v) > 1e-9)
    if non_zero < 3:
      continue
    var = statistics.pvariance(vals) if len(vals) > 1 else 0.0
    if var <= 1e-9:
      continue
    # Prefer byte means over ranges/counts for the model, while still leaving
    # enough range/count features to catch discrete state changes.
    priority = 2 if name.endswith("_mean") else 1
    can_ranked.append((var, priority, name))
  can_ranked.sort(key=lambda item: (item[1], item[0]), reverse=True)
  return sorted(non_can) + sorted(name for _, _, name in can_ranked[:max_can_features])


def feature_value(win: Window, name: str) -> float:
  return safe_float(win.features.get(name), 0.0)


def target_counts(windows: list[Window], routes: set[str] | None = None) -> Counter[str]:
  counts: Counter[str] = Counter()
  for win in windows:
    if routes is not None and win.route_id not in routes:
      continue
    for label in win.labels:
      counts[label] += 1
  return counts


def train_one_model(
  target: str,
  windows: list[Window],
  names: list[str],
  min_pos: int,
) -> Model | None:
  positives = [w for w in windows if target in w.labels]
  if len(positives) < min_pos:
    return None
  if target in NEGATIVE_LABELS or target in {"group_longitudinal_bad", "group_lateral_bad"}:
    negatives = [
      w for w in windows
      if target not in w.labels
      and w.route_id in TRAINING_ROUTES
      and w.route_id not in HUMAN_VALIDATION_ROUTES
      and not w.labels.intersection(NEGATIVE_LABELS - {target})
    ]
    if len(negatives) < max(20, len(positives)):
      negatives.extend([
        w for w in windows
        if target not in w.labels
        and w.route_id in HUMAN_VALIDATION_ROUTES
        and not w.labels.intersection(NEGATIVE_LABELS)
      ])
  else:
    negatives = [w for w in windows if target not in w.labels]
  if len(negatives) < max(10, len(positives)):
    return None

  stats: list[dict[str, float]] = []
  for name in names:
    pos_vals = [feature_value(w, name) for w in positives]
    neg_vals = [feature_value(w, name) for w in negatives]
    pos_mean = sum(pos_vals) / len(pos_vals)
    neg_mean = sum(neg_vals) / len(neg_vals)
    pos_var = statistics.pvariance(pos_vals) if len(pos_vals) > 1 else 0.0
    neg_var = statistics.pvariance(neg_vals) if len(neg_vals) > 1 else 0.0
    pooled = math.sqrt(max(1e-9, (pos_var + neg_var) / 2.0))
    effect = (pos_mean - neg_mean) / pooled
    if not math.isfinite(effect) or abs(effect) < 0.15:
      continue
    stats.append({
      "feature": name,
      "pos_mean": pos_mean,
      "neg_mean": neg_mean,
      "pooled_std": pooled,
      "effect": effect,
      "abs_effect": abs(effect),
    })
  stats.sort(key=lambda x: (x["abs_effect"], abs(x["pos_mean"] - x["neg_mean"])), reverse=True)
  top_stats = stats[:45]
  if not top_stats:
    return None
  temp_model = Model(target, len(positives), len(negatives), top_stats, 0.5)
  pos_scores = sorted(score_window(temp_model, w) for w in positives)
  threshold_idx = max(0, int(len(pos_scores) * 0.20) - 1)
  threshold = max(0.52, min(0.90, pos_scores[threshold_idx] if pos_scores else 0.6))
  temp_model.threshold = threshold
  return temp_model


_MODEL_WORKER_CONTEXT: dict[str, Any] = {}


def _init_model_worker(train_windows: list[Window], names: list[str], min_pos: int) -> None:
  _MODEL_WORKER_CONTEXT.clear()
  _MODEL_WORKER_CONTEXT.update({
    "train_windows": train_windows,
    "names": names,
    "min_pos": min_pos,
  })


def _train_model_worker(target: str) -> tuple[str, Model | None]:
  return target, train_one_model(
    target,
    _MODEL_WORKER_CONTEXT["train_windows"],
    _MODEL_WORKER_CONTEXT["names"],
    _MODEL_WORKER_CONTEXT["min_pos"],
  )


def train_models(windows: list[Window], names: list[str], min_pos: int = 3, workers: int = 1) -> dict[str, Model]:
  train_routes = set(TRAINING_ROUTES)
  train_windows = [w for w in windows if w.route_id in train_routes]
  counts = target_counts(train_windows)
  targets = sorted(label for label, count in counts.items() if count >= min_pos and label != "narration_other")
  models: dict[str, Model] = {}
  worker_count = max(1, min(int(workers or 1), len(targets)))
  if worker_count > 1:
    with ProcessPoolExecutor(
      max_workers=worker_count,
      initializer=_init_model_worker,
      initargs=(train_windows, names, min_pos),
    ) as executor:
      for target, model in executor.map(_train_model_worker, targets, chunksize=1):
        if model is not None:
          models[target] = model
  else:
    for target in targets:
      model = train_one_model(target, train_windows, names, min_pos)
      if model is not None:
        models[target] = model
  return models


def sigmoid(x: float) -> float:
  if x >= 50:
    return 1.0
  if x <= -50:
    return 0.0
  return 1.0 / (1.0 + math.exp(-x))


def score_window(model: Model, win: Window) -> float:
  weighted = 0.0
  total = 0.0
  for stat in model.feature_stats:
    value = feature_value(win, str(stat["feature"]))
    midpoint = (stat["pos_mean"] + stat["neg_mean"]) / 2.0
    pooled = max(1e-6, stat["pooled_std"])
    direction = 1.0 if stat["effect"] >= 0 else -1.0
    weight = min(3.0, abs(stat["effect"]))
    z = direction * (value - midpoint) / pooled
    weighted += max(-3.0, min(3.0, z)) * weight
    total += weight
  if total <= 0.0:
    return 0.0
  return sigmoid(weighted / math.sqrt(total))


def auc_score(labels: list[int], scores: list[float]) -> float | None:
  pos_count = sum(1 for y in labels if y)
  neg_count = len(labels) - pos_count
  if not pos_count or not neg_count:
    return None
  pairs = sorted(zip(scores, labels), key=lambda item: item[0])
  rank_sum = 0.0
  rank = 1
  i = 0
  while i < len(pairs):
    j = i + 1
    while j < len(pairs) and pairs[j][0] == pairs[i][0]:
      j += 1
    avg_rank = (rank + rank + (j - i) - 1) / 2.0
    rank_sum += sum(1 for _, y in pairs[i:j] if y) * avg_rank
    rank += j - i
    i = j
  return (rank_sum - pos_count * (pos_count + 1) / 2.0) / (pos_count * neg_count)


def default_cv_workers() -> int:
  raw = os.environ.get("BRICKPILOT_CV_WORKERS")
  if raw:
    try:
      return max(1, int(raw))
    except ValueError:
      pass
  return max(1, min(4, (os.cpu_count() or 2) - 1))


def _cross_validate_target_row(
  train: list[Window],
  eval_windows: list[Window],
  counts: Counter[str],
  names: list[str],
  min_pos: int,
  min_eval_pos: int,
  holdout: str,
  target: str,
) -> dict[str, Any] | None:
  count = counts.get(target, 0)
  if count < min_pos or target == "narration_other":
    return None
  positives_eval = sum(1 for w in eval_windows if target in w.labels)
  if positives_eval < min_eval_pos:
    return None
  model = train_one_model(target, train, names, min_pos)
  if model is None:
    return None
  scores = [score_window(model, w) for w in eval_windows]
  label_values = [1 if target in w.labels else 0 for w in eval_windows]
  auc = auc_score(label_values, scores)
  pos_scores = [s for y, s in zip(label_values, scores) if y]
  neg_scores = [s for y, s in zip(label_values, scores) if not y]
  ranked = sorted(zip(scores, label_values), reverse=True)
  top_k = max(1, min(len(ranked), positives_eval * 2))
  recall_top = sum(y for _, y in ranked[:top_k]) / positives_eval
  return {
    "holdout_route": holdout,
    "target": target,
    "train_pos_windows": count,
    "eval_pos_windows": positives_eval,
    "eval_windows": len(eval_windows),
    "auc": auc if auc is not None else "",
    "mean_pos_score": sum(pos_scores) / len(pos_scores) if pos_scores else "",
    "mean_neg_score": sum(neg_scores) / len(neg_scores) if neg_scores else "",
    "recall_at_2x_pos": recall_top,
    "threshold": model.threshold,
  }


def _cross_validate_holdout_rows(
  train_windows_all: list[Window],
  windows_by_route: dict[str, list[Window]],
  names: list[str],
  min_pos: int,
  min_eval_pos: int,
  holdout: str,
  targets: list[str],
) -> list[dict[str, Any]]:
  eval_windows = windows_by_route.get(holdout, [])
  if not eval_windows:
    return []
  train = [w for w in train_windows_all if w.route_id != holdout]
  counts = target_counts(train)
  rows: list[dict[str, Any]] = []
  for target in targets:
    row = _cross_validate_target_row(train, eval_windows, counts, names, min_pos, min_eval_pos, holdout, target)
    if row is not None:
      rows.append(row)
  return rows


_CV_WORKER_CONTEXT: dict[str, Any] = {}


def _init_cv_worker(
  train_windows_all: list[Window],
  windows_by_route: dict[str, list[Window]],
  names: list[str],
  min_pos: int,
  min_eval_pos: int,
  targets: list[str],
) -> None:
  _CV_WORKER_CONTEXT.clear()
  _CV_WORKER_CONTEXT.update({
    "train_windows_all": train_windows_all,
    "windows_by_route": windows_by_route,
    "names": names,
    "min_pos": min_pos,
    "min_eval_pos": min_eval_pos,
    "targets": targets,
  })


def _cross_validate_worker(holdout: str) -> list[dict[str, Any]]:
  return _cross_validate_holdout_rows(
    _CV_WORKER_CONTEXT["train_windows_all"],
    _CV_WORKER_CONTEXT["windows_by_route"],
    _CV_WORKER_CONTEXT["names"],
    _CV_WORKER_CONTEXT["min_pos"],
    _CV_WORKER_CONTEXT["min_eval_pos"],
    holdout,
    _CV_WORKER_CONTEXT["targets"],
  )


def cv_target_sort_key(target: str, count: int) -> tuple[int, int, str]:
  if target in CV_PRIORITY_TARGETS:
    priority = CV_PRIORITY_TARGETS.index(target)
  elif target in NEGATIVE_LABELS or target in REVIEW_PRIORITY_LABELS:
    priority = len(CV_PRIORITY_TARGETS)
  elif target.startswith("group_"):
    priority = len(CV_PRIORITY_TARGETS) + 1
  else:
    priority = len(CV_PRIORITY_TARGETS) + 2
  return (priority, -count, target)


def select_cv_targets(
  windows: list[Window],
  min_pos: int,
  requested_targets: list[str] | None = None,
  max_targets: int | None = None,
) -> list[str]:
  train_windows = [w for w in windows if w.route_id in TRAINING_ROUTES]
  counts = target_counts(train_windows)
  if requested_targets:
    seen: set[str] = set()
    targets = []
    for target in requested_targets:
      if target and target not in seen:
        targets.append(target)
        seen.add(target)
  else:
    targets = [
      target
      for target, count in counts.items()
      if count >= min_pos and target != "narration_other"
    ]
    targets.sort(key=lambda target: cv_target_sort_key(target, counts[target]))
  if max_targets is not None and max_targets > 0:
    targets = targets[:max_targets]
  return targets


def cross_validate(
  windows: list[Window],
  names: list[str],
  min_pos: int,
  *,
  holdout_routes: list[str] | None = None,
  targets: list[str] | None = None,
  max_targets: int | None = None,
  min_eval_pos: int = 1,
  output_path: Path | None = None,
  progress: bool = False,
  workers: int = 1,
) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  train_route_set = set(TRAINING_ROUTES)
  route_order = list(holdout_routes or TRAINING_ROUTES)
  route_order = [route_id for route_id in route_order if route_id in train_route_set]
  if not route_order:
    return out
  windows_by_route: dict[str, list[Window]] = defaultdict(list)
  train_windows_all = [w for w in windows if w.route_id in train_route_set]
  for win in train_windows_all:
    windows_by_route[win.route_id].append(win)
  cv_targets = select_cv_targets(train_windows_all, min_pos, targets, max_targets)
  tasks: list[str] = []
  for holdout in route_order:
    eval_windows = windows_by_route.get(holdout, [])
    if not eval_windows:
      continue
    if progress:
      print(f"[cv] holdout={holdout} targets={len(cv_targets)}", file=sys.stderr, flush=True)
    tasks.append(holdout)
  if not tasks:
    return out
  worker_count = max(1, min(int(workers or 1), len(tasks)))
  if progress and worker_count > 1:
    print(f"[cv] workers={worker_count} tasks={len(tasks)}", file=sys.stderr, flush=True)
  csv_file = None
  writer: csv.DictWriter | None = None
  if output_path is not None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_file = output_path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=CV_FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    csv_file.flush()
  try:
    if worker_count > 1:
      with ProcessPoolExecutor(
        max_workers=worker_count,
        initializer=_init_cv_worker,
        initargs=(train_windows_all, dict(windows_by_route), names, min_pos, min_eval_pos, cv_targets),
      ) as executor:
        row_iter = executor.map(_cross_validate_worker, tasks, chunksize=1)
        for rows in row_iter:
          for row in rows:
            out.append(row)
            if writer is not None and csv_file is not None:
              writer.writerow({k: json_safe(v) for k, v in row.items()})
              csv_file.flush()
    else:
      for holdout in tasks:
        rows = _cross_validate_holdout_rows(train_windows_all, windows_by_route, names, min_pos, min_eval_pos, holdout, cv_targets)
        for row in rows:
          out.append(row)
          if writer is not None and csv_file is not None:
            writer.writerow({k: json_safe(v) for k, v in row.items()})
            csv_file.flush()
  finally:
    if csv_file is not None:
      csv_file.close()
  return out


def top_feature_reason(model: Model, win: Window, limit: int = 5) -> str:
  rows = []
  for stat in model.feature_stats:
    value = feature_value(win, str(stat["feature"]))
    direction = "high" if stat["effect"] > 0 else "low"
    contrast = value - stat["neg_mean"]
    rows.append((abs(stat["effect"]) * abs(contrast), f"{stat['feature']}={value:.3g} ({direction})"))
  rows.sort(reverse=True)
  return "; ".join(text for _, text in rows[:limit])


def merge_predictions(
  target: str,
  scored: list[tuple[Window, float]],
  model: Model,
  max_gap_sec: float,
) -> list[dict[str, Any]]:
  selected = sorted(
    ((w, s) for w, s in scored if s >= model.threshold or s >= 0.70),
    key=lambda item: (item[0].route_id, item[0].start_sec, item[1]),
  )
  merged: list[dict[str, Any]] = []
  for win, score in selected:
    if not merged or win.start_sec > merged[-1]["end_sec"] + max_gap_sec:
      merged.append({
        "target": target,
        "route_id": win.route_id,
        "start_sec": win.start_sec,
        "end_sec": win.end_sec,
        "peak_score": score,
        "mean_score_sum": score,
        "window_count": 1,
        "peak_center_sec": win.center_sec,
        "reason": top_feature_reason(model, win),
        "speed_mph_mean": feature_value(win, "speed_mph_mean"),
        "a_ego_mps2_min": feature_value(win, "a_ego_mps2_min"),
        "speed_deficit_mph_max": feature_value(win, "speed_deficit_mph_max"),
        "brake_pressed_frac": feature_value(win, "brake_pressed_frac"),
        "gas_pressed_frac": feature_value(win, "gas_pressed_frac"),
      })
    else:
      cur = merged[-1]
      cur["end_sec"] = max(cur["end_sec"], win.end_sec)
      cur["mean_score_sum"] += score
      cur["window_count"] += 1
      if score > cur["peak_score"]:
        cur["peak_score"] = score
        cur["peak_center_sec"] = win.center_sec
        cur["reason"] = top_feature_reason(model, win)
  for row in merged:
    row["mean_score"] = row.pop("mean_score_sum") / row["window_count"]
    row["duration_sec"] = row["end_sec"] - row["start_sec"]
  merged.sort(key=lambda r: (r["peak_score"], r["duration_sec"]), reverse=True)
  return merged


def predictions_for_route(
  windows: list[Window],
  models: dict[str, Model],
  route_id: str,
  step_sec: float,
  per_target_limit: int = 8,
) -> list[dict[str, Any]]:
  route_windows = [w for w in windows if w.route_id == route_id]
  rows: list[dict[str, Any]] = []
  for target, model in models.items():
    if target.startswith("group_") or target in {"narration_other", "quality_human_baseline"}:
      continue
    scored = [(w, score_window(model, w)) for w in route_windows]
    merged = merge_predictions(target, scored, model, max_gap_sec=step_sec * 1.5)
    rows.extend(merged[:per_target_limit])
  rows.sort(key=lambda r: (r["peak_score"], r["duration_sec"]), reverse=True)
  for idx, row in enumerate(rows, 1):
    row["rank"] = idx
  return rows


def review_candidate_predictions(rows: list[dict[str, Any]], limit: int = 90) -> list[dict[str, Any]]:
  filtered = []
  for row in rows:
    target = str(row["target"])
    score = float(row["peak_score"])
    if target not in REVIEW_PRIORITY_LABELS:
      continue
    if target in NEGATIVE_LABELS and score < 0.62:
      continue
    if target not in NEGATIVE_LABELS and score < 0.70:
      continue
    filtered.append(dict(row))
  filtered.sort(key=lambda r: (float(r["peak_score"]), float(r["duration_sec"])), reverse=True)
  filtered = filtered[:limit]
  for idx, row in enumerate(filtered, 1):
    row["rank"] = idx
  return filtered


def model_rows(models: dict[str, Model]) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  for target, model in sorted(models.items()):
    for rank, stat in enumerate(model.feature_stats[:20], 1):
      row = {"target": target, "feature_rank": rank, "pos_windows": model.pos_count, "neg_windows": model.neg_count, "threshold": model.threshold}
      row.update(stat)
      rows.append(row)
  return rows


def can_candidate_rows(models: dict[str, Model]) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  rx = re.compile(r"can_bus(?P<bus>\d+)_addr(?P<addr>[0-9a-f]+)_b(?P<byte>\d+)_(?P<stat>mean|range)$")
  for target, model in sorted(models.items()):
    if not (target.startswith("phev_") or target.startswith("group_phev") or target in NEGATIVE_LABELS or target.startswith("group_longitudinal")):
      continue
    for rank, stat in enumerate(model.feature_stats, 1):
      match = rx.match(str(stat["feature"]))
      if not match:
        continue
      rows.append({
        "target": target,
        "rank": rank,
        "bus": int(match.group("bus")),
        "address_hex": f"0x{match.group('addr')}",
        "byte_index": int(match.group("byte")),
        "stat": match.group("stat"),
        "pos_mean": stat["pos_mean"],
        "background_mean": stat["neg_mean"],
        "effect": stat["effect"],
        "abs_effect": stat["abs_effect"],
        "interpretation_status": "candidate_correlation_not_dbc_semantics",
      })
  rows.sort(key=lambda r: (r["target"], -float(r["abs_effect"])))
  return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  if fieldnames is None:
    fieldnames = []
    for row in rows:
      for key in row.keys():
        if key not in fieldnames:
          fieldnames.append(key)
    if not fieldnames:
      fieldnames = ["empty"]
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
      writer.writerow({k: json_safe(v) for k, v in row.items()})


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8") as f:
    for row in rows:
      f.write(json.dumps(json_safe(row), sort_keys=True) + "\n")


def corpus_rows(atoms: list[LabelAtom]) -> list[dict[str, Any]]:
  return [
    {
      "route_id": a.route_id,
      "bookmark_id": a.source_bookmark_id,
      "session_id": a.session_id,
      "start_sec": a.start_sec,
      "end_sec": a.end_sec,
      "raw_text": a.raw_text,
      "canonical_label": a.canonical_label,
      "family": a.family,
      "polarity": a.polarity,
      "confidence": a.confidence,
      "tags": "|".join(a.tags),
    }
    for a in atoms
  ]


def window_rows(windows: list[Window], names: list[str]) -> list[dict[str, Any]]:
  rows = []
  for win in windows:
    row = {
      "route_id": win.route_id,
      "role": win.role,
      "start_sec": win.start_sec,
      "end_sec": win.end_sec,
      "center_sec": win.center_sec,
      "labels": "|".join(sorted(win.labels)),
      "raw_label_refs": "|".join(str(x) for x in sorted(set(win.raw_label_refs))),
      "raw_text_preview": " || ".join(win.raw_texts[:3]),
    }
    for name in names:
      value = win.features.get(name)
      if value is not None:
        row[name] = value
    rows.append(row)
  return rows


def route_summary_rows(infos: dict[str, RouteInfo], atoms: list[LabelAtom], windows: list[Window]) -> list[dict[str, Any]]:
  atom_counts = Counter(a.route_id for a in atoms)
  raw_bookmarks = Counter((a.route_id, a.source_bookmark_id) for a in atoms)
  out = []
  for route_id in ALL_ROUTES:
    info = infos[route_id]
    route_windows = [w for w in windows if w.route_id == route_id]
    labels = Counter()
    for atom in atoms:
      if atom.route_id == route_id:
        labels[atom.canonical_label] += 1
    out.append({
      "route_id": route_id,
      "role": info.role,
      "drive_type": info.drive_type,
      "brickpilot_version": info.brickpilot_version,
      "model_bundle": info.model_bundle,
      "duration_sec": info.duration_sec,
      "segment_count": info.segment_count,
      "started_at": info.started_at,
      "ended_at": info.ended_at,
      "atomic_label_count": atom_counts[route_id],
      "voice_bookmark_count": len([k for k in raw_bookmarks if k[0] == route_id]),
      "window_count": len(route_windows),
      "top_labels": ";".join(f"{k}:{v}" for k, v in labels.most_common(12)),
    })
  return out


def taxonomy(atoms: list[LabelAtom]) -> dict[str, Any]:
  counts = Counter(a.canonical_label for a in atoms)
  families = defaultdict(Counter)
  for atom in atoms:
    families[atom.family][atom.canonical_label] += 1
  return {
    "description": "Rule-normalized sidecar taxonomy for Brickpilot voice narration plus reviewed DB labels. Raw text is preserved per atom.",
    "label_count": len(counts),
    "labels": {
      label: {
        "count": count,
        "family": label_family(label),
        "polarity": label_polarity(label),
      }
      for label, count in sorted(counts.items())
    },
    "groups": {k: sorted(v) for k, v in GROUPS.items()},
    "families": {family: dict(counter.most_common()) for family, counter in sorted(families.items())},
  }


def report_text(
  out_dir: Path,
  infos: dict[str, RouteInfo],
  atoms: list[LabelAtom],
  windows: list[Window],
  models: dict[str, Model],
  eval_rows: list[dict[str, Any]],
  test_predictions: list[dict[str, Any]],
  all_test_predictions: list[dict[str, Any]],
  can_rows: list[dict[str, Any]],
  backup_dir: Path,
  backup_check: dict[str, Any],
) -> str:
  label_counts = Counter(a.canonical_label for a in atoms)
  family_counts = Counter(a.family for a in atoms)
  route_atom_counts = Counter(a.route_id for a in atoms)
  usable_eval = [r for r in eval_rows if isinstance(r.get("auc"), float)]
  mean_auc = sum(float(r["auc"]) for r in usable_eval) / len(usable_eval) if usable_eval else None
  high_preds = [r for r in test_predictions if r["peak_score"] >= 0.70]
  phev_can = [r for r in can_rows if str(r["target"]).startswith("phev_") or str(r["target"]).startswith("group_phev")]
  lines = [
    "# Brickpilot 0.4.0-beta Voice Labeler ML Pass",
    "",
    "## Scope",
    "",
    "The scoped Brickpilot 0.3.25.0/0.3.30.0 label-validation routes train the weak labeler, and reviewed DB labels from 0.4.x/0.5.x test routes are now folded into training. The held-out no-bookmark test route is still scored only.",
    "",
  ]
  for route_id in ALL_ROUTES:
    info = infos[route_id]
    lines.append(f"- `{route_id}`: {info.role}, {info.duration_sec:.1f}s, {info.segment_count} segments")
  lines.extend([
    "",
    "## Backup",
    "",
    f"- Raw voice sessions and route bookmark/label DB rows were backed up first at `{backup_dir}`.",
    f"- Backup precondition was verified before writing sidecar artifacts: {backup_check['route_count']} routes, {backup_check['bookmark_rows']} bookmark rows, {backup_check['label_rows']} label rows, {backup_check['session_count']} voice sessions.",
    "- Reviewed 0.4.x/0.5.x DB labels were backed up during normalization/calibration and are read as training atoms.",
    "- This pass does not mutate source bookmarks, transcripts, labels, or raw route data. It writes sidecar ML artifacts only.",
    "",
    "## Label Normalization",
    "",
    f"- Active training atoms after normalization: {len(atoms)} from {len(set((a.route_id, a.source_bookmark_id) for a in atoms))} source bookmark/label rows.",
    f"- Canonical labels: {len(label_counts)}.",
    f"- Families: " + ", ".join(f"{k}={v}" for k, v in family_counts.most_common()),
    f"- Per-route atoms: " + ", ".join(f"{k}={route_atom_counts[k]}" for k in TRAINING_ROUTES),
    "",
    "Top canonical labels:",
  ])
  for label, count in label_counts.most_common(18):
    lines.append(f"- `{label}`: {count}")
  lines.extend([
    "",
    "## Weak Labeler",
    "",
    f"- Training windows: {sum(1 for w in windows if w.route_id in TRAINING_ROUTES)}; test windows: {sum(1 for w in windows if w.route_id == TEST_ROUTE)}.",
    f"- Trained targets: {len(models)}.",
    "- Model type: per-label weak supervised nearest-contrast scorer over route_samples plus sampled CAN window features. Unlabeled validation windows are not treated as clean truth; the human-driven validation route is used as the main negative prior for bad-behavior labels.",
  ])
  if mean_auc is not None:
    lines.append(f"- Leave-one-route validation mean AUC across evaluable target/route pairs: {mean_auc:.3f} over {len(usable_eval)} pairs.")
  elif not eval_rows:
    lines.append("- Leave-one-route validation was skipped for the expanded 0.4.0-beta corpus to keep turnaround reasonable; the held-out no-bookmark test route is still scored.")
  else:
    lines.append("- Leave-one-route validation did not have enough positive/negative pairs for a stable mean AUC.")
  lines.extend([
    "",
    "## Held-Out Test Route Predictions",
    "",
    f"- Curated review-candidate intervals on `{TEST_ROUTE}`: {len(test_predictions)} from {len(all_test_predictions)} model outputs; {len(high_preds)} at score >= 0.70.",
    "- These are review candidates, not ground-truth labels. Use them to seed manual/RL review of the test route.",
    "",
    "| rank | target | start | end | peak | reason |",
    "|---:|---|---:|---:|---:|---|",
  ])
  for row in test_predictions[:18]:
    reason = str(row.get("reason", "")).replace("|", "/")[:140]
    lines.append(
      f"| {row['rank']} | `{row['target']}` | {float(row['start_sec']):.1f} | {float(row['end_sec']):.1f} | {float(row['peak_score']):.3f} | {reason} |"
    )
  lines.extend([
    "",
    "## PHEV/CAN Findings",
    "",
    f"- Label-conditioned CAN candidate rows: {len(can_rows)}; PHEV-specific rows: {len(phev_can)}.",
    "- CAN rows are correlations between normalized label windows and sampled frame bytes. They identify where to inspect DBC/firmware behavior next; they do not assert decoded semantics yet.",
    "",
    "Top PHEV CAN candidates:",
    "",
    "| target | bus | address | byte | stat | effect |",
    "|---|---:|---|---:|---|---:|",
  ])
  for row in sorted(phev_can, key=lambda r: abs(float(r["effect"])), reverse=True)[:20]:
    lines.append(
      f"| `{row['target']}` | {row['bus']} | `{row['address_hex']}` | {row['byte_index']} | {row['stat']} | {float(row['effect']):.3f} |"
    )
  lines.extend([
    "",
    "## Outputs",
    "",
    f"- `{out_dir / 'route_summary.csv'}`",
    f"- `{out_dir / 'voice_label_corpus.csv'}`",
    f"- `{out_dir / 'voice_label_corpus.jsonl'}`",
    f"- `{out_dir / 'voice_label_taxonomy.json'}`",
    f"- `{out_dir / 'training_windows.csv'}`",
    f"- `{out_dir / 'model_feature_summary.csv'}`",
    f"- `{out_dir / 'model_eval_leave_one_route.csv'}`",
    f"- `{out_dir / 'test_route_predictions.csv'}`",
    f"- `{out_dir / 'test_route_predictions_all.csv'}`",
    f"- `{out_dir / 'can_signal_candidates.csv'}`",
    f"- `{out_dir / 'report.md'}`",
    f"- `{out_dir / 'run_summary.json'}`",
    "",
    "## Limits",
    "",
    "- The corpus is intentionally high-signal and biased toward narrated events. It should not be promoted as a full driving-quality ground truth set.",
    "- Voice timestamps are close enough for this pass, but sub-second/one-second offsets can still change exact CAN byte rankings around short events.",
    "- The test route has no voice labels by design, so every predicted interval needs manual or replay validation before becoming a training label.",
  ])
  return "\n".join(lines) + "\n"


def main() -> int:
  parser = argparse.ArgumentParser(description="Build 0.4.0-beta voice-label corpus, weak labeler, and PHEV/CAN candidates.")
  parser.add_argument("--config", default=str(Path.home() / ".config" / "brickpilot" / "drive_db.toml"))
  parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
  parser.add_argument("--stamp", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
  parser.add_argument("--window-sec", type=float, default=6.0)
  parser.add_argument("--step-sec", type=float, default=3.0)
  parser.add_argument("--min-pos", type=int, default=3)
  parser.add_argument("--max-can-model-features", type=int, default=700)
  parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
  parser.add_argument("--train-workers", type=int, default=None, help="Parallel target-training workers. Defaults to BRICKPILOT_CV_WORKERS or a conservative local core count; use 1 for serial parity debugging.")
  parser.add_argument("--skip-cross-validation", action="store_true")
  parser.add_argument("--fast-cross-validation", action="store_true", help="Run a prioritized leave-one-route-out CV subset instead of every eligible target.")
  parser.add_argument("--cv-target", action="append", default=[], help="Specific target label to leave-one-route-out validate. May be repeated.")
  parser.add_argument("--cv-route", action="append", default=[], help="Specific holdout route to validate. May be repeated.")
  parser.add_argument("--cv-max-targets", type=int, default=None, help="Limit leave-one-route-out CV to the top N priority targets.")
  parser.add_argument("--cv-min-eval-pos", type=int, default=1, help="Minimum positive eval windows required for a target on a holdout route.")
  parser.add_argument("--cv-workers", type=int, default=None, help="Parallel leave-one-route-out CV workers. Defaults to BRICKPILOT_CV_WORKERS or a conservative local core count; use 1 for serial parity debugging.")
  parser.add_argument("--cv-progress", action="store_true", help="Print leave-one-route-out CV progress to stderr.")
  parser.add_argument("--timings", action="store_true", help="Print and record stage timings for benchmark runs.")
  parser.add_argument("--skip-can", action="store_true")
  args = parser.parse_args()

  out_dir = Path(args.output_root) / f"ml_040_beta_voice_labeler_{args.stamp}"
  stage_timings: list[dict[str, Any]] = []
  stage_start = time.perf_counter()

  def mark_stage(name: str) -> None:
    nonlocal stage_start
    now = time.perf_counter()
    elapsed = now - stage_start
    stage_timings.append({"stage": name, "elapsed_sec": elapsed})
    if args.timings or args.cv_progress:
      print(f"[timing] {name} {elapsed:.2f}s", file=sys.stderr, flush=True)
    stage_start = now

  store = DriveStore(load_config(args.config))
  infos = fetch_route_infos(store)
  mark_stage("fetch_route_infos")
  backup_check = verify_voice_backup(Path(args.backup_dir), store, infos)
  mark_stage("verify_voice_backup")
  out_dir.mkdir(parents=True, exist_ok=True)
  atoms = fetch_voice_atoms(store, infos)
  mark_stage("fetch_voice_atoms")
  windows = build_windows(store, infos, atoms, args.window_sec, args.step_sec, skip_can=args.skip_can)
  mark_stage("build_windows")
  model_features = select_model_feature_names([w for w in windows if w.route_id in TRAINING_ROUTES], args.max_can_model_features)
  mark_stage("select_model_features")
  train_workers = args.train_workers if args.train_workers is not None else default_cv_workers()
  models = train_models(windows, model_features, min_pos=args.min_pos, workers=train_workers)
  mark_stage("train_models")
  cv_max_targets = args.cv_max_targets
  if args.fast_cross_validation and cv_max_targets is None and not args.cv_target:
    cv_max_targets = 32
  cv_targets = args.cv_target or None
  cv_routes = args.cv_route or None
  cv_workers = args.cv_workers if args.cv_workers is not None else default_cv_workers()
  eval_rows = [] if args.skip_cross_validation else cross_validate(
    windows,
    model_features,
    min_pos=args.min_pos,
    holdout_routes=cv_routes,
    targets=cv_targets,
    max_targets=cv_max_targets,
    min_eval_pos=args.cv_min_eval_pos,
    output_path=out_dir / "model_eval_leave_one_route.csv",
    progress=args.cv_progress,
    workers=cv_workers,
  )
  mark_stage("cross_validate")
  all_test_predictions = predictions_for_route(windows, models, TEST_ROUTE, args.step_sec)
  test_predictions = review_candidate_predictions(all_test_predictions)
  can_rows = can_candidate_rows(models)
  mark_stage("score_predictions")

  write_csv(out_dir / "route_summary.csv", route_summary_rows(infos, atoms, windows))
  write_csv(out_dir / "voice_label_corpus.csv", corpus_rows(atoms))
  write_jsonl(out_dir / "voice_label_corpus.jsonl", corpus_rows(atoms))
  (out_dir / "voice_label_taxonomy.json").write_text(json.dumps(taxonomy(atoms), indent=2, sort_keys=True), encoding="utf-8")
  write_csv(out_dir / "training_windows.csv", window_rows(windows, model_features))
  write_csv(out_dir / "model_feature_summary.csv", model_rows(models))
  write_csv(out_dir / "model_eval_leave_one_route.csv", eval_rows)
  write_csv(out_dir / "test_route_predictions.csv", test_predictions)
  write_csv(out_dir / "test_route_predictions_all.csv", all_test_predictions)
  write_csv(out_dir / "can_signal_candidates.csv", can_rows)
  mark_stage("write_artifacts")

  run_summary = {
    "title": "Brickpilot 0.4.0-beta voice labeler ML pass",
    "analysis_name": "ml_040_beta_voice_labeler",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "route_id": TEST_ROUTE,
    "routes": {rid: json_safe(vars(info)) for rid, info in infos.items()},
    "validation_routes": list(VALIDATION_ROUTES),
    "reviewed_training_routes_04x": list(REVIEWED_TRAINING_ROUTES_04X),
    "reviewed_training_routes_050": list(REVIEWED_TRAINING_ROUTES_050),
    "reviewed_training_routes_051": list(REVIEWED_TRAINING_ROUTES_051),
    "reviewed_training_routes_053": list(REVIEWED_TRAINING_ROUTES_053),
    "reviewed_training_routes_054": list(REVIEWED_TRAINING_ROUTES_054),
    "reviewed_training_routes_055": list(REVIEWED_TRAINING_ROUTES_055),
    "reviewed_training_routes_056": list(REVIEWED_TRAINING_ROUTES_056),
    "reviewed_training_routes": list(REVIEWED_TRAINING_ROUTES),
    "training_routes": list(TRAINING_ROUTES),
    "test_route": TEST_ROUTE,
    "human_validation_route": HUMAN_VALIDATION_ROUTE,
    "human_validation_routes": list(HUMAN_VALIDATION_ROUTES),
    "backup_dir": str(Path(args.backup_dir)),
    "backup_verified": backup_check,
    "output_dir": str(out_dir),
    "window_sec": args.window_sec,
    "step_sec": args.step_sec,
    "model_feature_count": len(model_features),
    "max_can_model_features": args.max_can_model_features,
    "train_workers": train_workers,
    "cross_validation_skipped": bool(args.skip_cross_validation),
    "cross_validation_fast": bool(args.fast_cross_validation),
    "cross_validation_rows": len(eval_rows),
    "cross_validation_targets_requested": list(args.cv_target),
    "cross_validation_routes_requested": list(args.cv_route),
    "cross_validation_max_targets": cv_max_targets,
    "cross_validation_min_eval_pos": args.cv_min_eval_pos,
    "cross_validation_workers": 0 if args.skip_cross_validation else cv_workers,
    "cross_validation_incremental_output": str(out_dir / "model_eval_leave_one_route.csv"),
    "voice_atomic_labels": len(atoms),
    "canonical_label_count": len(set(a.canonical_label for a in atoms)),
    "trained_target_count": len(models),
    "test_prediction_count": len(test_predictions),
    "test_prediction_all_count": len(all_test_predictions),
    "test_high_confidence_count": sum(1 for r in test_predictions if float(r["peak_score"]) >= 0.70),
    "can_candidate_count": len(can_rows),
    "stage_timings": stage_timings,
    "artifacts": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
  }
  (out_dir / "run_summary.json").write_text(json.dumps(json_safe(run_summary), indent=2, sort_keys=True), encoding="utf-8")
  (out_dir / "report.md").write_text(
    report_text(out_dir, infos, atoms, windows, models, eval_rows, test_predictions, all_test_predictions, can_rows, Path(args.backup_dir), backup_check),
    encoding="utf-8",
  )
  run_summary["artifacts"] = sorted(p.name for p in out_dir.iterdir() if p.is_file())
  (out_dir / "run_summary.json").write_text(json.dumps(json_safe(run_summary), indent=2, sort_keys=True), encoding="utf-8")
  print(json.dumps(json_safe(run_summary), indent=2, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
