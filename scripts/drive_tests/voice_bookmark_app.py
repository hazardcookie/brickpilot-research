#!/usr/bin/env python3
from __future__ import annotations

import argparse, datetime as dt, hashlib, json, mimetypes, os, re, shutil, subprocess, sys, threading, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2]))
TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])).expanduser()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
DATA_ROOT = Path(os.environ.get("BRICKPILOT_DRIVE_DB_ROOT", os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB"))).expanduser()
SESSION_ROOT = Path(os.environ.get("BRICKPILOT_VOICE_SESSION_ROOT", str(DATA_ROOT / "voice_bookmarks" / "sessions"))).expanduser()
APP_VERSION = "brickpilot-tools-0.3.25.0-voice-bookmark"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8788
for root in (REPO_ROOT, TOOLS_ROOT):
    root_text = str(root)
    if root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)
# Make venv-installed local helpers such as imageio-ffmpeg's ffmpeg wrapper visible
# to local STT backends even when the app is launched as `.venv/bin/python ...`
# without activating the virtualenv shell.
os.environ["PATH"] = f"{Path(sys.executable).parent}:{os.environ.get('PATH','')}"
from scripts.drive_tests.brickpilot_db.config import load_config
from scripts.drive_tests.brickpilot_db.store import DriveStore
from scripts.drive_tests.brickpilot_db import queries


def utc_now() -> dt.datetime: return dt.datetime.now(dt.timezone.utc)
def iso_now() -> str: return utc_now().isoformat()

def parse_time(value: Any) -> dt.datetime | None:
    if not value: return None
    if isinstance(value, (int, float)): return dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
    text = str(value).strip()
    if not text: return None
    if text.endswith("Z"): text = text[:-1] + "+00:00"
    try: out = dt.datetime.fromisoformat(text)
    except ValueError: return None
    if out.tzinfo is None: out = out.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return out.astimezone(dt.timezone.utc)

def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text()) if path.exists() else default

def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try: rows.append(json.loads(line))
        except json.JSONDecodeError: rows.append({"kind":"error","text":line,"parse_error":True})
    return rows

def session_dir(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if safe != session_id or not safe: raise ValueError("bad session id")
    return SESSION_ROOT / safe

def new_session_id() -> str: return utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]

def row_hash(row: dict[str, Any]) -> str:
    key = {"kind":row.get("kind"),"text":row.get("text"),"start_wall":row.get("start_wall") or row.get("wall_time"),"end_wall":row.get("end_wall"),"t_session_start_sec":row.get("t_session_start_sec"),"t_session_end_sec":row.get("t_session_end_sec")}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()

def _human_dt(value: Any) -> dt.datetime | None:
    parsed = parse_time(value)
    return parsed.astimezone() if parsed else None

def _fmt_time(value: Any) -> str:
    parsed = _human_dt(value)
    return parsed.strftime("%I:%M %p").lstrip("0") if parsed else "open"

def _fmt_date(value: Any) -> str:
    parsed = _human_dt(value)
    return parsed.strftime("%b %d, %Y").replace(" 0", " ") if parsed else "unknown date"

def human_time_range(meta: dict[str, Any]) -> str:
    start = _human_dt(meta.get("started_at_wall"))
    end = _human_dt(meta.get("ended_at_wall"))
    if not start: return "unknown time"
    if not end: return f"{_fmt_date(start)} {_fmt_time(start)}–open"
    if start.date() == end.date():
        return f"{_fmt_date(start)} {_fmt_time(start)}–{_fmt_time(end)}"
    return f"{_fmt_date(start)} {_fmt_time(start)} – {_fmt_date(end)} {_fmt_time(end)}"

def display_title_for(meta: dict[str, Any]) -> str:
    base = str(meta.get("custom_title") or meta.get("ride_type") or "Drive session").strip() or "Drive session"
    suffix = human_time_range(meta)
    return base if suffix in base else f"{base} — {suffix}"

def start_session(ride_type: str = "label validation", title: str | None = None) -> dict[str, Any]:
    sid = new_session_id(); root = session_dir(sid); (root / "audio").mkdir(parents=True, exist_ok=False)
    clean_title = (title or "").strip()
    meta = {"session_id":sid,"started_at_wall":iso_now(),"ended_at_wall":None,"timezone":dt.datetime.now().astimezone().tzinfo.tzname(None),"source":"brickpilot_voice_bookmark_app","app_version":APP_VERSION,"ride_type":ride_type,"custom_title":clean_title or None,"needs_transcription":True}
    meta["display_title"] = display_title_for(meta)
    (root / "session.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    append_jsonl(root / "events.jsonl", {"kind":"start","wall_time":meta["started_at_wall"],"server_received_at":iso_now(),"custom_title":meta.get("custom_title")})
    return meta

def update_session(session_id: str, **fields: Any) -> dict[str, Any]:
    root = session_dir(session_id); meta = read_json(root / "session.json", {}) or {}; meta.update(fields)
    if {"custom_title", "started_at_wall", "ended_at_wall", "ride_type"} & set(fields):
        meta["display_title"] = display_title_for(meta)
    (root / "session.json").write_text(json.dumps(meta, indent=2, sort_keys=True)); return meta

def set_session_title(session_id: str, title: str) -> dict[str, Any]:
    clean = (title or "").strip()
    append_jsonl(session_dir(session_id) / "events.jsonl", {"kind":"title_update","custom_title":clean or None,"server_received_at":iso_now()})
    return update_session(session_id, custom_title=clean or None)

def stop_session(session_id: str) -> dict[str, Any]:
    ended = iso_now(); append_jsonl(session_dir(session_id) / "events.jsonl", {"kind":"stop","wall_time":ended,"server_received_at":ended})
    return update_session(session_id, ended_at_wall=ended)

def _session_duration_sec(meta: dict[str, Any]) -> float | None:
    start = parse_time(meta.get("started_at_wall")); end = parse_time(meta.get("ended_at_wall"))
    if not start: return None
    if not end: end = utc_now()
    return max(0.0, (end - start).total_seconds())

def _is_ui_drive_session(meta: dict[str, Any], root: Path) -> bool:
    """Return true for user-level ride sessions, not smoke/audit implementation debris."""
    if not meta: return False
    if not meta.get("ended_at_wall"): return True
    ride_type = str(meta.get("ride_type") or "").lower()
    title = str(meta.get("custom_title") or "").strip()
    transcript_rows = len(read_jsonl(root / "transcript.jsonl"))
    audio_chunks = len(list((root / "audio").glob("*.webm"))) if (root / "audio").exists() else 0
    duration = _session_duration_sec(meta)
    if any(word in ride_type for word in ("audit", "smoke", "fake", "test")) and not title:
        return False
    if not title and transcript_rows == 0 and audio_chunks <= 1 and duration is not None and duration < 5.0:
        return False
    return True

def list_sessions(limit: int = 50, include_hidden: bool = False) -> list[dict[str, Any]]:
    SESSION_ROOT.mkdir(parents=True, exist_ok=True); out=[]
    for p in sorted(SESSION_ROOT.iterdir(), reverse=True):
        if not p.is_dir(): continue
        meta = read_json(p / "session.json", {}) or {}
        if meta:
            if not include_hidden and not _is_ui_drive_session(meta, p):
                continue
            sid = meta.get("session_id") or p.name
            transcript_rows = len(read_jsonl(p / "transcript.jsonl"))
            audio_chunks = len(list((p / "audio").glob("*.webm"))) if (p / "audio").exists() else 0
            meta.setdefault("display_title", display_title_for(meta)); meta["path"] = str(p); meta["transcript_rows"] = transcript_rows; meta["audio_chunks"] = audio_chunks; meta["recording"] = not bool(meta.get("ended_at_wall")); meta["duration_sec"] = _session_duration_sec(meta); meta["has_audio"] = audio_chunks > 0; meta["has_transcript"] = transcript_rows > 0; meta["transcribe_job"] = _transcribe_job_snapshot(sid); out.append(meta)
        if len(out) >= limit: break
    return out

def derive_tags(text: str) -> list[str]:
    t=text.lower(); rules={"brake":["brake","braking","stop","stopped"],"accel":["accelerate","accel","gas","launch"],"jerk":["jerk","jerky","lurch","surge","buck"],"creep":["creep","creeping"],"turn":["turn","left","right","corner"],"lead":["lead","car ahead","traffic"],"model":["model","path","lane"],"good":["good","nice","smooth","better"],"bad":["bad","wrong","weird","badly"]}
    tags=["voice_narration"]
    for tag, words in rules.items():
        if any(w in t for w in words): tags.append(tag)
    return sorted(set(tags))

def json_param(store: DriveStore, value: Any) -> Any: return store._json(value)
def tags_param(store: DriveStore, tags: list[str]) -> Any: return tags if store.dialect == "postgres" else json.dumps(tags)

def resolve_route(store: DriveStore, route_arg: str | None, review_job_id: int | None = None) -> dict[str, Any]:
    if review_job_id:
        row=store.one("SELECT r.* FROM review_jobs j JOIN routes r ON r.id=j.route_uuid WHERE j.id=?", (review_job_id,))
        if row: return dict(row)
    if not route_arg:
        row=store.one("SELECT * FROM routes ORDER BY updated_at DESC LIMIT 1")
        if row: return dict(row)
        raise SystemExit("No route supplied and no routes exist in DB")
    route_uuid = None
    try:
        route_uuid = str(uuid.UUID(str(route_arg)))
    except (TypeError, ValueError):
        pass
    if route_uuid:
        row=store.one("SELECT * FROM routes WHERE id=? OR route_id=? OR canonical_name=? OR route_label=?", (route_uuid,route_arg,route_arg,route_arg))
    else:
        row=store.one("SELECT * FROM routes WHERE route_id=? OR canonical_name=? OR route_label=?", (route_arg,route_arg,route_arg))
    if not row: raise SystemExit(f"Could not find route {route_arg!r}")
    return dict(row)

def route_start_wall(store: DriveStore, route_uuid: str, route_row: dict[str, Any]) -> dt.datetime | None:
    start=parse_time(route_row.get("started_at"))
    if start: return start
    tb=store.one("SELECT wall_start_at FROM route_timebases WHERE route_uuid=? ORDER BY confidence DESC LIMIT 1", (route_uuid,))
    return parse_time(tb["wall_start_at"] if tb else None)

def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _voice_row_relative_bounds(row: dict[str, Any], session_start: dt.datetime | None) -> tuple[float | None, float | None]:
    rel = _float_or_none(row.get("t_session_start_sec"))
    rel_end = _float_or_none(row.get("t_session_end_sec"))
    if session_start:
        start = parse_time(row.get("start_wall") or row.get("wall_time"))
        end = parse_time(row.get("end_wall"))
        if rel is None and start:
            rel = (start - session_start).total_seconds()
        if rel_end is None and end:
            rel_end = (end - session_start).total_seconds()
    return rel, rel_end

def _route_duration_sec(route_row: dict[str, Any]) -> float | None:
    duration = _float_or_none(route_row.get("duration_sec"))
    if duration is not None and duration > 0:
        return duration
    return None

def choose_voice_import_alignment(rows: list[dict[str, Any]], meta: dict[str, Any], route_row: dict[str, Any], start_wall: dt.datetime | None, offset_sec: float = 0.0) -> dict[str, Any]:
    if not start_wall:
        return {"use_session_relative": True, "method": "session_relative_fallback", "confidence": 0.45}
    session_start = parse_time(meta.get("started_at_wall"))
    if not session_start:
        return {"use_session_relative": False, "method": "wall_clock_route_started_at", "confidence": 0.85}

    wall_offset = (session_start - start_wall).total_seconds() + offset_sec
    probes: list[dict[str, float]] = []
    for row in rows:
        kind = str(row.get("kind") or "").lower()
        text = str(row.get("text") or "").strip()
        if kind not in {"final", "manual", "imported"} or not text:
            continue
        rel, rel_end = _voice_row_relative_bounds(row, session_start)
        if rel is None:
            continue
        wall_t = wall_offset + rel
        probes.append({"rel": rel, "rel_end": rel_end if rel_end is not None else rel, "wall": wall_t})

    if not probes:
        return {"use_session_relative": False, "method": "wall_clock_route_started_at", "confidence": 0.85, "alignment_wall_offset_sec": round(wall_offset, 3)}

    duration = _route_duration_sec(route_row)
    max_rel = max(p["rel_end"] for p in probes)
    rel_fits_route = duration is None or max_rel <= duration + 30.0
    hidden_start_rows = len([p for p in probes if p["wall"] < -1.0])
    earliest_wall = min(p["wall"] for p in probes)
    # Some imported label-validation rides preserve good session-relative audio timing,
    # while file mtimes can put the route wall clock after the voice session started.
    # If wall-clock import would drop the beginning of a fitting transcript, keep the
    # user's spoken timeline intact instead of shifting every label left.
    if wall_offset < -10.0 and earliest_wall < -5.0 and hidden_start_rows >= 1 and rel_fits_route:
        return {
            "use_session_relative": True,
            "method": "session_relative_negative_wall_guard",
            "confidence": 0.75,
            "alignment_wall_offset_sec": round(wall_offset, 3),
            "hidden_start_rows": hidden_start_rows,
            "max_session_relative_sec": round(max_rel, 3),
        }
    return {"use_session_relative": False, "method": "wall_clock_route_started_at", "confidence": 0.85, "alignment_wall_offset_sec": round(wall_offset, 3)}

def artifact_metadata_update(store: DriveStore, artifact_id: int, patch: dict[str, Any]) -> None:
    row=store.one("SELECT metadata_jsonb FROM artifacts WHERE id=?", (artifact_id,)); current={}
    if row and row["metadata_jsonb"]:
        raw=row["metadata_jsonb"]
        if isinstance(raw, dict): current=raw
        else:
            try: current=json.loads(raw)
            except Exception: current={}
    current.update(patch)
    store.execute("UPDATE artifacts SET metadata_jsonb=? WHERE id=?", (json_param(store,current), artifact_id))

def clear_voice_import_for_session(store: DriveStore, route_uuid: str, session_id: str) -> dict[str, int]:
    """Remove the previously imported voice projection for one route/session.

    The transcript file remains the source of truth. Re-importing should replace the
    UI-facing bookmark projection instead of stacking old rows from earlier ASR passes.
    """
    if not session_id:
        return {"bookmarks": 0, "events": 0}
    if store.dialect == "postgres":
        bcur = store.execute(
            """UPDATE bookmarks
               SET deleted_at=CURRENT_TIMESTAMP, deleted_by='voice_bookmark_app_reimport', version=version+1
               WHERE route_uuid=? AND source='voice_narration' AND deleted_at IS NULL
                 AND metadata_jsonb->>'session_id'=?""",
            (route_uuid, session_id),
        )
        ecur = store.execute(
            """DELETE FROM events
               WHERE route_uuid=? AND source='voice_narration'
                 AND raw_jsonb->>'session_id'=?""",
            (route_uuid, session_id),
        )
    else:
        compact = f'%"session_id":"{session_id}"%'
        spaced = f'%"session_id": "{session_id}"%'
        bcur = store.execute(
            """UPDATE bookmarks
               SET deleted_at=CURRENT_TIMESTAMP, deleted_by='voice_bookmark_app_reimport', version=version+1
               WHERE route_uuid=? AND source='voice_narration' AND deleted_at IS NULL
                 AND (metadata_jsonb LIKE ? OR metadata_jsonb LIKE ?)""",
            (route_uuid, compact, spaced),
        )
        ecur = store.execute(
            """DELETE FROM events
               WHERE route_uuid=? AND source='voice_narration'
                 AND (raw_jsonb LIKE ? OR raw_jsonb LIKE ?)""",
            (route_uuid, compact, spaced),
        )
    return {"bookmarks": int(getattr(bcur, "rowcount", 0) or 0), "events": int(getattr(ecur, "rowcount", 0) or 0)}

def import_voice_session(session: str, *, route: str | None, review_job_id: int | None, offset_sec: float = 0.0, config: str | None = None) -> dict[str, Any]:
    sdir = Path(session) if Path(session).exists() else session_dir(session)
    meta=read_json(sdir / "session.json", {}) or {}
    if not meta: raise SystemExit(f"missing session.json under {sdir}")
    sid=meta.get("session_id") or sdir.name; transcript_path=sdir / "transcript.jsonl"; rows=read_jsonl(transcript_path)
    cfg=load_config(config); store=DriveStore(cfg); store.migrate()
    try:
        route_row=resolve_route(store, route, review_job_id); route_uuid=str(route_row["id"]); start_wall=route_start_wall(store, route_uuid, route_row); session_start=parse_time(meta.get("started_at_wall"))
        alignment = choose_voice_import_alignment(rows, meta, route_row, start_wall, offset_sec)
        use_session_relative = bool(alignment.get("use_session_relative"))
        method=str(alignment.get("method") or "session_relative_fallback"); confidence=float(alignment.get("confidence") or 0.45)
        host_id=store.upsert_host(role="voice_bookmark_mac_app"); transcript_artifact_id=None; audio_artifact_ids=[]
        if transcript_path.exists():
            art=store.import_artifact(transcript_path, kind="json", host_id=host_id); transcript_artifact_id=art.id; artifact_metadata_update(store, art.id, {"voice_transcript":True,"session_id":sid,"app_version":APP_VERSION}); store.link_route_artifact(route_uuid, art.id, "voice_transcript")
        for apath in sorted((sdir / "audio").glob("*.webm")):
            art=store.import_artifact(apath, kind="voice_audio", host_id=host_id); audio_artifact_ids.append(art.id); artifact_metadata_update(store, art.id, {"voice_audio":True,"session_id":sid,"local_only":True}); store.link_route_artifact(route_uuid, art.id, "voice_audio")
        removed = clear_voice_import_for_session(store, route_uuid, str(sid))
        imported_bookmarks=imported_events=skipped=0
        for idx,row in enumerate(rows):
            kind=str(row.get("kind") or "").lower(); text=str(row.get("text") or "").strip()
            if kind not in {"final","manual","imported"} or not text: skipped += 1; continue
            if kind == "final":
                text = normalize_voice_label_text(text)
            start=parse_time(row.get("start_wall") or row.get("wall_time")); end=parse_time(row.get("end_wall"))
            rel, rel_end = _voice_row_relative_bounds(row, session_start)
            if use_session_relative and rel is not None:
                t_sec=float(rel)+offset_sec; end_sec=float(rel_end)+offset_sec if rel_end is not None else None
            elif start_wall and start:
                t_sec=(start-start_wall).total_seconds()+offset_sec; end_sec=((end-start_wall).total_seconds()+offset_sec) if end else None
            else:
                if rel is None: rel=0.0
                t_sec=float(rel)+offset_sec; end_sec=float(rel_end)+offset_sec if rel_end is not None else None
            if end_sec is not None and end_sec < t_sec: end_sec=t_sec
            rhash=row.get("row_hash") or row_hash(row); identity=hashlib.sha256(f"voice_narration|{route_uuid}|{sid}|{rhash}|{round(t_sec,3)}".encode()).hexdigest(); tags=derive_tags(text)
            metadata={"session_id":sid,"transcript_row_index":idx,"transcript_row_hash":rhash,"transcript_kind":kind,"confidence":row.get("confidence",confidence),"alignment_method":method,"alignment_confidence":confidence,"offset_sec":offset_sec,"start_wall":row.get("start_wall") or row.get("wall_time"),"end_wall":row.get("end_wall"),"audio_artifact_ids":audio_artifact_ids}
            for key in ("alignment_wall_offset_sec", "hidden_start_rows", "max_session_relative_sec"):
                if key in alignment:
                    metadata[key] = alignment[key]
            if store.dialect == "postgres":
                store.execute("""INSERT INTO bookmarks(route_uuid,bookmark_identity_hash,source,t_sec,end_sec,text,tags,artifact_id,updated_by,metadata_jsonb) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(bookmark_identity_hash) DO UPDATE SET updated_at=CURRENT_TIMESTAMP, text=EXCLUDED.text, tags=EXCLUDED.tags, metadata_jsonb=EXCLUDED.metadata_jsonb, deleted_at=NULL, deleted_by=NULL""", (route_uuid,identity,"voice_narration",t_sec,end_sec,text,tags_param(store,tags),transcript_artifact_id,"voice_bookmark_app",json_param(store,metadata)))
            else:
                store.execute("""INSERT INTO bookmarks(route_uuid,bookmark_identity_hash,source,t_sec,end_sec,text,tags,artifact_id,updated_by,metadata_jsonb) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(bookmark_identity_hash) DO UPDATE SET updated_at=CURRENT_TIMESTAMP, text=excluded.text, tags=excluded.tags, metadata_jsonb=excluded.metadata_jsonb, deleted_at=NULL, deleted_by=NULL""", (route_uuid,identity,"voice_narration",t_sec,end_sec,text,tags_param(store,tags),transcript_artifact_id,"voice_bookmark_app",json_param(store,metadata)))
            imported_bookmarks += 1
        packet={"session_id":sid,"route_uuid":route_uuid,"review_job_id":review_job_id,"alignment_method":method,"alignment_confidence":confidence,"offset_sec":offset_sec,"bookmarks":imported_bookmarks,"events_inserted":imported_events,"skipped_rows":skipped,"replaced_bookmarks":removed.get("bookmarks", 0),"removed_events":removed.get("events", 0),"transcript_artifact_id":transcript_artifact_id,"audio_artifact_ids":audio_artifact_ids,"local_session_dir":str(sdir)}
        for key in ("alignment_wall_offset_sec", "hidden_start_rows", "max_session_relative_sec"):
            if key in alignment:
                packet[key] = alignment[key]
        (sdir / "ai_review_packet.json").write_text(json.dumps(packet, indent=2, sort_keys=True))
        if review_job_id:
            identity=hashlib.sha256(f"import_voice|{review_job_id}|{route_uuid}|{sid}".encode()).hexdigest(); exists=False
            for erow in store.execute("SELECT payload_jsonb FROM label_events WHERE review_job_id=? AND action=?", (review_job_id,"import_voice")).fetchall():
                raw=erow["payload_jsonb"]; hay=json.dumps(raw, sort_keys=True) if isinstance(raw, dict) else str(raw)
                if identity in hay: exists=True; break
            if not exists: store.insert_label_event(review_job_id, "import_voice", packet | {"identity_hash":identity}, None, "voice_bookmark_app")
        store.commit(); timeline=queries.route_timeline(store, route_uuid)
        packet["timeline_voice_bookmarks"]=len([b for b in timeline.get("bookmarks",[]) if b.get("source")=="voice_narration"]); packet["timeline_voice_events"]=len([e for e in timeline.get("events",[]) if e.get("source")=="voice_narration"])
        return packet
    finally:
        store.close()

INDEX_HTML = r'''<!doctype html><meta charset="utf-8"><title>Brickpilot Voice Bookmarks</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}button,input{font:inherit}input{border-radius:8px;border:1px solid #30363d;padding:10px;background:#0d1117;color:#e6edf3}button{border:0;border-radius:10px;padding:12px 16px;margin:4px;background:#238636;color:white;cursor:pointer}button.stop{background:#da3633}button.secondary{background:#30363d}button:disabled{opacity:.55;cursor:not-allowed}.good{color:#56d364}.bad{color:#ff7b72}.recbox{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:10px;margin-top:10px}.card{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:16px;margin:12px 0;max-width:920px}.status{font-size:22px;font-weight:700}.muted{color:#8b949e}.transcript{white-space:pre-wrap;min-height:120px;background:#0d1117;border-radius:10px;padding:12px}.interim{color:#a5d6ff}.warn{color:#f2cc60}.pill{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:3px 9px;margin:2px;color:#8b949e}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.title{font-size:18px;font-weight:700}.session{padding:8px 0}</style>
<h1>Brickpilot Voice Bookmarks</h1>
<div class="card"><div class="status" id="status">Idle</div><div class="muted" id="timer">00:00</div><p class="warn" id="speechWarn"></p><div class="row"><input id="sessionTitle" size="42" placeholder="Session title, e.g. Costco drive / highway loop"><button id="start">Start Ride</button><button id="newSession" class="secondary">New Session</button><button class="stop" id="stop" disabled>Stop Ride</button><button class="secondary" onclick="refreshSessions()">Refresh Sessions</button></div><p class="muted">Use <b>New Session</b> when you park or start another separate drive before coming home. It saves the current session, then starts a fresh one.</p></div>
<div class="card"><h2>Manual bookmark</h2><input id="manual" size="70" placeholder="Quick note, e.g. jerky brake at stop sign"><button onclick="manualBookmark()">Add bookmark now</button></div>
<div class="card"><h2>Local realtime transcript <span class="pill">MacBook offline model</span></h2><p class="muted">This preview uses the local MacBook transcription backend only. No browser Web Speech and no internet. It is rough/laggy by design; use <b>Transcribe</b> after the ride for the higher-quality pass. Keep this page open until pending is 0.</p><div class="recbox" id="recHealth">Idle · captured 0 · saved 0 · pending 0</div><div class="transcript" id="finals"></div><div class="interim" id="interim">Local realtime preview appears here after the first saved audio chunks.</div></div>
<div class="card"><h2>Drive sessions</h2><p class="muted" id="transcribeStatus">One ride/session appears here with one final Transcribe button. It uses OpenAI for the post-ride pass when configured; realtime preview stays offline.</p><div class="row"><label class="muted"><input type="checkbox" id="showTechnical" onchange="refreshSessions()"> show technical/test sessions</label></div><div id="sessions"></div></div>
<script>
let session=null, mediaRecorder=null, mediaStream=null, chunkFlushes=[], chunkQueue=[], manualQueue=[], chunkIdx=0, chunkCaptured=0, chunkSaved=0, chunkFailed=0, flushingChunks=false, flushingManual=false, startedPerf=0, timer=null, realtimeTimer=null, realtimeBusy=false, recognition=null; const $=id=>document.getElementById(id);
function nowWall(){return new Date().toISOString()} function tsec(){return startedPerf?(performance.now()-startedPerf)/1000:0} async function postJson(url,obj){let r=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(obj||{})}); if(!r.ok)throw new Error(await r.text()); return await r.json()} function setStatus(s){$('status').textContent=s} function tick(){let s=Math.floor(tsec()),m=Math.floor(s/60);$('timer').textContent=String(m).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function currentTitle(){return $('sessionTitle').value.trim()}
function setControls(recording){$('start').disabled=recording; $('stop').disabled=!recording; $('newSession').disabled=false}
function updateRecHealth(extra=''){const el=$('recHealth'); if(!el)return; const pending=chunkQueue.length+manualQueue.length; const cls=pending?'warn':'good'; el.innerHTML=`${session?'Recording':'Idle'} · captured ${chunkCaptured} · saved ${chunkSaved} · pending <span class="${cls}">${pending}</span>${manualQueue.length?' · manual pending '+manualQueue.length:''}${chunkFailed?' · retries '+chunkFailed:''}${extra?' · '+extra:''}`;}
async function sendChunk(item){let r=await fetch('/api/chunk?session_id='+encodeURIComponent(item.session_id)+'&index='+item.index,{method:'POST',headers:{'content-type':item.type||'audio/webm','x-client-wall':item.wall,'x-client-session-sec':String(item.t)},body:item.blob}); if(!r.ok)throw new Error(await r.text()); return await r.json()}
async function sendManual(item){return await postJson('/api/bookmark',item)}
async function flushChunkQueue(){if(flushingChunks)return; flushingChunks=true; try{while(chunkQueue.length){const item=chunkQueue[0]; try{await sendChunk(item); chunkQueue.shift(); chunkSaved++; updateRecHealth('synced')}catch(e){chunkFailed++; console.warn('chunk upload queued for retry',e); updateRecHealth('server unreachable; keep page open'); break}}}finally{flushingChunks=false}}
function queueChunk(blob,type){if(!session||!blob||!blob.size)return; const item={session_id:session.session_id,index:chunkIdx++,blob,type,wall:nowWall(),t:tsec()}; chunkCaptured++; chunkQueue.push(item); updateRecHealth('captured'); const p=flushChunkQueue(); chunkFlushes.push(p); return p}
async function flushManualQueue(){if(flushingManual)return; flushingManual=true; try{while(manualQueue.length){try{await sendManual(manualQueue[0]); manualQueue.shift(); updateRecHealth('manual synced')}catch(e){console.warn('manual bookmark queued for retry',e); updateRecHealth('manual pending; keep page open'); break}}}finally{flushingManual=false}}
function queueManualBookmark(text){const item={session_id:session.session_id,text:text,wall_time:nowWall(),t_session_start_sec:tsec(),client_monotonic_ms:performance.now()}; manualQueue.push(item); updateRecHealth('manual queued'); flushManualQueue()}
window.addEventListener('online',()=>{updateRecHealth('browser online; retrying'); flushChunkQueue(); flushManualQueue()}); window.addEventListener('offline',()=>updateRecHealth('browser offline; still capturing in this page')); window.addEventListener('beforeunload',e=>{if(chunkQueue.length||manualQueue.length){e.preventDefault(); e.returnValue='Voice recorder still has unsaved chunks/bookmarks. Keep this page open until pending is 0.'}})
async function pollRealtime(){if(!session||realtimeBusy||chunkSaved<1)return; realtimeBusy=true; $('interim').textContent='Local realtime model working…'; try{let out=await postJson('/api/realtime-preview',{session_id:session.session_id,timeout:45}); if(out.text){$('finals').textContent=out.text.trim()+'\n'; $('interim').textContent='Local realtime preview · '+(out.audio_chunks||0)+' chunk(s) · '+(out.backend||'local')} else {$('interim').textContent=out.message||out.status||'Waiting for speech…'}}catch(e){$('interim').textContent='Local realtime preview delayed: '+e.message}finally{realtimeBusy=false}}
function startRealtime(){if(realtimeTimer)clearInterval(realtimeTimer); realtimeTimer=setInterval(pollRealtime,12000); setTimeout(pollRealtime,4500)}
function stopRealtime(){if(realtimeTimer)clearInterval(realtimeTimer); realtimeTimer=null}
async function startRide(){ if(session){return newSession()} $('start').disabled=true; try{session=await postJson('/api/start',{ride_type:'label validation',title:currentTitle()}); startedPerf=performance.now(); setStatus('Requesting microphone…'); timer=setInterval(tick,500); mediaStream=await navigator.mediaDevices.getUserMedia({audio:true}); setStatus('Recording '+(session.display_title||session.session_id)); setControls(true); chunkFlushes=[]; chunkQueue=[]; manualQueue=[]; chunkIdx=0; chunkCaptured=0; chunkSaved=0; chunkFailed=0; updateRecHealth('mic open'); mediaRecorder=new MediaRecorder(mediaStream); mediaRecorder.ondataavailable=ev=>queueChunk(ev.data, ev.data&&ev.data.type); mediaRecorder.start(3000); $('speechWarn').textContent='Local realtime preview uses MacBook offline model only.'; startRealtime()}catch(e){setStatus('Start failed: '+e.message); if(timer)clearInterval(timer); if(session)try{await postJson('/api/stop',{session_id:session.session_id,error:e.message})}catch(_){} if(mediaStream)mediaStream.getTracks().forEach(t=>t.stop()); session=null; mediaRecorder=null; mediaStream=null; setControls(false); updateRecHealth('start failed')}}
function startSpeech(){$("speechWarn").textContent="Browser/Web Speech preview is disabled; local realtime uses the MacBook model."; return}
async function stopRide(opts={}){ if(!session)return; $('stop').disabled=true; setStatus('Saving session…'); stopRealtime(); if(recognition)try{recognition.stop()}catch(e){} if(mediaRecorder&&mediaRecorder.state!='inactive'){await new Promise(resolve=>{mediaRecorder.onstop=resolve; try{mediaRecorder.requestData()}catch(e){} mediaRecorder.stop()})} await Promise.allSettled(chunkFlushes); await flushChunkQueue(); await flushManualQueue(); if(chunkQueue.length||manualQueue.length){setStatus('Waiting to sync '+(chunkQueue.length+manualQueue.length)+' pending item(s)… keep this page open'); $('stop').disabled=false; updateRecHealth('cannot stop until pending items sync'); return} if(mediaStream)mediaStream.getTracks().forEach(t=>t.stop()); clearInterval(timer); let stopped=await postJson('/api/stop',{session_id:session.session_id}); setStatus('Saved '+(stopped.display_title||stopped.session_id)); session=null; mediaRecorder=null; mediaStream=null; startedPerf=0; setControls(false); updateRecHealth('saved'); await refreshSessions(); if(opts.restart){$('finals').textContent=''; $('interim').textContent=''; await startRide()}}
async function newSession(){ if(session){await stopRide({restart:true})} else {await startRide()} }
async function manualBookmark(){let text=$('manual').value.trim(); if(!session||!text)return; $('manual').value=''; $('finals').textContent += '[manual] '+text+'\n'; queueManualBookmark(text)}
function htmlEscape(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function transcribeStatusMsg(p){if(!p)return ''; if(p.error)return p.error; if(p.job&&p.job.message)return p.job.message; if(p.guidance&&p.guidance.length)return p.guidance[0]; return p.message||p.status||''}
async function saveSessionTitle(id){const input=$('title_'+id); const el=$('transcribeStatus'); el.textContent='Saving title…'; try{let out=await postJson('/api/title',{session_id:id,title:input.value}); el.textContent='Saved title: '+(out.display_title||out.session_id); await refreshSessions()}catch(e){el.textContent='Title save failed: '+e.message}}
async function transcribeSession(id){const el=$('transcribeStatus'); el.textContent='Checking final transcription readiness for '+id+'…'; try{let pre=await postJson('/api/transcribe/status',{session_id:id}); if(pre.recording){el.textContent='Stop Ride before transcribing '+id+' so final audio chunks are flushed.'; return} if(pre.status==='no_backend'||pre.status==='model_required'||pre.status==='model_not_found'){el.textContent=transcribeStatusMsg(pre); return} el.textContent='Running final Transcribe for '+id+' with '+(pre.selected_backend||'best backend')+'…'; let out=await postJson('/api/transcribe',{session_id:id}); el.textContent=(out.status||'done')+': '+transcribeStatusMsg(out)+' rows='+(out.appended_rows??0)+' filtered='+(out.filtered_rows??0)+' replaced='+(out.replaced_rows??0); await refreshSessions()}catch(e){el.textContent='Transcribe failed: '+e.message}}
function fmtDuration(sec){if(sec==null)return ''; sec=Math.round(sec); let m=Math.floor(sec/60), s=sec%60; return m?`${m}m ${s}s`:`${s}s`}
async function refreshSessions(){let show=$('showTechnical')&&$('showTechnical').checked; let d=await (await fetch('/api/sessions'+(show?'?include_hidden=1':''))).json(); if(!d.sessions.length){$('sessions').innerHTML='<p class="muted">No drive sessions yet. Tap Start Ride to create one.</p>'; return} $('sessions').innerHTML=d.sessions.map(s=>{let rec=s.recording||!s.ended_at_wall, job=s.transcribe_job, msg=job?`<div class="muted">Transcription: ${htmlEscape(job.state)}${job.message?': '+htmlEscape(job.message):''}</div>`:''; let id=htmlEscape(s.session_id), title=htmlEscape(s.display_title||s.session_id), custom=htmlEscape(s.custom_title||''); let counts=[]; if(s.has_transcript)counts.push(`${s.transcript_rows} transcript row${s.transcript_rows==1?'':'s'}`); if(s.has_audio&&!s.has_transcript)counts.push('audio captured'); let dur=fmtDuration(s.duration_sec); if(dur)counts.push(dur); return `<div class="session"><div class="title">${title}</div><div class="muted">${counts.join(' · ')||'ready'}</div>${msg}<div class="row"><input id="title_${id}" size="36" placeholder="Drive title" value="${custom}"><button class="secondary" onclick="saveSessionTitle('${id}')">Save title</button><button class="secondary" onclick="transcribeSession('${id}')" ${rec?'disabled title="Stop ride first"':''}>Transcribe this ride</button>${rec?' <span class="warn">Recording/open — stop before final transcription.</span>':''}</div>${show?`<details><summary class="muted">technical details</summary><div class="muted">${id} · ${s.audio_chunks} internal audio chunk${s.audio_chunks==1?'':'s'}</div><code>${htmlEscape(s.path)}</code></details>`:''}</div>`}).join('<hr>')}
$('start').onclick=startRide; $('newSession').onclick=newSession; $('stop').onclick=()=>stopRide(); refreshSessions(); setControls(false);
</script>'''

class VoiceHandler(BaseHTTPRequestHandler):
    server_version="BrickpilotVoiceBookmark/1"
    def send_payload(self, status:int, payload:Any, ctype:str="application/json") -> None:
        body = payload if isinstance(payload, bytes) else (json.dumps(payload, indent=2, default=str).encode() if ctype=="application/json" else str(payload).encode())
        self.send_response(status); self.send_header("content-type", ctype); self.send_header("cache-control","no-store"); self.end_headers(); self.wfile.write(body)
    def body_json(self) -> dict[str,Any]:
        n=int(self.headers.get("content-length","0") or 0); return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path
        if path=="/": return self.send_payload(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        if path=="/api/sessions":
            qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); include_hidden = qs.get("include_hidden", ["0"])[0] in {"1", "true", "yes"}
            return self.send_payload(200, {"sessions":list_sessions(include_hidden=include_hidden),"voice_root":str(SESSION_ROOT),"include_hidden":include_hidden})
        if path=="/api/transcribe/status":
            qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); return self.send_payload(200, api_transcribe_status(qs.get("session_id", qs.get("session", [None]))[0], backend=qs.get("backend", ["auto"])[0], model=qs.get("model", [None])[0]))
        return self.send_payload(404,{"error":"not found"})
    def do_POST(self):
        parsed=urllib.parse.urlparse(self.path); path=parsed.path
        try:
            if path=="/api/start":
                data=self.body_json(); return self.send_payload(200, start_session(data.get("ride_type") or "label validation", title=data.get("title")))
            if path=="/api/stop": return self.send_payload(200, stop_session(self.body_json()["session_id"]))
            if path=="/api/title":
                data=self.body_json(); return self.send_payload(200, set_session_title(data["session_id"], data.get("title") or ""))
            if path=="/api/transcribe/status":
                data=self.body_json(); return self.send_payload(200, api_transcribe_status(data.get("session_id") or data.get("session"), backend=data.get("backend") or "auto", model=data.get("model")))
            if path=="/api/transcribe":
                data=self.body_json(); payload=api_transcribe_session(data.get("session_id") or data.get("session"), backend=data.get("backend") or "auto", model=data.get("model"), language=data.get("language") or "en", force=bool(data.get("force")), timeout=int(data.get("timeout") or 900), allow_running=bool(data.get("allow_running")))
                return self.send_payload(409 if payload.get("status") in {"recording","busy"} else 200, payload)
            if path=="/api/realtime-preview":
                data=self.body_json(); payload=realtime_preview(data.get("session_id") or data.get("session"), backend=data.get("backend") or "auto", model=data.get("model"), language=data.get("language") or "en", timeout=int(data.get("timeout") or 45)); return self.send_payload(200, payload)
            if path=="/api/transcript":
                data=self.body_json(); sid=data.pop("session_id"); data.setdefault("kind","final"); data["server_received_at"]=iso_now(); data.setdefault("row_hash", row_hash(data)); append_jsonl(session_dir(sid)/"transcript.jsonl", data)
                if data.get("text"): update_session(sid, needs_transcription=False)
                return self.send_payload(200,{"ok":True,"row_hash":data["row_hash"]})
            if path=="/api/bookmark":
                data=self.body_json(); sid=data.pop("session_id"); row={"kind":"manual","text":data.get("text",""),"start_wall":data.get("wall_time") or iso_now(),"end_wall":data.get("wall_time") or iso_now(),"t_session_start_sec":data.get("t_session_start_sec"),"t_session_end_sec":data.get("t_session_start_sec"),"server_received_at":iso_now(),"client_monotonic_ms":data.get("client_monotonic_ms")}; row["row_hash"]=row_hash(row); append_jsonl(session_dir(sid)/"transcript.jsonl", row); append_jsonl(session_dir(sid)/"events.jsonl", {"kind":"manual_bookmark", **row}); return self.send_payload(200,{"ok":True,"row_hash":row["row_hash"]})
            if path=="/api/chunk":
                qs=urllib.parse.parse_qs(parsed.query); sid=qs.get("session_id",[""])[0]; idx=int(qs.get("index",["0"])[0]); root=session_dir(sid); n=int(self.headers.get("content-length","0") or 0); payload=self.rfile.read(n); name=f"chunk_{idx:06d}.webm"; (root/"audio").mkdir(parents=True,exist_ok=True); (root/"audio"/name).write_bytes(payload); append_jsonl(root/"events.jsonl", {"kind":"audio_chunk","file":f"audio/{name}","bytes":len(payload),"client_wall":self.headers.get("x-client-wall"),"client_session_sec":self.headers.get("x-client-session-sec"),"server_received_at":iso_now(),"mime_type":self.headers.get("content-type")}); return self.send_payload(200,{"ok":True,"file":name,"bytes":len(payload)})
        except Exception as e: return self.send_payload(500,{"error":str(e)})
        return self.send_payload(404,{"error":"not found"})

def serve(bind: str, port: int, open_browser: bool) -> None:
    if bind not in {"127.0.0.1","localhost","::1"} and os.environ.get("BRICKPILOT_VOICE_ALLOW_NONLOCAL") != "1": raise SystemExit("Refusing non-local bind without BRICKPILOT_VOICE_ALLOW_NONLOCAL=1")
    SESSION_ROOT.mkdir(parents=True, exist_ok=True); httpd=ThreadingHTTPServer((bind,port), VoiceHandler); url=f"http://{bind}:{port}/"; print(f"Brickpilot voice bookmark app listening on {url} (local-only; raw audio stays under {SESSION_ROOT})")
    if open_browser: subprocess.Popen(["open", url])
    httpd.serve_forever()

BACKEND_ORDER = ("mlx-whisper", "whisper-cli", "whisper.cpp", "whisper-cpp", "whisper")
FINAL_BACKEND_ORDER = ("openai", *BACKEND_ORDER)
MODEL_ENV = ("BRICKPILOT_WHISPER_MODEL", "WHISPER_MODEL", "MLX_WHISPER_MODEL")
OPENAI_MODEL_ENV = ("BRICKPILOT_OPENAI_TRANSCRIBE_MODEL", "OPENAI_TRANSCRIBE_MODEL")
OPENAI_KEY_FILE_ENV = ("BRICKPILOT_OPENAI_API_KEY_FILE", "OPENAI_API_KEY_FILE")
DEFAULT_OPENAI_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
OPENAI_TRANSCRIBE_PROMPT = (
    "Brickpilot drive-labeling voice notes. Preserve short command-like labels. "
    "Important vocabulary: regen braking, re-gen, light regen, no regen, hard regen, "
    "brake, braking, parking brake, coasting, set speed, cruise, foot on gas, foot off gas, "
    "ultrasonic sensor, camera, lead car, comma, openpilot."
)


def _python_module_available(module: str) -> bool:
    try:
        return subprocess.run([sys.executable, "-c", f"import {module}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except Exception:
        return False


def _local_model_path(value: str | None) -> str | None:
    if not value: return None
    path = Path(value).expanduser()
    return str(path) if path.exists() else None

def _candidate_models() -> list[str]:
    seen: set[str] = set(); out: list[str] = []
    for name in MODEL_ENV:
        local = _local_model_path(os.environ.get(name))
        if local and local not in seen:
            seen.add(local); out.append(local)
    roots = [Path.home()/"models", Path.home()/".cache"/"whisper.cpp", Path.home()/".cache"/"whisper", Path.home()/"Library"/"Caches"/"whisper.cpp"]
    patterns = ("ggml-*.bin", "*.gguf", "*.bin")
    for root in roots:
        if not root.exists(): continue
        for model_dir in sorted(root.rglob("config.json"))[:50]:
            path = model_dir.parent
            s = str(path)
            if s not in seen:
                seen.add(s); out.append(s)
        for pat in patterns:
            for path in sorted(root.rglob(pat))[:50]:
                s = str(path)
                if s not in seen:
                    seen.add(s); out.append(s)
    return out


def _backend_inventory() -> dict[str, dict[str, Any]]:
    inv: dict[str, dict[str, Any]] = {}
    for name in BACKEND_ORDER:
        exe = shutil.which(name)
        inv[name] = {"available": bool(exe), "executable": exe}
    if not inv["mlx-whisper"]["available"]:
        inv["mlx-whisper"]["python_module"] = _python_module_available("mlx_whisper")
        inv["mlx-whisper"]["available"] = bool(inv["mlx-whisper"].get("python_module"))
    if not inv["whisper"]["available"]:
        inv["whisper"]["python_module"] = _python_module_available("whisper")
        inv["whisper"]["available"] = bool(inv["whisper"].get("python_module"))
    return inv


def _select_backend(preferred: str = "auto") -> tuple[str | None, dict[str, Any], dict[str, dict[str, Any]]]:
    inv = _backend_inventory()
    names = [preferred] if preferred != "auto" else list(BACKEND_ORDER)
    for name in names:
        info = inv.get(name)
        if info and info.get("available"):
            return name, info, inv
    return None, {}, inv


def _openai_api_key() -> str | None:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    for name in OPENAI_KEY_FILE_ENV:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return None


def _openai_model(model: str | None = None) -> str:
    if model:
        return model
    for name in OPENAI_MODEL_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return DEFAULT_OPENAI_TRANSCRIBE_MODEL


def _openai_base_url() -> str:
    base = (os.environ.get("BRICKPILOT_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    return base if base.endswith("/audio/transcriptions") else f"{base}/audio/transcriptions"


def _final_backend_inventory(model: str | None = None) -> dict[str, dict[str, Any]]:
    inv = _backend_inventory()
    inv["openai"] = {
        "available": bool(_openai_api_key()),
        "model": _openai_model(model),
        "endpoint": _openai_base_url(),
        "key_present": bool(_openai_api_key()),
    }
    return inv


def _select_final_backend(preferred: str = "auto", model: str | None = None) -> tuple[str | None, dict[str, Any], dict[str, dict[str, Any]]]:
    inv = _final_backend_inventory(model)
    names = [preferred] if preferred != "auto" else list(FINAL_BACKEND_ORDER)
    for name in names:
        info = inv.get(name)
        if info and info.get("available"):
            return name, info, inv
    return None, {}, inv

TRANSCRIBE_JOBS: dict[str, dict[str, Any]] = {}
TRANSCRIBE_LOCK = threading.Lock()


def _transcribe_job_snapshot(session_id: str) -> dict[str, Any] | None:
    with TRANSCRIBE_LOCK:
        job = TRANSCRIBE_JOBS.get(session_id)
        return dict(job) if job else None


def _set_transcribe_job(session_id: str, **fields: Any) -> dict[str, Any]:
    with TRANSCRIBE_LOCK:
        job = dict(TRANSCRIBE_JOBS.get(session_id) or {"session_id": session_id})
        job.update(fields)
        TRANSCRIBE_JOBS[session_id] = job
        return dict(job)


def api_transcribe_status(session: str | None = None, backend: str = "auto", model: str | None = None) -> dict[str, Any]:
    status = transcribe_status(session, backend=backend, model=model)
    sid = None
    if session:
        sdir = Path(session) if Path(session).exists() else session_dir(session)
        meta = read_json(sdir / "session.json", {}) or {}
        sid = meta.get("session_id") or sdir.name
        status["recording"] = not bool(meta.get("ended_at_wall")) if meta else None
    if sid:
        status["job"] = _transcribe_job_snapshot(sid)
    else:
        with TRANSCRIBE_LOCK:
            status["jobs"] = {k: dict(v) for k, v in TRANSCRIBE_JOBS.items()}
    return status


def api_transcribe_session(session: str | None, *, backend: str = "auto", model: str | None = None, language: str = "en", force: bool = False, timeout: int = 900, allow_running: bool = False) -> dict[str, Any]:
    if not session:
        return {"status":"error", "error":"session_id is required"}
    sdir = Path(session) if Path(session).exists() else session_dir(session)
    meta = read_json(sdir / "session.json", {}) or {}
    if not meta:
        return {"status":"error", "error":f"missing session.json under {sdir}"}
    sid = meta.get("session_id") or sdir.name
    if not meta.get("ended_at_wall") and not allow_running:
        payload = api_transcribe_status(str(sdir), backend=backend, model=model)
        payload.update({"status":"recording", "session_id":sid, "error":"Stop this recording before final transcription so the final audio chunks are flushed. Re-run after Stop Ride, or pass allow_running only for deliberate debugging."})
        return payload
    current = _transcribe_job_snapshot(sid)
    if current and current.get("state") == "running":
        return {"status":"busy", "session_id":sid, "job":current}
    _set_transcribe_job(sid, state="running", started_at=iso_now(), finished_at=None, message="Final transcription running…", progress={"phase":"transcribing"})
    try:
        result = transcribe_session(sid, backend=backend, model=model, language=language, force=force, timeout=timeout)
        _set_transcribe_job(sid, state="complete" if result.get("status") == "ok" else "complete_with_warnings", finished_at=iso_now(), message=f"Replaced final transcript with {result.get('appended_rows', 0)} cleaned row(s).", result=result, progress={"phase":"done"})
        return result | {"job": _transcribe_job_snapshot(sid)}
    except BaseException as e:
        _set_transcribe_job(sid, state="error", finished_at=iso_now(), message=str(e), error=str(e), progress={"phase":"error"})
        return {"status":"error", "session_id":sid, "error":str(e), "job":_transcribe_job_snapshot(sid)}


def _session_audio_events(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(root / "events.jsonl"):
        if row.get("kind") == "audio_chunk":
            name = row.get("file") or row.get("audio_file")
            if name:
                out[str(name)] = row
    return out


def _media_duration_sec(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe: return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip(): return max(0.0, float(r.stdout.strip()))
    except Exception:
        return None
    return None


def _ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        py = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
        r = subprocess.run([sys.executable, "-c", py], capture_output=True, text=True, timeout=5)
        path = r.stdout.strip()
        return path if r.returncode == 0 and path else None
    except Exception:
        return None


def _combined_audio_file(chunks: list[Path], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as out:
        for chunk in chunks:
            out.write(chunk.read_bytes())
    return output


def _openai_audio_segments(chunks: list[Path], work_dir: Path, segment_sec: float = 9.0) -> list[tuple[Path, float, float]]:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to remux browser WebM fragments for OpenAI transcription.")
    combined = _combined_audio_file(chunks, work_dir / "combined.webm")
    segment_dir = work_dir / "openai_segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    for old in segment_dir.glob("chunk_*.wav"):
        old.unlink()
    pattern = str(segment_dir / "chunk_%04d.wav")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(combined),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        str(segment_sec),
        "-reset_timestamps",
        "1",
        pattern,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffmpeg failed while preparing OpenAI transcription audio").strip())
    out: list[tuple[Path, float, float]] = []
    cursor = 0.0
    for segment in sorted(segment_dir.glob("chunk_*.wav")):
        duration = _media_duration_sec(segment) or segment_sec
        if duration <= 0.25:
            continue
        out.append((segment, cursor, cursor + duration))
        cursor += duration
    if not out:
        raise RuntimeError("ffmpeg produced no usable OpenAI transcription segments.")
    return out


def _clip_audio_file(src: Path, dest: Path, start_sec: float, end_sec: float, pad_sec: float = 0.15) -> Path:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to prepare aligned OpenAI transcription clips.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    clip_start = max(0.0, start_sec - pad_sec)
    clip_end = max(clip_start + 0.3, end_sec + pad_sec)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{clip_start:.3f}",
        "-to",
        f"{clip_end:.3f}",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffmpeg failed while preparing aligned OpenAI clip").strip())
    return dest


def _decode_audio_file(src: Path, dest: Path) -> Path:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to decode aligned OpenAI transcription audio.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffmpeg failed while decoding aligned OpenAI audio").strip())
    return dest


def _local_alignment_specs(chunks: list[Path], work_dir: Path, language: str, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected, info, inv = _select_backend("auto")
    if not selected:
        return [], {"alignment_backend": None, "alignment_error": "no local timing backend", "local_checked_backends": inv}
    models = _candidate_models()
    model = models[0] if models else None
    if not model:
        return [], {"alignment_backend": selected, "alignment_error": "no local timing model", "local_checked_backends": inv}
    combined = _combined_audio_file(chunks, work_dir / "combined.webm")
    result = _transcribe_with_backend(
        combined,
        selected,
        info,
        model,
        language,
        timeout,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    specs: list[dict[str, Any]] = []
    for seg in result.get("segments") or []:
        text = normalize_voice_label_text(str(seg.get("text") or ""))
        if not text:
            continue
        words = [w for w in (seg.get("words") or []) if _float_or_none(w.get("start")) is not None and _float_or_none(w.get("end")) is not None]
        if words:
            start = min(float(w["start"]) for w in words)
            end = max(float(w["end"]) for w in words)
        else:
            start = max(0.0, float(seg.get("start") or 0.0))
            end = max(start, float(seg.get("end") or start))
        start = max(0.0, start)
        end = max(start, end)
        reason = transcript_rejection_reason(text, [seg])
        if reason:
            continue
        specs.append({"text": text, "t_session_start_sec": start, "t_session_end_sec": end, "segments": [seg]})
    return _dedupe_row_specs(specs), {"alignment_backend": selected, "alignment_model": model, "local_checked_backends": inv, "combined_audio": str(combined)}


def _chunk_times(meta: dict[str, Any], audio_file: Path, event: dict[str, Any] | None) -> tuple[float | None, float | None, str | None, str | None]:
    duration = _media_duration_sec(audio_file)
    end_rel = None
    if event and (event.get("client_session_sec") not in (None, "") or event.get("t_session_sec") not in (None, "")):
        try: end_rel = float(event.get("client_session_sec") if event.get("client_session_sec") not in (None, "") else event.get("t_session_sec"))
        except (TypeError, ValueError): end_rel = None
    start_rel = max(0.0, end_rel - duration) if end_rel is not None and duration is not None else None
    end_wall = str(event.get("client_wall") or event.get("server_received_at")) if event else None
    start_wall = None
    end_dt = parse_time(end_wall)
    if end_dt and duration is not None:
        start_wall = (end_dt - dt.timedelta(seconds=duration)).isoformat()
    elif start_rel is not None and parse_time(meta.get("started_at_wall")):
        base = parse_time(meta.get("started_at_wall")); assert base is not None
        start_wall = (base + dt.timedelta(seconds=start_rel)).isoformat(); end_wall = (base + dt.timedelta(seconds=end_rel or start_rel)).isoformat()
    return start_rel, end_rel, start_wall, end_wall


def _run_json_command(cmd: list[str], timeout: int = 600) -> Any:
    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH','')}"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"command failed: {' '.join(cmd)}").strip())
    text = r.stdout.strip()
    if not text:
        return None
    return json.loads(text)


def _multipart_form(fields: list[tuple[str, str]], files: list[tuple[str, Path, str]]) -> tuple[bytes, str]:
    boundary = "----brickpilot-" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, path, content_type in files:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode())
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(path.read_bytes())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def _transcribe_with_openai(audio_file: Path, *, model: str, language: str, prompt: str, timeout: int) -> dict[str, Any]:
    key = _openai_api_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set; final cloud transcription is unavailable.")
    fields = [("model", model), ("response_format", "json")]
    if language:
        fields.append(("language", language))
    if prompt:
        fields.append(("prompt", prompt[-1800:]))
    content_type = "audio/webm" if audio_file.suffix.lower() == ".webm" else (mimetypes.guess_type(audio_file.name)[0] or "application/octet-stream")
    body, boundary = _multipart_form(fields, [("file", audio_file, content_type)])
    req = urllib.request.Request(
        _openai_base_url(),
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI transcription failed ({e.code}): {raw[:1000]}") from e
    data = json.loads(raw or "{}")
    return {"text": str(data.get("text") or "").strip(), "segments": data.get("segments") or [], "raw": data}


def normalize_voice_label_text(text: str) -> str:
    out = " ".join(str(text or "").strip().split())
    if not out:
        return ""
    replacements = [
        (r"\bparking\s+break\b", "parking brake"),
        (r"\bbreak(?=\s+(hard|light|medium|regen|region|braking|blend|pedal)\b)", "brake"),
        (r"\bcoast\s+sting\b", "Coasting"),
        (r"\bcoast\s+in\b", "Coasting"),
        (r"(?<!foot )\boff gas\b", "foot off gas"),
        (r"\bre[\s-]?gen\b", "regen"),
        (r"\bregions\b", "regens"),
        (r"\bregion\b", "regen"),
        (r"\bno\s+regen\s+braking\b", "no regen"),
        (r"\b(reverse|drive|cruise|set speed)\s+engage\b", r"\1 engaged"),
    ]
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    # Preserve normal sentence casing from ASR, but keep the domain token stable.
    out = re.sub(r"\bRegen\b", "regen", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out.strip()


def _phrase_parts(text: str) -> list[str]:
    parts: list[str] = []
    for part in re.split(r"[.!?;\n]+", text.lower()):
        clean = re.sub(r"[^a-z0-9 ]+", " ", part)
        clean = " ".join(clean.split())
        if clean:
            parts.append(clean)
    return parts


def _looks_repetitive(text: str) -> bool:
    parts = _phrase_parts(text)
    if len(parts) >= 5 and len(set(parts)) <= max(2, len(parts) // 6):
        return True
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < 18:
        return False
    for n in (2, 3, 4):
        grams = [" ".join(words[i:i+n]) for i in range(0, max(0, len(words) - n + 1))]
        if not grams:
            continue
        top = max(grams.count(g) for g in set(grams))
        if top >= 6 and top * n >= len(words) * 0.55:
            return True
    return False


def transcript_rejection_reason(text: str, segments: list[dict[str, Any]] | None = None) -> str | None:
    clean = " ".join(str(text or "").split())
    if not clean:
        return "blank"
    if _looks_repetitive(clean):
        return "repetitive_hallucination"
    for seg in segments or []:
        try:
            compression = float(seg.get("compression_ratio"))
        except (TypeError, ValueError):
            compression = 0.0
        try:
            no_speech = float(seg.get("no_speech_prob"))
        except (TypeError, ValueError):
            no_speech = 0.0
        try:
            avg_logprob = float(seg.get("avg_logprob"))
        except (TypeError, ValueError):
            avg_logprob = 0.0
        if compression and compression > 2.4:
            return "compression_failed"
        if no_speech >= 0.85:
            return "mostly_silence"
        if no_speech >= 0.65 and avg_logprob < -1.0:
            return "low_confidence_silence"
    return None


def _label_terms(text: str) -> set[str]:
    t = normalize_voice_label_text(text).lower()
    terms: set[str] = set()
    phrase_terms = {
        "foot_on_gas": ("foot on gas",),
        "foot_off_gas": ("foot off gas", "foot off", "off gas"),
        "set_speed": ("set speed",),
        "complete_stop": ("complete stop",),
        "parking_brake": ("parking brake",),
    }
    for term, phrases in phrase_terms.items():
        if any(phrase in t for phrase in phrases):
            terms.add(term)
    word_terms = {
        "regen": ("regen",),
        "brake": ("brake", "braking"),
        "gas": ("gas",),
        "ultrasonic": ("ultrasonic",),
        "sensor": ("sensor",),
        "camera": ("camera",),
        "parking": ("parking",),
        "reverse": ("reverse",),
        "drive": ("drive",),
        "cruise": ("cruise", "comma"),
        "coasting": ("coast", "coasting"),
        "ac": ("ac ", "a/c", "air conditioning"),
    }
    padded = f" {t} "
    for term, words in word_terms.items():
        if any(word in padded for word in words):
            terms.add(term)
    return terms


def reconcile_openai_label_text(timing_text: str, candidate_text: str) -> str:
    timing = normalize_voice_label_text(timing_text)
    candidate = normalize_voice_label_text(candidate_text)
    if not timing:
        return candidate
    if not candidate:
        return timing
    if timing.lower() == candidate.lower():
        return timing
    local_terms = _label_terms(timing)
    candidate_terms = _label_terms(candidate)
    if local_terms and candidate_terms and not (local_terms & candidate_terms):
        return timing
    if len(local_terms) == 1 and candidate_terms != local_terms:
        return timing
    if local_terms and len(candidate_terms - local_terms) >= 2:
        return timing
    if {"foot_on_gas", "foot_off_gas"} <= candidate_terms and not {"foot_on_gas", "foot_off_gas"} <= local_terms:
        return timing
    if len(candidate) < max(8, len(timing) * 0.55):
        return timing
    return candidate


def _dedupe_row_specs(row_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in sorted(row_specs, key=lambda r: float(r.get("t_session_start_sec") or 0.0)):
        text = str(spec.get("text") or "").strip()
        if not text:
            continue
        prev = out[-1] if out else None
        if prev:
            prev_text = str(prev.get("text") or "").strip().lower()
            start = float(spec.get("t_session_start_sec") or 0.0)
            prev_start = float(prev.get("t_session_start_sec") or 0.0)
            if text.lower() == prev_text and abs(start - prev_start) <= 5.0:
                continue
        out.append(spec)
    return out


def _rewrite_final_transcript_rows(transcript_path: Path, rows: list[dict[str, Any]]) -> int:
    old_rows = read_jsonl(transcript_path)
    kept = [row for row in old_rows if str(row.get("kind") or "").lower() != "final"]
    removed = len(old_rows) - len(kept)
    if removed and transcript_path.exists():
        backup = transcript_path.with_name(f"{transcript_path.name}.{utc_now().strftime('%Y%m%dT%H%M%SZ')}.bak")
        shutil.copy2(transcript_path, backup)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("w", encoding="utf-8") as f:
        for row in kept + rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    return removed


def _transcribe_with_backend(
    audio_file: Path,
    backend: str,
    info: dict[str, Any],
    model: str | None,
    language: str,
    timeout: int,
    *,
    word_timestamps: bool = False,
    condition_on_previous_text: bool = True,
) -> dict[str, Any]:
    if backend == "mlx-whisper":
        py = """
import json, sys, mlx_whisper
path, model, language, word_timestamps, condition_on_previous_text = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5] == "1"
kwargs = {"language": language} if language else {}
kwargs["condition_on_previous_text"] = condition_on_previous_text
if word_timestamps:
    kwargs["word_timestamps"] = True
if model:
    out = mlx_whisper.transcribe(path, path_or_hf_repo=model, **kwargs)
else:
    out = mlx_whisper.transcribe(path, **kwargs)
print(json.dumps(out, ensure_ascii=False, default=str))
"""
        data = _run_json_command([sys.executable, "-c", py, str(audio_file), model or "", language or "", "1" if word_timestamps else "0", "1" if condition_on_previous_text else "0"], timeout=timeout)
        return {"text": str((data or {}).get("text") or "").strip(), "segments": (data or {}).get("segments") or [], "raw": data}
    if backend in {"whisper-cli", "whisper.cpp", "whisper-cpp"}:
        exe = info.get("executable") or shutil.which(backend)
        if not model:
            raise RuntimeError(f"{backend} is installed but no local model was provided. Set BRICKPILOT_WHISPER_MODEL=/path/to/ggml-model.bin or pass --model.")
        outbase = audio_file.with_suffix(audio_file.suffix + ".whisper_tmp")
        cmd = [str(exe), "-m", model, "-f", str(audio_file), "-oj", "-of", str(outbase)]
        if language: cmd += ["-l", language]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0: raise RuntimeError((r.stderr or r.stdout).strip())
        jpath = Path(str(outbase) + ".json")
        data = json.loads(jpath.read_text()) if jpath.exists() else {"text": r.stdout.strip()}
        try: jpath.unlink()
        except OSError: pass
        text = data.get("transcription") or data.get("text") or " ".join(seg.get("text","") for seg in data.get("segments",[]))
        return {"text": str(text).strip(), "segments": data.get("segments") or [], "raw": data}
    if backend == "whisper":
        py = """
import json, sys, whisper
path, model, language, word_timestamps, condition_on_previous_text = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5] == "1"
name = model
if not name:
    raise SystemExit('openai-whisper backend requires --model or WHISPER_MODEL so no model is downloaded implicitly')
m = whisper.load_model(name)
kwargs = {"language": language} if language else {}
kwargs["condition_on_previous_text"] = condition_on_previous_text
if word_timestamps:
    kwargs["word_timestamps"] = True
out = m.transcribe(path, **kwargs)
print(json.dumps(out, ensure_ascii=False, default=str))
"""
        data = _run_json_command([sys.executable, "-c", py, str(audio_file), model or "", language or "", "1" if word_timestamps else "0", "1" if condition_on_previous_text else "0"], timeout=timeout)
        return {"text": str((data or {}).get("text") or "").strip(), "segments": (data or {}).get("segments") or [], "raw": data}
    raise RuntimeError(f"unsupported backend {backend}")


REALTIME_LOCK = threading.Lock()

def realtime_preview(session: str | None, *, backend: str = "auto", model: str | None = None, language: str = "en", timeout: int = 45) -> dict[str, Any]:
    if not session:
        return {"status":"error", "error":"session_id is required"}
    sdir = Path(session) if Path(session).exists() else session_dir(session)
    meta = read_json(sdir / "session.json", {}) or {}
    if not meta:
        return {"status":"error", "error":f"missing session.json under {sdir}"}
    chunks = sorted((sdir / "audio").glob("*.webm")) if (sdir / "audio").exists() else []
    if not chunks:
        return {"status":"waiting", "message":"Waiting for first saved audio chunks…", "audio_chunks":0, "local_only":True}
    selected, info, inv = _select_backend(backend)
    if not selected:
        return transcribe_status(str(sdir), backend=backend, model=model) | {"status":"no_backend", "audio_chunks":len(chunks), "local_only":True}
    if model:
        model = _local_model_path(model)
        if not model:
            return transcribe_status(str(sdir), backend=backend, model=None) | {"status":"model_not_found", "audio_chunks":len(chunks), "local_only":True}
    else:
        models = _candidate_models(); model = models[0] if models else None
    if not model:
        return transcribe_status(str(sdir), backend=backend, model=model) | {"status":"model_required", "audio_chunks":len(chunks), "local_only":True}
    if not REALTIME_LOCK.acquire(blocking=False):
        return {"status":"busy", "message":"Local realtime model is still processing the previous chunk window…", "audio_chunks":len(chunks), "backend":selected, "local_only":True}
    try:
        work = sdir / ".realtime"; work.mkdir(exist_ok=True)
        combined = work / "realtime_preview.webm"
        with combined.open("wb") as out:
            for chunk in chunks:
                out.write(chunk.read_bytes())
        result = _transcribe_with_backend(combined, selected, info, model, language, timeout)
        text = str(result.get("text") or "").strip()
        payload = {"status":"ok", "session_id":meta.get("session_id") or sdir.name, "text":text, "audio_chunks":len(chunks), "backend":selected, "model":model, "local_only":True, "updated_at":iso_now()}
        (work / "realtime_preview.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    except subprocess.TimeoutExpired:
        return {"status":"timeout", "message":"Local realtime model is lagging; recording continues and high-quality Transcribe remains available after stop.", "audio_chunks":len(chunks), "backend":selected, "local_only":True}
    except Exception as e:
        return {"status":"error", "error":str(e), "audio_chunks":len(chunks), "backend":selected, "local_only":True}
    finally:
        REALTIME_LOCK.release()

def transcribe_status(session: str | None = None, backend: str = "auto", model: str | None = None) -> dict[str, Any]:
    final_selected, final_info, final_inv = _select_final_backend(backend, model=model)
    selected, info, inv = _select_backend("auto" if backend == "openai" else backend)
    models = _candidate_models()
    sdir = (Path(session) if session and Path(session).exists() else session_dir(session)) if session else None
    audio = sorted((sdir / "audio").glob("*.webm")) if sdir and (sdir / "audio").exists() else []
    guidance = []
    if not final_selected:
        guidance.append("Set OPENAI_API_KEY for the preferred high-quality final pass, or install a local backend: `pipx install mlx-whisper` (Apple Silicon) or install whisper.cpp/whisper-cli plus a local ggml model.")
    model_local = _local_model_path(model) if model else None
    if model and backend != "openai" and final_selected != "openai" and not model_local:
        guidance.append("The supplied --model/env model is not an existing local path. Refusing model names/IDs so Whisper backends cannot auto-download. Use a downloaded local model file/directory path.")
    if final_selected and final_selected != "openai" and not (model_local or models):
        guidance.append("A local model is required so this command never downloads models during a drive workflow. Pass --model or set BRICKPILOT_WHISPER_MODEL / WHISPER_MODEL / MLX_WHISPER_MODEL to an already-present local model path.")
    if final_selected == "openai":
        status = "ready"
        status_model = final_info.get("model")
    elif final_selected and (model_local or models):
        status = "ready"
        status_model = model_local or (models[0] if models else model)
    else:
        status = "model_not_found" if final_selected and model else ("model_required" if final_selected else "no_backend")
        status_model = model_local or model
    message = "Final Transcribe will use OpenAI gpt-4o transcription when OPENAI_API_KEY is present; realtime preview remains local-only/offline." if final_selected == "openai" else ("Final Transcribe will use the best available local backend; realtime preview remains local-only/offline." if final_selected else "No final transcription backend found; existing audio/manual bookmarks are safe.")
    return {"status":status, "selected_backend":final_selected, "backend_info":final_info, "checked_backends":final_inv, "local_preview_backend":selected, "local_preview_backend_info":info, "local_checked_backends":inv, "candidate_models":models[:10], "model":status_model, "session":str(sdir) if sdir else session, "audio_chunks":len(audio), "message":message, "guidance":guidance}


def transcribe_session(session: str, *, backend: str = "auto", model: str | None = None, language: str = "en", force: bool = False, timeout: int = 900) -> dict[str, Any]:
    sdir = Path(session) if Path(session).exists() else session_dir(session)
    meta = read_json(sdir / "session.json", {}) or {}
    if not meta:
        return {"status":"error", "session_id":sdir.name, "session_path":str(sdir), "error":f"missing session.json under {sdir}", "appended_rows":0, "skipped_chunks":0, "error_count":1}
    audio_dir = sdir / "audio"; chunks = sorted(audio_dir.glob("*.webm")) if audio_dir.exists() else []
    if not chunks:
        return {"status":"no_audio", "session_id":meta.get("session_id") or sdir.name, "session_path":str(sdir), "error":f"no audio chunks under {audio_dir}", "audio_chunks":0, "appended_rows":0, "skipped_chunks":0, "error_count":0}
    selected, info, inv = _select_final_backend(backend, model=model)
    if not selected:
        return transcribe_status(str(sdir), backend=backend, model=model) | {"transcribed_chunks":0, "appended_rows":0}

    chosen_model = _openai_model(model) if selected == "openai" else model
    if selected != "openai":
        if chosen_model:
            chosen_model = _local_model_path(chosen_model)
            if not chosen_model:
                return transcribe_status(str(sdir), backend=backend, model=None) | {"status":"model_not_found", "transcribed_chunks":0, "appended_rows":0}
        else:
            models = _candidate_models(); chosen_model = models[0] if models else None
        if not chosen_model:
            return transcribe_status(str(sdir), backend=backend, model=chosen_model) | {"transcribed_chunks":0, "appended_rows":0}

    skipped = errors = filtered = transcribed_chunks = 0
    error_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    row_specs: list[dict[str, Any]] = []
    alignment_info: dict[str, Any] = {}
    start_wall = meta.get("started_at_wall"); end_wall = meta.get("ended_at_wall"); duration = meta.get("duration_sec")
    base_wall = parse_time(start_wall)
    work_dir = sdir / ".transcribe"; work_dir.mkdir(exist_ok=True)

    try:
        if selected == "openai":
            prompt_tail = ""
            alignment_specs, alignment_info = _local_alignment_specs(chunks, work_dir, language, min(timeout, 900))
            if alignment_specs:
                combined_path = _decode_audio_file(Path(str(alignment_info.get("combined_audio") or work_dir / "combined.webm")), work_dir / "combined_16k.wav")
                clip_dir = work_dir / "openai_aligned"
                for old in clip_dir.glob("clip_*.wav") if clip_dir.exists() else []:
                    old.unlink()
                openai_inputs = []
                for idx, spec in enumerate(alignment_specs):
                    start_rel = float(spec.get("t_session_start_sec") or 0.0)
                    end_rel = float(spec.get("t_session_end_sec") or start_rel)
                    clip = _clip_audio_file(combined_path, clip_dir / f"clip_{idx:04d}.wav", start_rel, end_rel)
                    openai_inputs.append((clip, start_rel, end_rel, "local_alignment", spec))
            else:
                openai_inputs = [(audio_file, start_rel, end_rel, "fixed_window", {}) for audio_file, start_rel, end_rel in _openai_audio_segments(chunks, work_dir)]
            for audio_file, start_rel, end_rel, alignment_strategy, timing_spec in openai_inputs:
                sw = ew = None
                if sw is None and base_wall:
                    sw = (base_wall + dt.timedelta(seconds=start_rel)).isoformat()
                if ew is None and base_wall:
                    ew = (base_wall + dt.timedelta(seconds=end_rel)).isoformat()
                prompt = OPENAI_TRANSCRIBE_PROMPT
                if prompt_tail:
                    prompt += " Previous labels: " + prompt_tail[-500:]
                result = _transcribe_with_openai(audio_file, model=str(chosen_model), language=language, prompt=prompt, timeout=min(timeout, 120))
                transcribed_chunks += 1
                openai_text = normalize_voice_label_text(str(result.get("text") or ""))
                timing_text = str(timing_spec.get("text") or "")
                text = reconcile_openai_label_text(timing_text, openai_text)
                reason = transcript_rejection_reason(text, result.get("segments") or [])
                if reason:
                    filtered += 1; rejected_rows.append({"audio_file":str(audio_file.relative_to(sdir)) if sdir in audio_file.parents else str(audio_file),"text":text,"reason":reason,"t_session_start_sec":start_rel,"t_session_end_sec":end_rel}); continue
                row_specs.append({"text":text,"t_session_start_sec":start_rel,"t_session_end_sec":end_rel,"start_wall":sw,"end_wall":ew,"audio_file":str(audio_file.relative_to(sdir)) if sdir in audio_file.parents else str(audio_file),"segments":result.get("segments") or [],"raw_usage":(result.get("raw") or {}).get("usage"),"alignment_strategy":alignment_strategy,"alignment_backend":alignment_info.get("alignment_backend"),"alignment_model":alignment_info.get("alignment_model"),"timing_text":timing_text,"openai_text":openai_text})
                prompt_tail = (prompt_tail + " " + text).strip()
        else:
            combined = work_dir / "combined.webm"
            with combined.open("wb") as out:
                for chunk in chunks:
                    out.write(chunk.read_bytes())
            result = _transcribe_with_backend(combined, selected, info, str(chosen_model) if chosen_model else None, language, timeout)
            transcribed_chunks = len(chunks)
            segments = result.get("segments") or []
            if segments:
                for seg in segments:
                    text = normalize_voice_label_text(str(seg.get("text") or ""))
                    if not text:
                        skipped += 1; continue
                    a = float(seg.get("start") or 0.0); b = float(seg.get("end") or a)
                    reason = transcript_rejection_reason(text, [seg])
                    if reason:
                        filtered += 1; rejected_rows.append({"audio_file":".transcribe/combined.webm","text":text,"reason":reason,"t_session_start_sec":a,"t_session_end_sec":b}); continue
                    row_specs.append({"text":text,"t_session_start_sec":a,"t_session_end_sec":b,"start_wall":(base_wall + dt.timedelta(seconds=a)).isoformat() if base_wall else None,"end_wall":(base_wall + dt.timedelta(seconds=b)).isoformat() if base_wall else None,"audio_file":".transcribe/combined.webm","segments":[seg]})
            else:
                text = normalize_voice_label_text(str(result.get("text") or ""))
                reason = transcript_rejection_reason(text, [])
                if reason:
                    filtered += 1; rejected_rows.append({"audio_file":".transcribe/combined.webm","text":text,"reason":reason,"t_session_start_sec":0.0,"t_session_end_sec":float(duration) if duration is not None else None})
                elif text:
                    row_specs.append({"text":text,"t_session_start_sec":0.0,"t_session_end_sec":float(duration) if duration is not None else None,"start_wall":start_wall,"end_wall":end_wall,"audio_file":".transcribe/combined.webm","segments":[]})
                else:
                    skipped += 1
    except Exception as e:
        errors += 1; error_rows.append({"audio_file":"audio/*.webm" if selected == "openai" else ".transcribe/combined.webm","error":str(e)[:1000]})

    rows: list[dict[str, Any]] = []
    for spec in _dedupe_row_specs(row_specs):
        row = {"kind":"final","source":"final_transcribe","text":spec.get("text"),"start_wall":spec.get("start_wall"),"end_wall":spec.get("end_wall"),"t_session_start_sec":spec.get("t_session_start_sec"),"t_session_end_sec":spec.get("t_session_end_sec"),"server_received_at":iso_now(),"audio_file":spec.get("audio_file"),"audio_chunks":len(chunks),"backend":selected,"model":chosen_model,"language":language,"confidence":None,"local_only":selected!="openai","transcription_pass":"final"}
        if spec.get("segments"): row["segments"] = spec["segments"]
        if spec.get("raw_usage"): row["usage"] = spec["raw_usage"]
        if spec.get("alignment_strategy"): row["alignment_strategy"] = spec["alignment_strategy"]
        if spec.get("alignment_backend"): row["alignment_backend"] = spec["alignment_backend"]
        if spec.get("alignment_model"): row["alignment_model"] = spec["alignment_model"]
        if spec.get("timing_text"): row["timing_text"] = spec["timing_text"]
        if spec.get("openai_text"): row["openai_text"] = spec["openai_text"]
        row["row_hash"] = row_hash(row)
        rows.append(row)

    replaced_rows = 0
    if rows and not errors:
        replaced_rows = _rewrite_final_transcript_rows(sdir / "transcript.jsonl", rows)
        patch = {"needs_transcription":False,"final_transcribed_at":iso_now(),"final_transcription_backend":selected,"final_transcription_model":chosen_model}
        if selected != "openai":
            patch.update({"offline_transcribed_at":patch["final_transcribed_at"],"offline_transcription_backend":selected})
        update_session(meta.get("session_id") or sdir.name, **patch)
    if rejected_rows:
        (work_dir / "rejected_segments.jsonl").write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rejected_rows), encoding="utf-8")
    status = "ok" if rows and not errors else ("partial" if rows else ("error" if errors else "empty"))
    return {"status":status, "session_id":meta.get("session_id") or sdir.name, "session_path":str(sdir), "backend":selected, "model":chosen_model, "audio_chunks":len(chunks), "transcribed_chunks":transcribed_chunks, "appended_rows":len(rows), "replaced_rows":replaced_rows, "skipped_chunks":skipped + filtered, "filtered_rows":filtered, "error_count":errors, "errors":error_rows, "checked_backends":inv, "alignment":alignment_info, "message":f"Final transcript replaced with {len(rows)} cleaned row(s) using {selected}." if rows and not errors else (error_rows[0]["error"] if error_rows else "No usable speech rows found.")}

def make_fake_session(text: str = "manual note brake was jerky but turn felt good") -> dict[str, Any]:
    meta=start_session("label validation smoke"); sid=meta["session_id"]; root=session_dir(sid); start=parse_time(meta["started_at_wall"]) or utc_now(); row={"kind":"manual","text":text,"start_wall":(start+dt.timedelta(seconds=3)).isoformat(),"end_wall":(start+dt.timedelta(seconds=5)).isoformat(),"t_session_start_sec":3.0,"t_session_end_sec":5.0,"confidence":1.0,"server_received_at":iso_now()}; row["row_hash"]=row_hash(row); append_jsonl(root/"transcript.jsonl", row); (root/"audio"/"chunk_000000.webm").write_bytes(b"fake-webm-smoke-local-only\n"); stop_session(sid); return {"session_id":sid,"path":str(root)}

def main(argv: list[str] | None = None) -> int:
    ap=argparse.ArgumentParser(description="Local Brickpilot voice-bookmark recorder and DriveStore importer."); sub=ap.add_subparsers(dest="cmd", required=True)
    sp=sub.add_parser("serve"); sp.add_argument("--bind", default=DEFAULT_BIND); sp.add_argument("--port", type=int, default=DEFAULT_PORT); sp.add_argument("--no-open", action="store_true")
    ip=sub.add_parser("import"); ip.add_argument("--session", required=True); ip.add_argument("--route"); ip.add_argument("--review-job-id", type=int); ip.add_argument("--offset-sec", type=float, default=0.0); ip.add_argument("--config")
    tp=sub.add_parser("transcribe", description="Final post-ride transcription for recorded audio chunks. Uses OpenAI gpt-4o transcription when OPENAI_API_KEY is present; realtime preview remains local-only."); tp.add_argument("--session"); tp.add_argument("--backend", default="auto", choices=["auto", *FINAL_BACKEND_ORDER]); tp.add_argument("--model", help="OpenAI model name for --backend openai, or existing local model path for local backends."); tp.add_argument("--language", default="en"); tp.add_argument("--force", action="store_true", help="Compatibility flag; final transcript rows are replaced by default"); tp.add_argument("--status", action="store_true", help="Only show backend/session readiness, do not transcribe"); tp.add_argument("--timeout", type=int, default=900)
    rp=sub.add_parser("realtime-preview", description="Run one local-only realtime preview pass over the current session audio chunks."); rp.add_argument("--session", required=True); rp.add_argument("--backend", default="auto", choices=["auto", *BACKEND_ORDER]); rp.add_argument("--model"); rp.add_argument("--language", default="en"); rp.add_argument("--timeout", type=int, default=45)
    fp=sub.add_parser("fake-session"); fp.add_argument("--text", default="manual note brake was jerky but turn felt good")
    sm=sub.add_parser("smoke-import"); sm.add_argument("--route"); sm.add_argument("--review-job-id", type=int); sm.add_argument("--config"); sm.add_argument("--offset-sec", type=float, default=0.0)
    args=ap.parse_args(argv)
    if args.cmd=="serve": serve(args.bind,args.port,not args.no_open); return 0
    if args.cmd=="import": print(json.dumps(import_voice_session(args.session, route=args.route, review_job_id=args.review_job_id, offset_sec=args.offset_sec, config=args.config), indent=2, sort_keys=True)); return 0
    if args.cmd=="transcribe":
        payload = transcribe_status(args.session, backend=args.backend, model=args.model) if args.status or not args.session else transcribe_session(args.session, backend=args.backend, model=args.model, language=args.language, force=args.force, timeout=args.timeout)
        print(json.dumps(payload, indent=2, sort_keys=True)); return 0
    if args.cmd=="realtime-preview":
        print(json.dumps(realtime_preview(args.session, backend=args.backend, model=args.model, language=args.language, timeout=args.timeout), indent=2, sort_keys=True)); return 0
    if args.cmd=="fake-session": print(json.dumps(make_fake_session(args.text), indent=2, sort_keys=True)); return 0
    if args.cmd=="smoke-import":
        sess=make_fake_session(); print(json.dumps({"fake_session":sess,"import":import_voice_session(sess["session_id"], route=args.route, review_job_id=args.review_job_id, offset_sec=args.offset_sec, config=args.config)}, indent=2, sort_keys=True)); return 0
    return 2
if __name__ == "__main__": raise SystemExit(main())
