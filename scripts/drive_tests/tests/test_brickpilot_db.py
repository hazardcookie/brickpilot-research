from pathlib import Path
import datetime as dt
import json
import os
import tempfile
from scripts.drive_tests.brickpilot_db.config import DriveDbConfig
from scripts.drive_tests.brickpilot_db.store import DriveStore
from scripts.drive_tests.brickpilot_db.ingest import _manifest_from_init_data, inventory, import_root, parse_metadata_file
from scripts.drive_tests.brickpilot_db import queries

def test_store_roundtrip_artifact_route_label(tmp_path: Path):
    cfg = DriveDbConfig(database_url=f"sqlite:///{tmp_path/'drive.sqlite'}", artifact_root=tmp_path/'artifacts')
    st = DriveStore(cfg); st.migrate(); host = st.upsert_host('testhost','tester','macbook')
    src = tmp_path/'0000015a--abc'/'0'; src.mkdir(parents=True); f = src/'qlog.bz2'; f.write_bytes(b'hello')
    route = st.upsert_route('0000015a--abc', source_device='comma-test', segment_count=1)
    seg = st.upsert_segment(route, 0, {'qlog': True})
    art = st.import_artifact(f, host_id=host); st.link_route_artifact(route, art.id, 'qlog', seg); st.commit()
    assert (cfg.artifact_root / art.relative_path).read_bytes() == b'hello'
    assert st.conn.execute('select count(*) from route_artifacts').fetchone()[0] == 1
    inbox = st.create_inbox('unit'); job = st.upsert_review_job(inbox, route, 'legacy-1')
    st.insert_label_event(job, 'create', {'label':'smooth'}); st.commit()
    assert st.conn.execute('select count(*) from label_events').fetchone()[0] == 1

def test_inventory_and_import_idempotent(tmp_path: Path):
    root = tmp_path/'raw'/'from_comma'/'0000015a--abcdef01'/'0'; root.mkdir(parents=True)
    (root/'qlog.bz2').write_bytes(b'qlog'); (root/'rlog.bz2').write_bytes(b'rlog')
    inv = inventory(tmp_path/'raw')
    assert inv['file_count'] == 2
    cfg_path = tmp_path/'cfg.toml'; cfg_path.write_text(f'database_url = "sqlite:///{tmp_path}/db.sqlite"\nartifact_root = "{tmp_path}/artifacts"\n')
    one = import_root(tmp_path/'raw', str(cfg_path), dry_run=False)
    two = import_root(tmp_path/'raw', str(cfg_path), dry_run=False)
    assert one['imported_files'] == two['imported_files'] == 2
    assert len(list((tmp_path/'artifacts').rglob('*'))) > 0

def test_camera_kind_classification_keeps_camera_roles_distinct():
    assert DriveStore.kind_for_path(Path("fcamera.hevc")) == "fcamera"
    assert DriveStore.kind_for_path(Path("ecamera.hevc")) == "ecamera"
    assert DriveStore.kind_for_path(Path("dcamera.hevc")) == "dcamera"
    assert DriveStore.kind_for_path(Path("qcamera.ts")) == "qcamera"
    assert DriveStore.kind_for_path(Path("qcamera.hevc")) == "qcamera"
    assert DriveStore.kind_for_path(Path("camera.hevc")) == "camera"

def test_bookmark_tag_reason_records_import_as_bookmarks(tmp_path: Path):
    cfg = DriveDbConfig(database_url=f"sqlite:///{tmp_path/'drive.sqlite'}", artifact_root=tmp_path/'artifacts')
    st = DriveStore(cfg); st.migrate()
    route = st.upsert_route('0000016f--427a57d416', source_device='comma-test', segment_count=1)
    src = tmp_path / 'bookmark_tags_route.jsonl'
    src.write_text(json.dumps({"reason": "phev_context", "route_time_sec": 12.5, "tags": ["ev_launch_lag"]}) + "\n")
    art = st.import_artifact(src)

    counts = parse_metadata_file(st, src, route, art.id)
    st.commit()

    row = st.one("SELECT t_sec, text, tags FROM bookmarks WHERE route_uuid=?", (route,))
    assert counts["bookmarks"] == 1
    assert row["t_sec"] == 12.5
    assert row["text"] == "phev_context"
    assert json.loads(row["tags"]) == ["ev_launch_lag"]

def test_settings_manifest_promotes_route_software_metadata(tmp_path: Path):
    cfg = DriveDbConfig(database_url=f"sqlite:///{tmp_path/'drive.sqlite'}", artifact_root=tmp_path/'artifacts')
    root = tmp_path / "raw" / "from_comma" / "00000189--6fb7c85de1"
    root.mkdir(parents=True)
    (root / "settings_manifest.json").write_text(json.dumps({
        "source": "web_ingest_remote_settings",
        "route_id": "00000189--6fb7c85de1",
        "ride_type": "label validation",
        "captured_at": "2026-05-16T17:50:00Z",
        "software": {
            "branch": "candidate/brickpilot-0.4.0",
            "commit": "abcdef123456",
            "brand": "Brickpilot",
            "brickpilot_version": "0.4.0-dev",
            "display_version": "Brickpilot 0.4.0-dev",
            "version": "0.4.0-dev",
            "openpilot_version": "2026.05.06-4477",
        },
        "params": {
            "GitBranch": "candidate/brickpilot-0.4.0",
            "GitCommit": "abcdef123456",
            "Version": "2026.05.06-4477",
            "ExperimentalMode": "1",
            "ModelManager_ActiveBundle": json.dumps({"displayName": "WMI V12", "internalName": "WMIV12"}),
        },
        "params_raw": {
            "ExperimentalMode": {"size_bytes": 1, "sha256": "x", "base64": "MQ==", "text": "1"},
        },
    }) + "\n", encoding="utf-8")
    cfg_path = tmp_path/'cfg.toml'
    cfg_path.write_text(f'database_url = "{cfg.database_url}"\nartifact_root = "{cfg.artifact_root}"\n')

    result = import_root(tmp_path / "raw", str(cfg_path), dry_run=False)

    st = DriveStore(cfg)
    try:
        route = st.one("SELECT route_id, route_label, branch, brickpilot_version, model_bundle, metadata_jsonb FROM routes")
    finally:
        st.close()
    metadata = json.loads(route["metadata_jsonb"])
    assert result["metadata_counts"]["settings_manifests"] == 1
    assert route["branch"] == "candidate/brickpilot-0.4.0"
    assert route["brickpilot_version"] == "0.4.0-dev"
    assert route["model_bundle"] == "WMI V12"
    assert "WMI V12" in route["route_label"]
    assert "Brickpilot 0.4.0-dev" in route["route_label"]
    assert metadata["settings"]["ExperimentalMode"] == "1"
    assert metadata["software"]["openpilot_version"] == "2026.05.06-4477"
    assert metadata["settings_manifest"]["raw_param_count"] == 1

def test_log_init_data_manifest_preserves_ride_settings():
    class Entry:
        def __init__(self, key: str, value: bytes):
            self.key = key
            self.value = value

    class Params:
        entries = [
            Entry("GitBranch", b"staging"),
            Entry("GitCommit", b"abc123"),
            Entry("ExperimentalMode", b"1"),
            Entry("ModelManager_ActiveBundle", b'{"displayName":"WMI V12"}'),
            Entry("ApiToken", b"secret"),
        ]

    class InitData:
        params = Params()
        version = "2026.05.06-4477"
        gitBranch = "staging"
        gitCommit = "abc123"
        gitRemote = "git@example.com:brickpilot.git"
        dirty = False

    manifest = _manifest_from_init_data(InitData())

    assert manifest["source"] == "log_init_data"
    assert manifest["params"]["ExperimentalMode"] == "1"
    assert manifest["params"]["ApiToken"] == "[redacted]"
    assert manifest["software"]["branch"] == "staging"
    assert manifest["software"]["openpilot_version"] == "2026.05.06-4477"

def test_import_root_infers_route_start_from_segment_completion_mtimes(tmp_path: Path):
    cfg = DriveDbConfig(database_url=f"sqlite:///{tmp_path/'drive.sqlite'}", artifact_root=tmp_path/'artifacts')
    route_id = "00000189--6fb7c85de1"
    route_root = tmp_path / "raw" / "from_comma" / route_id
    start_ts = dt.datetime(2026, 5, 16, 16, 39, 42, tzinfo=dt.timezone.utc).timestamp()
    for segment, close_offset in ((0, 60), (4, 300)):
        seg_dir = route_root / f"{route_id}--{segment}"
        seg_dir.mkdir(parents=True)
        f = seg_dir / "qcamera.ts"
        f.write_bytes(b"fake-video")
        close_ts = start_ts + close_offset
        os.utime(f, (close_ts, close_ts))
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(f'database_url = "{cfg.database_url}"\nartifact_root = "{cfg.artifact_root}"\n')

    result = import_root(tmp_path / "raw", str(cfg_path), dry_run=False)

    st = DriveStore(cfg)
    try:
        row = st.one("SELECT started_at FROM routes WHERE route_id=?", (route_id,))
    finally:
        st.close()
    assert result["metadata_counts"]["route_wall_time_segment_inferred"] == 1
    assert dt.datetime.fromisoformat(row["started_at"]).timestamp() == start_ts

def test_dynamic_label_validation_includes_raw_video_only_jobs(tmp_path: Path):
    cfg = DriveDbConfig(database_url=f"sqlite:///{tmp_path/'drive.sqlite'}", artifact_root=tmp_path/'artifacts')
    st = DriveStore(cfg); st.migrate(); host = st.upsert_host('testhost','tester','macbook')
    route = st.upsert_route('0000016f--427a57d416', source_device='comma-test', segment_count=1)
    seg = st.upsert_segment(route, 0, {'qlog': True, 'rlog': True, 'qcamera': True})
    files = {
        'qcamera.ts': b'video',
        'qlog.bz2': b'qlog',
        'rlog.bz2': b'rlog',
    }
    artifacts = {}
    src = tmp_path / 'raw'
    src.mkdir()
    for name, body in files.items():
        fp = src / name
        fp.write_bytes(body)
        art = st.import_artifact(fp, host_id=host)
        artifacts[name] = art
        st.link_route_artifact(route, art.id, art.kind, seg)
    st.execute("INSERT INTO video_sync_segments(route_uuid,artifact_id,segment_index,route_start_sec,video_start_sec,duration_sec,confidence) VALUES(?,?,?,?,?,?,?)", (route, artifacts['qcamera.ts'].id, 0, 0, 0, 60, 1.0))
    st.execute("INSERT INTO route_samples(route_uuid,t_sec,speed_mph) VALUES(?,?,?)", (route, 0.0, 15.0))
    inbox = st.create_inbox('logdrive_label_validation')
    st.commit()

    jobs = queries.list_jobs(st, inbox)
    assert [j['route_id'] for j in jobs] == ['0000016f--427a57d416']
    assert jobs[0]['media_status'] == 'raw video only'
    assert jobs[0]['raw_video_artifact_count'] == 1
    assert jobs[0]['playable_video_artifact_count'] == 0
