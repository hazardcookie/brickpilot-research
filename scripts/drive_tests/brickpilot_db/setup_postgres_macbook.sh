#!/usr/bin/env bash
set -euo pipefail
DB_NAME="${BRICKPILOT_DRIVE_DB_NAME:-brickpilot_drive}"
DB_USER="${BRICKPILOT_DRIVE_DB_USER:-brickpilot}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if ! command -v brew >/dev/null; then echo "Homebrew not found; install Homebrew first" >&2; exit 2; fi
if ! command -v psql >/dev/null; then
  echo "Installing PostgreSQL 16 with Homebrew (no sudo)"
  brew install postgresql@16
  export PATH="$(brew --prefix postgresql@16)/bin:$PATH"
fi
brew services start postgresql@16 || true
createuser "$DB_USER" 2>/dev/null || true
createdb -O "$DB_USER" "$DB_NAME" 2>/dev/null || true
if [[ -x "$PYTHON_BIN" ]]; then "$PYTHON_BIN" -m pip install 'psycopg[binary]'; else python3 -m pip install 'psycopg[binary]'; fi
cat <<EOF
Postgres preflight complete.
Set database_url in ~/.config/brickpilot/drive_db.toml, e.g.:
database_url = "postgresql://$DB_USER@localhost/$DB_NAME"
Then run: python3 -m scripts.drive_tests.brickpilot_db.migrate --config ~/.config/brickpilot/drive_db.toml
EOF
