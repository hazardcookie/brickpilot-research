from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os, tomllib

DATA_ROOT = Path(os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB")).expanduser()
DEFAULT_CONFIG = Path(os.environ.get("BRICKPILOT_DRIVE_DB_CONFIG", Path.home() / ".config" / "brickpilot" / "drive_db.toml")).expanduser()
DEFAULT_ARTIFACT_ROOT = Path(os.environ.get("BRICKPILOT_DRIVE_ARTIFACT_ROOT", DATA_ROOT / "artifacts")).expanduser()
DEFAULT_BACKUP_ROOT = Path(os.environ.get("BRICKPILOT_BACKUP_ROOT", DATA_ROOT / "backups")).expanduser()
@dataclass(frozen=True)
class DriveDbConfig:
    database_url: str = "sqlite:///:memory:"
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    backup_root: Path = DEFAULT_BACKUP_ROOT
    api_bind: str = "127.0.0.1"
    api_port: int = 8766
    api_token: str | None = None
    source_host_role: str = "macbook"
    privacy_default: str = "local-only"
    @property
    def is_postgres(self) -> bool: return self.database_url.startswith(("postgres://", "postgresql://"))
    @property
    def is_sqlite(self) -> bool: return self.database_url.startswith("sqlite://")
def load_config(path: str | os.PathLike[str] | None = None) -> DriveDbConfig:
    cfg_path = Path(path or os.environ.get("BRICKPILOT_DRIVE_DB_CONFIG", DEFAULT_CONFIG)); data = {}
    if cfg_path.exists(): data = tomllib.loads(cfg_path.read_text())
    return DriveDbConfig(
        database_url=os.environ.get("BRICKPILOT_DRIVE_DATABASE_URL") or data.get("database_url", "sqlite:///:memory:"),
        artifact_root=Path(os.environ.get("BRICKPILOT_DRIVE_ARTIFACT_ROOT") or data.get("artifact_root", DEFAULT_ARTIFACT_ROOT)),
        backup_root=Path(data.get("backup_root", DEFAULT_BACKUP_ROOT)), api_bind=data.get("api_bind", "127.0.0.1"),
        api_port=int(data.get("api_port", 8766)), api_token=os.environ.get("BRICKPILOT_DRIVE_API_TOKEN") or data.get("api_token"),
        source_host_role=data.get("source_host_role", "macbook"), privacy_default=data.get("privacy_default", "local-only"))
