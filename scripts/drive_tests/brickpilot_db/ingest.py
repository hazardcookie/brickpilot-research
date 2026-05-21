from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, re, time
from collections import Counter, defaultdict
from statistics import median
from pathlib import Path
from .config import load_config
from .store import DriveStore

ROUTE_RE = re.compile(r"(?P<route>[0-9a-fA-F]{8,}--[0-9a-fA-F]{8,}|[0-9a-fA-F]{8,}--[^/]+)")
SAFE_LOGDRIVE_ROUTE_RE = re.compile(r"(?P<dongle>[0-9a-fA-F]{8,})_(?P<ts>\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2})")
SEG_RE = re.compile(r"(?:^|--|/)(?P<seg>\d+)(?:$|--|/)")
BULK_SUFFIXES = {".bz2", ".zst", ".hevc", ".ts", ".mp4", ".json", ".jsonl", ".csv", ".md", ".sqlite", ".db", ".txt", ".m4a", ".wav", ".mp3"}
SENSITIVE_PARAM_NAMES = ("token", "secret", "password", "private", "ssh", "athena", "jwt", "prime", "github")

def route_from_path(path: Path) -> str | None:
    for part in path.parts[::-1]:
        m = ROUTE_RE.search(part)
        if m: return m.group("route")
        m = SAFE_LOGDRIVE_ROUTE_RE.search(part)
        if m: return f"{m.group('dongle')}|{m.group('ts')}"
    return None

def segment_from_path(path: Path) -> int | None:
    # Segment directories are named either "<route>--<segment>" or just "<segment>".
    # Do not treat the route prefix itself (for example 00000152 in
    # 00000152--09ae941a33) as a segment number. That bug put every imported
    # 00000152 segment at t=9120s and left the DB-backed labeler timeline blank.
    for part in path.parts[::-1]:
        if part.isdigit():
            return int(part)
        m = re.search(r"--(?P<seg>\d+)$", part)
        if m:
            return int(m.group("seg"))
    return None

def interesting_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file(): continue
        low = p.name.lower()
        if p.suffix.lower() in BULK_SUFFIXES or any(x in low for x in ("qlog", "rlog", "qcamera", "fcamera", "ecamera", "drive_jobs", "drive_data", "labels", "bookmark", "report", "clip")):
            yield p

def inventory(root: Path) -> dict:
    files = list(interesting_files(root)); by_kind = Counter(); bytes_by_kind = Counter(); routes = defaultdict(lambda: {"files":0,"bytes":0,"segments":set(),"kinds":Counter()})
    for p in files:
        kind = DriveStore.kind_for_path(p); sz = p.stat().st_size; by_kind[kind]+=1; bytes_by_kind[kind]+=sz
        rid = route_from_path(p) or "unknown"
        routes[rid]["files"] += 1; routes[rid]["bytes"] += sz; routes[rid]["kinds"][kind]+=1
        seg = segment_from_path(p)
        if seg is not None: routes[rid]["segments"].add(seg)
    return {"root": str(root), "file_count": len(files), "bytes": sum(bytes_by_kind.values()), "kinds": dict(by_kind), "bytes_by_kind": dict(bytes_by_kind), "routes": {k:{"files":v["files"],"bytes":v["bytes"],"segments":sorted(v["segments"]),"kinds":dict(v["kinds"])} for k,v in routes.items()}}

def _iter_json_records(path: Path):
    try:
        if path.suffix.lower() == ".jsonl":
            for line in path.read_text(errors="replace").splitlines():
                if line.strip(): yield json.loads(line)
        else:
            data = json.loads(path.read_text(errors="replace"))
            if isinstance(data, list): yield from [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for key in ("jobs", "labels", "bookmarks", "events", "clips", "drive_jobs"):
                    if isinstance(data.get(key), list):
                        for item in data[key]:
                            if isinstance(item, dict): yield {"_collection": key, **item}
                yield data
    except Exception:
        return

def _identity(prefix: str, payload: dict) -> str:
    return hashlib.sha256((prefix + json.dumps(payload, sort_keys=True, default=str)).encode()).hexdigest()

def route_hint_from_record(rec: dict) -> str | None:
    for key in ("route_id", "route", "canonical_route", "canonical_name", "openpilot_route"):
        value = rec.get(key)
        if isinstance(value, dict):
            nested = route_hint_from_record(value)
            if nested:
                return nested
        if isinstance(value, str) and ROUTE_RE.search(value):
            return ROUTE_RE.search(value).group("route")
    for key in ("path", "source_path", "video_path", "video_url", "log_path", "clip", "clip_path"):
        value = rec.get(key)
        if isinstance(value, str):
            hinted = route_from_path(Path(value))
            if hinted: return hinted
    return None

def route_from_metadata_file(path: Path) -> str | None:
    counts = Counter()
    for rec in _iter_json_records(path):
        hint = route_hint_from_record(rec)
        if hint: counts[hint] += 1
    return counts.most_common(1)[0][0] if counts else None

def _insert_ignore(store: DriveStore, sql: str, params: tuple):
    store.execute((sql + " ON CONFLICT DO NOTHING") if store.dialect == "postgres" else sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), params)

def _tags_param(store: DriveStore, tags) -> object:
    if tags is None:
        tags = []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, (list, tuple)):
        tags = [str(tags)]
    tags = [str(t) for t in tags]
    return tags if store.dialect == "postgres" else json.dumps(tags)

def _json_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}

def _settings_text_params(manifest: dict) -> dict[str, str]:
    params = manifest.get("params")
    if isinstance(params, dict):
        return {str(k): str(v) for k, v in params.items() if v is not None}
    out: dict[str, str] = {}
    raw = manifest.get("params_raw")
    if isinstance(raw, dict):
        for key, entry in raw.items():
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                out[str(key)] = str(entry["text"])
    return out

def _param_text(value) -> str:
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="replace").strip()
        except Exception:
            return repr(value)
    else:
        text = str(value).strip()
    return "[binary]" if "\x00" in text else text

def _software_from_settings_manifest(manifest: dict, settings: dict[str, str]) -> dict[str, str]:
    software = {str(k): str(v) for k, v in _json_obj(manifest.get("software")).items() if v not in (None, "")}
    mapping = {
        "branch": ("GitBranch", "Branch"),
        "commit": ("GitCommit", "Commit"),
        "version": ("BrickpilotVersion", "CustomBrandVersion", "BRICKPILOT_LONGITUDINAL_VERSION"),
        "remote": ("GitRemote",),
    }
    for target, keys in mapping.items():
        if software.get(target):
            continue
        for key in keys:
            if settings.get(key):
                software[target] = settings[key]
                break
    if software.get("brickpilot_version") and not software.get("version"):
        software["version"] = software["brickpilot_version"]
    if software.get("version") and not software.get("brickpilot_version"):
        software["brickpilot_version"] = software["version"]
    if settings.get("Version") and not software.get("openpilot_version"):
        software["openpilot_version"] = settings["Version"]
    brand = software.get("brand") or "Brickpilot"
    if software.get("brickpilot_version") and not software.get("display_version"):
        software["display_version"] = f"{brand} {software['brickpilot_version']}"
    return software

def _model_bundle_from_settings(settings: dict[str, str]) -> str | None:
    for key in ("ModelManager_ActiveBundle", "ModelManager_ActiveModel", "ModelManager_Selector", "ModelRunner", "Model"):
        raw = settings.get(key)
        if not raw or raw == "[redacted]":
            continue
        obj = _json_obj(raw)
        if obj:
            for field in ("displayName", "internalName", "name", "model", "fileName"):
                value = obj.get(field)
                if value:
                    return str(value)
            models = obj.get("models")
            if isinstance(models, list) and models:
                first = models[0]
                if isinstance(first, dict):
                    artifact = first.get("artifact")
                    if isinstance(artifact, dict) and artifact.get("fileName"):
                        return str(artifact["fileName"])
        return str(raw)[:160]
    return None

def _route_label_from_settings(route_id: str, software: dict[str, str], model_bundle: str | None) -> str:
    model = (model_bundle or "").strip()
    version = (software.get("display_version") or "").strip()
    if not version and (software.get("brickpilot_version") or software.get("version")):
        version = f"{software.get('brand') or 'Brickpilot'} {software.get('brickpilot_version') or software.get('version')}".strip()
    branch = (software.get("branch") or "").strip()
    parts = [route_id]
    if model:
        parts.append(model[:52])
    if version:
        parts.append(version[:32])
    elif branch:
        parts.append(branch[:42])
    return " | ".join(parts)

def _apply_settings_manifest(store: DriveStore, path: Path, route_uuid: str, artifact_id: int, manifest: dict) -> Counter:
    counts = Counter()
    next_settings = _settings_text_params(manifest)
    next_software = _software_from_settings_manifest(manifest, next_settings)
    source = str(manifest.get("source") or "settings_manifest")
    raw_params = manifest.get("params_raw")
    raw_param_count = len(raw_params) if isinstance(raw_params, dict) else 0
    route_row = store.one("SELECT route_id, canonical_name, route_label, metadata_jsonb FROM routes WHERE id=?", (route_uuid,))
    route_data = dict(route_row) if route_row else {}
    route_id = str(route_data.get("route_id") or manifest.get("route_id") or route_uuid)
    current_label = str(route_data.get("route_label") or "")
    canonical_name = str(route_data.get("canonical_name") or "")
    metadata = _json_obj(route_data.get("metadata_jsonb"))
    existing_settings = {str(k): str(v) for k, v in _json_obj(metadata.get("settings")).items() if v is not None}
    existing_software = {str(k): str(v) for k, v in _json_obj(metadata.get("software")).items() if v not in (None, "")}
    if source == "web_ingest_remote_settings" and metadata.get("settings_source") == "log_init_data":
        settings = {**next_settings, **existing_settings}
    else:
        settings = {**existing_settings, **next_settings}
    software = {**existing_software, **next_software}
    model_bundle = _model_bundle_from_settings(settings) or software.get("model_bundle")
    if model_bundle:
        software["model_bundle"] = str(model_bundle)
    metadata["settings"] = settings
    metadata["software"] = software
    if source == "log_init_data":
        metadata["settings_source"] = "log_init_data"
    metadata["settings_manifest"] = {
        "artifact_id": artifact_id,
        "source_path": str(path),
        "source": source,
        "status": manifest.get("status") or ("ok" if settings or raw_param_count else "empty"),
        "captured_at": manifest.get("captured_at"),
        "params_dir": manifest.get("params_dir"),
        "settings_count": len(settings),
        "raw_param_count": raw_param_count,
        "error": manifest.get("error"),
    }
    if manifest.get("ride_type"):
        metadata["ride_type"] = manifest.get("ride_type")
    should_name = not current_label or current_label in {route_id, canonical_name} or current_label.startswith(route_id)
    next_label = _route_label_from_settings(route_id, software, model_bundle) if should_name else None
    store.execute(
        """UPDATE routes
           SET branch=COALESCE(?, branch),
               brickpilot_version=COALESCE(?, brickpilot_version),
               model_bundle=COALESCE(?, model_bundle),
               route_label=COALESCE(?, route_label),
               metadata_jsonb=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            software.get("branch") or None,
            software.get("version") or None,
            model_bundle or None,
            next_label,
            store._json(metadata),
            route_uuid,
        ),
    )
    counts["settings_manifests"] += 1
    if settings:
        counts["settings_params"] += len(settings)
    if raw_param_count:
        counts["settings_raw_params"] += raw_param_count
    return counts

def parse_metadata_file(store: DriveStore, path: Path, route_uuid: str, artifact_id: int, route_for_id=None) -> dict[str, int]:
    counts = Counter()
    low = path.name.lower()
    if not (low.endswith((".json", ".jsonl")) or "drive_jobs" in low or "drive_data" in low or "label" in low or "bookmark" in low): return {}
    if "settings_manifest" in low:
        manifest = _json_obj(path.read_text(errors="replace"))
        if manifest:
            counts.update(_apply_settings_manifest(store, path, route_uuid, artifact_id, manifest))
    inbox_id = None
    for rec in _iter_json_records(path):
        coll = rec.get("_collection", "")
        if "drive_jobs" in low or coll in ("jobs", "drive_jobs") or rec.get("job_id") or rec.get("legacy_job_id"):
            inbox_id = inbox_id or store.create_inbox(path.parent.name or "imported", "imported manual labeler/review jobs")
            rec_route_uuid = route_for_id(route_hint_from_record(rec)) if route_for_id and route_hint_from_record(rec) else route_uuid
            legacy = str(rec.get("legacy_job_id") or rec.get("job_id") or rec.get("id") or _identity("job", rec)[:16])
            store.upsert_review_job(inbox_id, rec_route_uuid, legacy, status=rec.get("status", "pending"), selected=rec.get("selected"), ride_type=rec.get("ride_type"), route_label=rec.get("route_label"), sort_order=rec.get("sort_order")); counts["review_jobs"] += 1
        if "label" in low or coll == "labels" or rec.get("label") or rec.get("label_kind"):
            rec_route_uuid = route_for_id(route_hint_from_record(rec)) if route_for_id and route_hint_from_record(rec) else route_uuid
            ident = rec.get("label_identity_hash") or _identity("label", {**rec, "route_uuid": rec_route_uuid})
            _insert_ignore(store, "INSERT INTO labels(route_uuid,review_job_id,label_identity_hash,label_kind,label,severity,start_sec,end_sec,notes,tags,created_by,metadata_jsonb) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (rec_route_uuid, rec.get("review_job_id"), ident, rec.get("label_kind") or rec.get("kind"), rec.get("label") or rec.get("text"), rec.get("severity"), rec.get("start_sec") or rec.get("t_sec") or rec.get("t"), rec.get("end_sec") or rec.get("end_t") or rec.get("end_t_sec"), rec.get("notes"), _tags_param(store, rec.get("tags", [])), "importer", store._json({"source_artifact_id": artifact_id, "raw": rec})))
            store.insert_label_event(rec.get("review_job_id"), "import_label", rec, None, "importer"); counts["labels"] += 1
        if "bookmark" in low or coll in ("bookmarks", "events") or rec.get("bookmark") or rec.get("reason") or (rec.get("t_sec") is not None and not rec.get("label")):
            rec_route_uuid = route_for_id(route_hint_from_record(rec)) if route_for_id and route_hint_from_record(rec) else route_uuid
            ident = rec.get("bookmark_identity_hash") or _identity("bookmark", {**rec, "route_uuid": rec_route_uuid})
            text = rec.get("text") or rec.get("summary") or rec.get("label") or rec.get("reason")
            _insert_ignore(store, "INSERT INTO bookmarks(route_uuid,bookmark_identity_hash,source,t_sec,end_sec,text,tags,artifact_id,metadata_jsonb) VALUES(?,?,?,?,?,?,?,?,?)", (rec_route_uuid, ident, rec.get("source", "import"), rec.get("t_sec") or rec.get("start_sec") or rec.get("t") or rec.get("route_time_sec"), rec.get("end_sec") or rec.get("end_t") or rec.get("end_t_sec"), text, _tags_param(store, rec.get("tags", [])), artifact_id, store._json({"raw": rec}))); counts["bookmarks"] += 1
    return dict(counts)


SEGMENT_LEN_SEC = 60.0
MPH_PER_MPS = 2.2369362920544

def _round_float(value, ndigits=3):
    try:
        v = float(value)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v, ndigits)

def _route_time(log_mono_time: int, first_mono_time: int, segment_index: int | None) -> float:
    seg = int(segment_index or 0)
    delta = (int(log_mono_time) - int(first_mono_time)) / 1e9
    base = seg * SEGMENT_LEN_SEC
    # Some comma copies preserve route-relative monotime in later segments.
    if seg > 0 and (base - SEGMENT_LEN_SEC * 0.75) <= delta <= (base + SEGMENT_LEN_SEC * 1.75):
        return delta
    return base + delta

def _manifest_from_init_data(init_data) -> dict:
    params = {}
    try:
        entries = list(init_data.params.entries)
    except Exception:
        entries = []
    for entry in entries:
        key = str(getattr(entry, "key", "") or "")
        if not key:
            continue
        value = _param_text(getattr(entry, "value", ""))
        params[key] = "[redacted]" if any(token in key.lower() for token in SENSITIVE_PARAM_NAMES) else value
    version = _param_text(getattr(init_data, "version", ""))
    if version and "Version" not in params:
        params["Version"] = version
    return {
        "schema_version": 1,
        "source": "log_init_data",
        "status": "ok",
        "software": {
            "branch": _param_text(getattr(init_data, "gitBranch", "")),
            "commit": _param_text(getattr(init_data, "gitCommit", "")),
            "remote": _param_text(getattr(init_data, "gitRemote", "")),
            "dirty": bool(getattr(init_data, "dirty", False)),
            "openpilot_version": version,
        },
        "params": params,
        "params_raw": {},
    }

def _read_log_derived(path: Path, segment_index: int | None, max_can_batches: int = 180) -> dict:
    """Extract compact route samples/CAN/events from a copied qlog/rlog.

    This is intentionally local-only and lossy: enough for the DB-backed manual
    labeler timeline and cutover gate, not a replacement for raw logs.
    """
    out = {"samples": [], "can": [], "events": [], "duration_sec": 0.0, "counts": Counter(), "settings_manifest": None}
    try:
        import sys
        tools_root = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[3]))
        tools_root = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path.cwd())).expanduser()
        repo_root = Path(os.environ.get("BRICKPILOT_REPO_ROOT", tools_root.parent / "brickpilot")).expanduser()
        for root in (tools_root, repo_root):
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
        import zstandard as zstd  # type: ignore
        from cereal import log as capnp_log  # type: ignore
        dat = path.read_bytes()
        if dat.startswith(b"\x28\xB5\x2F\xFD"):
            with zstd.ZstdDecompressor().stream_reader(dat) as reader:
                dat = reader.read()
        first_mono = None
        last_sample_t = -1e9
        last_can_t = -1e9
        can_batches = 0
        latest_plan = {}
        latest_lead = {}
        for msg in capnp_log.Event.read_multiple_bytes(dat):
            if first_mono is None:
                first_mono = int(msg.logMonoTime)
            try:
                typ = msg.which()
            except Exception:
                continue
            out["counts"][typ] += 1
            rt = _route_time(int(msg.logMonoTime), first_mono, segment_index)
            out["duration_sec"] = max(float(out["duration_sec"]), rt)
            if typ == "initData" and not out.get("settings_manifest"):
                out["settings_manifest"] = _manifest_from_init_data(msg.initData)
            elif typ == "longitudinalPlan":
                try:
                    accels = list(getattr(msg.longitudinalPlan, "accels", []))
                    latest_plan = {"accel_cmd": _round_float(accels[0], 3) if accels else None}
                except Exception:
                    latest_plan = {}
            elif typ == "radarState":
                try:
                    lead = msg.radarState.leadOne
                    latest_lead = {"lead_status": bool(getattr(lead, "status", False)), "lead_d_rel_m": _round_float(getattr(lead, "dRel", None), 2), "lead_v_rel_mps": _round_float(getattr(lead, "vRel", None), 2)}
                except Exception:
                    latest_lead = {}
            elif typ == "carState":
                if rt - last_sample_t < 0.20:
                    continue
                cs = msg.carState
                sample = {
                    "t_sec": round(rt, 3),
                    "speed_mph": _round_float(float(getattr(cs, "vEgo", 0.0)) * MPH_PER_MPS, 2),
                    "set_speed_mph": _round_float(float(getattr(cs, "vCruise", 0.0)) * 0.62137119223733, 1),
                    "a_ego_mps2": _round_float(getattr(cs, "aEgo", None), 3),
                    "gas_pressed": bool(getattr(cs, "gasPressed", False)),
                    "brake_pressed": bool(getattr(cs, "brakePressed", False)),
                    **latest_lead,
                    "raw": {"source": path.name, "segment_index": segment_index, **latest_plan},
                }
                out["samples"].append(sample)
                last_sample_t = rt
            elif typ == "can":
                if can_batches >= max_can_batches or rt - last_can_t < 0.25:
                    continue
                for i, frame in enumerate(msg.can):
                    if i >= 48:
                        break
                    try:
                        out["can"].append({"t_sec": round(rt, 3), "bus": int(frame.src), "address": int(frame.address), "data_hex": bytes(frame.dat).hex().upper(), "name": f"0x{int(frame.address):X}", "hint": "", "is_unknown": True, "raw": {"segment_index": segment_index}})
                    except Exception:
                        continue
                can_batches += 1
                last_can_t = rt
            elif typ in {"userBookmark", "bookmarkButton"}:
                out["events"].append({"source": "log", "event_type": typ, "t_sec": round(rt, 3), "severity": "bookmark", "summary": typ, "raw": {"source": path.name, "segment_index": segment_index}})
    except Exception as exc:
        out["error"] = str(exc).splitlines()[0][:240]
    out["counts"] = dict(out["counts"])
    return out

def _clear_derived_for_route(store: DriveStore, route_uuid: str):
    for table in ("route_samples", "can_frames_sampled", "events", "video_sync_segments"):
        store.execute(f"DELETE FROM {table} WHERE route_uuid=?", (route_uuid,))

def _executemany(store: DriveStore, sql: str, rows: list[tuple]):
    if not rows:
        return
    cur = store.conn.cursor()
    try:
        cur.executemany(store.sql(sql), rows)
    finally:
        cur.close()

def _insert_ignore_sql(store: DriveStore, sql: str) -> str:
    return (sql + " ON CONFLICT DO NOTHING") if store.dialect == "postgres" else sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)

def _insert_log_derived(store: DriveStore, route_uuid: str, segment_id: int | None, segment_index: int | None, artifact_id: int, path: Path) -> Counter:
    counts = Counter()
    derived = _read_log_derived(path, segment_index)
    manifest = derived.get("settings_manifest")
    if isinstance(manifest, dict):
        counts.update(_apply_settings_manifest(store, path, route_uuid, artifact_id, manifest))
    sample_rows = [
        (route_uuid, row.get("t_sec"), row.get("speed_mph"), row.get("set_speed_mph"), row.get("a_ego_mps2"), row.get("gas_pressed"), row.get("brake_pressed"), row.get("lead_status"), row.get("lead_d_rel_m"), row.get("lead_v_rel_mps"), store._json(row.get("raw", {})))
        for row in derived.get("samples", [])
    ]
    _executemany(store, _insert_ignore_sql(store, "INSERT INTO route_samples(route_uuid,t_sec,speed_mph,set_speed_mph,a_ego_mps2,gas_pressed,brake_pressed,lead_status,lead_d_rel_m,lead_v_rel_mps,raw_jsonb) VALUES(?,?,?,?,?,?,?,?,?,?,?)"), sample_rows)
    counts["route_samples"] += len(sample_rows)
    can_rows = [
        (route_uuid, row.get("t_sec"), row.get("bus"), row.get("address"), row.get("data_hex"), row.get("name"), row.get("hint"), row.get("is_unknown"), store._json(row.get("raw", {})))
        for row in derived.get("can", [])
    ]
    _executemany(store, "INSERT INTO can_frames_sampled(route_uuid,t_sec,bus,address,data_hex,name,hint,is_unknown,raw_jsonb) VALUES(?,?,?,?,?,?,?,?,?)", can_rows)
    counts["can_frames"] += len(can_rows)
    event_rows = []
    for row in derived.get("events", []):
        ident = _identity("event", {**row, "route_uuid": route_uuid})
        event_rows.append((route_uuid, row.get("source"), row.get("event_type"), row.get("t_sec"), row.get("end_t_sec"), row.get("severity"), row.get("summary"), store._json({**row.get("raw", {}), "identity": ident})))
    _executemany(store, _insert_ignore_sql(store, "INSERT INTO events(route_uuid,source,event_type,t_sec,end_t_sec,severity,summary,raw_jsonb) VALUES(?,?,?,?,?,?,?,?)"), event_rows)
    counts["events"] += len(event_rows)
    dur = float(derived.get("duration_sec") or 0.0)
    if dur > 0:
        if segment_id is not None:
            seg_dur = max(0.0, dur - float(segment_index or 0) * SEGMENT_LEN_SEC)
            store.execute("UPDATE route_segments SET duration_sec=GREATEST(COALESCE(duration_sec,0), ?) WHERE id=?", (seg_dur, segment_id)) if store.dialect == "postgres" else store.execute("UPDATE route_segments SET duration_sec=max(COALESCE(duration_sec,0), ?) WHERE id=?", (seg_dur, segment_id))
        store.execute("UPDATE routes SET duration_sec=GREATEST(COALESCE(duration_sec,0), ?), updated_at=CURRENT_TIMESTAMP WHERE id=?", (dur, route_uuid)) if store.dialect == "postgres" else store.execute("UPDATE routes SET duration_sec=max(COALESCE(duration_sec,0), ?), updated_at=CURRENT_TIMESTAMP WHERE id=?", (dur, route_uuid))
        counts["duration_updates"] += 1
    if derived.get("error"):
        counts["log_parse_errors"] += 1
    return counts

def _insert_video_sync(store: DriveStore, route_uuid: str, segment_index: int | None, artifact_id: int, path: Path) -> Counter:
    if segment_index is None:
        return Counter()
    duration = SEGMENT_LEN_SEC
    try:
        import subprocess, json as _json
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], text=True, capture_output=True, timeout=15)
        if proc.returncode == 0:
            val = float((_json.loads(proc.stdout or "{}" ).get("format") or {}).get("duration") or 0)
            if val > 0:
                duration = val
    except Exception:
        pass
    _insert_ignore(store, "INSERT INTO video_sync_segments(route_uuid,artifact_id,segment_index,route_start_sec,video_start_sec,duration_sec,source_path,confidence,raw_jsonb) VALUES(?,?,?,?,?,?,?,?,?)", (route_uuid, artifact_id, segment_index, float(segment_index) * SEGMENT_LEN_SEC, 0.0, duration, str(path), 0.6, store._json({"source": "ingest_camera_default"})))
    return Counter({"video_sync": 1})

def import_root(root: Path, config_path: str | None=None, dry_run: bool=True) -> dict:
    inv = inventory(root)
    if dry_run: return {"dry_run": True, "inventory": inv, "warnings": [], "reconciliation": {"duplicate_rule": "route/source path and content sha256 are idempotent; metadata rows use identity hashes", "conflict_rule": "existing content-addressed artifacts are linked, not copied", "route_mapping": "route ids come from paths first, then route_id/route/path fields inside metadata records, then root-name fallback"}}
    cfg = load_config(config_path); store = DriveStore(cfg); store.migrate(); host_id = store.upsert_host(role=cfg.source_host_role)
    if store.dialect == "postgres": run = store.execute("INSERT INTO ingest_runs(source_host,source_root,status,summary_jsonb) VALUES(?,?,?,?) RETURNING id", (None, str(root), "running", store._json(inv))).fetchone()["id"]
    else: run = store.execute("INSERT INTO ingest_runs(source_host,source_root,status,summary_jsonb) VALUES(?,?,?,?)", (None, str(root), "running", json.dumps(inv))).lastrowid
    started = time.time(); inserted = 0; imported_bytes = 0; warnings = []; actions = Counter(); metadata_counts = Counter(); route_cache = {}; derived_cleared: set[str] = set(); route_file_times: dict[str, list[tuple[float, int | None]]] = {}
    def ensure_route(rid: str) -> str:
        if rid not in route_cache:
            route_cache[rid] = store.upsert_route(rid, source_device="macbook", segment_count=len(inv["routes"].get(rid,{}).get("segments",[])), metadata={"import_root_private": True})
        return route_cache[rid]
    if store.dialect == "postgres":
        store.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"brickpilot_drive_import:{root}",))
    for idx, p in enumerate(interesting_files(root)):
        savepoint = f"ingest_item_{idx}"
        if store.dialect == "postgres":
            store.execute(f"SAVEPOINT {savepoint}")
        try:
            rid = route_from_path(p) or (route_from_metadata_file(p) if p.suffix.lower() in (".json", ".jsonl") else None) or root.name; seg = segment_from_path(p)
            route_uuid = ensure_route(rid)
            route_file_times.setdefault(route_uuid, []).append((p.stat().st_mtime, seg))
            seg_id = store.upsert_segment(route_uuid, seg, {store.kind_for_path(p): True}) if seg is not None else None
            art = store.import_artifact(p, host_id=host_id)
            kind = art.kind
            if route_uuid not in derived_cleared and kind in {"rlog", "qlog", "qcamera", "fcamera", "ecamera", "video"}:
                _clear_derived_for_route(store, route_uuid); derived_cleared.add(route_uuid)
            store.link_route_artifact(route_uuid, art.id, kind, seg_id)
            if kind in {"rlog", "qlog"}:
                # Prefer full rlog-derived timeline data when both logs exist; keep qlog as artifact but avoid duplicate CAN/telemetry imports.
                if not (kind == "qlog" and (p.parent / "rlog.zst").exists()):
                    metadata_counts.update(_insert_log_derived(store, route_uuid, seg_id, seg, art.id, p))
            if kind in {"qcamera", "fcamera", "ecamera", "video"}:
                metadata_counts.update(_insert_video_sync(store, route_uuid, seg, art.id, p))
            if p.name in ("drive_jobs.json", "drive_data.json") or p.suffix.lower() in (".jsonl", ".json", ".sqlite", ".db"):
                _insert_ignore(store, "INSERT INTO legacy_files(artifact_id,source_path,parser_status) VALUES(?,?,?)", (art.id, str(p), "preserved_private_path"))
                metadata_counts.update(parse_metadata_file(store, p, route_uuid, art.id, ensure_route))
            inserted += 1; imported_bytes += p.stat().st_size; actions["artifact_attempts"] += 1
            store.execute("INSERT INTO ingest_items(ingest_run_id, route_uuid, artifact_id, action, status, message) VALUES(?,?,?,?,?,?)", (run, route_uuid, art.id, "linked_existing", "ok", p.name))
            if store.dialect == "postgres":
                store.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as e:
            if store.dialect == "postgres":
                store.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                store.execute(f"RELEASE SAVEPOINT {savepoint}")
            warnings.append(f"{p}: {e}"); actions["errors"] += 1; store.execute("INSERT INTO ingest_items(ingest_run_id, action, status, message) VALUES(?,?,?,?)", (run, "import", "error", f"{p}: {e}"))
    for route_uuid, file_times in route_file_times.items():
        if not file_times:
            continue
        raw_times = [mtime for mtime, _seg in file_times if mtime > 0]
        segment_times = [(mtime, seg) for mtime, seg in file_times if mtime > 0 and seg is not None]
        route_row = store.one("SELECT duration_sec FROM routes WHERE id=?", (route_uuid,))
        duration = float(route_row["duration_sec"] or 0) if route_row else 0.0
        start_candidates = []
        for mtime, seg in segment_times:
            elapsed_at_close = float(int(seg) + 1) * SEGMENT_LEN_SEC
            if duration > 0:
                segment_start = float(int(seg)) * SEGMENT_LEN_SEC
                if duration >= segment_start:
                    elapsed_at_close = min(elapsed_at_close, duration)
            start_candidates.append(mtime - elapsed_at_close)
        if start_candidates:
            start_ts = median(start_candidates)
            metadata_counts["route_wall_time_segment_inferred"] += 1
        elif raw_times:
            start_ts = min(raw_times)
        else:
            continue
        end_ts = start_ts + duration if duration > 0 else max(raw_times or [start_ts])
        started_at = dt.datetime.fromtimestamp(start_ts, dt.timezone.utc).isoformat()
        ended_at = dt.datetime.fromtimestamp(end_ts, dt.timezone.utc).isoformat()
        store.execute(
            "UPDATE routes SET started_at=?, ended_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (started_at, ended_at, route_uuid),
        )
        metadata_counts["route_wall_time_updates"] += 1
    summary = {"inventory": inv, "imported_files": inserted, "imported_bytes": imported_bytes, "duration_sec": round(time.time()-started, 3), "files_per_sec": round(inserted/max(time.time()-started,0.001), 3), "bytes_per_sec": round(imported_bytes/max(time.time()-started,0.001), 1), "actions": dict(actions), "metadata_counts": dict(metadata_counts), "warnings_count": len(warnings)}
    store.execute("UPDATE ingest_runs SET status=?, finished_at=CURRENT_TIMESTAMP, summary_jsonb=?, error=? WHERE id=?", ("finished_with_warnings" if warnings else "finished", store._json(summary), "\n".join(warnings) if warnings else None, run))
    store.commit(); store.close()
    return {"dry_run": False, **summary, "warnings": warnings}

def main() -> int:
    ap = argparse.ArgumentParser(description="Inventory/import Brickpilot drive artifacts into central DB/artifact store")
    ap.add_argument("roots", nargs="+", type=Path); ap.add_argument("--config"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(); out = [import_root(r, ns.config, dry_run=ns.dry_run) for r in ns.roots]
    if ns.json: print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for item in out: print(json.dumps(item["inventory"], indent=2, sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())
