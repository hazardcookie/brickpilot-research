#!/usr/bin/env python3
"""Deterministic /logdrive automation helpers.

This module is intentionally offline-first: discovery, copy planning, status, and
verification can all run against local fixtures without touching a comma device.
Real SSH/rsync execution is explicit and conservative.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

TOOLS_ROOT = Path(os.environ.get("BRICKPILOT_TOOLS_ROOT", Path(__file__).resolve().parents[2]))
REPO_ROOT = TOOLS_ROOT
OPENPILOT_REPO_ROOT = Path(os.environ.get("BRICKPILOT_REPO_ROOT", TOOLS_ROOT.parent / "brickpilot")).expanduser()
DEFAULT_REALDATA = Path("/data/media/0/realdata")
DEFAULT_DATA_ROOT = Path(os.environ.get("BRICKPILOT_DRIVE_DATA_ROOT", os.environ.get("BRICKPILOT_DATA_ROOT", Path.home() / "BrickpilotDriveDB"))).expanduser()
DEFAULT_RAW_ROOT = Path(os.environ.get("BRICKPILOT_LOGDRIVE_RAW_ROOT", DEFAULT_DATA_ROOT / "imports/raw/from_comma"))
DEFAULT_RUNS_ROOT = Path(os.environ.get("BRICKPILOT_LOGDRIVE_RUNS_ROOT", DEFAULT_DATA_ROOT / "logdrive_runs"))
LOG_FILE_NAMES = ("rlog.bz2", "rlog.zst", "rlog", "qlog.bz2", "qlog.zst", "qlog")
VIDEO_FILE_NAMES = (
  "fcamera.hevc",
  "dcamera.hevc",
  "ecamera.hevc",
  "qcamera.ts",
  "qcamera.hevc",
)
IMPORTANT_FILE_NAMES = LOG_FILE_NAMES + VIDEO_FILE_NAMES
SEGMENT_RE = re.compile(r"^(?P<route>.+)--(?P<segment>\d+)$")


class LogdriveError(RuntimeError):
  """User-correctable logdrive automation failure."""


class DriveType(enum.Enum):
  LABEL_VALIDATION = "label-validation"
  TEST = "test"
  NORMAL = "normal"

  @classmethod
  def parse(cls, value: str) -> "DriveType":
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
      "label": cls.LABEL_VALIDATION,
      "validation": cls.LABEL_VALIDATION,
      "label-validation": cls.LABEL_VALIDATION,
      "test": cls.TEST,
      "normal": cls.NORMAL,
    }
    try:
      return aliases[normalized]
    except KeyError as exc:
      allowed = ", ".join(dt.value for dt in cls)
      raise argparse.ArgumentTypeError(f"invalid drive type {value!r}; expected one of: {allowed}") from exc

  @property
  def include_video(self) -> bool:
    return self is DriveType.LABEL_VALIDATION

  @property
  def required_kinds(self) -> tuple[str, ...]:
    return ("logs", "video") if self.include_video else ("logs",)


@dataclasses.dataclass(frozen=True)
class Segment:
  route_id: str
  segment: int
  path: Path
  files: tuple[str, ...]
  size_bytes: int
  mtime: float

  @property
  def has_logs(self) -> bool:
    return any(name in LOG_FILE_NAMES or name.startswith(("rlog", "qlog")) for name in self.files)

  @property
  def has_video(self) -> bool:
    return any(name in VIDEO_FILE_NAMES or name.endswith((".hevc", ".ts")) for name in self.files)


@dataclasses.dataclass(frozen=True)
class Candidate:
  number: int
  route_id: str
  dongle_id: str | None
  segments: tuple[Segment, ...]
  start_mtime: float
  end_mtime: float
  total_size_bytes: int
  reason: str

  @property
  def segment_count(self) -> int:
    return len(self.segments)


@dataclasses.dataclass(frozen=True)
class CopyItem:
  source: str
  destination: str
  kind: str
  size_bytes: int
  required: bool


@dataclasses.dataclass(frozen=True)
class CopyPlan:
  run_id: str
  drive_type: DriveType
  route_id: str
  destination_root: str
  items: tuple[CopyItem, ...]
  dry_run: bool = True


def repo_path(path: str | Path) -> Path:
  p = Path(path)
  return p if p.is_absolute() else REPO_ROOT / p


def safe_route_dir_name(route_id: str) -> str:
  return re.sub(r"[^A-Za-z0-9_.=-]+", "_", route_id.replace("|", "_"))[:180]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
  fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      f.write(data)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp_name, path)
  finally:
    if os.path.exists(tmp_name):
      os.unlink(tmp_name)


def read_json(path: Path) -> dict[str, Any]:
  with path.open("r", encoding="utf-8") as f:
    return json.load(f)


def run_state_path(run_id: str, runs_root: Path = DEFAULT_RUNS_ROOT) -> Path:
  return runs_root / run_id / "status.json"


def update_run_state(run_id: str, status: str, runs_root: Path = DEFAULT_RUNS_ROOT, **extra: Any) -> dict[str, Any]:
  now = int(time.time())
  path = run_state_path(run_id, runs_root)
  state: dict[str, Any] = {}
  if path.exists():
    state = read_json(path)
  state.update({"run_id": run_id, "status": status, "updated_at": now})
  state.setdefault("created_at", now)
  state.update(extra)
  atomic_write_json(path, state)
  return state


def bounded_retry(
  action: Callable[[], Any],
  *,
  attempts: int = 3,
  initial_delay_sec: float = 0.5,
  backoff: float = 2.0,
  retry_exceptions: tuple[type[BaseException], ...] = (subprocess.SubprocessError, OSError),
) -> Any:
  if attempts < 1:
    raise ValueError("attempts must be >= 1")
  delay = initial_delay_sec
  last_exc: BaseException | None = None
  for attempt in range(1, attempts + 1):
    try:
      return action()
    except retry_exceptions as exc:
      last_exc = exc
      if attempt == attempts:
        break
      time.sleep(delay)
      delay *= backoff
  assert last_exc is not None
  raise last_exc


def segment_from_dir(path: Path) -> Segment | None:
  match = SEGMENT_RE.match(path.name)
  if not match or not path.is_dir():
    return None
  files = tuple(sorted(child.name for child in path.iterdir() if child.is_file() and child.name in IMPORTANT_FILE_NAMES))
  size = sum((path / name).stat().st_size for name in files)
  mtime = max([path.stat().st_mtime] + [(path / name).stat().st_mtime for name in files])
  return Segment(match.group("route"), int(match.group("segment")), path, files, size, mtime)


def discover_segments(realdata_root: Path) -> list[Segment]:
  root = Path(realdata_root)
  if not root.exists():
    raise LogdriveError(f"realdata root does not exist: {root}")
  segments: list[Segment] = []
  for child in root.iterdir():
    seg = segment_from_dir(child)
    if seg is not None:
      segments.append(seg)
  return sorted(segments, key=lambda s: (s.route_id, s.segment))


def route_dongle_id(route_id: str) -> str | None:
  return route_id.split("|", 1)[0] if "|" in route_id else None


def build_candidates(segments: Sequence[Segment], *, max_candidates: int = 8) -> list[Candidate]:
  grouped: dict[str, list[Segment]] = {}
  for seg in segments:
    grouped.setdefault(seg.route_id, []).append(seg)

  raw: list[Candidate] = []
  for route_id, segs in grouped.items():
    ordered = tuple(sorted(segs, key=lambda s: s.segment))
    size = sum(s.size_bytes for s in ordered)
    start = min(s.mtime for s in ordered)
    end = max(s.mtime for s in ordered)
    log_count = sum(1 for s in ordered if s.has_logs)
    video_count = sum(1 for s in ordered if s.has_video)
    if len(ordered) == 1:
      reason = "single-segment candidate; may be a tail/parking/offroad segment"
    else:
      reason = f"{len(ordered)} contiguous/plausible segments with logs in {log_count} segments"
    if video_count:
      reason += f" and video in {video_count} segments"
    raw.append(Candidate(0, route_id, route_dongle_id(route_id), ordered, start, end, size, reason))

  raw.sort(key=lambda c: (c.end_mtime, c.segment_count), reverse=True)
  return [dataclasses.replace(candidate, number=i) for i, candidate in enumerate(raw[:max_candidates], start=1)]


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
  return {
    "number": candidate.number,
    "route_id": candidate.route_id,
    "dongle_id": candidate.dongle_id,
    "segment_count": candidate.segment_count,
    "segments": [s.segment for s in candidate.segments],
    "start_mtime": candidate.start_mtime,
    "end_mtime": candidate.end_mtime,
    "total_size_bytes": candidate.total_size_bytes,
    "reason": candidate.reason,
  }


def format_candidate_list(candidates: Sequence[Candidate]) -> str:
  lines = []
  for c in candidates:
    segments = f"{c.segments[0].segment}..{c.segments[-1].segment}" if c.segments else "none"
    lines.append(
      f"{c.number}. {c.route_id} — {c.segment_count} segment(s) [{segments}], "
      f"{c.total_size_bytes} bytes, dongle={c.dongle_id or 'unknown'} — {c.reason}"
    )
  return "\n".join(lines)


def _copy_file_names_for_drive_type(drive_type: DriveType) -> set[str]:
  names = set(LOG_FILE_NAMES)
  if drive_type.include_video:
    names.update(VIDEO_FILE_NAMES)
  return names


def make_copy_plan(
  candidate: Candidate,
  *,
  run_id: str,
  drive_type: DriveType,
  raw_root: Path = DEFAULT_RAW_ROOT,
  dry_run: bool = True,
) -> CopyPlan:
  dest_root = raw_root / safe_route_dir_name(candidate.route_id)
  wanted = _copy_file_names_for_drive_type(drive_type)
  items: list[CopyItem] = []
  for segment in candidate.segments:
    seg_dest = dest_root / f"{segment.segment:04d}"
    for file_name in segment.files:
      if file_name not in wanted:
        continue
      kind = "video" if file_name in VIDEO_FILE_NAMES else "logs"
      items.append(CopyItem(str(segment.path / file_name), str(seg_dest / file_name), kind, (segment.path / file_name).stat().st_size, True))
  return CopyPlan(run_id, drive_type, candidate.route_id, str(dest_root), tuple(items), dry_run=dry_run)


def copy_plan_to_dict(plan: CopyPlan) -> dict[str, Any]:
  return {
    "run_id": plan.run_id,
    "drive_type": plan.drive_type.value,
    "required_kinds": list(plan.drive_type.required_kinds),
    "route_id": plan.route_id,
    "destination_root": plan.destination_root,
    "dry_run": plan.dry_run,
    "items": [dataclasses.asdict(item) for item in plan.items],
  }


def ensure_destination_under_raw(destination: Path, raw_root: Path = DEFAULT_RAW_ROOT) -> None:
  try:
    destination.resolve().relative_to(raw_root.resolve())
  except ValueError as exc:
    raise LogdriveError(f"refusing destination outside raw root: {destination}") from exc


def execute_copy_plan(plan: CopyPlan, *, raw_root: Path = DEFAULT_RAW_ROOT) -> None:
  """Execute a local fixture copy plan.

  Real comma ingestion should prefer rsync_copy_plan; this helper is only for local
  files/fixtures and is a no-op while plan.dry_run is true.
  """
  if plan.dry_run:
    return
  for item in plan.items:
    dest = Path(item.destination)
    ensure_destination_under_raw(dest, raw_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item.source, dest)


def rsync_copy_plan(
  plan: CopyPlan,
  *,
  raw_root: Path = DEFAULT_RAW_ROOT,
  execute: bool = False,
  attempts: int = 2,
) -> list[list[str]]:
  """Build or execute conservative per-file rsync commands.

  The default is command construction only. Execution requires both execute=True
  and plan.dry_run=False. Commands do not use --delete and destinations are
  constrained under the configured raw root (default ~/BrickpilotDriveDB/imports/raw/from_comma).
  """
  commands: list[list[str]] = []
  for item in plan.items:
    dest = Path(item.destination)
    ensure_destination_under_raw(dest, raw_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = ["rsync", "-a", "--partial", "--protect-args", item.source, str(dest)]
    commands.append(command)
    if execute and not plan.dry_run:
      def run(command: list[str] = command) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
      bounded_retry(run, attempts=attempts)
  return commands


def file_sha256(path: Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()


def verify_copy_plan(plan: CopyPlan, *, checksum: bool = False) -> dict[str, Any]:
  results: list[dict[str, Any]] = []
  ok = True
  seen_kinds: set[str] = set()
  for item in plan.items:
    dest = Path(item.destination)
    exists = dest.exists()
    size = dest.stat().st_size if exists else 0
    item_ok = exists and size == item.size_bytes and size > 0
    ok = ok and item_ok
    if item_ok:
      seen_kinds.add(item.kind)
    entry: dict[str, Any] = {
      "destination": item.destination,
      "kind": item.kind,
      "expected_size_bytes": item.size_bytes,
      "actual_size_bytes": size,
      "ok": item_ok,
    }
    if checksum and item_ok:
      entry["sha256"] = file_sha256(dest)
    results.append(entry)

  missing_kinds = [kind for kind in plan.drive_type.required_kinds if kind not in seen_kinds]
  ok = ok and not missing_kinds and bool(plan.items)
  return {
    "ok": ok,
    "run_id": plan.run_id,
    "route_id": plan.route_id,
    "drive_type": plan.drive_type.value,
    "missing_required_kinds": missing_kinds,
    "files": results,
  }


def load_plan(path: Path) -> CopyPlan:
  data = read_json(path)
  drive_type = DriveType.parse(data["drive_type"])
  items = tuple(CopyItem(**item) for item in data.get("items", []))
  return CopyPlan(data["run_id"], drive_type, data["route_id"], data["destination_root"], items, data.get("dry_run", True))


def snapshot_device_state(
  *,
  host: str = "comma@192.168.1.138",
  dry_run: bool = True,
  fixture: Path | None = None,
  timeout_sec: int = 15,
) -> dict[str, Any]:
  if fixture is not None:
    return read_json(fixture)
  commands = {
    "hostname": "hostname || true",
    "openpilot_branch": "cd /data/openpilot && git rev-parse --abbrev-ref HEAD || true",
    "openpilot_commit": "cd /data/openpilot && git rev-parse HEAD || true",
    "openpilot_status": "cd /data/openpilot && git status --short || true",
    "custom_brand_version": "cat /data/openpilot/common/version.h 2>/dev/null | grep CUSTOM_BRAND_VERSION || true",
    "params_model": "for k in ModelManager_ActiveBundle ModelManager_ActiveModel ModelManager_Generation ModelManager_Selector ModelRunner; do printf '%s=' \"$k\"; cat /data/params/d/$k 2>/dev/null || true; printf '\\n'; done",
    "recent_routes": "find /data/media/0/realdata -maxdepth 1 -type d -name '*--[0-9]*' -printf '%f %T@ %s\\n' 2>/dev/null | sort | tail -80 || true",
  }
  if dry_run:
    return {"dry_run": True, "host": host, "commands": commands}

  result: dict[str, Any] = {"dry_run": False, "host": host, "captured_at": int(time.time()), "outputs": {}}
  for key, command in commands.items():
    def run() -> subprocess.CompletedProcess[str]:
      return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout_sec}", host, command],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_sec + 5,
      )
    completed = bounded_retry(run, attempts=2)
    result["outputs"][key] = {
      "returncode": completed.returncode,
      "stdout": completed.stdout,
      "stderr": completed.stderr,
    }
  return result


def run_id_default() -> str:
  return time.strftime("%Y%m%d_%H%M%S")


def select_candidates(candidates: Sequence[Candidate], numbers: Sequence[int]) -> list[Candidate]:
  by_number = {candidate.number: candidate for candidate in candidates}
  selected = []
  for number in numbers:
    if number not in by_number:
      raise LogdriveError(f"candidate number not found: {number}")
    selected.append(by_number[number])
  return selected


def parse_numbers(raw: str) -> list[int]:
  if not raw:
    return []
  values = []
  for part in raw.replace(",", " ").split():
    values.append(int(part))
  return values


def cmd_discover(args: argparse.Namespace) -> int:
  run_id = args.run_id or run_id_default()
  segments = discover_segments(repo_path(args.realdata_root) if args.local else Path(args.realdata_root))
  candidates = build_candidates(segments, max_candidates=args.max_candidates)
  payload = {"run_id": run_id, "candidates": [candidate_to_dict(c) for c in candidates]}
  update_run_state(run_id, "discovered", repo_path(args.runs_root), **payload)
  if args.json:
    print(json.dumps(payload, indent=2, sort_keys=True))
  else:
    print(format_candidate_list(candidates))
  return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
  run_id = args.run_id or run_id_default()
  state = snapshot_device_state(host=args.host, dry_run=args.dry_run, fixture=repo_path(args.fixture) if args.fixture else None)
  out_path = repo_path(args.runs_root) / run_id / "snapshot.json"
  atomic_write_json(out_path, state)
  update_run_state(run_id, "snapshotted", repo_path(args.runs_root), snapshot_path=str(out_path))
  print(json.dumps(state, indent=2, sort_keys=True))
  return 0


def cmd_copy_plan(args: argparse.Namespace) -> int:
  run_id = args.run_id or run_id_default()
  drive_type = DriveType.parse(args.drive_type)
  segments = discover_segments(repo_path(args.realdata_root) if args.local else Path(args.realdata_root))
  candidates = build_candidates(segments, max_candidates=args.max_candidates)
  selected = select_candidates(candidates, parse_numbers(args.candidates))
  if len(selected) != 1:
    raise LogdriveError("first-slice copy-plan expects exactly one selected candidate")
  plan = make_copy_plan(selected[0], run_id=run_id, drive_type=drive_type, raw_root=repo_path(args.raw_root), dry_run=args.dry_run)
  plan_payload = copy_plan_to_dict(plan)
  plan_path = repo_path(args.runs_root) / run_id / "copy_plan.json"
  atomic_write_json(plan_path, plan_payload)
  update_run_state(run_id, "copy_planned", repo_path(args.runs_root), copy_plan_path=str(plan_path), copy_plan=plan_payload)
  print(json.dumps(plan_payload, indent=2, sort_keys=True))
  return 0


def ensure_validation_review_job(plan: CopyPlan, config_path: str | None = None) -> dict[str, Any]:
  """Ensure verified label-validation imports appear in the DB-backed human review UI."""
  if plan.drive_type is not DriveType.LABEL_VALIDATION:
    return {"created": False, "reason": "not label-validation"}
  from scripts.drive_tests.brickpilot_db.config import load_config
  from scripts.drive_tests.brickpilot_db.store import DriveStore

  cfg = load_config(config_path)
  store = DriveStore(cfg)
  route_uuid = store.upsert_route(plan.route_id, source_device=cfg.source_host_role, drive_type="label-validation", route_label=plan.route_id, segment_count=0, metadata={"logdrive_run_id": plan.run_id, "review_auto_created": True})
  inbox_id = store.create_inbox("logdrive_label_validation", "verified /logdrive label-validation drives awaiting human review")
  job_id = store.upsert_review_job(inbox_id, route_uuid, None, status="pending", selected=True, ride_type="label validation", route_label=plan.route_id)
  store.commit()
  return {"created": True, "inbox_id": inbox_id, "review_job_id": job_id, "route_uuid": route_uuid, "route_id": plan.route_id}


def cmd_verify(args: argparse.Namespace) -> int:
  run_id = args.run_id
  plan_path = repo_path(args.plan) if args.plan else repo_path(args.runs_root) / run_id / "copy_plan.json"
  plan = load_plan(plan_path)
  result = verify_copy_plan(plan, checksum=args.checksum)
  verify_path = repo_path(args.runs_root) / run_id / "verify.json"
  atomic_write_json(verify_path, result)
  status = "verified" if result["ok"] else "verify_failed"
  state_extra: dict[str, Any] = {"verify_path": str(verify_path), "verify": result}
  if result["ok"] and getattr(args, "db_import", True):
    try:
      from scripts.drive_tests.brickpilot_db.ingest import import_root
      payload = {"dry_run": True, "verified": result, "root": plan.destination_root} if args.db_import_dry_run else import_root(Path(plan.destination_root), args.db_config, dry_run=False)
      payload["verified"] = result
      if not args.db_import_dry_run:
        payload["review_job"] = ensure_validation_review_job(plan, args.db_config)
      out_path = repo_path(args.runs_root) / run_id / "drive_db_import.json"
      atomic_write_json(out_path, payload)
      status = "verified_db_imported" if not args.db_import_dry_run else "verified_db_import_dry_run"
      state_extra.update(drive_db_import_path=str(out_path), drive_db_import=payload)
    except Exception as exc:
      status = "verified_db_import_failed"
      state_extra.update(drive_db_import_error=str(exc))
      result = {**result, "db_import_error": str(exc)}
  update_run_state(run_id, status, repo_path(args.runs_root), **state_extra)
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0 if result["ok"] else 2



def cmd_import_verified(args: argparse.Namespace) -> int:
  """Import a verified /logdrive copy into the central DB/artifact store.

  This is intentionally opt-in and should be called only after verify_copy_plan
  returns ok=True (the safe-to-turn-off gate remains verification, not DB import).
  """
  run_id = args.run_id
  plan_path = repo_path(args.plan) if args.plan else repo_path(args.runs_root) / run_id / "copy_plan.json"
  plan = load_plan(plan_path)
  verify = verify_copy_plan(plan, checksum=args.checksum)
  if not verify.get("ok"):
    raise LogdriveError("refusing DB import: copy plan is not verified")
  if args.dry_run:
    payload = {"dry_run": True, "verified": verify, "root": plan.destination_root}
  else:
    from scripts.drive_tests.brickpilot_db.ingest import import_root
    payload = import_root(Path(plan.destination_root), args.config, dry_run=False)
    payload["verified"] = verify
    payload["review_job"] = ensure_validation_review_job(plan, args.config)
  out_path = repo_path(args.runs_root) / run_id / "drive_db_import.json"
  atomic_write_json(out_path, payload)
  update_run_state(run_id, "db_imported" if not args.dry_run else "db_import_dry_run", repo_path(args.runs_root), drive_db_import_path=str(out_path), drive_db_import=payload)
  print(json.dumps(payload, indent=2, sort_keys=True))
  return 0

def cmd_status(args: argparse.Namespace) -> int:
  path = run_state_path(args.run_id, repo_path(args.runs_root))
  if not path.exists():
    raise LogdriveError(f"no status for run_id={args.run_id}")
  print(json.dumps(read_json(path), indent=2, sort_keys=True))
  return 0


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Deterministic /logdrive discovery, planning, snapshot, and verification")
  parser.set_defaults(func=None)
  common = argparse.ArgumentParser(add_help=False)
  common.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
  common_with_optional_run = argparse.ArgumentParser(add_help=False, parents=[common])
  common_with_optional_run.add_argument("--run-id")

  discover = parser.add_subparsers(dest="command", required=True)

  p = discover.add_parser("discover", parents=[common_with_optional_run], help="discover local/fixture comma route candidates")
  p.add_argument("--realdata-root", default=str(DEFAULT_REALDATA))
  p.add_argument("--local", action="store_true", help="resolve realdata-root relative to the repo")
  p.add_argument("--max-candidates", type=int, default=8)
  p.add_argument("--json", action="store_true")
  p.set_defaults(func=cmd_discover)

  p = discover.add_parser("snapshot", parents=[common_with_optional_run], help="snapshot comma software/settings/model state")
  p.add_argument("--host", default="comma@192.168.1.138")
  p.add_argument("--dry-run", action="store_true", default=False)
  p.add_argument("--fixture", help="local JSON fixture to use instead of SSH")
  p.set_defaults(func=cmd_snapshot)

  p = discover.add_parser("copy-plan", parents=[common_with_optional_run], help="write a dry-run copy plan for selected candidate")
  p.add_argument("--realdata-root", required=True)
  p.add_argument("--local", action="store_true")
  p.add_argument("--candidates", required=True, help="candidate number, e.g. '1'")
  p.add_argument("--drive-type", type=str, default=DriveType.TEST.value)
  p.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
  p.add_argument("--max-candidates", type=int, default=8)
  p.add_argument("--dry-run", action="store_true", default=True)
  p.set_defaults(func=cmd_copy_plan)

  p = discover.add_parser("verify", parents=[common], help="verify destinations in a copy plan, then import verified local copy into Drive DB by default")
  p.add_argument("--run-id", required=True)
  p.add_argument("--plan")
  p.add_argument("--checksum", action="store_true")
  p.add_argument("--db-import", dest="db_import", action="store_true", default=True, help="import into central DB after verification succeeds (default)")
  p.add_argument("--no-db-import", dest="db_import", action="store_false", help="only verify the copied files; do not import into Drive DB")
  p.add_argument("--db-config", help="Drive DB config path for post-verify import")
  p.add_argument("--db-import-dry-run", action="store_true", help="record the post-verify DB import plan without writing DB rows")
  p.set_defaults(func=cmd_verify)

  p = discover.add_parser("import-verified", parents=[common], help="opt-in central DB import after a verified copy plan")
  p.add_argument("--run-id", required=True)
  p.add_argument("--plan")
  p.add_argument("--config")
  p.add_argument("--checksum", action="store_true")
  p.add_argument("--dry-run", action="store_true")
  p.set_defaults(func=cmd_import_verified)

  p = discover.add_parser("status", parents=[common], help="show run status JSON")
  p.add_argument("--run-id", required=True)
  p.set_defaults(func=cmd_status)
  return parser


def main(argv: Sequence[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  try:
    return args.func(args)
  except LogdriveError as exc:
    parser.exit(1, f"logdrive: {exc}\n")


if __name__ == "__main__":
  raise SystemExit(main())
