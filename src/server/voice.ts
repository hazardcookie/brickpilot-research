import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import type { BrickpilotPaths } from "./lib";
import { appendJsonl, iterJsonl, moveRecoverably, readJsonFile, safeJoin, safeSessionName } from "./lib";
import type { VoiceSession } from "../shared/types";

function isoNow(): string {
  return new Date().toISOString();
}

function newSessionId(): string {
  return isoNow().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z") + "-" + crypto.randomBytes(4).toString("hex");
}

function sessionDir(paths: BrickpilotPaths, sessionId: string): string {
  return safeJoin(paths.voiceSessionRoot, safeSessionName(sessionId));
}

function parseTime(value: unknown): number | null {
  if (!value) return null;
  const ms = Date.parse(String(value));
  return Number.isFinite(ms) ? ms : null;
}

function durationSec(meta: Record<string, unknown>): number | undefined {
  const start = parseTime(meta.started_at_wall);
  if (!start) return undefined;
  const end = parseTime(meta.ended_at_wall) || Date.now();
  return Math.max(0, (end - start) / 1000);
}

function displayTitle(meta: Record<string, unknown>): string {
  const title = String(meta.custom_title || meta.ride_type || "Drive session").trim();
  const start = meta.started_at_wall ? new Date(String(meta.started_at_wall)) : null;
  const end = meta.ended_at_wall ? new Date(String(meta.ended_at_wall)) : null;
  const range = start ? `${start.toLocaleString()}-${end ? end.toLocaleTimeString() : "open"}` : "unknown time";
  return title.includes(range) ? title : `${title} - ${range}`;
}

export async function startVoiceSession(paths: BrickpilotPaths, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const sessionId = newSessionId();
  const root = sessionDir(paths, sessionId);
  await fsp.mkdir(path.join(root, "audio"), { recursive: true });
  const meta: Record<string, unknown> = {
    session_id: sessionId,
    started_at_wall: isoNow(),
    ended_at_wall: null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    source: "brickpilot_unified_ui",
    app_version: "brickpilot-ui-0.3.30.0",
    ride_type: String(body.ride_type || "label validation"),
    custom_title: body.title ? String(body.title).trim() : null,
    needs_transcription: true
  };
  meta.display_title = displayTitle(meta);
  await fsp.writeFile(path.join(root, "session.json"), JSON.stringify(meta, null, 2) + "\n");
  appendJsonl(path.join(root, "events.jsonl"), { kind: "start", wall_time: meta.started_at_wall, server_received_at: isoNow(), custom_title: meta.custom_title });
  return meta;
}

export async function stopVoiceSession(paths: BrickpilotPaths, sessionId: string): Promise<Record<string, unknown>> {
  const root = sessionDir(paths, sessionId);
  const meta = readJsonFile<Record<string, unknown>>(path.join(root, "session.json"), {});
  meta.ended_at_wall = isoNow();
  meta.duration_sec = durationSec(meta);
  meta.display_title = displayTitle(meta);
  appendJsonl(path.join(root, "events.jsonl"), { kind: "stop", wall_time: meta.ended_at_wall, server_received_at: meta.ended_at_wall });
  await fsp.writeFile(path.join(root, "session.json"), JSON.stringify(meta, null, 2) + "\n");
  return meta;
}

export async function setVoiceTitle(paths: BrickpilotPaths, sessionId: string, title: string): Promise<Record<string, unknown>> {
  const root = sessionDir(paths, sessionId);
  const meta = readJsonFile<Record<string, unknown>>(path.join(root, "session.json"), {});
  meta.custom_title = title.trim() || null;
  meta.display_title = displayTitle(meta);
  appendJsonl(path.join(root, "events.jsonl"), { kind: "title_update", custom_title: meta.custom_title, server_received_at: isoNow() });
  await fsp.writeFile(path.join(root, "session.json"), JSON.stringify(meta, null, 2) + "\n");
  return meta;
}

export async function saveVoiceChunk(paths: BrickpilotPaths, sessionId: string, index: number, bytes: Buffer, contentType: string, wall?: string, t?: string): Promise<Record<string, unknown>> {
  const root = sessionDir(paths, sessionId);
  const audioDir = path.join(root, "audio");
  await fsp.mkdir(audioDir, { recursive: true });
  const fileName = `chunk_${String(index).padStart(6, "0")}.webm`;
  const out = safeJoin(audioDir, fileName);
  await fsp.writeFile(out, bytes);
  appendJsonl(path.join(root, "events.jsonl"), {
    kind: "audio_chunk",
    index,
    content_type: contentType || "audio/webm",
    size_bytes: bytes.length,
    audio_file: `audio/${fileName}`,
    client_wall: wall || null,
    t_session_sec: t ? Number(t) : null,
    server_received_at: isoNow()
  });
  return { ok: true, session_id: sessionId, index, size_bytes: bytes.length };
}

export async function addVoiceBookmark(paths: BrickpilotPaths, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const sessionId = String(body.session_id || "");
  const root = sessionDir(paths, sessionId);
  const text = String(body.text || body.transcript || "").trim();
  const start = body.t_session_start_sec ?? body.t ?? null;
  const end = body.t_session_end_sec ?? body.end_t ?? start;
  const row: Record<string, unknown> = {
    kind: "manual",
    source: "brickpilot_unified_ui",
    text,
    start_wall: body.start_wall || body.wall_time || isoNow(),
    end_wall: body.end_wall || body.wall_time || isoNow(),
    t_session_start_sec: start,
    t_session_end_sec: end,
    confidence: 1.0,
    server_received_at: isoNow()
  };
  row.row_hash = crypto.createHash("sha256").update(JSON.stringify(row)).digest("hex");
  appendJsonl(path.join(root, "transcript.jsonl"), row);
  appendJsonl(path.join(root, "events.jsonl"), { kind: "manual_bookmark", text, t_session_start_sec: start, server_received_at: isoNow() });
  return { ok: true, session_id: sessionId, row };
}

export async function listVoiceSessions(paths: BrickpilotPaths, includeHidden = false): Promise<VoiceSession[]> {
  await fsp.mkdir(paths.voiceSessionRoot, { recursive: true });
  const entries = await fsp.readdir(paths.voiceSessionRoot, { withFileTypes: true });
  const sessions: VoiceSession[] = [];
  for (const entry of entries.sort((a, b) => b.name.localeCompare(a.name))) {
    if (!entry.isDirectory()) continue;
    const root = path.join(paths.voiceSessionRoot, entry.name);
    const meta = readJsonFile<Record<string, unknown>>(path.join(root, "session.json"), {});
    if (!meta.session_id) continue;
    const transcriptRows = iterJsonl(path.join(root, "transcript.jsonl")).length;
    const audioDir = path.join(root, "audio");
    const audioChunks = fs.existsSync(audioDir) ? fs.readdirSync(audioDir).filter((name) => name.endsWith(".webm")).length : 0;
    const duration = durationSec(meta);
    const title = String(meta.custom_title || "");
    if (!includeHidden && !meta.ended_at_wall && !title && transcriptRows === 0 && audioChunks === 0) {
      continue;
    }
    sessions.push({
      session_id: String(meta.session_id),
      display_title: String(meta.display_title || displayTitle(meta)),
      custom_title: title || undefined,
      started_at_wall: meta.started_at_wall ? String(meta.started_at_wall) : undefined,
      ended_at_wall: meta.ended_at_wall ? String(meta.ended_at_wall) : undefined,
      duration_sec: duration,
      recording: !Boolean(meta.ended_at_wall),
      has_audio: audioChunks > 0,
      has_transcript: transcriptRows > 0,
      transcript_rows: transcriptRows,
      audio_chunks: audioChunks,
      path: root
    });
  }
  return sessions.slice(0, 100);
}

export async function deleteVoiceSession(paths: BrickpilotPaths, sessionId: string, confirm: boolean): Promise<Record<string, unknown>> {
  if (!confirm) throw new Error("confirmation_required");
  const root = sessionDir(paths, sessionId);
  if (!fs.existsSync(root)) throw new Error("session not found");
  const dest = await moveRecoverably(root, paths.voiceQuarantineRoot, safeSessionName(sessionId));
  return { deleted: true, session_id: sessionId, quarantined_to: dest };
}

function runPython(paths: BrickpilotPaths, args: string[], timeoutMs = 900000): Promise<Record<string, unknown>> {
  const script = path.join(paths.toolsRoot, "scripts", "drive_tests", "voice_bookmark_app.py");
  return new Promise((resolve, reject) => {
    const child = spawn(paths.pythonPath, [script, ...args], {
      cwd: paths.toolsRoot,
      env: {
        ...process.env,
        BRICKPILOT_VOICE_SESSION_ROOT: paths.voiceSessionRoot,
        BRICKPILOT_DATA_ROOT: paths.dataRoot,
        BRICKPILOT_DRIVE_DB_ROOT: paths.dataRoot,
        BRICKPILOT_REPO_ROOT: paths.repoRoot,
        BRICKPILOT_TOOLS_ROOT: paths.toolsRoot,
        BRICKPILOT_DRIVE_DB_CONFIG: paths.dbConfigPath,
        PYTHONPATH: [paths.toolsRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
      }
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("python worker timed out"));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(stderr.trim() || `python worker exited ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout || "{}"));
      } catch {
        resolve({ ok: true, output: stdout.trim() });
      }
    });
  });
}

export function transcribeStatus(paths: BrickpilotPaths, sessionId?: string): Promise<Record<string, unknown>> {
  const args = ["transcribe", "--status"];
  if (sessionId) args.push("--session", sessionId);
  return runPython(paths, args, 120000);
}

export function transcribeSession(paths: BrickpilotPaths, sessionId: string): Promise<Record<string, unknown>> {
  return runPython(paths, ["transcribe", "--session", sessionId]);
}

export function realtimePreview(paths: BrickpilotPaths, sessionId: string): Promise<Record<string, unknown>> {
  return runPython(paths, ["realtime-preview", "--session", sessionId], 60000);
}

export function importVoiceIntoDrive(paths: BrickpilotPaths, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const args = ["import", "--session", String(body.session_id || "")];
  if (body.route_uuid || body.route) args.push("--route", String(body.route_uuid || body.route));
  if (body.review_job_id) args.push("--review-job-id", String(body.review_job_id));
  if (body.offset_sec != null) args.push("--offset-sec", String(body.offset_sec));
  if (paths.dbConfigPath) args.push("--config", paths.dbConfigPath);
  return runPython(paths, args);
}
