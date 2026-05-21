from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, time
from pathlib import Path
from .config import load_config

def artifact_manifest(root: Path) -> list[dict]:
    rows=[]
    if not root.exists(): return rows
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h=hashlib.sha256(); size=0
            with p.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024), b''):
                    h.update(chunk); size += len(chunk)
            rows.append({"path": str(p.relative_to(root)), "sha256": h.hexdigest(), "size_bytes": size})
    return rows

def _write_restore_smoke(out: Path) -> None:
    script = '''#!/usr/bin/env bash
set -euo pipefail
MODE="${1:-dry-run}"
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 - "$HERE/artifact_manifest.json" "${ARTIFACT_ROOT:-}" <<'PY2'
import json, pathlib, sys, hashlib
manifest = json.load(open(sys.argv[1]))
root = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
print(f"manifest entries={len(manifest)}")
if root and root.exists():
  checked=0
  for row in manifest[:1000]:
    p=root/row['path']
    if p.exists():
      h=hashlib.sha256(p.read_bytes()).hexdigest(); assert h == row['sha256'], row['path']; checked += 1
  print(f"verified_existing_artifacts={checked}")
PY2
if [[ "$MODE" == "restore" ]]; then
  : "${RESTORE_DATABASE_URL:?set temp RESTORE_DATABASE_URL}"
  test -f "$HERE/brickpilot_drive.dump"
  PATH="/opt/homebrew/bin:/opt/homebrew/opt/postgresql@16/bin:$PATH"
  pg_restore --no-owner --exit-on-error --dbname "$RESTORE_DATABASE_URL" "$HERE/brickpilot_drive.dump"
  psql "$RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 -c "select version from schema_migrations order by applied_at desc limit 1;"
else
  echo "dry-run only; set MODE=restore RESTORE_DATABASE_URL=<temp db> to exercise pg_restore"
fi
'''
    target=out/'restore_smoke.sh'; target.write_text(script); target.chmod(0o755)

def backup(config_path: str | None=None, dry_run: bool=False) -> dict:
    cfg=load_config(config_path); ts=time.strftime('%Y%m%d_%H%M%S'); out=cfg.backup_root/ts
    result={"backup_dir": str(out), "database_url": cfg.database_url.split('@')[-1], "artifact_root": str(cfg.artifact_root), "consistency": "pg_dump transaction snapshot plus post-dump artifact manifest"}
    if dry_run: return result
    out.mkdir(parents=True, exist_ok=True)
    marker={"started_at": ts, "pid": os.getpid(), "note": "Writers should pause for backup or use DB-level advisory lock before calling this helper."}
    (out/'BACKUP_IN_PROGRESS.json').write_text(json.dumps(marker, indent=2))
    try:
        if cfg.is_postgres:
            dump=out/'brickpilot_drive.dump'; pg_dump = shutil.which('pg_dump') or '/opt/homebrew/bin/pg_dump'; subprocess.check_call([pg_dump,'--format=custom','--serializable-deferrable','--file',str(dump),cfg.database_url]); result['pg_dump']=str(dump)
        else: result['warning']='sqlite/offline mode: no pg_dump created; manifest and restore dry-run script still written'
        manifest=artifact_manifest(cfg.artifact_root); (out/'artifact_manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True)); result['artifact_count']=len(manifest)
        _write_restore_smoke(out); (out/'BACKUP_COMPLETE.json').write_text(json.dumps({**marker, "completed_at": time.strftime('%Y%m%d_%H%M%S'), "artifact_count": len(manifest)}, indent=2))
    finally:
        (out/'BACKUP_IN_PROGRESS.json').unlink(missing_ok=True)
    return result

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--config'); ap.add_argument('--dry-run', action='store_true'); ns=ap.parse_args(); print(json.dumps(backup(ns.config, ns.dry_run), indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
