from scripts.drive_tests.train_voice_labeler_0325 import (
  HUMAN_VALIDATION_ROUTE,
  HUMAN_VALIDATION_ROUTES,
  REVIEWED_TRAINING_ROUTES_04X,
  REVIEWED_TRAINING_ROUTES_050,
  REVIEWED_TRAINING_ROUTES_051,
  REVIEWED_TRAINING_ROUTES_053,
  REVIEWED_TRAINING_ROUTES_054,
  REVIEWED_TRAINING_ROUTES_055,
  REVIEWED_TRAINING_ROUTES_056,
  REVIEWED_TRAINING_ROUTES_057,
  REVIEWED_TRAINING_ROUTES,
  TEST_ROUTE,
  TRAINING_ROUTES,
  VALIDATION_ROUTES,
  VALIDATION_ROUTES_0330,
  Window,
  auc_score,
  cross_validate,
  label_family,
  label_polarity,
  normalize_labels,
  train_models,
)


def test_normalize_voice_labels_preserves_phev_and_accessory_context() -> None:
  labels = normalize_labels(
    "Eco mode automatic mode, EV light on, hard regen into the charge section. "
    "AC on, phone on wireless charger, parking sensor on."
  )

  assert {
    "eco_mode",
    "automatic_mode",
    "ev_light_on",
    "phev_regen_hard",
    "power_meter_charge",
    "hvac_ac_on",
    "phone_wireless_charger_on",
    "parking_sensor_on",
  } <= labels
  assert label_family("phev_regen_hard") == "phev"


def test_negative_drive_behavior_keeps_specific_training_targets() -> None:
  labels = normalize_labels(
    "Bad braking, missed stop sign, too lazy getting to target speed, ping pong steering."
  )

  assert {
    "braking_bad",
    "missed_stop",
    "traffic_control_context",
    "accel_too_lazy",
    "accel_target_gap",
    "steering_ping_pong",
    "quality_bad",
  } <= labels
  assert "quality_good" not in labels
  assert label_family("braking_bad") == "longitudinal"
  assert label_polarity("braking_bad") == "negative"


def test_050_braking_stop_review_labels_are_first_class_targets() -> None:
  assert label_family("lead_present_context") == "longitudinal"
  assert label_family("lead_brake_bad") == "longitudinal"
  assert label_family("lead_brake_good") == "longitudinal"
  assert label_family("resume_bad") == "longitudinal"
  assert label_family("regen_light_coast") == "phev"

  assert label_polarity("lead_brake_bad") == "negative"
  assert label_polarity("braking_absent") == "negative"
  assert label_polarity("stop_hold_fail") == "negative"
  assert label_polarity("driver_brake_intervention") == "negative"
  assert label_polarity("lead_brake_good") == "positive"


def test_051_braking_voice_phrases_map_to_stop_stack_labels() -> None:
  late_lead = normalize_labels("Lead present late braking driver break needed")
  assert {
    "lead_present_context",
    "lead_brake_bad",
    "braking_bad",
    "braking_late",
    "driver_brake",
    "driver_brake_intervention",
    "human_intervention_brake",
  } <= late_lead

  good_stop = normalize_labels("Lead vehicle good full stop.")
  assert {
    "lead_present_context",
    "lead_brake_good",
    "brake_good",
    "stop_complete",
    "quality_good",
  } <= good_stop

  stop_go = normalize_labels("Too aggressive on stop and go driver brake needed.")
  assert {
    "stop_go",
    "stop_go_bad",
    "driver_brake_intervention",
    "braking_bad",
  } <= stop_go


def test_055_rolling_traffic_stop_phrases_map_to_actionable_labels() -> None:
  rolling = normalize_labels("This needs to be more of a rolling kind of stop, not a full stop.")
  assert {
    "braking_early",
    "unnecessary_braking",
    "overbraked_rolling_traffic",
    "quality_bad",
  } <= rolling

  overbrake = normalize_labels("Over braking. Needs to be more of a roll.")
  assert {
    "braking_early",
    "unnecessary_braking",
    "overbraked_rolling_traffic",
    "quality_bad",
  } <= overbrake

  catchup = normalize_labels("Go. It's tough. Slow catch up.")
  assert {
    "accel_too_lazy",
    "accel_target_gap",
    "resume_lazy",
  } <= catchup

  absent = normalize_labels("Lead ahead. No brake.")
  assert {
    "lead_present_context",
    "braking_absent",
    "braking_bad",
  } <= absent


def test_056_native_pacing_and_follow_distance_phrases_are_first_class() -> None:
  good = normalize_labels("Good pacing. Good rolling stop. Good follow distance.")
  assert {
    "pacing_good",
    "rolling_follow_good",
    "follow_distance_good",
    "brake_good",
    "quality_good",
  } <= good
  assert label_family("pacing_good") == "longitudinal"
  assert label_polarity("pacing_good") == "positive"

  bad = normalize_labels("Follow distance too far. Overcommitting braking too early. Bad acceleration.")
  assert {
    "follow_distance_too_far",
    "follow_distance_bad",
    "braking_early",
    "unnecessary_braking",
    "overbraked_rolling_traffic",
    "accel_too_lazy",
    "quality_bad",
  } <= bad
  assert label_family("follow_distance_too_far") == "longitudinal"
  assert label_polarity("follow_distance_too_far") == "negative"
  assert label_polarity("overbraked_rolling_traffic") == "negative"

  profile = normalize_labels("Standard follow distance. Aggressive follow distance. Distance four. I don't like this distance.")
  assert {
    "follow_profile_standard",
    "follow_profile_aggressive",
    "follow_distance_setting_4",
    "follow_distance_bad",
    "follow_distance_too_far",
    "quality_bad",
  } <= profile
  assert label_family("follow_distance_setting_4") == "longitudinal"

  assert {"brake_light", "driver_brake_intervention", "braking_bad"} <= normalize_labels("Brake present. Driver braking needed.")
  assert {"brake_good", "quality_good"} <= normalize_labels("Good for stop.")
  assert {"phev_regen_coast", "regen_light_coast"} <= normalize_labels("Coasting.")
  assert {"gear_neutral", "gear_drive"} <= normalize_labels("Off neutral drive.")
  assert {"steering_good", "quality_good"} <= normalize_labels("Good lane centering.")


def test_normalize_0330_stationary_and_intervention_labels() -> None:
  stationary = normalize_labels("Auto hold active. Foot off brake. Auto hold release. Drive creep no gas.")
  assert {
    "auto_hold_active",
    "auto_hold_release",
    "drive_creep_no_gas",
    "driver_no_brake",
    "driver_no_gas",
  } <= stationary
  assert label_family("auto_hold_active") == "stationary"

  intervention = normalize_labels("Brake too late. Accel too lazy, human intervention gas. Low speed lateral bad.")
  assert {
    "braking_bad",
    "braking_late",
    "accel_too_lazy",
    "human_intervention_gas",
    "low_speed_lateral_bad",
  } <= intervention
  assert label_polarity("human_intervention_gas") == "negative"


def test_normalize_04x_reviewed_test_drive_phrases() -> None:
  blocky = normalize_labels("Blocky steering Good acceleration")
  assert {"steering_jerk", "accel_good", "quality_good"} <= blocky
  assert "steering_good" not in blocky

  smooth_exit = normalize_labels("Smooth steering Blocky steering")
  assert {"steering_good", "steering_jerk", "smooth_driving"} <= smooth_exit

  return_texture = normalize_labels("Stair stepping steering return, manual steering override, steering too damp.")
  assert {
    "steering_jerk",
    "driver_steering",
    "human_intervention_steering",
    "steering_too_damped",
  } <= return_texture
  assert label_family("human_intervention_steering") == "lateral"
  assert label_polarity("human_intervention_steering") == "negative"
  assert label_polarity("steering_too_damped") == "negative"

  longitudinal = normalize_labels("Late acceleration, bad follow distance, been on the gas.")
  assert {
    "accel_too_lazy",
    "accel_target_gap",
    "follow_distance_bad",
    "quality_bad",
    "driver_gas",
    "human_intervention_gas",
  } <= longitudinal
  assert label_family("follow_distance_bad") == "longitudinal"
  assert label_polarity("follow_distance_bad") == "negative"


def test_normalize_054_stop_stack_field_phrases() -> None:
  assert {"driver_brake", "brake_light"} <= normalize_labels("Brake engaged.")
  assert {"brake_good", "stop_complete", "quality_good"} <= normalize_labels("Good stop.")
  assert {"accel_good", "quality_good"} <= normalize_labels("Good resume.")
  assert {"phev_regen_coast", "regen_light_coast", "quality_good"} <= normalize_labels("Good coasting.")
  assert {"phev_regen_light"} <= normalize_labels("Regen braking.")
  assert {"gear_park"} <= normalize_labels("Parking engaged.")
  assert {"comma_engaged", "set_speed"} <= normalize_labels("Cruise engaged.")
  assert {"comma_engaged"} <= normalize_labels("Comma engage.")
  assert {
    "lead_present_context",
    "lead_brake_bad",
    "braking_bad",
    "braking_late",
    "driver_brake_intervention",
  } <= normalize_labels("Late present late braking driver brake needed.")
  assert {"missed_stop", "quality_bad"} <= normalize_labels("Bad miss stop.")
  mixed = normalize_labels("Bad pacing. Good braking. Good resume. Bad pacing.")
  assert {"pacing_bad", "brake_good", "accel_good", "quality_bad"} <= mixed
  assert "braking_bad" not in mixed


def test_route_scope_is_the_040_beta_validation_and_test_set() -> None:
  all_routes = {*VALIDATION_ROUTES, TEST_ROUTE}

  assert len(VALIDATION_ROUTES) == 11
  assert len(VALIDATION_ROUTES_0330) == 5
  assert len(all_routes) == 12
  assert len(REVIEWED_TRAINING_ROUTES_04X) == 8
  assert len(REVIEWED_TRAINING_ROUTES_050) == 1
  assert len(REVIEWED_TRAINING_ROUTES_051) == 1
  assert len(REVIEWED_TRAINING_ROUTES_053) == 1
  assert len(REVIEWED_TRAINING_ROUTES_054) == 3
  assert len(REVIEWED_TRAINING_ROUTES_055) == 1
  assert len(REVIEWED_TRAINING_ROUTES_056) == 2
  assert len(REVIEWED_TRAINING_ROUTES_057) == 1
  assert len(REVIEWED_TRAINING_ROUTES) == 18
  assert len(TRAINING_ROUTES) == 29
  assert TEST_ROUTE == "00000195--d936b2944f"
  assert TEST_ROUTE not in VALIDATION_ROUTES
  assert TEST_ROUTE not in TRAINING_ROUTES
  assert set(VALIDATION_ROUTES) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_04X) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_050) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_051) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_053) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_054) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_055) < set(TRAINING_ROUTES)
  assert set(REVIEWED_TRAINING_ROUTES_057) < set(TRAINING_ROUTES)
  assert "000001d3--8074b1f3f1" in REVIEWED_TRAINING_ROUTES_04X
  assert "000001d9--9609d9a67f" in REVIEWED_TRAINING_ROUTES_04X
  assert "000001e5--3c82eba9a4" in REVIEWED_TRAINING_ROUTES_050
  assert "000001eb--51605e23a9" in REVIEWED_TRAINING_ROUTES_051
  assert "000001f5--33fd956c5f" in REVIEWED_TRAINING_ROUTES_053
  assert "000001fa--ab5dda8ae2" in REVIEWED_TRAINING_ROUTES_054
  assert "000001fb--097115907b" in REVIEWED_TRAINING_ROUTES_054
  assert "000001fe--3bc01df03a" in REVIEWED_TRAINING_ROUTES_054
  assert "00000203--5d7d932abf" in REVIEWED_TRAINING_ROUTES_055
  assert "00000207--a8c307d230" in REVIEWED_TRAINING_ROUTES_056
  assert "00000209--f9ffcb0581" in REVIEWED_TRAINING_ROUTES_056
  assert "00000213--ab5b813126" in REVIEWED_TRAINING_ROUTES_057
  assert HUMAN_VALIDATION_ROUTE == "00000199--d5f5711730"
  assert HUMAN_VALIDATION_ROUTE in VALIDATION_ROUTES
  assert "000001a2--3ede8392f4" in HUMAN_VALIDATION_ROUTES


def test_cross_validate_can_stream_targeted_rows(tmp_path) -> None:
  route_a = "0000018e--5d3d27b763"
  route_b = "00000197--a5c011c5f6"

  def window(route_id: str, idx: int, labels: set[str], metric: float) -> Window:
    return Window(
      route_id=route_id,
      role="label_validation",
      start_sec=idx * 3.0,
      end_sec=idx * 3.0 + 6.0,
      center_sec=idx * 3.0 + 3.0,
      features={"metric": metric},
      labels=labels,
    )

  windows: list[Window] = []
  windows.extend(window(route_a, i, {"steering_jerk"}, 10.0) for i in range(2))
  windows.extend(window(route_a, i + 2, set(), 0.0) for i in range(6))
  windows.extend(window(route_b, i, {"steering_jerk"}, 9.0) for i in range(3))
  windows.extend(window(route_b, i + 3, set(), 0.0) for i in range(24))

  out = tmp_path / "cv.csv"
  rows = cross_validate(
    windows,
    ["metric"],
    min_pos=2,
    holdout_routes=[route_a],
    targets=["steering_jerk"],
    output_path=out,
  )

  assert len(rows) == 1
  assert rows[0]["holdout_route"] == route_a
  assert rows[0]["target"] == "steering_jerk"
  assert rows[0]["eval_pos_windows"] == 2
  assert rows[0]["auc"] == 1.0
  text = out.read_text()
  assert "holdout_route,target,train_pos_windows" in text
  assert f"{route_a},steering_jerk" in text


def test_auc_score_matches_pairwise_definition_with_ties() -> None:
  labels = [1, 0, 1, 0, 1, 0]
  scores = [0.7, 0.2, 0.5, 0.5, 0.2, 0.2]
  positives = [score for label, score in zip(labels, scores) if label]
  negatives = [score for label, score in zip(labels, scores) if not label]
  wins = 0.0
  total = 0.0
  for pos in positives:
    for neg in negatives:
      total += 1.0
      if pos > neg:
        wins += 1.0
      elif pos == neg:
        wins += 0.5

  assert auc_score(labels, scores) == wins / total


def test_parallel_cross_validate_matches_serial_rows(tmp_path) -> None:
  route_a = "0000018e--5d3d27b763"
  route_b = "00000197--a5c011c5f6"

  def window(route_id: str, idx: int, labels: set[str], metric: float) -> Window:
    return Window(
      route_id=route_id,
      role="label_validation",
      start_sec=idx * 3.0,
      end_sec=idx * 3.0 + 6.0,
      center_sec=idx * 3.0 + 3.0,
      features={"metric": metric},
      labels=labels,
    )

  windows: list[Window] = []
  windows.extend(window(route_a, i, {"steering_jerk"}, 10.0) for i in range(3))
  windows.extend(window(route_a, i + 3, set(), 0.0) for i in range(18))
  windows.extend(window(route_b, i, {"steering_jerk"}, 9.0) for i in range(3))
  windows.extend(window(route_b, i + 3, set(), 0.0) for i in range(18))

  kwargs = {
    "min_pos": 2,
    "holdout_routes": [route_a, route_b],
    "targets": ["steering_jerk"],
  }
  serial = cross_validate(windows, ["metric"], output_path=tmp_path / "serial.csv", workers=1, **kwargs)
  parallel = cross_validate(windows, ["metric"], output_path=tmp_path / "parallel.csv", workers=2, **kwargs)

  assert parallel == serial
  assert (tmp_path / "parallel.csv").read_text() == (tmp_path / "serial.csv").read_text()


def test_parallel_train_models_matches_serial_models() -> None:
  route_a = "0000018e--5d3d27b763"
  route_b = "00000197--a5c011c5f6"

  def window(route_id: str, idx: int, labels: set[str], metric: float) -> Window:
    return Window(
      route_id=route_id,
      role="label_validation",
      start_sec=idx * 3.0,
      end_sec=idx * 3.0 + 6.0,
      center_sec=idx * 3.0 + 3.0,
      features={"metric": metric},
      labels=labels,
    )

  windows: list[Window] = []
  windows.extend(window(route_a, i, {"steering_jerk"}, 10.0) for i in range(3))
  windows.extend(window(route_a, i + 3, set(), 0.0) for i in range(18))
  windows.extend(window(route_b, i, {"steering_jerk"}, 9.0) for i in range(3))
  windows.extend(window(route_b, i + 3, set(), 0.0) for i in range(18))

  serial = train_models(windows, ["metric"], min_pos=2, workers=1)
  parallel = train_models(windows, ["metric"], min_pos=2, workers=2)

  assert parallel == serial
