from __future__ import annotations

import json
from pathlib import Path

from scripts.drive_tests import manual_drive_labeler as ml


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_label_validation_route_ids_reads_all_generated_inboxes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ml, "DEFAULT_OUT_BASE", tmp_path)
    write_json(
        tmp_path / "manual_drive_labeler_a" / "drive_jobs.json",
        {"jobs": [{"route_id": "r1", "ride_type": "label validation"}, {"route_id": "r2", "ride_metadata": "normal drive"}]},
    )
    write_json(
        tmp_path / "manual_drive_labeler_b" / "drive_jobs.json",
        {"jobs": [{"route_id": "r3", "ride_metadata": "label validation"}]},
    )

    assert ml.label_validation_route_ids() == {"r1", "r3"}
    assert [job[1]["route_id"] for job in ml.prior_label_validation_jobs()] == ["r1", "r3"]


def test_generated_ui_contains_voice_import_flow(tmp_path: Path) -> None:
    ml.write_ui(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    server = (tmp_path / "manual_label_server.py").read_text(encoding="utf-8")

    assert "Voice bookmarks" in html
    assert "loadVoiceSessions" in html
    assert "importVoiceBookmarks" in html
    assert "id=voiceOffset" in html
    assert "offset_sec:off" in html
    assert "refreshCurrentDrive" in html
    assert "data=await fetchJson(job.data_path" in html
    assert "/api/voice-sessions" in server
    assert "/api/import-voice" in server


def test_generated_ui_contains_specific_phev_label_taxonomy(tmp_path: Path) -> None:
    ml.write_ui(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    for label in (
        "ev_launch_lag",
        "engine_transition",
        "hev_transition",
        "regen_blend",
        "brake_blend",
        "no_lead_lazy",
        "lead_resume_lazy",
        "too_eager_surge",
        "good_phev_transition",
    ):
        assert f"<option>{label}</option>" in html


def test_generated_ui_keeps_timeline_clicks_in_route_time(tmp_path: Path) -> None:
    ml.write_ui(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert "routeCursor" in html
    assert "Math.abs(vt-lastVideoTime)>tol" in html
    assert "function timelineRatio" in html
    assert "return vw.start+timelineRatio(ev,c)*vw.span" in html
    assert "return null" in html


def test_generated_ui_steps_frames_through_route_seek(tmp_path: Path) -> None:
    ml.write_ui(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert "function frameStep(dir){const fps=Number(data&&data.video_fps)||20; v.pause(); const base=Number.isFinite(routeCursor)?routeCursor:currentRouteTime(); seek(base+dir/Math.max(1,fps))}" in html
    assert "lastVideoTime=target" in html
    assert "frameStep(dir){const fps=(data&&data.video_fps)||20; v.pause(); v.currentTime" not in html


def test_generated_inbox_uses_drive_wall_time_not_review_ingest_time(tmp_path: Path) -> None:
    ml.write_ui(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    server = (tmp_path / "manual_label_server.py").read_text(encoding="utf-8")

    assert "drive time unavailable" in html
    assert "route_start_wall_time':j.get('route_start_wall_time')" in server
    assert "j.get('started_at')" not in server
    assert "j.get('ended_at')" not in server
