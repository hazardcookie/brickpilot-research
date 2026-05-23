#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser().resolve()
DATA_ROOT = Path(os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB")).expanduser().resolve()
DB_CONFIG = Path(os.environ.get("BRICKPILOT_DRIVE_DB_CONFIG", Path.home() / ".config" / "brickpilot" / "drive_db.toml")).expanduser().resolve()

for root in (REPO_ROOT, TOOLS_ROOT):
  while str(root) in sys.path:
    sys.path.remove(str(root))
for root in (REPO_ROOT, TOOLS_ROOT):
  sys.path.insert(0, str(root))


def module_path(module: Any) -> str:
  return str(Path(module.__file__).resolve())


def assert_inside(root: Path, value: Path | str) -> str:
  resolved = Path(value).resolve()
  if resolved != root and root not in resolved.parents:
    raise RuntimeError(f"{resolved} is outside {root}")
  return str(resolved)


def count_rows(store: Any, table: str) -> int | None:
  try:
    row = store.one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"] if isinstance(row, dict) else row[0])
  except Exception:
    return None


def verify_imports() -> dict[str, Any]:
  from scripts.drive_tests import analyze_routes, logdrive_automation, manual_drive_labeler, review_queue, voice_bookmark_app
  from scripts.drive_tests.brickpilot_db import config

  modules = {
    "analyze_routes": module_path(analyze_routes),
    "logdrive_automation": module_path(logdrive_automation),
    "manual_drive_labeler": module_path(manual_drive_labeler),
    "review_queue": module_path(review_queue),
    "voice_bookmark_app": module_path(voice_bookmark_app),
    "brickpilot_db.config": module_path(config),
  }
  outside = {name: file for name, file in modules.items() if not Path(file).resolve().is_relative_to(TOOLS_ROOT)}
  if outside:
    raise RuntimeError(f"module import escaped tools repo: {outside}")
  return {
    "modules": modules,
    "defaults": {
      "analyze_routes_DATA_ROOT": str(analyze_routes.DATA_ROOT),
      "logdrive_DEFAULT_RAW_ROOT": str(logdrive_automation.DEFAULT_RAW_ROOT),
      "logdrive_DEFAULT_RUNS_ROOT": str(logdrive_automation.DEFAULT_RUNS_ROOT),
      "manual_labeler_DEFAULT_OUT_BASE": str(manual_drive_labeler.DEFAULT_OUT_BASE),
      "review_queue_DEFAULT_OUT": str(review_queue.DEFAULT_OUT),
      "db_DEFAULT_ARTIFACT_ROOT": str(config.DEFAULT_ARTIFACT_ROOT),
      "db_DEFAULT_BACKUP_ROOT": str(config.DEFAULT_BACKUP_ROOT),
    },
  }


def verify_logdrive_policy() -> dict[str, Any]:
  from scripts.drive_tests import logdrive_automation as la

  with tempfile.TemporaryDirectory(prefix="brickpilot-logdrive-policy-") as tmp:
    root = Path(tmp)
    realdata = root / "realdata"
    raw = root / "raw"
    route = "aaaaaaaaaaaaaaaa|2026-05-15--12-00-00"
    segment = realdata / f"{route}--0"
    segment.mkdir(parents=True)
    (segment / "rlog.zst").write_bytes(b"rlog")
    (segment / "qlog.zst").write_bytes(b"qlog")
    (segment / "fcamera.hevc").write_bytes(b"video")
    candidate = la.build_candidates(la.discover_segments(realdata))[0]
    normal = la.make_copy_plan(candidate, run_id="verify", drive_type=la.DriveType.NORMAL, raw_root=raw)
    test = la.make_copy_plan(candidate, run_id="verify", drive_type=la.DriveType.TEST, raw_root=raw)
    validation = la.make_copy_plan(candidate, run_id="verify", drive_type=la.DriveType.LABEL_VALIDATION, raw_root=raw)
    if any(item.kind == "video" for item in normal.items + test.items):
      raise RuntimeError("normal/test copy policy included video")
    if not any(item.kind == "video" for item in validation.items):
      raise RuntimeError("label-validation copy policy omitted video")
    return {
      "normal_items": len(normal.items),
      "test_items": len(test.items),
      "label_validation_items": len(validation.items),
      "label_validation_video_items": sum(1 for item in validation.items if item.kind == "video"),
    }


def verify_data_tools() -> dict[str, Any]:
  from scripts.drive_tests.brickpilot_db.backup import backup
  from scripts.drive_tests.brickpilot_db.config import load_config
  from scripts.drive_tests.brickpilot_db.ingest import inventory
  from scripts.drive_tests.brickpilot_db.store import DriveStore

  raw_root = DATA_ROOT / "imports" / "raw" / "from_comma"
  inv = inventory(raw_root)
  cfg = load_config(DB_CONFIG)
  if not Path(cfg.artifact_root).resolve().is_relative_to(DATA_ROOT):
    raise RuntimeError(f"artifact root is outside data root: {cfg.artifact_root}")
  counts: dict[str, int | None] = {}
  store = DriveStore(cfg)
  try:
    for table in ("routes", "artifacts", "route_artifacts", "review_jobs", "labels", "bookmarks", "route_samples", "can_frames_sampled", "events", "ingest_runs"):
      counts[table] = count_rows(store, table)
  finally:
    store.close()
  return {
    "raw_inventory": {
      "root": inv["root"],
      "file_count": inv["file_count"],
      "bytes": inv["bytes"],
      "kinds": inv["kinds"],
      "route_count": len(inv["routes"]),
    },
    "db_counts": counts,
    "backup_dry_run": backup(str(DB_CONFIG), dry_run=True),
  }


def run_analysis_smoke(stamp: str) -> dict[str, Any]:
  out_dir = DATA_ROOT / "analysis_exports" / f"split_verify_{stamp}" / "analyze_routes_empty"
  catalog = DATA_ROOT / "manifests" / f"split_verify_empty_catalog_{stamp}.yaml"
  catalog.parent.mkdir(parents=True, exist_ok=True)
  catalog.write_text("defaults: {}\nroutes: []\n", encoding="utf-8")
  env = {
    **os.environ,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": f"{TOOLS_ROOT}:{REPO_ROOT}:{os.environ.get('PYTHONPATH', '')}",
    "BRICKPILOT_TOOLS_ROOT": str(TOOLS_ROOT),
    "BRICKPILOT_REPO_ROOT": str(REPO_ROOT),
    "BRICKPILOT_DATA_ROOT": str(DATA_ROOT),
    "BRICKPILOT_DRIVE_DB_CONFIG": str(DB_CONFIG),
  }
  cmd = [
    sys.executable,
    "-m",
    "scripts.drive_tests.analyze_routes",
    "--catalog",
    str(catalog),
    "--out",
    str(out_dir),
    "--skip-report",
    "--skip-plots",
    "--max-segments",
    "0",
  ]
  proc = subprocess.run(cmd, cwd=TOOLS_ROOT, env=env, capture_output=True, text=True, timeout=180)
  return {
    "command": cmd,
    "cwd": str(TOOLS_ROOT),
    "returncode": proc.returncode,
    "stdout_tail": proc.stdout[-2000:],
    "stderr_tail": proc.stderr[-2000:],
    "out_dir": assert_inside(DATA_ROOT, out_dir),
    "created": sorted(p.name for p in out_dir.glob("*")) if out_dir.exists() else [],
  }


def main() -> int:
  stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
  manifest = {
    "schema": "brickpilot_split_tools_verify_v1",
    "created_at": stamp,
    "tools_root": str(TOOLS_ROOT),
    "repo_root": str(REPO_ROOT),
    "data_root": str(DATA_ROOT),
    "db_config": str(DB_CONFIG),
    "imports": verify_imports(),
    "logdrive_policy": verify_logdrive_policy(),
    "data_tools": verify_data_tools(),
    "analysis_smoke": run_analysis_smoke(stamp),
  }
  if manifest["analysis_smoke"]["returncode"] != 0:
    manifest["ok"] = False
  else:
    manifest["ok"] = True
  out = DATA_ROOT / "manifests" / f"tools_split_verify_{stamp}.json"
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({"ok": manifest["ok"], "manifest": str(out), "summary": manifest["data_tools"]["db_counts"], "analysis_out": manifest["analysis_smoke"]["out_dir"]}, indent=2, sort_keys=True))
  return 0 if manifest["ok"] else 1


if __name__ == "__main__":
  raise SystemExit(main())
