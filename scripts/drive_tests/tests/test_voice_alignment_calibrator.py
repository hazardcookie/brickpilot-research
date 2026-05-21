from __future__ import annotations

from scripts.drive_tests.voice_alignment_calibrator import (
  calibrate_route,
  classify_anchor_text,
  detect_edges,
  route_offset_is_applicable,
  route_offset_from_suggestions,
)


def test_anchor_text_requires_manual_or_driver_context() -> None:
  assert classify_anchor_text("manual braking") == "brake"
  assert classify_anchor_text("driver gas override") == "gas"
  assert classify_anchor_text("manual steering override") == "steer"
  assert classify_anchor_text("bad braking") is None
  assert classify_anchor_text("blocky steering return") is None


def test_calibrate_route_snaps_manual_brake_and_gas_to_nearest_edges() -> None:
  samples = [
    {"t_sec": 9.0, "brake_pressed": False, "gas_pressed": False},
    {"t_sec": 10.0, "brake_pressed": True, "gas_pressed": False},
    {"t_sec": 10.6, "brake_pressed": True, "gas_pressed": False},
    {"t_sec": 11.0, "brake_pressed": False, "gas_pressed": False},
    {"t_sec": 19.0, "brake_pressed": False, "gas_pressed": False},
    {"t_sec": 20.4, "brake_pressed": False, "gas_pressed": True},
    {"t_sec": 22.0, "brake_pressed": False, "gas_pressed": False},
  ]
  bookmarks = [
    {"id": 1, "t_sec": 10.8, "text": "manual brake needed"},
    {"id": 2, "t_sec": 19.6, "text": "driver gas"},
    {"id": 3, "t_sec": 30.0, "text": "bad braking"},
  ]

  edges, suggestions, route_offset = calibrate_route("route", bookmarks, samples)

  assert [edge.family for edge in detect_edges("route", samples) if edge.edge == "rising"] == ["brake", "gas"]
  assert len(edges) == 4
  assert len(suggestions) == 2
  assert suggestions[0].edge_t_sec == 10.0
  assert suggestions[0].offset_sec == -0.8
  assert suggestions[0].confidence >= 0.7
  assert suggestions[1].edge_t_sec == 20.4
  assert suggestions[1].offset_sec == 0.8
  assert route_offset is not None
  assert route_offset.anchor_count == 2


def test_route_offset_requires_multiple_high_confidence_anchors() -> None:
  _, suggestions, _ = calibrate_route(
    "route",
    [{"id": 1, "t_sec": 10.0, "text": "manual brake"}],
    [{"t_sec": 10.5, "brake_pressed": True, "gas_pressed": False}],
  )

  assert route_offset_from_suggestions("route", suggestions) is None


def test_noisy_route_offset_is_reported_but_not_applied_globally() -> None:
  _, suggestions, route_offset = calibrate_route(
    "route",
    [
      {"id": 1, "t_sec": 10.0, "text": "manual brake"},
      {"id": 2, "t_sec": 20.0, "text": "manual brake"},
      {"id": 3, "t_sec": 30.0, "text": "driver gas"},
    ],
    [
      {"t_sec": 10.2, "brake_pressed": True, "gas_pressed": False},
      {"t_sec": 15.0, "brake_pressed": False, "gas_pressed": False},
      {"t_sec": 22.4, "brake_pressed": True, "gas_pressed": False},
      {"t_sec": 25.0, "brake_pressed": False, "gas_pressed": False},
      {"t_sec": 27.4, "brake_pressed": False, "gas_pressed": True},
    ],
  )

  assert len(suggestions) == 3
  assert route_offset is not None
  assert route_offset.mad_sec > 0.75
  assert not route_offset_is_applicable(route_offset)
