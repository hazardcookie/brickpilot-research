from __future__ import annotations

import json
from pathlib import Path

from scripts.drive_tests import voice_bookmark_app as vb
from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def make_review_db(tmp_path: Path) -> tuple[Path, str, int]:
    cfg_path = tmp_path / "drive_db.toml"
    cfg_path.write_text(
        f'database_url = "sqlite:///{tmp_path / "drive.sqlite"}"\nartifact_root = "{tmp_path / "artifacts"}"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    store = DriveStore(cfg)
    store.migrate()
    try:
        route_uuid = store.upsert_route("route-a", source_device="test")
        inbox_id = store.create_inbox("test_inbox")
        review_job_id = store.upsert_review_job(inbox_id, route_uuid)
        store.commit()
        return cfg_path, route_uuid, review_job_id
    finally:
        store.close()


def test_voice_label_normalizer_keeps_regen_domain_terms() -> None:
    assert vb.normalize_voice_label_text("Light region.") == "Light regen."
    assert vb.normalize_voice_label_text("Coasting light region.") == "Coasting light regen."
    assert vb.normalize_voice_label_text("parking break engaged") == "Parking brake engaged"
    assert vb.normalize_voice_label_text("re gen braking") == "Regen braking"
    assert vb.normalize_voice_label_text("Reverse Engage") == "Reverse engaged"
    assert vb.normalize_voice_label_text("Drive Engage") == "Drive engaged"


def test_transcript_quality_filter_rejects_repetition_and_bad_compression() -> None:
    repeated = "Foot on gas. " * 20
    assert vb.transcript_rejection_reason(repeated, []) == "repetitive_hallucination"
    assert vb.transcript_rejection_reason("Foot on gas.", [{"compression_ratio": 25.0}]) == "compression_failed"


def test_openai_text_reconcile_keeps_timing_anchor_on_contradiction() -> None:
    assert vb.reconcile_openai_label_text("AC on.", "light regen") == "AC on."
    assert vb.reconcile_openai_label_text("Light region.", "Hard regen. Coasting light regen.") == "Light regen."
    assert vb.reconcile_openai_label_text("Brake hard region.", "Brake hard regen.") == "Brake hard regen."


def test_rewrite_final_transcript_rows_preserves_manual_rows(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    append_jsonl(transcript, {"kind": "manual", "text": "manual"})
    append_jsonl(transcript, {"kind": "final", "source": "offline_transcribe", "text": "old"})
    removed = vb._rewrite_final_transcript_rows(transcript, [{"kind": "final", "source": "final_transcribe", "text": "new"}])

    rows = vb.read_jsonl(transcript)
    assert removed == 1
    assert [row["text"] for row in rows] == ["manual", "new"]
    assert list(tmp_path.glob("transcript.jsonl.*.bak"))


def test_import_voice_session_replaces_previous_projection(tmp_path: Path) -> None:
    cfg_path, route_uuid, review_job_id = make_review_db(tmp_path)

    session_dir = tmp_path / "sessions" / "s1"
    write_json(session_dir / "session.json", {"session_id": "s1", "started_at_wall": "2026-05-16T16:00:00+00:00", "ended_at_wall": "2026-05-16T16:01:00+00:00"})
    append_jsonl(session_dir / "transcript.jsonl", {"kind": "final", "text": "Light region.", "t_session_start_sec": 5.0, "t_session_end_sec": 5.0})

    first = vb.import_voice_session(str(session_dir), route=None, review_job_id=review_job_id, config=str(cfg_path))
    second = vb.import_voice_session(str(session_dir), route=None, review_job_id=review_job_id, config=str(cfg_path))

    store = DriveStore(load_config(cfg_path))
    try:
        active = store.execute("SELECT * FROM bookmarks WHERE route_uuid=? AND source='voice_narration' AND deleted_at IS NULL", (route_uuid,)).fetchall()
        events = store.execute("SELECT * FROM events WHERE route_uuid=? AND source='voice_narration'", (route_uuid,)).fetchall()
    finally:
        store.close()

    assert first["bookmarks"] == 1
    assert second["bookmarks"] == 1
    assert len(active) == 1
    assert active[0]["text"] == "Light regen."
    assert len(events) == 0


def test_resolve_route_accepts_non_uuid_route_id(tmp_path: Path) -> None:
    cfg_path, route_uuid, _review_job_id = make_review_db(tmp_path)
    store = DriveStore(load_config(cfg_path))
    try:
        row = vb.resolve_route(store, "route-a")
    finally:
        store.close()

    assert row["id"] == route_uuid


def test_local_alignment_uses_word_level_bounds(tmp_path: Path, monkeypatch) -> None:
    chunk = tmp_path / "chunk.webm"
    chunk.write_bytes(b"fake-webm")

    monkeypatch.setattr(vb, "_select_backend", lambda _preferred="auto": ("mlx-whisper", {}, {}))
    monkeypatch.setattr(vb, "_candidate_models", lambda: ["fake-model"])

    def fake_transcribe(*_args, **kwargs):
        assert kwargs["word_timestamps"] is True
        assert kwargs["condition_on_previous_text"] is False
        return {
            "segments": [
                {
                    "text": " Reverse engaged.",
                    "start": 30.0,
                    "end": 36.0,
                    "words": [
                        {"word": "Reverse", "start": 31.38, "end": 32.78},
                        {"word": "engaged", "start": 32.78, "end": 33.38},
                    ],
                }
            ]
        }

    monkeypatch.setattr(vb, "_transcribe_with_backend", fake_transcribe)

    specs, _info = vb._local_alignment_specs([chunk], tmp_path, "en", 5)

    assert specs[0]["t_session_start_sec"] == 31.38
    assert specs[0]["t_session_end_sec"] == 33.38


def test_import_voice_session_keeps_session_timing_when_wall_clock_cuts_beginning(tmp_path: Path) -> None:
    cfg_path, route_uuid, review_job_id = make_review_db(tmp_path)
    store = DriveStore(load_config(cfg_path))
    try:
        store.execute(
            "UPDATE routes SET started_at=?, duration_sec=? WHERE id=?",
            ("2026-05-16T16:40:44+00:00", 301.0, route_uuid),
        )
        store.commit()
    finally:
        store.close()

    session_dir = tmp_path / "sessions" / "s1"
    write_json(
        session_dir / "session.json",
        {
            "session_id": "s1",
            "started_at_wall": "2026-05-16T16:39:50+00:00",
            "ended_at_wall": "2026-05-16T16:43:40+00:00",
        },
    )
    append_jsonl(
        session_dir / "transcript.jsonl",
        {
            "kind": "final",
            "text": "AC on.",
            "start_wall": "2026-05-16T16:39:50+00:00",
            "end_wall": "2026-05-16T16:39:51+00:00",
            "t_session_start_sec": 0.0,
            "t_session_end_sec": 1.0,
        },
    )
    append_jsonl(
        session_dir / "transcript.jsonl",
        {
            "kind": "final",
            "text": "Parking break engaged.",
            "start_wall": "2026-05-16T16:43:35+00:00",
            "end_wall": "2026-05-16T16:43:36+00:00",
            "t_session_start_sec": 225.0,
            "t_session_end_sec": 226.0,
        },
    )

    result = vb.import_voice_session(str(session_dir), route=None, review_job_id=review_job_id, config=str(cfg_path))

    store = DriveStore(load_config(cfg_path))
    try:
        active = store.execute(
            "SELECT t_sec,text FROM bookmarks WHERE route_uuid=? AND source='voice_narration' AND deleted_at IS NULL ORDER BY t_sec",
            (route_uuid,),
        ).fetchall()
    finally:
        store.close()

    assert result["alignment_method"] == "session_relative_negative_wall_guard"
    assert result["hidden_start_rows"] == 1
    assert [(round(row["t_sec"], 1), row["text"]) for row in active] == [(0.0, "AC on."), (225.0, "Parking brake engaged.")]
