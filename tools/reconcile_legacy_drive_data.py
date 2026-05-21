#!/usr/bin/env python3
"""Reconcile legacy Brickpilot drive data into the active external data store.

This script copies missing legacy route/output folders into the current
BrickpilotDriveDB layout and optionally imports them into the central DB/artifact
store. It never writes raw drive data into a git repository.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DATA_ROOT = Path(os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB")).expanduser()
DEFAULT_CONFIG = Path(os.environ.get("BRICKPILOT_DRIVE_DB_CONFIG", Path.home() / ".config" / "brickpilot" / "drive_db.toml")).expanduser()
TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[1])).expanduser()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
ROUTE_RE = re.compile(r"(?P<route>[0-9a-fA-F]{8,}--[0-9a-fA-F]{8,}|[0-9a-fA-F]{8,}--[^/]+)")

for root in (REPO_ROOT, TOOLS_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def ensure_tools_package_imports() -> None:
    """Keep copied tooling imports owned by this repo, even in reused interpreters."""
    for root in (REPO_ROOT, TOOLS_ROOT):
        root_text = str(root)
        if root_text in sys.path:
            sys.path.remove(root_text)
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(TOOLS_ROOT))
    script_modules = sorted(
        (name for name in sys.modules if name == "scripts" or name.startswith("scripts.")),
        key=lambda item: item.count("."),
        reverse=True,
    )
    for name in script_modules:
        module = sys.modules.get(name)
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        try:
            module_paths = [str(path) for path in getattr(module, "__path__", [])]
        except Exception:
            module_paths = []
        if module_file:
            owned = Path(module_file).resolve().is_relative_to(TOOLS_ROOT)
        else:
            owned = any(Path(path).resolve().is_relative_to(TOOLS_ROOT) for path in module_paths)
        if not owned:
            sys.modules.pop(name, None)


@dataclass(frozen=True)
class LegacySource:
    name: str
    root: Path

    @property
    def raw_root(self) -> Path:
        candidates = (
            self.root / "analysis" / "drive_tests" / "raw" / "from_comma",
            self.root / "drive_tests" / "raw" / "from_comma",
        )
        return next((path for path in candidates if path.exists()), candidates[0])

    @property
    def drive_tests_root(self) -> Path:
        candidates = (
            self.root / "analysis" / "drive_tests",
            self.root / "drive_tests",
        )
        return next((path for path in candidates if path.exists()), candidates[0])

    @property
    def analysis_root(self) -> Path:
        candidate = self.root / "analysis"
        return candidate if candidate.exists() else self.root


SOURCES = (
    LegacySource("macbook_20260514", DATA_ROOT / "analysis_exports" / "from_macbook_repo_20260514T202755Z"),
    LegacySource("macmini_20260514", DATA_ROOT / "analysis_exports" / "from_macmini_repo_20260514T202808Z"),
    LegacySource("quarantine_20260514", DATA_ROOT / "quarantine" / "repo_analysis_after_staging_20260514T205542Z"),
)


def route_id_from_name(name: str) -> str:
    match = ROUTE_RE.search(name)
    return match.group("route") if match else name


def run(cmd: list[str], *, dry_run: bool) -> None:
    if dry_run:
        print("DRY-RUN", " ".join(cmd))
        return
    subprocess.run(cmd, check=True)


def rsync_dir(src: Path, dst: Path, *, dry_run: bool, extra_excludes: list[str] | None = None) -> dict[str, Any]:
    excludes = [
        "--exclude=.DS_Store",
        "--exclude=__pycache__/",
        "--exclude=*.pyc",
    ]
    for pattern in extra_excludes or []:
        excludes.append(f"--exclude={pattern}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--ignore-existing", f"--link-dest={src.resolve()}", *excludes, f"{src}/", f"{dst}/"]
    run(cmd, dry_run=dry_run)
    return {"source": str(src), "destination": str(dst)}


def copy_missing_raw_routes(source: LegacySource, active_raw: Path, *, dry_run: bool) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    if not source.raw_root.exists():
        return copied
    active_raw.mkdir(parents=True, exist_ok=True)
    existing_route_ids = {
        route_id_from_name(p.name)
        for p in active_raw.iterdir()
        if p.is_dir()
    }
    for src in sorted(p for p in source.raw_root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        rid = route_id_from_name(src.name)
        dst = active_raw / rid
        if rid in existing_route_ids or dst.exists():
            continue
        copied.append({
            "route_id": rid,
            "source_name": source.name,
            **rsync_dir(src, dst, dry_run=dry_run, extra_excludes=["_bad_flat_copy_attempt/"]),
        })
        existing_route_ids.add(rid)
    return copied


def copy_named_children(
    source: LegacySource,
    parent_glob: str,
    destination_root: Path,
    *,
    dry_run: bool,
    excludes: list[str] | None = None,
) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    root = source.drive_tests_root
    if not root.exists():
        return copied
    for src in sorted(root.glob(parent_glob)):
        if not src.is_dir():
            continue
        dst = destination_root / "recovered_legacy" / source.name / src.name
        copied.append({
            "source_name": source.name,
            **rsync_dir(src, dst, dry_run=dry_run, extra_excludes=excludes),
        })
    return copied


def copy_analysis_outputs(source: LegacySource, destination_root: Path, *, dry_run: bool) -> list[dict[str, Any]]:
    analysis_root = source.analysis_root
    if not analysis_root.exists():
        return []
    dst = destination_root / "recovered_legacy" / source.name / "analysis"
    return [{
        "source_name": source.name,
        **rsync_dir(
            analysis_root,
            dst,
            dry_run=dry_run,
            extra_excludes=[
                "drive_tests/raw/",
                "drive_tests/logdrive/",
                "drive_tests/logdrive_runs/",
                "drive_tests/manual_drive_labeler*/",
                "drive_tests/review_queue*/",
                "*.bz2",
                "*.zst",
                "*.hevc",
                "*.ts",
                "*.mp4",
                "*.mov",
                "*.webm",
                "*.m4a",
                "*.wav",
                "*.mp3",
                "*.sqlite",
                "*.db",
            ],
        ),
    }]


def inventory_dir(path: Path) -> dict[str, Any]:
    files = 0
    total = 0
    for item in path.rglob("*") if path.exists() else []:
        if item.is_file():
            files += 1
            total += item.stat().st_size
    return {"path": str(path), "files": files, "bytes": total}


def import_roots(roots: list[Path], config: Path, *, dry_run: bool) -> list[dict[str, Any]]:
    if dry_run:
        return [{"root": str(root), "dry_run": True} for root in roots]
    ensure_tools_package_imports()
    from scripts.drive_tests.brickpilot_db.ingest import import_root

    results = []
    for root in roots:
        if root.exists():
            results.append(import_root(root, str(config), dry_run=False))
    return results


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _json_value(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _catalog_paths(tools_root: Path) -> list[Path]:
    paths: list[Path] = []
    current_catalog = tools_root / "scripts" / "drive_tests" / "route_catalog.yaml"
    if current_catalog.exists():
        paths.append(current_catalog)
    for source in SOURCES:
        root = source.drive_tests_root
        if not root.exists():
            continue
        paths.extend(sorted(root.glob("*catalog*.yaml")))
        paths.extend(sorted(root.glob("*catalog*.yml")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _catalog_route_entries(path: Path) -> list[dict[str, Any]]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("PyYAML is required for route catalog import") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    if isinstance(data, dict):
        raw_routes = data.get("routes") or data.get("items") or []
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    elif isinstance(data, list):
        raw_routes = data
        defaults = {}
    else:
        raw_routes = []
        defaults = {}
    entries: list[dict[str, Any]] = []
    for raw in raw_routes:
        if not isinstance(raw, dict):
            continue
        route_text = raw.get("route_id") or raw.get("route") or raw.get("canonical_route") or raw.get("canonical_name")
        if not isinstance(route_text, str):
            continue
        rid = route_id_from_name(route_text)
        if not ROUTE_RE.search(rid):
            continue
        entries.append({
            "route_id": rid,
            "canonical_name": route_text,
            "label": raw.get("label") or raw.get("name"),
            "model": raw.get("model") or raw.get("model_bundle"),
            "drive_type": raw.get("drive_type") or raw.get("ride_type"),
            "notes": raw.get("notes"),
            "settings": raw.get("settings") if isinstance(raw.get("settings"), dict) else None,
            "raw": raw,
            "defaults": defaults,
            "catalog_path": str(path),
        })
    return entries


def import_route_catalogs(config: Path, tools_root: Path, *, dry_run: bool) -> dict[str, Any]:
    paths = _catalog_paths(tools_root)
    summary: dict[str, Any] = {
        "catalog_files": len(paths),
        "entries_seen": 0,
        "routes_created": 0,
        "routes_updated": 0,
        "skipped": 0,
        "paths": [str(path) for path in paths],
    }
    if dry_run:
        for path in paths:
            summary["entries_seen"] += len(_catalog_route_entries(path))
        return summary

    ensure_tools_package_imports()
    from scripts.drive_tests.brickpilot_db.config import load_config
    from scripts.drive_tests.brickpilot_db.store import DriveStore

    store = DriveStore(load_config(str(config)))
    try:
        for path in paths:
            for entry in _catalog_route_entries(path):
                summary["entries_seen"] += 1
                rid = entry["route_id"]
                row = store.one("SELECT id, route_label, model_bundle, drive_type, metadata_jsonb FROM routes WHERE route_id=? ORDER BY created_at LIMIT 1", (rid,))
                if row is None:
                    route_uuid = store.upsert_route(
                        rid,
                        source_device="catalog",
                        canonical_name=entry.get("canonical_name") or rid,
                        route_label=entry.get("label"),
                        drive_type=entry.get("drive_type"),
                        metadata={},
                    )
                    row = store.one("SELECT id, route_label, model_bundle, drive_type, metadata_jsonb FROM routes WHERE id=?", (route_uuid,))
                    summary["routes_created"] += 1
                else:
                    route_uuid = _row_get(row, "id")
                    summary["routes_updated"] += 1
                if not route_uuid:
                    summary["skipped"] += 1
                    continue
                metadata = _json_value(_row_get(row, "metadata_jsonb"))
                catalog_entries = metadata.setdefault("legacy_route_catalogs", [])
                catalog_record = {
                    "catalog_path": entry["catalog_path"],
                    "label": entry.get("label"),
                    "model": entry.get("model"),
                    "drive_type": entry.get("drive_type"),
                    "notes": entry.get("notes"),
                    "settings": entry.get("settings"),
                    "defaults": entry.get("defaults"),
                    "raw": entry.get("raw"),
                }
                fingerprint = json.dumps(catalog_record, sort_keys=True, default=str)
                existing = {json.dumps(item, sort_keys=True, default=str) for item in catalog_entries if isinstance(item, dict)}
                if fingerprint not in existing:
                    catalog_entries.append(catalog_record)
                route_label = entry.get("label") or _row_get(row, "route_label")
                model_bundle = entry.get("model") or _row_get(row, "model_bundle")
                drive_type = entry.get("drive_type") or _row_get(row, "drive_type")
                store.execute(
                    "UPDATE routes SET route_label=?, model_bundle=?, drive_type=?, metadata_jsonb=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (route_label, model_bundle, drive_type, store._json(metadata), route_uuid),
                )
        store.commit()
    finally:
        store.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", type=Path, default=DATA_ROOT)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--execute", action="store_true", help="perform copies/imports; default is dry-run")
    ap.add_argument("--skip-import", action="store_true", help="copy folders but do not import into DB/artifact store")
    ap.add_argument("--skip-artifact-import", action="store_true", help="skip artifact/root import but still import route catalog metadata")
    ap.add_argument("--skip-catalog-import", action="store_true", help="skip route catalog metadata import")
    ap.add_argument("--only-analysis", action="store_true", help="copy/import recovered analysis outputs and route catalogs only")
    ap.add_argument("--source", action="append", choices=[s.name for s in SOURCES], help="limit reconciliation to one source name; can be repeated")
    args = ap.parse_args()

    data_root = args.data_root
    dry_run = not args.execute
    active_raw = data_root / "imports" / "raw" / "from_comma"
    active_logdrive = data_root / "logdrive_runs"
    active_labeler = data_root / "labeler_outputs"
    active_analysis = data_root / "analysis_exports"
    manifest_dir = data_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    raw_copies: list[dict[str, Any]] = []
    logdrive_copies: list[dict[str, Any]] = []
    labeler_copies: list[dict[str, Any]] = []
    analysis_copies: list[dict[str, Any]] = []
    selected_sources = [source for source in SOURCES if not args.source or source.name in set(args.source)]
    for source in selected_sources:
        if not args.only_analysis:
            raw_copies.extend(copy_missing_raw_routes(source, active_raw, dry_run=dry_run))
            logdrive_copies.extend(copy_named_children(source, "logdrive/*", active_logdrive, dry_run=dry_run))
            logdrive_copies.extend(copy_named_children(source, "logdrive_runs/*", active_logdrive, dry_run=dry_run))
            labeler_copies.extend(copy_named_children(
                source,
                "manual_drive_labeler*",
                active_labeler,
                dry_run=dry_run,
                excludes=["*.mp4", "*.webm", "*.hevc", "*.ts"],
            ))
            labeler_copies.extend(copy_named_children(
                source,
                "review_queue*",
                active_labeler,
                dry_run=dry_run,
                excludes=["*.mp4", "*.webm", "*.hevc", "*.ts"],
            ))
        analysis_copies.extend(copy_analysis_outputs(source, active_analysis, dry_run=dry_run))

    if args.source:
        import_targets = [active_analysis / "recovered_legacy" / source.name for source in selected_sources]
        if not args.only_analysis:
            if raw_copies:
                import_targets.append(active_raw)
            import_targets.extend(active_logdrive / "recovered_legacy" / source.name for source in selected_sources)
            import_targets.extend(active_labeler / "recovered_legacy" / source.name for source in selected_sources)
    else:
        import_targets = (
            [active_analysis / "recovered_legacy"]
        if args.only_analysis
        else [
            active_raw,
            active_logdrive / "recovered_legacy",
            active_labeler / "recovered_legacy",
            active_analysis / "recovered_legacy",
        ]
        )
    import_results = [] if (args.skip_import or args.skip_artifact_import) else import_roots(import_targets, args.config, dry_run=dry_run)
    catalog_results = {} if (args.skip_import or args.skip_catalog_import) else import_route_catalogs(args.config, TOOLS_ROOT, dry_run=dry_run)

    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": dry_run,
        "data_root": str(data_root),
        "sources": [{"name": s.name, "root": str(s.root)} for s in selected_sources],
        "raw_copies": raw_copies,
        "logdrive_copies": logdrive_copies,
        "labeler_copies": labeler_copies,
        "analysis_copies": analysis_copies,
        "inventories": {
            "active_raw": inventory_dir(active_raw),
            "recovered_logdrive": inventory_dir(active_logdrive / "recovered_legacy"),
            "recovered_labeler": inventory_dir(active_labeler / "recovered_legacy"),
            "recovered_analysis": inventory_dir(active_analysis / "recovered_legacy"),
        },
        "import_results": import_results,
        "catalog_results": catalog_results,
    }
    suffix = "dry_run" if dry_run else dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = manifest_dir / f"legacy_data_reconciliation_{suffix}.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "manifest": str(out),
        "dry_run": dry_run,
        "raw_copied": len(raw_copies),
        "logdrive_copied": len(logdrive_copies),
        "labeler_copied": len(labeler_copies),
        "analysis_copied": len(analysis_copies),
        "imported_roots": len(import_results),
        "catalog_results": catalog_results,
        "inventories": manifest["inventories"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
