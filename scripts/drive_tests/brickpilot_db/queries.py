from __future__ import annotations
import json
from pathlib import Path
from .store import DriveStore

ROOT = Path(__file__).resolve().parents[3] if (Path(__file__).resolve().parents[3] / "analysis").exists() else Path(__file__).resolve().parents[2]


def rows(cur): return [dict(r) for r in cur.fetchall()]
def list_routes(store: DriveStore, limit: int=100):
    return rows(store.execute("SELECT id, route_id, canonical_name, route_label, segment_count, updated_at FROM routes ORDER BY updated_at DESC LIMIT ?", (limit,)))

def route_timeline(store: DriveStore, route_uuid: str):
    labels = rows(store.execute("SELECT * FROM labels WHERE route_uuid=? AND deleted_at IS NULL ORDER BY start_sec", (route_uuid,)))
    bookmarks = rows(store.execute("SELECT * FROM bookmarks WHERE route_uuid=? AND deleted_at IS NULL ORDER BY t_sec", (route_uuid,)))
    artifacts = rows(store.execute("""SELECT a.id,a.kind,a.mime_type,a.size_bytes,a.artifact_path,ra.role,ra.start_sec,ra.duration_sec
        FROM artifacts a JOIN route_artifacts ra ON ra.artifact_id=a.id
        WHERE ra.route_uuid=?
        ORDER BY CASE a.kind WHEN 'full_drive_video' THEN 0 WHEN 'clip' THEN 1 WHEN 'qcamera' THEN 2 WHEN 'fcamera' THEN 3 WHEN 'ecamera' THEN 4 ELSE 9 END,
                 coalesce(ra.start_sec,0), a.id""", (route_uuid,)))
    samples = rows(store.execute("SELECT t_sec AS t, speed_mph, set_speed_mph, a_ego_mps2, gas_pressed, brake_pressed, lead_status, lead_d_rel_m, lead_v_rel_mps FROM route_samples WHERE route_uuid=? ORDER BY t_sec LIMIT 20000", (route_uuid,)))
    can_rows = rows(store.execute("SELECT t_sec AS t, bus, address, data_hex, name, hint, is_unknown FROM can_frames_sampled WHERE route_uuid=? ORDER BY t_sec LIMIT 20000", (route_uuid,)))
    can = []
    for row in can_rows:
        if not can or can[-1].get("t") != row.get("t"):
            can.append({"t": row.get("t"), "frames": []})
        can[-1]["frames"].append({"src": row.get("bus"), "addr": row.get("address"), "data": row.get("data_hex"), "name": row.get("name"), "hint": row.get("hint"), "unknown": row.get("is_unknown")})
    events = rows(store.execute("SELECT id, source, event_type AS type, t_sec AS t, end_t_sec AS end_t, severity, summary FROM events WHERE route_uuid=? ORDER BY t_sec", (route_uuid,)))
    video_sync = rows(store.execute("SELECT artifact_id, segment_index, route_start_sec, video_start_sec, duration_sec, source_path, confidence FROM video_sync_segments WHERE route_uuid=? ORDER BY route_start_sec, segment_index", (route_uuid,)))
    return {"labels": labels, "bookmarks": bookmarks, "artifacts": artifacts, "telemetry": {"samples": samples}, "can": {"samples": can}, "events": events, "video_sync": video_sync}

def list_inboxes(store: DriveStore): return rows(store.execute("SELECT * FROM review_inboxes ORDER BY created_at DESC"))





def _legacy_validation_route_ids() -> set[str]:
    out: set[str] = set()
    for fp in (ROOT / 'analysis' / 'drive_tests').glob('manual_drive_labeler_*/jobs/*/drive_data.json'):
        try:
            obj = json.loads(fp.read_text())
        except Exception:
            continue
        rid = (obj.get('route') or {}).get('route_id')
        if rid == '0000015c--c960fea484':
            continue
        samples = (obj.get('telemetry') or {}).get('samples') or []
        sync = obj.get('video_sync') or []
        video = obj.get('video_url') or obj.get('video_path') or ''
        if rid and samples and sync and video:
            out.add(str(rid))
    return out


def _playable_video_predicate(alias: str = "a", route_alias: str = "ra") -> str:
    return (
        f"(({alias}.kind IN ('full_drive_video','clip') "
        f"OR {route_alias}.role IN ('full_drive_video','clip') "
        f"OR {alias}.mime_type='video/mp4') AND COALESCE({alias}.size_bytes,0)>0)"
    )

def _raw_video_predicate(alias: str = "a", route_alias: str = "ra") -> str:
    return (
        f"(({alias}.kind IN ('qcamera','fcamera','ecamera','dcamera','camera') "
        f"OR {route_alias}.role IN ('qcamera','fcamera','ecamera','dcamera','camera') "
        f"OR lower(COALESCE({alias}.artifact_path,'')) LIKE '%%camera%%' "
        f"OR lower(COALESCE({alias}.artifact_path,'')) LIKE '%%.hevc' "
        f"OR lower(COALESCE({alias}.artifact_path,'')) LIKE '%%.ts') "
        f"AND COALESCE({alias}.size_bytes,0)>0)"
    )

def _review_video_predicate(alias: str = "a", route_alias: str = "ra") -> str:
    return f"({_playable_video_predicate(alias, route_alias)} OR {_raw_video_predicate(alias, route_alias)})"

def _log_artifact_predicate(alias: str = "a") -> str:
    return f"({alias}.kind IN ('qlog','rlog') AND COALESCE({alias}.size_bytes,0)>0)"

def _reviewable_timeline_predicate(route_alias: str = "r", *, dialect: str = "postgres") -> str:
    legacy_ids = sorted(_legacy_validation_route_ids())
    legacy_sql = ",".join(repr(x) for x in legacy_ids)
    legacy_clause = f" OR {route_alias}.route_id IN ({legacy_sql})" if legacy_ids else ""
    return (
        f"((EXISTS (SELECT 1 FROM route_samples rs WHERE rs.route_uuid={route_alias}.id) "
        f"AND EXISTS (SELECT 1 FROM video_sync_segments vs WHERE vs.route_uuid={route_alias}.id))"
        f"{legacy_clause})"
    )

def ensure_dynamic_label_validation_jobs(store: DriveStore, inbox_id: int):
    inbox = store.execute("SELECT name FROM review_inboxes WHERE id=?", (inbox_id,)).fetchone()
    name = (inbox["name"] if isinstance(inbox, dict) else inbox[0]) if inbox else ""
    if name != "logdrive_label_validation":
        return
    video_pred = _review_video_predicate()
    log_pred = _log_artifact_predicate()
    timeline_pred = _reviewable_timeline_predicate(dialect=store.dialect)
    sql = f"""
        INSERT INTO review_jobs(inbox_id, route_uuid, legacy_job_id, status, selected, ride_type, route_label, sort_order, updated_by)
        SELECT ?, r.id, NULL, 'pending', true, 'label validation',
               COALESCE(r.route_label, r.canonical_name, r.route_id), NULL, 'dynamic_inbox'
        FROM routes r
        WHERE r.route_id <> '0000015c--c960fea484'
        AND EXISTS (
            SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id
            WHERE ra.route_uuid=r.id AND {video_pred}
        )
        AND EXISTS (
            SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id
            WHERE ra.route_uuid=r.id AND {log_pred}
        )
        AND {timeline_pred}
        AND NOT EXISTS (
            SELECT 1 FROM review_jobs j
            WHERE j.inbox_id=? AND j.route_uuid=r.id AND j.legacy_job_id IS NULL
        )
    """
    if store.dialect == "postgres":
        sql += " ON CONFLICT(inbox_id,route_uuid) WHERE legacy_job_id IS NULL DO NOTHING"
    else:
        sql = sql.replace("INSERT INTO review_jobs", "INSERT OR IGNORE INTO review_jobs")
    store.execute(sql, (inbox_id, inbox_id)); store.commit()

def list_jobs(store: DriveStore, inbox_id: int):
    ensure_dynamic_label_validation_jobs(store, inbox_id)
    order = "ORDER BY COALESCE(j.sort_order, 1000000), j.created_at DESC, r.started_at DESC NULLS LAST, j.id DESC" if store.dialect == "postgres" else "ORDER BY COALESCE(j.sort_order, 1000000), j.created_at DESC, r.started_at DESC, j.id DESC"
    video_pred = _review_video_predicate()
    playable_video_pred = _playable_video_predicate()
    raw_video_pred = _raw_video_predicate()
    log_pred = _log_artifact_predicate()
    timeline_pred = _reviewable_timeline_predicate(dialect=store.dialect)
    return rows(store.execute(f"""SELECT j.*, r.route_id, r.canonical_name, r.segment_count,
            COALESCE(r.duration_sec, (SELECT SUM(COALESCE(s.duration_sec, 60)) FROM route_segments s WHERE s.route_uuid=r.id)) AS duration_sec,
            r.started_at, r.ended_at,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {video_pred}) AS video_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {playable_video_pred}) AS playable_video_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {raw_video_pred}) AS raw_video_artifact_count,
            CASE
              WHEN EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {playable_video_pred}) THEN 'ready'
              WHEN EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {raw_video_pred}) THEN 'raw video only'
              ELSE 'missing'
            END AS media_status,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {log_pred}) AS log_artifact_count,
            (SELECT COUNT(*) FROM video_sync_segments vs WHERE vs.route_uuid=r.id) AS video_sync_count,
            (SELECT COUNT(*) FROM route_samples rs WHERE rs.route_uuid=r.id) AS sample_count,
            (SELECT COUNT(*) FROM events e WHERE e.route_uuid=r.id) AS event_count,
            (SELECT COUNT(*) FROM bookmarks b WHERE b.route_uuid=r.id AND b.deleted_at IS NULL) AS bookmark_count
        FROM review_jobs j LEFT JOIN routes r ON r.id=j.route_uuid
        WHERE j.inbox_id=? AND COALESCE(j.status, '') NOT LIKE ?
          AND COALESCE(r.route_id, '') <> '0000015c--c960fea484'
          AND (j.legacy_job_id IS NOT NULL OR (
            EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {video_pred})
            AND EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND {log_pred})
            AND {timeline_pred}
          )) {order}""", (inbox_id, 'superseded%')))

def finished_reviews(store: DriveStore, limit: int=200):
    return rows(store.execute("""SELECT f.*, j.legacy_job_id, j.ride_type, coalesce(j.route_label,r.route_label,r.canonical_name,r.route_id) AS route_label, r.route_id
        FROM finished_reviews f LEFT JOIN review_jobs j ON j.id=f.review_job_id LEFT JOIN routes r ON r.id=f.route_uuid
        ORDER BY f.finished_at DESC LIMIT ?""", (limit,)))
