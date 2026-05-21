#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, os, re, shutil, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore
from scripts.drive_tests.brickpilot_db import queries

CFG = load_config(); STORE = DriveStore(CFG); STORE.migrate(); ALLOW_INSECURE_DEV = os.environ.get('BRICKPILOT_DRIVE_DB_INSECURE_DEV') in ('1','true','yes')

def ok(handler, payload, status=200, ctype="application/json"):
    body = json.dumps(payload, indent=2, default=str).encode() if ctype == "application/json" else payload
    handler.send_response(status); handler.send_header("content-type", ctype); handler.send_header("cache-control", "no-store"); handler.end_headers(); handler.wfile.write(body)

def tags_param(value):
    return (value or []) if STORE.dialect == 'postgres' else json.dumps(value or [])

def authorized(handler) -> bool:
    if not CFG.api_token: return bool(ALLOW_INSECURE_DEV)
    auth = handler.headers.get("authorization", "")
    token = handler.headers.get("x-brickpilot-token")
    return auth == f"Bearer {CFG.api_token}" or token == CFG.api_token


def delete_review_job_route(data):
    """Delete one imported validation ride from DB plus its private artifact files."""
    if data.get('confirm') is not True:
        return {"error": "confirmation_required", "message": "Set confirm=true only after the user confirms deletion."}, 400
    review_job_id = data.get('review_job_id')
    route_uuid = data.get('route_uuid')
    if not review_job_id:
        return {"error": "review_job_id_required"}, 400
    row = STORE.execute("""SELECT j.id, j.route_uuid, j.version, j.ride_type, i.name AS inbox_name
        FROM review_jobs j JOIN review_inboxes i ON i.id=j.inbox_id
        WHERE j.id=?""", (review_job_id,)).fetchone()
    if not row:
        return {"error": "review job not found"}, 404
    inbox_name = row["inbox_name"] if isinstance(row, dict) else row[4]
    ride_type = row["ride_type"] if isinstance(row, dict) else row[3]
    if inbox_name != "logdrive_label_validation" or ride_type != "label validation":
        return {"error": "delete_not_allowed_for_job", "message": "Only imported logdrive label-validation review jobs can be deleted here."}, 403
    db_route_uuid = str(row["route_uuid"] if isinstance(row, dict) else row[1])
    current_version = row["version"] if isinstance(row, dict) else row[2]
    if route_uuid and str(route_uuid) != db_route_uuid:
        return {"error": "route_mismatch"}, 409
    expected = data.get('expected_version')
    if expected is not None and int(expected) != int(current_version):
        return {"error": "version_conflict", "current_version": current_version}, 409
    route_uuid = db_route_uuid

    art_rows = [dict(r) for r in STORE.execute("""SELECT DISTINCT a.id, a.artifact_path, a.size_bytes
        FROM artifacts a JOIN route_artifacts ra ON ra.artifact_id=a.id
        WHERE ra.route_uuid=?""", (route_uuid,)).fetchall()]
    artifact_ids = [r['id'] for r in art_rows]
    orphan_ids = []
    for aid in artifact_ids:
        refs = STORE.execute("SELECT COUNT(DISTINCT route_uuid) AS n FROM route_artifacts WHERE artifact_id=? AND route_uuid<>?", (aid, route_uuid)).fetchone()
        n = refs['n'] if isinstance(refs, dict) else refs[0]
        if int(n or 0) == 0:
            orphan_ids.append(aid)

    deleted_dir = CFG.artifact_root / 'deleted_artifacts' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') / str(review_job_id)
    moved_files = []
    for r in art_rows:
        if r['id'] not in orphan_ids:
            continue
        src = (CFG.artifact_root / r['artifact_path']).resolve()
        try:
            rel_path = src.relative_to(CFG.artifact_root.resolve())
        except ValueError:
            continue
        if not src.exists():
            continue
        dst = deleted_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved_files.append(str(dst))

    STORE.execute("DELETE FROM label_events WHERE review_job_id=?", (review_job_id,))
    STORE.execute("DELETE FROM finished_reviews WHERE review_job_id=?", (review_job_id,))
    STORE.execute("DELETE FROM labels WHERE route_uuid=? AND review_job_id=?", (route_uuid, review_job_id))
    STORE.execute("DELETE FROM review_jobs WHERE id=?", (review_job_id,))
    remaining = STORE.execute("SELECT COUNT(*) AS n FROM review_jobs WHERE route_uuid=?", (route_uuid,)).fetchone()
    remaining_n = remaining['n'] if isinstance(remaining, dict) else remaining[0]
    if int(remaining_n or 0) == 0:
        STORE.execute("DELETE FROM bookmarks WHERE route_uuid=?", (route_uuid,))
        STORE.execute("DELETE FROM input_set_members WHERE route_uuid=?", (route_uuid,))
        STORE.execute("DELETE FROM video_sync_segments WHERE route_uuid=?", (route_uuid,))
        STORE.execute("UPDATE analysis_runs SET route_uuid=NULL WHERE route_uuid=?", (route_uuid,))
        STORE.execute("UPDATE ingest_items SET route_uuid=NULL WHERE route_uuid=?", (route_uuid,))
        STORE.execute("DELETE FROM routes WHERE id=?", (route_uuid,))
    for aid in orphan_ids:
        STORE.execute("DELETE FROM source_paths WHERE artifact_id=?", (aid,))
        STORE.execute("DELETE FROM legacy_files WHERE artifact_id=?", (aid,))
        STORE.execute("UPDATE ingest_items SET artifact_id=NULL WHERE artifact_id=?", (aid,))
        STORE.execute("DELETE FROM input_set_members WHERE artifact_id=?", (aid,))
        STORE.execute("DELETE FROM analysis_outputs WHERE artifact_id=?", (aid,))
        STORE.execute("DELETE FROM ml_run_outputs WHERE artifact_id=?", (aid,))
        STORE.execute("DELETE FROM artifacts WHERE id=?", (aid,))
    STORE.commit()
    return {"deleted": True, "route_uuid": route_uuid, "artifact_count": len(artifact_ids), "orphan_artifacts_deleted": len(orphan_ids), "files_moved_to": str(deleted_dir) if moved_files else "", "moved_file_count": len(moved_files)}, 200

class Handler(BaseHTTPRequestHandler):
    def _json_body(self):
        n=int(self.headers.get('content-length','0') or 0)
        return json.loads(self.rfile.read(n) or b'{}')
    def _artifact_response(self, artifact_id: int, *, head: bool = False):
        row = STORE.execute("SELECT artifact_path,mime_type,size_bytes FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row: return ok(self, {"error":"not found"}, 404)
        artifact_rel = row["artifact_path"] if isinstance(row, dict) else row[0]
        p = (CFG.artifact_root / artifact_rel).resolve()
        if not str(p).startswith(str(CFG.artifact_root.resolve())): return ok(self, {"error":"bad artifact path"}, 403)
        mime = (row["mime_type"] if isinstance(row, dict) else row[1]) or "application/octet-stream"
        size = row["size_bytes"] if isinstance(row, dict) else row[2]
        self.send_response(200); self.send_header("content-type", mime); self.send_header("content-length", str(size or p.stat().st_size)); self.end_headers()
        if not head:
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024*1024), b""):
                    self.wfile.write(chunk)
        return
    def do_HEAD(self):
        if not authorized(self): return ok(self, {"error":"unauthorized"}, 401)
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"/api/artifacts/(\d+)$", path)
        if m: return self._artifact_response(int(m.group(1)), head=True)
        self.send_response(404); self.end_headers(); return
    def do_GET(self):
        if not authorized(self): return ok(self, {"error":"unauthorized"}, 401)
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/routes": return ok(self, {"routes": queries.list_routes(STORE, 500)})
        m = re.match(r"/api/routes/([^/]+)/timeline$", path)
        if m: return ok(self, queries.route_timeline(STORE, m.group(1)))
        if path == "/api/review/inboxes": return ok(self, {"inboxes": queries.list_inboxes(STORE)})
        m = re.match(r"/api/review/inboxes/(\d+)/jobs$", path)
        if m: return ok(self, {"jobs": queries.list_jobs(STORE, int(m.group(1)))})
        if path == "/api/finished-reviews": return ok(self, {"reviews": queries.finished_reviews(STORE)})
        m = re.match(r"/api/artifacts/(\d+)$", path)
        if m: return self._artifact_response(int(m.group(1)))
        return ok(self, {"error":"not found"}, 404)
    def do_POST(self):
        if not authorized(self): return ok(self, {"error":"unauthorized"}, 401)
        path = urllib.parse.urlparse(self.path).path; data = self._json_body()
        if path == "/api/labels":
            expected = data.get("expected_version")
            label_id = data.get("id")
            if label_id:
                row = STORE.execute("SELECT version FROM labels WHERE id=? AND deleted_at IS NULL", (label_id,)).fetchone()
                if not row: return ok(self, {"error":"not found"}, 404)
                current = row["version"] if isinstance(row, dict) else row[0]
                if expected is None or int(current) != int(expected): return ok(self, {"error":"version_conflict", "current_version": current}, 409)
                STORE.execute("UPDATE labels SET label=?, severity=?, start_sec=?, end_sec=?, notes=?, tags=?, version=version+1, updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE id=?", (data.get('label'), data.get('severity'), data.get('start_sec'), data.get('end_sec'), data.get('notes'), tags_param(data.get('tags')), data.get('updated_by','api'), label_id))
                STORE.insert_label_event(data.get('review_job_id'), 'update', data, label_id, data.get('updated_by','api'))
            else:
                identity = data.get('label_identity_hash') or __import__('hashlib').sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

                if STORE.dialect == 'postgres':
                    cur=STORE.execute("INSERT INTO labels(route_uuid,review_job_id,label_identity_hash,label_kind,label,severity,start_sec,end_sec,notes,tags,created_by,metadata_jsonb) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(label_identity_hash) DO UPDATE SET updated_at=CURRENT_TIMESTAMP RETURNING id", (data.get('route_uuid'), data.get('review_job_id'), identity, data.get('label_kind'), data.get('label'), data.get('severity'), data.get('start_sec'), data.get('end_sec'), data.get('notes'), tags_param(data.get('tags')), data.get('created_by','api'), STORE._json(data.get('metadata',{}))))
                    label_id=cur.fetchone()['id']
                else:
                    cur=STORE.execute("INSERT INTO labels(route_uuid,review_job_id,label_identity_hash,label_kind,label,severity,start_sec,end_sec,notes,tags,created_by,metadata_jsonb) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (data.get('route_uuid'), data.get('review_job_id'), identity, data.get('label_kind'), data.get('label'), data.get('severity'), data.get('start_sec'), data.get('end_sec'), data.get('notes'), tags_param(data.get('tags')), data.get('created_by','api'), json.dumps(data.get('metadata',{}))))
                    label_id=cur.lastrowid
                STORE.insert_label_event(data.get('review_job_id'), 'create', data, label_id, data.get('created_by','api'))
            STORE.commit(); return ok(self, {"id": label_id})
        if path == "/api/delete-label":
            row=STORE.execute("SELECT version FROM labels WHERE id=? AND deleted_at IS NULL", (data.get('id'),)).fetchone()
            if not row: return ok(self, {"error":"not found"}, 404)
            current = row["version"] if isinstance(row, dict) else row[0]
            if data.get('expected_version') is None or int(current) != int(data.get('expected_version')): return ok(self, {"error":"version_conflict", "current_version": current}, 409)
            STORE.execute("UPDATE labels SET deleted_at=CURRENT_TIMESTAMP, deleted_by=?, version=version+1 WHERE id=?", (data.get('deleted_by','api'), data.get('id'))); STORE.insert_label_event(data.get('review_job_id'), 'delete', data, data.get('id'), data.get('deleted_by','api')); STORE.commit(); return ok(self, {"deleted": True})
        if path == "/api/finish-review":
            row=STORE.execute("SELECT version FROM review_jobs WHERE id=?", (data.get('review_job_id'),)).fetchone()
            if not row: return ok(self, {"error":"review job not found"}, 404)
            current = row["version"] if isinstance(row, dict) else row[0]
            expected = data.get('expected_version')
            if expected is None or int(current) != int(expected): return ok(self, {"error":"version_conflict", "current_version": current}, 409)
            STORE.execute("INSERT INTO finished_reviews(review_job_id,route_uuid,finished_by,label_count,notes,snapshot_jsonb) VALUES(?,?,?,?,?,?)", (data.get('review_job_id'), data.get('route_uuid'), data.get('finished_by','api'), data.get('label_count'), data.get('notes'), STORE._json(data)))
            STORE.execute("UPDATE review_jobs SET status='finished', version=version+1, updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE id=?", (data.get('finished_by','api'), data.get('review_job_id'))); STORE.insert_label_event(data.get('review_job_id'), 'finish_review', data, None, data.get('finished_by','api')); STORE.commit(); return ok(self, {"finished": True})
        if path == "/api/import-voice":
            STORE.insert_label_event(data.get('review_job_id'), 'import_voice', data, None, data.get('created_by','api')); STORE.commit(); return ok(self, {"imported": True})
        if path == "/api/delete-review-job":
            payload, status = delete_review_job_route(data); return ok(self, payload, status)
        return ok(self, {"error":"not found"}, 404)

def main():
    global ALLOW_INSECURE_DEV
    ap=argparse.ArgumentParser(); ap.add_argument('--insecure-dev-no-token', action='store_true', help='allow unauthenticated local development only when api_token is absent')
    args=ap.parse_args(); ALLOW_INSECURE_DEV = ALLOW_INSECURE_DEV or bool(args.insecure_dev_no_token)
    if not CFG.api_token and not ALLOW_INSECURE_DEV:
        raise SystemExit('Refusing to start drive DB API without api_token. Use --insecure-dev-no-token or BRICKPILOT_DRIVE_DB_INSECURE_DEV=1 only for local development.')
    httpd=ThreadingHTTPServer((CFG.api_bind, CFG.api_port), Handler)
    mode='token-required' if CFG.api_token else 'INSECURE-DEV-NO-TOKEN'
    print(f"Brickpilot drive DB API listening on http://{CFG.api_bind}:{CFG.api_port} ({mode})")
    httpd.serve_forever()
if __name__ == "__main__": main()
