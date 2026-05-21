from __future__ import annotations
import hashlib, json, mimetypes, os, socket, sqlite3, tempfile, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .config import DriveDbConfig

SQLITE_SCHEMA = r'''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS hosts(id INTEGER PRIMARY KEY AUTOINCREMENT, hostname TEXT NOT NULL, username TEXT, role TEXT, last_seen_at TEXT, UNIQUE(hostname, username));
CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY, route_id TEXT NOT NULL, canonical_name TEXT, route_label TEXT, dongle_id TEXT, device_serial TEXT, source_device_route_key TEXT UNIQUE, source_device TEXT, vehicle TEXT, branch TEXT, brickpilot_version TEXT, model_bundle TEXT, drive_type TEXT, started_at TEXT, ended_at TEXT, duration_sec REAL, segment_count INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, metadata_jsonb TEXT DEFAULT '{}', UNIQUE(source_device, route_id));
CREATE TABLE IF NOT EXISTS route_segments(id INTEGER PRIMARY KEY AUTOINCREMENT, route_uuid TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE, segment_index INTEGER NOT NULL, duration_sec REAL, has_qlog INTEGER DEFAULT 0, has_rlog INTEGER DEFAULT 0, has_qcamera INTEGER DEFAULT 0, has_fcamera INTEGER DEFAULT 0, has_ecamera INTEGER DEFAULT 0, metadata_jsonb TEXT DEFAULT '{}', UNIQUE(route_uuid, segment_index));
CREATE TABLE IF NOT EXISTS route_timebases(route_uuid TEXT REFERENCES routes(id) ON DELETE CASCADE, monotonic_start_sec REAL, wall_start_at TEXT, wall_end_at TEXT, source TEXT, timezone TEXT, confidence REAL, raw_jsonb TEXT DEFAULT '{}', PRIMARY KEY(route_uuid, source));
CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, mime_type TEXT, size_bytes INTEGER NOT NULL, artifact_path TEXT NOT NULL, original_path TEXT, source_host_id INTEGER REFERENCES hosts(id), copy_status TEXT DEFAULT 'verified', verified_at TEXT, sha256_verified INTEGER DEFAULT 0, imported_at TEXT DEFAULT CURRENT_TIMESTAMP, metadata_jsonb TEXT DEFAULT '{}');
UPDATE artifacts SET kind='fcamera' WHERE kind='qcamera' AND lower(coalesce(metadata_jsonb, '') || ' ' || coalesce(original_path, '') || ' ' || coalesce(artifact_path, '')) LIKE '%fcamera%';
UPDATE artifacts SET kind='ecamera' WHERE kind='qcamera' AND lower(coalesce(metadata_jsonb, '') || ' ' || coalesce(original_path, '') || ' ' || coalesce(artifact_path, '')) LIKE '%ecamera%';
UPDATE artifacts SET kind='dcamera' WHERE kind='qcamera' AND lower(coalesce(metadata_jsonb, '') || ' ' || coalesce(original_path, '') || ' ' || coalesce(artifact_path, '')) LIKE '%dcamera%';
CREATE TABLE IF NOT EXISTS source_paths(artifact_id INTEGER REFERENCES artifacts(id), host_id INTEGER REFERENCES hosts(id), path TEXT NOT NULL, mtime REAL, size_bytes INTEGER, first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(host_id, path));
CREATE TABLE IF NOT EXISTS route_artifacts(route_uuid TEXT REFERENCES routes(id), segment_id INTEGER REFERENCES route_segments(id), artifact_id INTEGER REFERENCES artifacts(id), role TEXT NOT NULL, start_sec REAL, duration_sec REAL);
UPDATE route_artifacts SET role='fcamera' WHERE role='qcamera' AND artifact_id IN (SELECT id FROM artifacts WHERE kind='fcamera');
UPDATE route_artifacts SET role='ecamera' WHERE role='qcamera' AND artifact_id IN (SELECT id FROM artifacts WHERE kind='ecamera');
UPDATE route_artifacts SET role='dcamera' WHERE role='qcamera' AND artifact_id IN (SELECT id FROM artifacts WHERE kind='dcamera');
CREATE UNIQUE INDEX IF NOT EXISTS route_artifacts_segment_uq ON route_artifacts(route_uuid, segment_id, artifact_id, role) WHERE segment_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS route_artifacts_route_uq ON route_artifacts(route_uuid, artifact_id, role) WHERE segment_id IS NULL;
CREATE TABLE IF NOT EXISTS video_sync_segments(id INTEGER PRIMARY KEY AUTOINCREMENT, route_uuid TEXT REFERENCES routes(id), artifact_id INTEGER REFERENCES artifacts(id), segment_index INTEGER, route_start_sec REAL, video_start_sec REAL, duration_sec REAL, source_path TEXT, confidence REAL, raw_jsonb TEXT DEFAULT '{}', UNIQUE(route_uuid, artifact_id, segment_index));
CREATE TABLE IF NOT EXISTS review_inboxes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, purpose TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, query_jsonb TEXT DEFAULT '{}', metadata_jsonb TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS review_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, inbox_id INTEGER REFERENCES review_inboxes(id), route_uuid TEXT REFERENCES routes(id), legacy_job_id TEXT, status TEXT DEFAULT 'pending', selected INTEGER, ride_type TEXT, route_label TEXT, sort_order INTEGER, version INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_by TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS review_jobs_legacy_uq ON review_jobs(inbox_id, route_uuid, legacy_job_id) WHERE legacy_job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS review_jobs_route_uq ON review_jobs(inbox_id, route_uuid) WHERE legacy_job_id IS NULL;
CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY AUTOINCREMENT, route_uuid TEXT REFERENCES routes(id), review_job_id INTEGER REFERENCES review_jobs(id), label_identity_hash TEXT UNIQUE, label_kind TEXT, label TEXT, severity TEXT, start_sec REAL, end_sec REAL, notes TEXT, tags TEXT DEFAULT '[]', version INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, created_by TEXT, updated_by TEXT, deleted_at TEXT, deleted_by TEXT, metadata_jsonb TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS label_events(id INTEGER PRIMARY KEY AUTOINCREMENT, label_id INTEGER REFERENCES labels(id), review_job_id INTEGER REFERENCES review_jobs(id), action TEXT, payload_jsonb TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP, created_by TEXT);
CREATE TABLE IF NOT EXISTS bookmarks(id INTEGER PRIMARY KEY AUTOINCREMENT, route_uuid TEXT REFERENCES routes(id), bookmark_identity_hash TEXT UNIQUE, source TEXT, t_sec REAL, end_sec REAL, text TEXT, tags TEXT DEFAULT '[]', artifact_id INTEGER REFERENCES artifacts(id), version INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_by TEXT, deleted_at TEXT, deleted_by TEXT, metadata_jsonb TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS finished_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, review_job_id INTEGER REFERENCES review_jobs(id), route_uuid TEXT REFERENCES routes(id), finished_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_by TEXT, label_count INTEGER, notes TEXT, snapshot_jsonb TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS legacy_files(id INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id INTEGER REFERENCES artifacts(id), source_path TEXT UNIQUE, parser_status TEXT, counts_jsonb TEXT DEFAULT '{}', warnings_jsonb TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS route_samples(route_uuid TEXT REFERENCES routes(id) ON DELETE CASCADE, t_sec REAL NOT NULL, speed_mph REAL, set_speed_mph REAL, a_ego_mps2 REAL, gas_pressed INTEGER, brake_pressed INTEGER, lead_status INTEGER, lead_d_rel_m REAL, lead_v_rel_mps REAL, raw_jsonb TEXT DEFAULT '{}', PRIMARY KEY(route_uuid,t_sec));
CREATE TABLE IF NOT EXISTS can_frames_sampled(route_uuid TEXT REFERENCES routes(id) ON DELETE CASCADE, t_sec REAL NOT NULL, bus INTEGER, address INTEGER, data_hex TEXT, name TEXT, hint TEXT, is_unknown INTEGER, raw_jsonb TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS can_frames_sampled_route_time_idx ON can_frames_sampled(route_uuid, t_sec);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, route_uuid TEXT REFERENCES routes(id) ON DELETE CASCADE, source TEXT, event_type TEXT, t_sec REAL, end_t_sec REAL, severity TEXT, summary TEXT, raw_jsonb TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS ingest_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, source_host TEXT, source_root TEXT, started_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, status TEXT DEFAULT 'running', tool_id INTEGER, selected_route_ids TEXT, summary_jsonb TEXT DEFAULT '{}', error TEXT);
CREATE TABLE IF NOT EXISTS ingest_items(id INTEGER PRIMARY KEY AUTOINCREMENT, ingest_run_id INTEGER REFERENCES ingest_runs(id), route_uuid TEXT, artifact_id INTEGER, action TEXT, status TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS input_sets(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, fingerprint TEXT UNIQUE, params_jsonb TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS analysis_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, route_uuid TEXT, input_set_id INTEGER, analysis_name TEXT, started_at TEXT, finished_at TEXT, status TEXT, params_jsonb TEXT DEFAULT '{}', summary_jsonb TEXT DEFAULT '{}', report_artifact_id INTEGER, created_by TEXT);
INSERT OR IGNORE INTO schema_migrations(version) VALUES('001_initial_postgres');
'''

@dataclass
class ArtifactRecord:
    id: int; sha256: str; size_bytes: int; relative_path: str; kind: str

class DriveStore:
    def __init__(self, cfg: DriveDbConfig):
        self.cfg = cfg
        self.dialect = "postgres" if cfg.is_postgres else "sqlite"
        if cfg.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ModuleNotFoundError as e:
                raise RuntimeError("Postgres URL configured but psycopg is not installed. Run scripts/drive_tests/brickpilot_db/setup_postgres_macbook.sh and install 'psycopg[binary]' in the Python env, or use sqlite:/// for offline tests.") from e
            self.conn = psycopg.connect(cfg.database_url, row_factory=dict_row)
        else:
            db_path = cfg.database_url.removeprefix("sqlite://")
            self.conn = sqlite3.connect(":memory:" if db_path in ("", ":memory:", "/:memory:") else db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
    def sql(self, text: str) -> str: return text.replace("?", "%s") if self.dialect == "postgres" else text
    def execute(self, text: str, params: tuple[Any, ...] = ()): return self.conn.execute(self.sql(text), params)
    def one(self, text: str, params: tuple[Any, ...] = ()): return self.execute(text, params).fetchone()
    def _json(self, value: Any) -> Any:
        if self.dialect == "postgres":
            from psycopg.types.json import Jsonb
            return Jsonb(value if value is not None else {})
        return json.dumps(value if value is not None else {})
    def migrate(self):
        if self.dialect == "postgres":
            with self.conn.cursor() as cur: cur.execute(Path(__file__).with_name("schema.sql").read_text())
            self.conn.commit()
        else:
            self.conn.executescript(SQLITE_SCHEMA); self.conn.commit()
    def close(self): self.conn.close()
    def commit(self): self.conn.commit()
    def upsert_host(self, hostname: str | None = None, username: str | None = None, role: str | None = None) -> int:
        hostname = hostname or socket.gethostname(); username = username or os.environ.get("USER")
        if self.dialect == "postgres":
            row = self.one("""INSERT INTO hosts(hostname, username, role, last_seen_at) VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(hostname, username) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP, role=coalesce(EXCLUDED.role,hosts.role) RETURNING id""", (hostname, username, role)); return int(row["id"])
        self.execute("INSERT OR IGNORE INTO hosts(hostname, username, role, last_seen_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (hostname, username, role))
        self.execute("UPDATE hosts SET last_seen_at=CURRENT_TIMESTAMP, role=coalesce(?,role) WHERE hostname=? AND username IS ?", (role, hostname, username))
        return int(self.one("SELECT id FROM hosts WHERE hostname=? AND username IS ?", (hostname, username))[0])
    def upsert_route(self, route_id: str, *, source_device: str="unknown", metadata: dict | None=None, **fields) -> str:
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"brickpilot:{source_device}:{route_id}")); source_key = fields.get("source_device_route_key") or f"{source_device}:{route_id}"
        if self.dialect == "postgres":
            row = self.one("""INSERT INTO routes(id,route_id,canonical_name,route_label,source_device_route_key,source_device,drive_type,segment_count,metadata_jsonb)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_device_route_key) DO UPDATE SET updated_at=CURRENT_TIMESTAMP, segment_count=GREATEST(routes.segment_count, EXCLUDED.segment_count), metadata_jsonb=EXCLUDED.metadata_jsonb RETURNING id""", (rid, route_id, fields.get("canonical_name") or route_id, fields.get("route_label"), source_key, source_device, fields.get("drive_type"), fields.get("segment_count",0), self._json(metadata or {}))); return str(row["id"])
        self.execute("""INSERT INTO routes(id,route_id,canonical_name,route_label,source_device_route_key,source_device,drive_type,segment_count,metadata_jsonb)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_device_route_key) DO UPDATE SET updated_at=CURRENT_TIMESTAMP, segment_count=max(routes.segment_count, excluded.segment_count), metadata_jsonb=excluded.metadata_jsonb""", (rid, route_id, fields.get("canonical_name") or route_id, fields.get("route_label"), source_key, source_device, fields.get("drive_type"), fields.get("segment_count",0), self._json(metadata or {})))
        return str(self.one("SELECT id FROM routes WHERE source_device_route_key=?", (source_key,))[0])
    def upsert_segment(self, route_uuid: str, segment_index: int, flags: dict[str,bool] | None=None) -> int:
        flags = flags or {}; vals = (route_uuid, segment_index, bool(flags.get("qlog",False)), bool(flags.get("rlog",False)), bool(flags.get("qcamera",False)), bool(flags.get("fcamera",False)), bool(flags.get("ecamera",False)))
        if self.dialect == "postgres":
            row = self.one("""INSERT INTO route_segments(route_uuid,segment_index,has_qlog,has_rlog,has_qcamera,has_fcamera,has_ecamera) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(route_uuid, segment_index) DO UPDATE SET has_qlog=route_segments.has_qlog OR EXCLUDED.has_qlog, has_rlog=route_segments.has_rlog OR EXCLUDED.has_rlog, has_qcamera=route_segments.has_qcamera OR EXCLUDED.has_qcamera, has_fcamera=route_segments.has_fcamera OR EXCLUDED.has_fcamera, has_ecamera=route_segments.has_ecamera OR EXCLUDED.has_ecamera RETURNING id""", vals); return int(row["id"])
        self.execute("""INSERT INTO route_segments(route_uuid,segment_index,has_qlog,has_rlog,has_qcamera,has_fcamera,has_ecamera) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(route_uuid, segment_index) DO UPDATE SET has_qlog=max(has_qlog,excluded.has_qlog), has_rlog=max(has_rlog,excluded.has_rlog), has_qcamera=max(has_qcamera,excluded.has_qcamera), has_fcamera=max(has_fcamera,excluded.has_fcamera), has_ecamera=max(has_ecamera,excluded.has_ecamera)""", tuple(int(v) if isinstance(v,bool) else v for v in vals))
        return int(self.one("SELECT id FROM route_segments WHERE route_uuid=? AND segment_index=?", (route_uuid, segment_index))[0])
    @staticmethod
    def kind_for_path(path: Path) -> str:
        n = path.name.lower()
        if "fcamera" in n: return "fcamera"
        if "ecamera" in n: return "ecamera"
        if "dcamera" in n: return "dcamera"
        if "qcamera" in n: return "qcamera"
        if n.endswith(".hevc"): return "camera"
        if "rlog" in n: return "rlog"
        if "qlog" in n: return "qlog"
        if "bookmark" in n and n.endswith((".m4a", ".wav", ".mp3")): return "voice_audio"
        if n.endswith(".mp4") and ("clip" in n or "review" in n): return "clip"
        if n.endswith(".mp4"): return "full_drive_video"
        if n.endswith((".sqlite", ".db")): return "sqlite"
        if n.endswith((".json", ".jsonl")): return "json"
        if n.endswith(".csv"): return "csv"
        if n.endswith((".md", ".txt")): return "report"
        return "other"
    def _hash_to_temp(self, src: Path) -> tuple[str,int,Path]:
        self.cfg.artifact_root.mkdir(parents=True, exist_ok=True); h = hashlib.sha256(); size = 0
        fd, tmp = tempfile.mkstemp(prefix="artifact-", suffix=".tmp", dir=self.cfg.artifact_root)
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            for chunk in iter(lambda: inp.read(1024*1024), b""):
                h.update(chunk); out.write(chunk); size += len(chunk)
            out.flush(); os.fsync(out.fileno())
        return h.hexdigest(), size, Path(tmp)
    def import_artifact(self, src: Path, *, kind: str | None=None, host_id: int | None=None, copy: bool=True) -> ArtifactRecord:
        src = Path(src); kind = kind or self.kind_for_path(src); host_id = host_id or self.upsert_host(role=self.cfg.source_host_role)
        sha, size, tmp = self._hash_to_temp(src); rel = Path("sha256") / sha[:2] / sha[2:4] / sha; final = self.cfg.artifact_root / rel; final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists(): tmp.unlink(missing_ok=True)
        else: os.replace(tmp, final)
        mime = mimetypes.guess_type(src.name)[0]; meta = {"name": src.name, "privacy": self.cfg.privacy_default, "source_path_private": True}
        if self.dialect == "postgres":
            row = self.one("""INSERT INTO artifacts(sha256,kind,mime_type,size_bytes,artifact_path,original_path,source_host_id,copy_status,verified_at,sha256_verified,metadata_jsonb)
                VALUES(?,?,?,?,?,NULL,?,'verified',CURRENT_TIMESTAMP,true,?) ON CONFLICT(sha256) DO UPDATE SET imported_at=CURRENT_TIMESTAMP RETURNING id""", (sha, kind, mime, size, str(rel), host_id, self._json(meta))); art_id = int(row["id"])
        else:
            self.execute("""INSERT INTO artifacts(sha256,kind,mime_type,size_bytes,artifact_path,original_path,source_host_id,copy_status,verified_at,sha256_verified,metadata_jsonb)
                VALUES(?,?,?,?,?,NULL,?,'verified',CURRENT_TIMESTAMP,1,?) ON CONFLICT(sha256) DO UPDATE SET imported_at=CURRENT_TIMESTAMP""", (sha, kind, mime, size, str(rel), host_id, self._json(meta))); art_id = int(self.one("SELECT id FROM artifacts WHERE sha256=?", (sha,))[0])
        st = src.stat(); mtime = None if self.dialect == "postgres" else st.st_mtime
        self.execute("INSERT INTO source_paths(artifact_id,host_id,path,mtime,size_bytes,last_seen_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(host_id,path) DO UPDATE SET artifact_id=excluded.artifact_id, last_seen_at=CURRENT_TIMESTAMP, size_bytes=excluded.size_bytes", (art_id, host_id, str(src), mtime, st.st_size))
        return ArtifactRecord(art_id, sha, size, str(rel), kind)
    def link_route_artifact(self, route_uuid: str, artifact_id: int, role: str, segment_id: int | None=None):
        self.execute(("INSERT INTO route_artifacts(route_uuid,segment_id,artifact_id,role) VALUES(?,?,?,?) ON CONFLICT DO NOTHING" if self.dialect == "postgres" else "INSERT OR IGNORE INTO route_artifacts(route_uuid,segment_id,artifact_id,role) VALUES(?,?,?,?)"), (route_uuid, segment_id, artifact_id, role))
    def create_inbox(self, name: str, purpose: str | None=None) -> int:
        if self.dialect == "postgres":
            row = self.one("INSERT INTO review_inboxes(name,purpose) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET purpose=coalesce(EXCLUDED.purpose,review_inboxes.purpose) RETURNING id", (name, purpose)); return int(row["id"])
        self.execute("INSERT OR IGNORE INTO review_inboxes(name,purpose) VALUES(?,?)", (name, purpose)); return int(self.one("SELECT id FROM review_inboxes WHERE name=?", (name,))[0])
    def upsert_review_job(self, inbox_id: int, route_uuid: str, legacy_job_id: str | None=None, **fields) -> int:
        vals = (inbox_id, route_uuid, legacy_job_id, fields.get("status","pending"), fields.get("selected"), fields.get("ride_type"), fields.get("route_label"), fields.get("sort_order"))
        if self.dialect == "postgres":
            sql = """INSERT INTO review_jobs(inbox_id,route_uuid,legacy_job_id,status,selected,ride_type,route_label,sort_order) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(inbox_id,route_uuid) WHERE legacy_job_id IS NULL DO UPDATE SET updated_at=CURRENT_TIMESTAMP RETURNING id""" if legacy_job_id is None else """INSERT INTO review_jobs(inbox_id,route_uuid,legacy_job_id,status,selected,ride_type,route_label,sort_order) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(inbox_id,route_uuid,legacy_job_id) WHERE legacy_job_id IS NOT NULL DO UPDATE SET updated_at=CURRENT_TIMESTAMP RETURNING id"""
            return int(self.one(sql, vals)["id"])
        self.execute("INSERT OR IGNORE INTO review_jobs(inbox_id,route_uuid,legacy_job_id,status,selected,ride_type,route_label,sort_order) VALUES(?,?,?,?,?,?,?,?)", vals)
        self.execute("UPDATE review_jobs SET updated_at=CURRENT_TIMESTAMP WHERE inbox_id=? AND route_uuid=? AND legacy_job_id IS ?", (inbox_id, route_uuid, legacy_job_id))
        return int(self.one("SELECT id FROM review_jobs WHERE inbox_id=? AND route_uuid=? AND legacy_job_id IS ?", (inbox_id, route_uuid, legacy_job_id))[0])
    def insert_label_event(self, review_job_id: int | None, action: str, payload: dict, label_id: int | None=None, by: str="importer"):
        self.execute("INSERT INTO label_events(label_id,review_job_id,action,payload_jsonb,created_by) VALUES(?,?,?,?,?)", (label_id, review_job_id, action, self._json(payload), by))
