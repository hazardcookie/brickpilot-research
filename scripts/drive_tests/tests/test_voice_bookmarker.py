from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.drive_tests import voice_bookmarker as vb


def write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_parse_time_text_supports_hhmmss_and_seconds() -> None:
    assert vb.parse_time_text("01:02 late brake") == (62.0, "late brake")
    assert vb.parse_time_text("1:02:03.5 smooth curve") == (3723.5, "smooth curve")
    assert vb.parse_time_text("12.25 close lead") == (12.25, "close lead")
    assert vb.parse_time_text("not timestamped") is None


def test_read_transcript_plain_text_normalizes_bookmarks(tmp_path: Path) -> None:
    src = tmp_path / "transcript.txt"
    src.write_text("00:01.5 brake was late\n2.0 nice accel\n", encoding="utf-8")
    rows = vb.read_transcript(src)
    assert [r["elapsed_sec"] for r in rows] == [1.5, 2.0]
    assert rows[0]["text"] == "brake was late"
    assert rows[0]["id"].startswith("voice:0001:")


def test_bookmark_to_label_matches_manual_labeler_draft_shape() -> None:
    label = vb.bookmark_to_label(
        {"elapsed_sec": 10.0, "text": "close lead and late brake", "id": "b1"},
        job={"job_id": "job-a", "route_id": "route-a", "route_label": "Morning"},
        route={"ride_type": "test drive"},
        window_sec=4.0,
    )
    assert label["job_id"] == "job-a"
    assert label["label_kind"] == "drive"
    assert label["start_time_sec"] == 8.0
    assert label["end_time_sec"] == 12.0
    assert label["label"] == "late_brake"
    assert "voice" in label["tags"]
    assert "lead" in label["tags"]
    assert label["notes"] == "close lead and late brake"


def test_import_bookmarks_appends_to_selected_manual_labeler_job(tmp_path: Path) -> None:
    labeler = tmp_path / "manual_drive_labeler_demo"
    job_dir = labeler / "jobs" / "job-a"
    job_dir.mkdir(parents=True)
    write_json(
        labeler / "drive_jobs.json",
        {
            "default_job_id": "job-a",
            "jobs": [
                {
                    "job_id": "job-a",
                    "job_dir": "jobs/job-a",
                    "data_path": "jobs/job-a/drive_data.json",
                    "route_id": "route-a",
                    "route_label": "Route A",
                    "selected": True,
                }
            ],
        },
    )
    write_json(job_dir / "drive_data.json", {"route": {"route_id": "route-a", "route_label": "Route A", "ride_type": "label validation"}})

    result = vb.import_bookmarks(
        bookmarks=[{"elapsed_sec": 5.0, "text": "good smooth turn", "id": "v1"}],
        labeler_dir=labeler,
        window_sec=2.0,
    )

    assert result["count"] == 1
    target = job_dir / "drive_labels.jsonl"
    rows = vb.jsonl_rows(target)
    assert len(rows) == 1
    assert rows[0]["route_id"] == "route-a"
    assert rows[0]["label"] == "good_behavior"
    assert rows[0]["voice_bookmark_id"] == "v1"


def test_record_detaches_recorder_stdin(tmp_path: Path) -> None:
    popen_calls: list[dict[str, object]] = []

    class FakeProc:
        def poll(self) -> int | None:
            return 0

    def fake_popen(*args: object, **kwargs: object) -> FakeProc:
        popen_calls.append(dict(kwargs))
        return FakeProc()

    with patch.object(vb, "recorder_command", return_value=["ffmpeg", "fake"]), patch.object(vb.subprocess, "Popen", side_effect=fake_popen), patch("builtins.input", side_effect=["/q"]):
        vb.command_record(type("Args", (), {"name": "stdin-test", "out_dir": str(tmp_path / "session"), "no_audio": False, "backend": "auto", "device": "", "notes": ""})())

    assert popen_calls
    assert popen_calls[0]["stdin"] is subprocess.DEVNULL


def test_import_dry_run_does_not_write(tmp_path: Path) -> None:
    labeler = tmp_path / "labeler"
    labeler.mkdir()
    write_json(labeler / "drive_data.json", {"route": {"route_id": "r1", "analysis_name": "job"}})
    result = vb.import_bookmarks(
        bookmarks=[{"elapsed_sec": 1.0, "text": "bookmark", "id": "v1"}],
        labeler_dir=labeler,
        dry_run=True,
    )
    assert result["count"] == 1
    assert not (labeler / "drive_labels.jsonl").exists()


def test_inferred_labels_match_manual_labeler_taxonomy() -> None:
    allowed = vb.MANUAL_LABELER_LABELS
    samples = [
        "good smooth turn",
        "late brake behind lead",
        "hard brake regen",
        "close lead cut in",
        "engine kicked on",
        "steering wobble in lane",
        "random bookmark",
    ]
    for text in samples:
        label, tags = vb.infer_label_and_tags(text)
        assert label in allowed
        assert "voice" in tags


def test_inferred_labels_cover_specific_phev_taxonomy() -> None:
    samples = {
        "ev launch lag from stop": "ev_launch_lag",
        "lead resume lazy after traffic moved": "lead_resume_lazy",
        "good phev smooth transition": "good_phev_transition",
        "regen blend felt weird": "regen_blend",
        "brake blend was late": "brake_blend",
    }
    for text, expected in samples.items():
        label, tags = vb.infer_label_and_tags(text)
        assert label == expected
        assert "voice" in tags


def test_rejects_drive_jobs_data_path_outside_labeler_dir(tmp_path: Path) -> None:
    labeler = tmp_path / "manual_drive_labeler_demo"
    job_dir = labeler / "jobs" / "job-a"
    job_dir.mkdir(parents=True)
    outside = tmp_path / "outside_drive_data.json"
    write_json(outside, {"route": {"route_id": "escaped"}})
    write_json(
        labeler / "drive_jobs.json",
        {
            "default_job_id": "job-a",
            "jobs": [
                {
                    "job_id": "job-a",
                    "job_dir": "jobs/job-a",
                    "data_path": "../outside_drive_data.json",
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="unsafe data_path"):
        vb.import_bookmarks(
            bookmarks=[{"elapsed_sec": 5.0, "text": "good", "id": "v1"}],
            labeler_dir=labeler,
        )
