from __future__ import annotations

import os
from pathlib import Path


def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


TOOLS_ROOT = env_path("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2])
OPENPILOT_REPO_ROOT = env_path("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")
DATA_ROOT = env_path("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB")
DB_CONFIG = env_path("BRICKPILOT_DRIVE_DB_CONFIG", Path.home() / ".config" / "brickpilot" / "drive_db.toml")
ANALYSIS_EXPORT_ROOT = env_path("BRICKPILOT_ANALYSIS_ROOT", DATA_ROOT / "analysis_exports")
BACKUP_ROOT = env_path("BRICKPILOT_BACKUP_ROOT", DATA_ROOT / "backups")
