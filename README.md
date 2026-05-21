# Brickpilot Research

Local tools, ML workflows, and a web UI for Brickpilot development and drive review.

This repository combines the Brickpilot research interface with the Python tools
used for route ingest, Drive DB maintenance, manual labels, voice bookmarks,
review videos, and offline analysis. It is designed to stay public-safe: raw
routes, videos, audio, databases, secrets, and generated analysis artifacts
belong outside the repo.

## Contents

- `src/`: React/Vite client plus the local Node/Express API.
- `scripts/drive_tests/`: route analysis, manual labeler, voice bookmark,
  Drive DB, logdrive ingest, replay, and ML helper scripts.
- `tools/`: repo-level verification and data reconciliation helpers.
- `tests/`: UI/server unit tests.

## Local Setup

```bash
npm install
npm run dev
```

The app listens on `http://127.0.0.1:8791`.

Configure paths with environment variables or a local `.env` file:

```bash
export BRICKPILOT_REPO_ROOT=/path/to/brickpilot
export BRICKPILOT_TOOLS_ROOT=/path/to/brickpilot-research
export BRICKPILOT_PYTHON=python3
export BRICKPILOT_DATA_ROOT=$HOME/BrickpilotDriveDB
export BRICKPILOT_DRIVE_DB_CONFIG=$HOME/.config/brickpilot/drive_db.toml
```

`BRICKPILOT_DRIVE_DB_ROOT` is accepted as a compatibility alias for
`BRICKPILOT_DATA_ROOT`.

## Verification

```bash
npm test
npm run typecheck
npm run build
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest scripts/drive_tests/tests
python3 tools/verify_split_tools.py
```

Some Python tools need an openpilot/Brickpilot checkout and local route data.
Set `BRICKPILOT_REPO_ROOT`, `BRICKPILOT_DATA_ROOT`, and
`BRICKPILOT_DRIVE_DB_CONFIG` before running those workflows.

## Data Boundary

Keep these outside git:

- raw comma routes and camera video
- voice recordings and transcripts
- SQLite/Postgres dumps and local DB configs
- generated labeler output, reports, plots, manifests, model artifacts
- any tokens, credentials, VINs, or private vehicle/user metadata
