from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logdrive_automation as la  # noqa: E402


class LogdriveAutomationTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory()
    self.root = Path(self.tmp.name)
    self.realdata = self.root / "realdata"
    self.realdata.mkdir()
    self.runs = self.root / "runs"
    self.raw = self.root / "raw"

  def tearDown(self) -> None:
    self.tmp.cleanup()

  def write_segment(self, route: str, segment: int, *, logs: bool = True, video: bool = False) -> Path:
    path = self.realdata / f"{route}--{segment}"
    path.mkdir()
    if logs:
      (path / "rlog.bz2").write_bytes(f"rlog-{route}-{segment}".encode())
      (path / "qlog.bz2").write_bytes(f"qlog-{route}-{segment}".encode())
    if video:
      (path / "fcamera.hevc").write_bytes(f"video-{route}-{segment}".encode())
    return path

  def test_discover_groups_and_numbers_candidates(self) -> None:
    main_route = "aaaaaaaaaaaaaaaa|2026-05-13--14-00-00"
    tail_route = "aaaaaaaaaaaaaaaa|2026-05-13--14-30-00"
    self.write_segment(main_route, 0, video=True)
    self.write_segment(main_route, 1, video=True)
    self.write_segment(tail_route, 0)

    candidates = la.build_candidates(la.discover_segments(self.realdata), max_candidates=8)

    self.assertEqual([candidate.number for candidate in candidates], [1, 2])
    self.assertEqual({candidate.route_id for candidate in candidates}, {main_route, tail_route})
    by_route = {candidate.route_id: candidate for candidate in candidates}
    self.assertEqual(by_route[main_route].segment_count, 2)
    self.assertEqual(by_route[tail_route].segment_count, 1)
    rendered = la.format_candidate_list(candidates)
    self.assertIn("1.", rendered)
    self.assertIn("segment", rendered)

  def test_drive_type_policy_controls_copy_plan_video(self) -> None:
    route = "bbbbbbbbbbbbbbbb|2026-05-13--15-00-00"
    self.write_segment(route, 0, video=True)
    candidate = la.build_candidates(la.discover_segments(self.realdata))[0]

    test_plan = la.make_copy_plan(candidate, run_id="run", drive_type=la.DriveType.TEST, raw_root=self.raw)
    validation_plan = la.make_copy_plan(candidate, run_id="run", drive_type=la.DriveType.LABEL_VALIDATION, raw_root=self.raw)

    self.assertTrue(test_plan.items)
    self.assertFalse(any(item.kind == "video" for item in test_plan.items))
    self.assertTrue(any(item.kind == "video" for item in validation_plan.items))
    self.assertEqual(la.DriveType.TEST.required_kinds, ("logs",))
    self.assertEqual(la.DriveType.LABEL_VALIDATION.required_kinds, ("logs", "video"))

  def test_atomic_state_status_and_verify_plan(self) -> None:
    route = "cccccccccccccccc|2026-05-13--16-00-00"
    self.write_segment(route, 0, video=True)
    candidate = la.build_candidates(la.discover_segments(self.realdata))[0]
    plan = la.make_copy_plan(candidate, run_id="run123", drive_type=la.DriveType.LABEL_VALIDATION, raw_root=self.raw)

    # Simulate a completed local copy without invoking rsync/SSH.
    for item in plan.items:
      dest = Path(item.destination)
      dest.parent.mkdir(parents=True, exist_ok=True)
      dest.write_bytes(Path(item.source).read_bytes())

    result = la.verify_copy_plan(plan, checksum=True)
    self.assertTrue(result["ok"])
    self.assertFalse(result["missing_required_kinds"])
    self.assertTrue(all(entry["actual_size_bytes"] > 0 for entry in result["files"]))
    self.assertTrue(all("sha256" in entry for entry in result["files"]))

    state = la.update_run_state("run123", "verified", self.runs, verify=result)
    self.assertEqual(state["status"], "verified")
    status = json.loads((self.runs / "run123" / "status.json").read_text())
    self.assertEqual(status["run_id"], "run123")

  def test_verify_fails_when_required_video_missing_for_label_validation(self) -> None:
    route = "dddddddddddddddd|2026-05-13--17-00-00"
    self.write_segment(route, 0, logs=True, video=False)
    candidate = la.build_candidates(la.discover_segments(self.realdata))[0]
    plan = la.make_copy_plan(candidate, run_id="run124", drive_type=la.DriveType.LABEL_VALIDATION, raw_root=self.raw)

    for item in plan.items:
      dest = Path(item.destination)
      dest.parent.mkdir(parents=True, exist_ok=True)
      dest.write_bytes(Path(item.source).read_bytes())

    result = la.verify_copy_plan(plan)
    self.assertFalse(result["ok"])
    self.assertEqual(result["missing_required_kinds"], ["video"])

  def test_snapshot_dry_run_is_read_only_command_skeleton(self) -> None:
    snapshot = la.snapshot_device_state(dry_run=True, host="comma@example")
    self.assertTrue(snapshot["dry_run"])
    self.assertEqual(snapshot["host"], "comma@example")
    commands = snapshot["commands"]
    self.assertIn("openpilot_commit", commands)
    self.assertIn("recent_routes", commands)
    self.assertNotIn("rsync", "\n".join(commands.values()).lower())

  def test_cli_copy_plan_writes_status_and_plan_json(self) -> None:
    route = "eeeeeeeeeeeeeeee|2026-05-13--18-00-00"
    self.write_segment(route, 0, video=True)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
      rc = la.main([
        "copy-plan",
        "--run-id", "cli-run",
        "--runs-root", str(self.runs),
        "--realdata-root", str(self.realdata),
        "--candidates", "1",
        "--drive-type", "test",
        "--raw-root", str(self.raw),
      ])
    self.assertEqual(rc, 0)
    self.assertTrue((self.runs / "cli-run" / "copy_plan.json").exists())
    status = json.loads((self.runs / "cli-run" / "status.json").read_text())
    self.assertEqual(status["status"], "copy_planned")


if __name__ == "__main__":
  unittest.main()
