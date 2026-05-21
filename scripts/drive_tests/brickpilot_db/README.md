# Brickpilot central drive DB

Initial production scaffold for the MacBook-Pro source-of-truth drive database.

## Production setup (MacBook Pro)

1. Install/start PostgreSQL 16+ and Python driver (no sudo required on the MacBook):
   ```bash
   scripts/drive_tests/brickpilot_db/setup_postgres_macbook.sh
   ```
   Manual equivalent:
   ```bash
   brew install postgresql@16
   brew services start postgresql@16
   createuser brickpilot || true
   createdb -O brickpilot brickpilot_drive || true
   .venv/bin/python -m pip install 'psycopg[binary]'
   ```
2. Copy `scripts/drive_tests/brickpilot_db/brickpilot_db.example.toml` to `~/.config/brickpilot/drive_db.toml` and set a real password/token.
3. Apply schema:
   ```bash
   python3 -m scripts.drive_tests.brickpilot_db.migrate --config ~/.config/brickpilot/drive_db.toml
   ```
4. Inventory first, then import only after reviewing reconciliation output:
   ```bash
   python3 -m scripts.drive_tests.brickpilot_db.ingest --dry-run --json ~/BrickpilotDriveDB/imports/raw/from_comma ~/BrickpilotDriveDB/logdrive_runs > /tmp/drive_db_inventory.json
   python3 -m scripts.drive_tests.brickpilot_db.ingest --config ~/.config/brickpilot/drive_db.toml ~/BrickpilotDriveDB/imports/raw/from_comma
   ```

Runtime support: `DriveStore` uses Postgres through `psycopg` when `database_url` is `postgresql://...`; if the driver is missing it raises a setup error instead of pretending production writes work. SQLite remains the offline/test dialect.

## API

`drive_db_server.py` exposes local-token endpoints for review/logdrive integration:

- `GET /api/routes`
- `GET /api/routes/<uuid>/timeline`
- `GET /api/review/inboxes`
- `GET /api/review/inboxes/<id>/jobs`
- `GET /api/artifacts/<id>` (artifact ID only; no arbitrary file paths)
- `POST /api/labels`, `/api/delete-label`, `/api/finish-review`, `/api/import-voice`

Mutable label writes use optimistic `expected_version`; stale writes return HTTP 409.

### API token smoke

`drive_db_server.py` must be run with a non-empty `api_token` in `~/.config/brickpilot/drive_db.toml`. All `/api/*` requests require `Authorization: Bearer <token>`; smoke by confirming an unauthenticated `GET /api/routes` returns `401` and the same request with the token returns `200`. Do not commit or print the private config/token.

### /logdrive DB hook

The safe-to-turn-off gate remains `logdrive_automation verify`: the copied segment(s) must exist with nonzero matching sizes before the user is told they can turn the car off. After that gate, `logdrive_automation import-verified --run-id <run_id> --config ~/.config/brickpilot/drive_db.toml` can import the verified destination root into the central DB/artifact store. The command refuses to import if verification fails; use `--dry-run` for a local smoke without writing DB rows.

## Privacy and paths

Public API responses expose artifact IDs and artifact-root-relative paths only. Absolute source paths are retained in `source_paths` / `legacy_files` as private local provenance and should not be returned to clients.

## Backup/restore smoke

Run `python3 -m scripts.drive_tests.brickpilot_db.backup --config ~/.config/brickpilot/drive_db.toml`. Each backup writes `artifact_manifest.json`, consistency markers, and an executable `restore_smoke.sh` that can dry-run manifest verification or restore into a temp Postgres DB with `RESTORE_DATABASE_URL=... ./restore_smoke.sh restore`.
