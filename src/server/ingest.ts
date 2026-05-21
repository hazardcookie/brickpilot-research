import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import type { BrickpilotPaths } from "./lib";
import { fileSizeIfExists, progressFromSizes, safeJoin } from "./lib";
import type { IngestCandidate, IngestConnection, IngestProgress, IngestRun, IngestRideType } from "../shared/types";

const commaWifiTarget = process.env.BRICKPILOT_COMMA_WIFI_SSH || process.env.BRICKPILOT_COMMA_SSH || "comma@192.168.1.138";
const commaUsbTarget = process.env.BRICKPILOT_COMMA_USB_SSH || "comma@192.168.43.1";
const commaWifiPort = process.env.BRICKPILOT_COMMA_WIFI_PORT || process.env.BRICKPILOT_COMMA_SSH_PORT || "";
const commaUsbPort = process.env.BRICKPILOT_COMMA_USB_PORT || "";
const commaUsbMode = (process.env.BRICKPILOT_COMMA_USB_MODE || "adb").toLowerCase();
const commaAdbSshPort = process.env.BRICKPILOT_COMMA_ADB_SSH_PORT || "2222";
const realdataRoot = process.env.BRICKPILOT_COMMA_REALDATA || "/data/media/0/realdata";
const segmentRe = /^(?<route>.+)--(?<segment>\d+)$/;
const routeIdRe = /^[A-Za-z0-9_.|=-]+$/;
const logFileNames = new Set(["rlog.bz2", "rlog.zst", "rlog", "qlog.bz2", "qlog.zst", "qlog"]);
const videoFileNames = new Set(["fcamera.hevc", "dcamera.hevc", "ecamera.hevc", "qcamera.ts", "qcamera.hevc"]);

interface RemoteSegmentFile {
  segment: number;
  segmentName: string;
  remotePath: string;
  fileName: string;
  sizeBytes: number;
  mtime: number;
}

interface WebIngestState {
  activeRun?: IngestRun;
  history?: IngestRun[];
}

const cancelRequestedRunIds = new Set<string>();
const activeStatuses = new Set<IngestRun["status"]>(["discovering", "copying", "importing"]);

class IngestCancelled extends Error {
  constructor(message = "Ingest canceled by user") {
    super(message);
    this.name = "IngestCancelled";
  }
}

interface SshTarget {
  target: string;
  port?: string;
  ephemeralHostKey?: boolean;
}

export function canonicalIngestRideType(value: unknown): IngestRideType {
  const normalized = String(value || "").trim().toLowerCase().replaceAll("_", " ").replaceAll("-", " ");
  if (normalized.includes("label") || normalized.includes("validation")) return "label validation";
  if (normalized.includes("test")) return "test drive";
  return "normal drive";
}

export function canonicalIngestConnection(value: unknown): IngestConnection {
  const normalized = String(value || "").trim().toLowerCase().replaceAll("_", " ").replaceAll("-", " ");
  return normalized.includes("usb") ? "usb" : "wifi";
}

export function canonicalIncludeVideo(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  const normalized = String(value || "").trim().toLowerCase().replaceAll("_", " ").replaceAll("-", " ");
  return ["1", "true", "yes", "y", "on", "include video", "video"].includes(normalized);
}

function directSshTargetForConnection(connection: IngestConnection): SshTarget {
  return connection === "usb" ? { target: commaUsbTarget, port: commaUsbPort } : { target: commaWifiTarget, port: commaWifiPort };
}

function sshArgsForTarget(target: SshTarget, remoteCommand: string): string[] {
  const port = target.port;
  return [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    ...(target.ephemeralHostKey ? [
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-o", "LogLevel=ERROR",
    ] : []),
    ...(port ? ["-p", port] : []),
    target.target,
    remoteCommand
  ];
}

export function safeRouteDirName(routeId: string): string {
  if (/[\\/]/.test(routeId) || routeId.includes("..")) throw new Error("invalid route id");
  const safe = routeId.replaceAll("|", "_").replace(/[^A-Za-z0-9_.=-]+/g, "_").slice(0, 180);
  if (!safe || !routeIdRe.test(safe)) throw new Error("invalid route id");
  return safe;
}

export function ingestFileKind(fileName: string): "logs" | "video" | "ignore" {
  const name = path.basename(fileName).toLowerCase();
  if (logFileNames.has(name) || name.startsWith("rlog") || name.startsWith("qlog")) return "logs";
  if (videoFileNames.has(name) || name.includes("camera") || name.endsWith(".hevc") || name.endsWith(".ts")) return "video";
  return "ignore";
}

export function shouldCopyForRideType(fileName: string, _rideType: IngestRideType, includeVideoInput: unknown = false): boolean {
  const kind = ingestFileKind(fileName);
  if (kind === "logs") return true;
  return kind === "video" && canonicalIncludeVideo(includeVideoInput);
}

export function destinationForRemoteFile(paths: BrickpilotPaths, routeId: string, segmentName: string, fileName: string): string {
  const routeDir = safeRouteDirName(routeId);
  const safeSegment = safeRouteDirName(segmentName);
  const safeFile = path.basename(fileName);
  return safeJoin(path.join(paths.dataRoot, "imports", "raw", "from_comma"), path.join(routeDir, "segments", safeSegment, safeFile));
}

function statePath(paths: BrickpilotPaths): string {
  return path.join(paths.dataRoot, "logdrive_runs", "web-ingest-state.json");
}

async function readState(paths: BrickpilotPaths): Promise<WebIngestState> {
  try {
    return JSON.parse(await fsp.readFile(statePath(paths), "utf8")) as WebIngestState;
  } catch {
    return {};
  }
}

async function writeState(paths: BrickpilotPaths, state: WebIngestState): Promise<void> {
  const filePath = statePath(paths);
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  await fsp.writeFile(filePath, JSON.stringify(state, null, 2) + "\n");
}

function appendHistory(history: IngestRun[] | undefined, run: IngestRun): IngestRun[] {
  const rows = history || [];
  const exists = rows.some((item) => item.run_id === run.run_id && item.status === run.status);
  return exists ? rows : [...rows.slice(-19), run];
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function run(cmd: string, args: string[], timeoutMs = 30000, cwd?: string, env?: NodeJS.ProcessEnv): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`${cmd} timed out`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      code === 0 ? resolve(stdout) : reject(new Error(stderr.trim() || `${cmd} exited ${code}`));
    });
  });
}

async function executablePath(name: string): Promise<string | null> {
  try {
    const out = await run("sh", ["-lc", `command -v ${shellQuote(name)}`], 5000);
    return out.trim() || null;
  } catch {
    return null;
  }
}

function adbDeviceState(devicesOut: string): { hasDevice: boolean; detail: string } {
  const rows = devicesOut.trim().split(/\r?\n/).slice(1).map((line) => line.trim()).filter(Boolean);
  const connected = rows.filter((line) => /\tdevice(?:\s|$)/.test(line));
  if (connected.length) return { hasDevice: true, detail: connected.join(", ") };
  if (rows.length) return { hasDevice: false, detail: rows.join(", ") };
  return { hasDevice: false, detail: "" };
}

async function adbUsbSshTarget(): Promise<SshTarget> {
  const adbPath = await executablePath("adb");
  if (!adbPath) {
    throw new Error("USB ingest needs Android platform tools: adb is not installed. Install with `brew install android-platform-tools`, or set BRICKPILOT_COMMA_USB_MODE=direct for a USB network SSH target.");
  }
  const devicesOut = await run(adbPath, ["devices"], 10000);
  const deviceState = adbDeviceState(devicesOut);
  if (!deviceState.hasDevice) {
    const detail = deviceState.detail ? ` adb devices: ${deviceState.detail}.` : "";
    throw new Error(`USB ingest could not see the comma over ADB.${detail} Enable ADB on the comma, plug into its data USB port, and use a data-capable cable.`);
  }
  // ADB forwards are lost across cable/device cycles. Refresh this on every USB
  // setup instead of caching the local SSH target for the process lifetime.
  await run(adbPath, ["forward", `tcp:${commaAdbSshPort}`, "tcp:22"], 10000);
  return { target: "comma@127.0.0.1", port: commaAdbSshPort, ephemeralHostKey: true };
}

async function sshTargetForConnection(connection: IngestConnection): Promise<SshTarget> {
  if (connection !== "usb") return directSshTargetForConnection(connection);
  if (commaUsbMode === "direct") return directSshTargetForConnection(connection);
  if (commaUsbMode === "auto") {
    try {
      return await adbUsbSshTarget();
    } catch (adbErr) {
      try {
        return directSshTargetForConnection(connection);
      } catch {
        throw adbErr;
      }
    }
  }
  return adbUsbSshTarget();
}

export function settingsManifestDestination(paths: BrickpilotPaths, routeId: string): string {
  const routeDir = safeRouteDirName(routeId);
  return safeJoin(path.join(paths.dataRoot, "imports", "raw", "from_comma"), path.join(routeDir, "settings_manifest.json"));
}

export function settingsSnapshotRemoteCommand(routeId: string, rideType: IngestRideType, connection: IngestConnection = "wifi"): string {
  return [
    `BRICKPILOT_ROUTE_ID=${shellQuote(routeId)}`,
    `BRICKPILOT_RIDE_TYPE=${shellQuote(rideType)}`,
    `BRICKPILOT_INGEST_CONNECTION=${shellQuote(connection)}`,
    "python3 - <<'PY'",
    String.raw`
import base64, hashlib, json, os, re, subprocess, time
from pathlib import Path

SENSITIVE = ("token", "secret", "password", "private", "ssh", "athena", "jwt", "prime", "github")
MAX_RAW_BYTES = 2_000_000

def run(cmd, cwd=None):
  try:
    return subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=4).strip()
  except Exception:
    return ""

def text_value(data):
  try:
    text = data.decode("utf-8")
  except Exception:
    return None
  if "\x00" in text:
    return None
  return text.strip()

def read_text(path):
  try:
    return Path(path).read_text(errors="replace")
  except Exception:
    return ""

def py_const(text, name):
  m = re.search(rf"^\s*{re.escape(name)}\s*:\s*str\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
  if not m:
    m = re.search(rf"^\s*{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
  return m.group(1).strip() if m else ""

params_dir = next((p for p in (Path("/data/params/d"), Path("/persist/comma/params/d")) if p.exists()), None)
params = {}
params_raw = {}
if params_dir:
  for p in sorted(params_dir.iterdir()):
    if not p.is_file():
      continue
    try:
      data = p.read_bytes()
    except Exception as exc:
      params_raw[p.name] = {"error": str(exc)[:240]}
      continue
    is_sensitive = any(s in p.name.lower() for s in SENSITIVE)
    entry = {
      "size_bytes": len(data),
      "sha256": hashlib.sha256(data).hexdigest(),
      "mtime": p.stat().st_mtime,
    }
    if is_sensitive:
      entry["omitted_raw_reason"] = "sensitive_param_redacted"
    elif len(data) <= MAX_RAW_BYTES:
      entry["base64"] = base64.b64encode(data).decode("ascii")
    else:
      entry["omitted_raw_reason"] = "larger than MAX_RAW_BYTES"
    text = text_value(data)
    if text is not None:
      entry["text"] = "[redacted]" if is_sensitive else text
      params[p.name] = entry["text"]
    params_raw[p.name] = entry

repo = next((p for p in (Path("/data/openpilot"), Path("/data/sunnypilot"), Path("/data/brickpilot")) if (p / ".git").exists()), None)
version_py = read_text(repo / "system/version.py") if repo else ""
longitudinal_py = read_text(repo / "selfdrive/controls/lib/brickpilot_longitudinal.py") if repo else ""
brand = py_const(version_py, "CUSTOM_BRAND_NAME") or "Brickpilot"
brickpilot_version = py_const(version_py, "CUSTOM_BRAND_VERSION") or py_const(longitudinal_py, "BRICKPILOT_LONGITUDINAL_VERSION")
longitudinal_version = py_const(longitudinal_py, "BRICKPILOT_LONGITUDINAL_VERSION")
software = {
  "repo": str(repo) if repo else "",
  "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo)) if repo else "",
  "commit": run(["git", "rev-parse", "HEAD"], cwd=str(repo)) if repo else "",
  "remote": run(["git", "config", "--get", "remote.origin.url"], cwd=str(repo)) if repo else "",
  "dirty": bool(run(["git", "status", "--short"], cwd=str(repo))) if repo else False,
  "brand": brand,
  "version": brickpilot_version,
  "brickpilot_version": brickpilot_version,
  "display_version": (brand + " " + brickpilot_version).strip() if brickpilot_version else "",
  "longitudinal_version": longitudinal_version,
  "openpilot_version": params.get("Version", ""),
}
print(json.dumps({
  "schema_version": 1,
  "source": "web_ingest_remote_settings",
  "status": "ok" if params_dir else "params_dir_missing",
  "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "route_id": os.environ.get("BRICKPILOT_ROUTE_ID", ""),
  "ride_type": os.environ.get("BRICKPILOT_RIDE_TYPE", ""),
  "ingest_connection": os.environ.get("BRICKPILOT_INGEST_CONNECTION", ""),
  "params_dir": str(params_dir) if params_dir else "",
  "software": software,
  "params": params,
  "params_raw": params_raw,
}, sort_keys=True))
`,
    "PY"
  ].join("\n");
}

export async function writeRemoteSettingsManifest(paths: BrickpilotPaths, routeId: string, rideType: IngestRideType, connection: IngestConnection = "wifi"): Promise<string> {
  const dest = settingsManifestDestination(paths, routeId);
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  let manifest: Record<string, unknown>;
  let resolvedTarget: SshTarget | null = null;
  try {
    const target = await sshTargetForConnection(connection);
    resolvedTarget = target;
    const stdout = await run(
      "ssh",
      sshArgsForTarget(target, settingsSnapshotRemoteCommand(routeId, rideType, connection)),
      45000
    );
    manifest = JSON.parse(stdout || "{}") as Record<string, unknown>;
    manifest.status = manifest.status || "ok";
  } catch (err) {
    manifest = {
      schema_version: 1,
      source: "web_ingest_remote_settings",
      status: "error",
      captured_at: new Date().toISOString(),
      route_id: routeId,
      ride_type: rideType,
      ingest_connection: connection,
      error: err instanceof Error ? err.message : String(err)
    };
  }
  manifest.route_id = routeId;
  manifest.ride_type = rideType;
  manifest.ingest_connection = connection;
  manifest.ingest_target = resolvedTarget?.target || directSshTargetForConnection(connection).target;
  manifest.local_manifest_path = dest;
  await fsp.writeFile(dest, JSON.stringify(manifest, null, 2) + "\n", "utf8");
  return dest;
}

async function remoteSegmentFiles(limit = 12, connection: IngestConnection = "wifi"): Promise<RemoteSegmentFile[]> {
  const script = [
    "set -eu",
    `cd ${shellQuote(realdataRoot)}`,
    "find . -mindepth 1 -maxdepth 1 -type d -name '*--[0-9]*' -printf '%T@\\t%f\\n' | sort -nr | head -" + Number(limit * 16),
    "true"
  ].join("; ");
  const target = await sshTargetForConnection(connection);
  const segmentsOut = await run("ssh", sshArgsForTarget(target, script), 30000);
  const segmentNames = segmentsOut.trim().split(/\r?\n/).filter(Boolean).map((line) => line.split("\t")[1]).filter(Boolean);
  if (!segmentNames.length) return [];
  const filePredicates = [...logFileNames, ...videoFileNames].map((name) => `-name ${shellQuote(name)}`).join(" -o ");
  const listScript = [
    "set -eu",
    `cd ${shellQuote(realdataRoot)}`,
    ...segmentNames.map((seg) => `find ${shellQuote(seg)} -maxdepth 1 -type f \\( ${filePredicates} \\) -printf '%h\\t%f\\t%s\\t%T@\\n'`)
  ].join("; ");
  const filesOut = await run("ssh", sshArgsForTarget(target, listScript), 60000);
  return filesOut.trim().split(/\r?\n/).filter(Boolean).flatMap((line) => {
    const [segmentName, fileName, size, mtime] = line.split("\t");
    const match = segmentRe.exec(path.basename(segmentName || ""));
    if (!match?.groups) return [];
    return [{
      segment: Number(match.groups.segment),
      segmentName: path.basename(segmentName),
      remotePath: `${realdataRoot}/${segmentName}/${fileName}`,
      fileName,
      sizeBytes: Number(size || 0),
      mtime: Number(mtime || 0)
    }];
  });
}

function candidatesFromFiles(files: RemoteSegmentFile[], limit: number): IngestCandidate[] {
  const grouped = new Map<string, RemoteSegmentFile[]>();
  for (const file of files) {
    const match = segmentRe.exec(file.segmentName);
    if (!match?.groups) continue;
    grouped.set(match.groups.route, [...(grouped.get(match.groups.route) || []), file]);
  }
  const candidates = [...grouped.entries()].map(([routeId, routeFiles]) => {
    const segments = new Map<number, RemoteSegmentFile[]>();
    for (const file of routeFiles) segments.set(file.segment, [...(segments.get(file.segment) || []), file]);
    const logFiles = routeFiles.filter((file) => ingestFileKind(file.fileName) === "logs");
    const videoFiles = routeFiles.filter((file) => ingestFileKind(file.fileName) === "video");
    const newest = Math.max(...routeFiles.map((file) => file.mtime), 0);
    return {
      route_id: routeId,
      segment_count: segments.size,
      segments: [...segments.keys()].sort((a, b) => a - b),
      updated_at: newest ? new Date(newest * 1000).toISOString() : null,
      log_file_count: logFiles.length,
      video_file_count: videoFiles.length,
      total_file_count: routeFiles.length,
      log_bytes: logFiles.reduce((sum, file) => sum + file.sizeBytes, 0),
      video_bytes: videoFiles.reduce((sum, file) => sum + file.sizeBytes, 0),
      total_bytes: routeFiles.reduce((sum, file) => sum + file.sizeBytes, 0),
      reason: `${segments.size} segment(s), ${logFiles.length} log file(s), ${videoFiles.length} video file(s)`
    } satisfies IngestCandidate;
  });
  return candidates.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""))).slice(0, limit);
}

export async function discoverIngestCandidates(_paths: BrickpilotPaths, limit = 10, connectionInput: unknown = "wifi"): Promise<IngestCandidate[]> {
  return candidatesFromFiles(await remoteSegmentFiles(limit, canonicalIngestConnection(connectionInput)), limit);
}

async function filesForCandidate(paths: BrickpilotPaths, routeId: string, rideType: IngestRideType, connection: IngestConnection, includeVideo: boolean): Promise<IngestRun["files"]> {
  const allFiles = await remoteSegmentFiles(20, connection);
  const selected = allFiles.filter((file) => {
    const match = segmentRe.exec(file.segmentName);
    return match?.groups?.route === routeId && shouldCopyForRideType(file.fileName, rideType, includeVideo);
  });
  return selected.map((file) => {
    const dest = destinationForRemoteFile(paths, routeId, file.segmentName, file.fileName);
    return {
      rel: path.relative(path.join(paths.dataRoot, "imports", "raw", "from_comma"), dest),
      remote: file.remotePath,
      dest,
      tmp: `${dest}.part`,
      expectedBytes: file.sizeBytes,
      remoteMtime: file.mtime,
      kind: ingestFileKind(file.fileName) as "logs" | "video"
    };
  });
}

async function removePartialFiles(run: IngestRun): Promise<void> {
  await Promise.all(run.files.map(async (file) => {
    try {
      await fsp.rm(file.tmp, { force: true });
    } catch {
      // Best-effort cleanup; the active SSH copy may still be unwinding.
    }
  }));
}

async function copyOneAttempt(file: IngestRun["files"][number], connection: IngestConnection, isCanceled?: () => boolean): Promise<void> {
  if (isCanceled?.()) throw new IngestCancelled();
  await fsp.mkdir(path.dirname(file.tmp), { recursive: true });
  const target = await sshTargetForConnection(connection);
  await new Promise<void>((resolve, reject) => {
    const ssh = spawn("ssh", sshArgsForTarget(target, `cat ${shellQuote(file.remote)}`));
    const out = fs.createWriteStream(file.tmp);
    let lastProgress = Date.now();
    ssh.stdout.on("data", () => { lastProgress = Date.now(); });
    ssh.stdout.pipe(out);
    let stderr = "";
    let settled = false;
    let idleTimer: NodeJS.Timeout | undefined;
    const finishWithCleanup = (err?: Error) => {
      if (settled) return;
      settled = true;
      clearInterval(cancelTimer);
      if (idleTimer) clearInterval(idleTimer);
      err ? reject(err) : resolve();
    };
    const cancelTimer = setInterval(() => {
      if (!isCanceled?.()) return;
      ssh.kill("SIGTERM");
      out.destroy();
      finishWithCleanup(new IngestCancelled());
    }, 350);
    idleTimer = setInterval(() => {
      if (settled || isCanceled?.()) return;
      if (Date.now() - lastProgress < 20000) return;
      ssh.kill("SIGTERM");
      out.destroy();
      finishWithCleanup(new Error(`copy stalled for ${file.rel}`));
    }, 1000);
    ssh.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    ssh.on("error", (err) => finishWithCleanup(err));
    out.on("error", (err) => {
      if (isCanceled?.()) return finishWithCleanup(new IngestCancelled());
      finishWithCleanup(err);
    });
    ssh.on("close", (code) => {
      out.close();
      if (isCanceled?.()) return finishWithCleanup(new IngestCancelled());
      code === 0 ? finishWithCleanup() : finishWithCleanup(new Error(stderr.trim() || `copy failed for ${file.rel}`));
    });
  });
  if (isCanceled?.()) throw new IngestCancelled();
  const size = await fileSizeIfExists(file.tmp);
  if (size !== file.expectedBytes || size <= 0) throw new Error(`copy verification failed for ${file.rel}`);
  await fsp.rename(file.tmp, file.dest);
  if (Number(file.remoteMtime) > 0) {
    await fsp.utimes(file.dest, Number(file.remoteMtime), Number(file.remoteMtime));
  }
}

async function copyOne(file: IngestRun["files"][number], connection: IngestConnection, isCanceled?: () => boolean): Promise<void> {
  if ((await fileSizeIfExists(file.dest)) === file.expectedBytes) {
    if (Number(file.remoteMtime) > 0) {
      await fsp.utimes(file.dest, Number(file.remoteMtime), Number(file.remoteMtime));
    }
    return;
  }
  let lastError: Error | null = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      await copyOneAttempt(file, connection, isCanceled);
      return;
    } catch (err) {
      if (err instanceof IngestCancelled) throw err;
      lastError = err instanceof Error ? err : new Error(String(err));
      await fsp.rm(file.tmp, { force: true });
      if (attempt < 3) await sleep(750 * attempt);
    }
  }
  throw lastError || new Error(`copy failed for ${file.rel}`);
}

async function runImport(paths: BrickpilotPaths, routeId: string): Promise<Record<string, unknown>> {
  const importedRoot = path.join(paths.dataRoot, "imports", "raw", "from_comma", safeRouteDirName(routeId));
  const stdout = await run(
    paths.pythonPath,
    ["-m", "scripts.drive_tests.brickpilot_db.ingest", "--config", paths.dbConfigPath, "--json", importedRoot],
    900000,
    paths.toolsRoot,
    {
      ...process.env,
      BRICKPILOT_REPO_ROOT: paths.repoRoot,
      BRICKPILOT_TOOLS_ROOT: paths.toolsRoot,
      BRICKPILOT_DATA_ROOT: paths.dataRoot,
      BRICKPILOT_DRIVE_DB_CONFIG: paths.dbConfigPath,
      PYTHONPATH: [paths.toolsRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
    }
  );
  return JSON.parse(stdout || "[]") as Record<string, unknown>;
}

async function progressForRun(run?: IngestRun): Promise<IngestProgress> {
  if (!run) return { expectedBytes: 0, copiedBytes: 0, expectedFiles: 0, copiedFiles: 0, percent: 0, complete: false };
  const sizes = await Promise.all(run.files.map(async (file) => ({
    expectedBytes: file.expectedBytes,
    copiedBytes: (await fileSizeIfExists(file.dest)) || (await fileSizeIfExists(file.tmp))
  })));
  return progressFromSizes(sizes);
}

export async function getWebIngestProgress(paths: BrickpilotPaths): Promise<{ activeRun: IngestRun | null; history: IngestRun[]; progress: IngestProgress }> {
  const state = await readState(paths);
  return {
    activeRun: state.activeRun || null,
    history: (state.history || []).slice(-10).reverse(),
    progress: await progressForRun(state.activeRun)
  };
}

export async function cancelWebIngest(paths: BrickpilotPaths, runId?: string): Promise<{ canceled: boolean; cleared: boolean; run: IngestRun | null }> {
  const state = await readState(paths);
  const activeRun = state.activeRun || null;
  if (!activeRun) return { canceled: false, cleared: false, run: null };
  if (runId && activeRun.run_id !== runId) throw new Error(`active ingest is ${activeRun.run_id}, not ${runId}`);

  const now = new Date().toISOString();
  const isActive = activeStatuses.has(activeRun.status);
  if (isActive) cancelRequestedRunIds.add(activeRun.run_id);
  const nextRun: IngestRun = {
    ...activeRun,
    status: isActive ? "canceled" : activeRun.status,
    finished_at: activeRun.finished_at || now,
    message: isActive ? "Canceled by user" : activeRun.message
  };
  await removePartialFiles(activeRun);
  await writeState(paths, { ...state, activeRun: undefined, history: appendHistory(state.history, nextRun) });
  return { canceled: isActive, cleared: !isActive, run: nextRun };
}

export async function startWebIngest(
  paths: BrickpilotPaths,
  routeId: string,
  rideTypeInput: unknown,
  connectionInput: unknown = "wifi",
  includeVideoInput: unknown = false,
  onImported?: (run: IngestRun) => Promise<void>
): Promise<IngestRun> {
  const rideType = canonicalIngestRideType(rideTypeInput);
  const connection = canonicalIngestConnection(connectionInput);
  const includeVideo = canonicalIncludeVideo(includeVideoInput);
  const routeKey = safeRouteDirName(routeId);
  const existing = await readState(paths);
  if (existing.activeRun && activeStatuses.has(existing.activeRun.status)) {
    throw new Error(`ingest already running for ${existing.activeRun.route_id}`);
  }
  const runId = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  const run: IngestRun = {
    run_id: runId,
    route_id: routeId,
    route_key: routeKey,
    ride_type: rideType,
    connection,
    include_video: includeVideo,
    status: "discovering",
    started_at: new Date().toISOString(),
    files: []
  };
  await writeState(paths, { ...existing, activeRun: run });

  void (async () => {
    let current = run;
    const isCanceled = () => cancelRequestedRunIds.has(run.run_id);
    const throwIfCanceled = () => {
      if (isCanceled()) throw new IngestCancelled();
    };
    try {
      const files = await filesForCandidate(paths, routeId, rideType, connection, includeVideo);
      throwIfCanceled();
      if (!files.length) throw new Error("no copyable log/video files found for selected route and ride type");
      current = { ...current, status: "copying", files };
      await writeState(paths, { ...(await readState(paths)), activeRun: current });
      for (const file of files) {
        throwIfCanceled();
        await copyOne(file, connection, isCanceled);
        throwIfCanceled();
        await writeState(paths, { ...(await readState(paths)), activeRun: current });
      }
      throwIfCanceled();
      await writeRemoteSettingsManifest(paths, routeId, rideType, connection);
      throwIfCanceled();
      current = { ...current, status: "importing" };
      await writeState(paths, { ...(await readState(paths)), activeRun: current });
      const importResult = await runImport(paths, routeId);
      throwIfCanceled();
      current = { ...current, status: "complete", finished_at: new Date().toISOString(), import_result: importResult };
      if (onImported) await onImported(current);
    } catch (err) {
      const canceled = err instanceof IngestCancelled || isCanceled();
      current = {
        ...current,
        status: canceled ? "canceled" : "error",
        finished_at: new Date().toISOString(),
        message: canceled ? "Canceled by user" : (err instanceof Error ? err.message : String(err))
      };
      if (canceled) await removePartialFiles(current);
    } finally {
      cancelRequestedRunIds.delete(run.run_id);
    }
    const latest = await readState(paths);
    const keepActive = current.status !== "canceled";
    await writeState(paths, {
      ...latest,
      activeRun: keepActive ? current : undefined,
      history: appendHistory(latest.history, current)
    });
  })();

  return run;
}
