import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export interface BrickpilotPaths {
  repoRoot: string;
  toolsRoot: string;
  pythonPath: string;
  dataRoot: string;
  artifactRoot: string;
  labelerOutputRoot: string;
  voiceSessionRoot: string;
  voiceQuarantineRoot: string;
  dbConfigPath: string;
}

export function defaultPaths(): BrickpilotPaths {
  const projectRoot = path.resolve(process.env.BRICKPILOT_PROJECT_ROOT || process.cwd());
  const home = os.homedir();
  const dataRoot = process.env.BRICKPILOT_DATA_ROOT || process.env.BRICKPILOT_DRIVE_DB_ROOT || path.join(home, "BrickpilotDriveDB");
  return {
    repoRoot: process.env.BRICKPILOT_REPO_ROOT || path.resolve(projectRoot, "..", "brickpilot"),
    toolsRoot: process.env.BRICKPILOT_TOOLS_ROOT || projectRoot,
    pythonPath: process.env.BRICKPILOT_PYTHON || "python3",
    dataRoot,
    artifactRoot: process.env.BRICKPILOT_ARTIFACT_ROOT || path.join(dataRoot, "artifacts"),
    labelerOutputRoot: process.env.BRICKPILOT_LABELER_OUTPUT_ROOT || path.join(dataRoot, "labeler_outputs"),
    voiceSessionRoot: process.env.BRICKPILOT_VOICE_SESSION_ROOT || path.join(dataRoot, "voice_bookmarks", "sessions"),
    voiceQuarantineRoot: process.env.BRICKPILOT_VOICE_QUARANTINE_ROOT || path.join(dataRoot, "quarantine", "voice_sessions_deleted"),
    dbConfigPath: process.env.BRICKPILOT_DRIVE_DB_CONFIG || path.join(home, ".config", "brickpilot", "drive_db.toml")
  };
}

export function parseSimpleToml(input: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of input.split(/\r?\n/)) {
    const line = rawLine.replace(/\s+#.*$/, "").trim();
    if (!line || line.startsWith("#") || line.startsWith("[")) continue;
    const match = line.match(/^([A-Za-z0-9_.-]+)\s*=\s*(.*)$/);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    out[match[1]] = value;
  }
  return out;
}

export async function readTomlFile(filePath: string): Promise<Record<string, string>> {
  return parseSimpleToml(await fsp.readFile(filePath, "utf8"));
}

export function safeJoin(root: string, rel: string): string {
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(resolvedRoot, rel);
  if (resolved !== resolvedRoot && !resolved.startsWith(resolvedRoot + path.sep)) {
    throw new Error("path escapes allowed root");
  }
  return resolved;
}

export function safeSessionName(sessionId: string): string {
  if (!/^[A-Za-z0-9_.:-]+$/.test(sessionId)) throw new Error("invalid session id");
  return sessionId;
}

export async function moveRecoverably(src: string, quarantineRoot: string, nameHint: string): Promise<string> {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  const dest = path.join(quarantineRoot, `${stamp}-${nameHint}`);
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  await fsp.rename(src, dest);
  return dest;
}

export function progressFromSizes(files: Array<{ expectedBytes: number; copiedBytes: number }>): {
  expectedBytes: number;
  copiedBytes: number;
  expectedFiles: number;
  copiedFiles: number;
  percent: number;
  complete: boolean;
} {
  const expectedBytes = files.reduce((sum, file) => sum + Math.max(0, file.expectedBytes || 0), 0);
  const copiedBytes = files.reduce((sum, file) => sum + Math.min(Math.max(0, file.copiedBytes || 0), Math.max(0, file.expectedBytes || 0)), 0);
  const expectedFiles = files.length;
  const copiedFiles = files.filter((file) => file.expectedBytes > 0 && file.copiedBytes >= file.expectedBytes).length;
  const percent = expectedBytes > 0 ? Math.min(100, Math.round((copiedBytes / expectedBytes) * 1000) / 10) : 0;
  return { expectedBytes, copiedBytes, expectedFiles, copiedFiles, percent, complete: expectedFiles > 0 && copiedFiles === expectedFiles };
}

export async function fileSizeIfExists(filePath: string): Promise<number> {
  try {
    return (await fsp.stat(filePath)).size;
  } catch (err: unknown) {
    if (typeof err === "object" && err && "code" in err && (err as { code?: string }).code === "ENOENT") return 0;
    throw err;
  }
}

export function readJsonFile<T>(filePath: string, fallback: T): T {
  try {
    if (!fs.existsSync(filePath) || fs.statSync(filePath).size === 0) return fallback;
    return JSON.parse(fs.readFileSync(filePath, "utf8")) as T;
  } catch {
    return fallback;
  }
}

export function iterJsonl(filePath: string): unknown[] {
  try {
    if (!fs.existsSync(filePath)) return [];
    return fs.readFileSync(filePath, "utf8").split(/\r?\n/).flatMap((line) => {
      if (!line.trim()) return [];
      try {
        return [JSON.parse(line)];
      } catch {
        return [];
      }
    });
  } catch {
    return [];
  }
}

export function appendJsonl(filePath: string, row: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(filePath, JSON.stringify(row) + "\n");
}
