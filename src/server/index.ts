import express from "express";
import path from "node:path";
import fs from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import type { Request, Response, NextFunction } from "express";
import { defaultPaths } from "./lib";
import {
  artifactPath,
  createPool,
  deletedCatalogRouteIds,
  deleteLabel,
  deleteRide,
  driveJobsPayload,
  ensureDynamicReviewJobs,
  finishedReviews,
  finishReview,
  getLogdriveInboxId,
  getReviewJob,
  jobData,
  labelsForJob,
  listReviewJobs,
  listRides,
  patchRides,
  refreshTimelineStats,
  saveLabel,
  timelineForRoute
} from "./db";
import {
  addVoiceBookmark,
  deleteVoiceSession,
  importVoiceIntoDrive,
  listVoiceSessions,
  realtimePreview,
  saveVoiceChunk,
  setVoiceTitle,
  startVoiceSession,
  stopVoiceSession,
  transcribeSession,
  transcribeStatus
} from "./voice";
import {
  driveTimeForRoute,
  extractRouteIds,
  getDataCatalog,
  mergeDiscoveredRides,
  reconciliationSummary,
  reportsForRide
} from "./reconciliation";
import {
  cancelWebIngest,
  canonicalIngestRideType,
  discoverIngestCandidates,
  getWebIngestProgress,
  startWebIngest
} from "./ingest";
import { getMlOverview } from "./ml";
import type { IngestRun } from "../shared/types";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, "../..");

function stripInlineComment(value: string): string {
  let quote = "";
  for (let i = 0; i < value.length; i += 1) {
    const c = value[i];
    if ((c === "'" || c === "\"") && value[i - 1] !== "\\") {
      quote = quote === c ? "" : (quote || c);
    }
    if (c === "#" && !quote && /\s/.test(value[i - 1] || " ")) {
      return value.slice(0, i).trimEnd();
    }
  }
  return value.trim();
}

function unquoteEnvValue(value: string): string {
  const trimmed = stripInlineComment(value).trim();
  if ((trimmed.startsWith("\"") && trimmed.endsWith("\"")) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1).replace(/\\n/g, "\n").replace(/\\"/g, "\"");
  }
  return trimmed;
}

function loadDotEnv(filePath: string): void {
  if (!fs.existsSync(filePath)) return;
  for (const line of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(trimmed);
    if (!match) continue;
    const [, key, raw] = match;
    if (process.env[key] == null) process.env[key] = unquoteEnvValue(raw);
  }
}

loadDotEnv(path.join(projectRoot, ".env"));

const paths = defaultPaths();
const app = express();
const port = Number(process.env.BRICKPILOT_UI_PORT || 8791);
const bind = process.env.BRICKPILOT_UI_BIND || "127.0.0.1";

const pool = await createPool(paths);
await ensureDynamicReviewJobs(pool);

app.disable("x-powered-by");

function asyncHandler(fn: (req: Request, res: Response, next: NextFunction) => Promise<unknown>) {
  return (req: Request, res: Response, next: NextFunction) => {
    Promise.resolve(fn(req, res, next)).catch(next);
  };
}

function sendError(res: Response, err: unknown) {
  const message = err instanceof Error ? err.message : String(err);
  if (message === "confirmation_required") {
    res.status(400).json({ ok: false, error: "confirmation_required" });
    return;
  }
  if (message === "version_conflict") {
    res.status(409).json({ ok: false, error: "version_conflict" });
    return;
  }
  if (/not found/i.test(message)) {
    res.status(404).json({ ok: false, error: message });
    return;
  }
  res.status(400).json({ ok: false, error: message });
}

function toolsPythonEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    BRICKPILOT_REPO_ROOT: paths.repoRoot,
    BRICKPILOT_TOOLS_ROOT: paths.toolsRoot,
    BRICKPILOT_DATA_ROOT: paths.dataRoot,
    BRICKPILOT_DRIVE_DB_CONFIG: paths.dbConfigPath,
    PYTHONPATH: [paths.toolsRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
  };
}

async function generateReviewVideo(routeId: string): Promise<Record<string, unknown>> {
  const script = path.join(paths.toolsRoot, "scripts", "drive_tests", "generate_review_video.py");
  return await new Promise<Record<string, unknown>>((resolve, reject) => {
    const child = spawn(paths.pythonPath, [script, "--route", routeId, "--config", paths.dbConfigPath, "--json"], {
      cwd: paths.toolsRoot,
      env: toolsPythonEnv()
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) reject(new Error(stderr.trim() || `review video generator exited ${code}`));
      else resolve(JSON.parse(stdout || "{}"));
    });
  });
}

app.post("/api/voice/chunk", express.raw({ type: "*/*", limit: "250mb" }), asyncHandler(async (req, res) => {
  const sessionId = String(req.query.session_id || "");
  const index = Number(req.query.index || 0);
  const body = Buffer.isBuffer(req.body) ? req.body : Buffer.from(req.body || "");
  const out = await saveVoiceChunk(paths, sessionId, index, body, req.headers["content-type"] || "audio/webm", String(req.headers["x-client-wall"] || ""), String(req.headers["x-client-session-sec"] || ""));
  res.json(out);
}));

app.post("/api/chunk", express.raw({ type: "*/*", limit: "250mb" }), asyncHandler(async (req, res) => {
  const sessionId = String(req.query.session_id || "");
  const index = Number(req.query.index || 0);
  const body = Buffer.isBuffer(req.body) ? req.body : Buffer.from(req.body || "");
  const out = await saveVoiceChunk(paths, sessionId, index, body, req.headers["content-type"] || "audio/webm", String(req.headers["x-client-wall"] || ""), String(req.headers["x-client-session-sec"] || ""));
  res.json(out);
}));

app.use(express.json({ limit: "25mb" }));

app.get("/api/health", (_req, res) => {
  res.json({
    ok: true,
    brand: "Brickpilot",
    version: "0.3.30.0",
    repo_root: paths.repoRoot,
    tools_root: paths.toolsRoot,
    data_root: paths.dataRoot,
    artifact_root: paths.artifactRoot
  });
});

app.get("/api/ml/overview", asyncHandler(async (_req, res) => {
  res.json(await getMlOverview(pool, paths));
}));

async function markWebIngestRide(run: IngestRun): Promise<void> {
  const rideType = canonicalIngestRideType(run.ride_type);
  const route = await pool.query<{ id: string; route_label: string | null }>(
    `SELECT id, route_label
     FROM routes
     WHERE route_id=$1 OR canonical_name=$1
     ORDER BY updated_at DESC
     LIMIT 1`,
    [run.route_id]
  );
  const row = route.rows[0];
  if (!row) return;
  await pool.query(
    `UPDATE routes
     SET drive_type=$2,
         metadata_jsonb=COALESCE(metadata_jsonb, '{}'::jsonb) || $3::jsonb,
         updated_at=now()
     WHERE id=$1`,
    [row.id, rideType, JSON.stringify({ ride_type: rideType, web_ingest_run_id: run.run_id, web_ingest_include_video: run.include_video === true })]
  );
  if (rideType === "label validation" || rideType === "test drive") {
    const inboxId = await getLogdriveInboxId(pool);
    await pool.query(
      `INSERT INTO review_jobs(inbox_id, route_uuid, legacy_job_id, status, selected, ride_type, route_label, updated_by)
       VALUES($1,$2,NULL,'pending',true,$3,$4,'web_ingest')
       ON CONFLICT(inbox_id, route_uuid) WHERE legacy_job_id IS NULL
       DO UPDATE SET status='pending', selected=true, ride_type=EXCLUDED.ride_type, route_label=EXCLUDED.route_label, updated_at=now(), updated_by='web_ingest'`,
      [inboxId, row.id, rideType, row.route_label || run.route_id]
    );
    if (rideType === "label validation" && run.include_video) {
      run.import_result = {
        ...(run.import_result || {}),
        review_video: await generateReviewVideo(row.id)
      };
    }
  }
  try {
    await refreshTimelineStats(pool);
  } catch (err) {
    console.warn(`Post-ingest timeline stats refresh failed for ${run.route_id}: ${err instanceof Error ? err.message : String(err)}`);
  }
  await ensureDynamicReviewJobs(pool);
}

app.get("/api/ingest/candidates", asyncHandler(async (req, res) => {
  const limit = Number(req.query.limit || 10);
  res.json({ candidates: await discoverIngestCandidates(paths, Number.isFinite(limit) ? limit : 10, req.query.connection) });
}));

app.get("/api/ingest/progress", asyncHandler(async (_req, res) => {
  res.json(await getWebIngestProgress(paths));
}));

app.post("/api/ingest/start", asyncHandler(async (req, res) => {
  const routeId = String(req.body?.route_id || "");
  if (!routeId) throw new Error("route_id required");
  const run = await startWebIngest(paths, routeId, req.body?.ride_type, req.body?.connection, req.body?.include_video, markWebIngestRide);
  res.json({ ok: true, run });
}));

app.post("/api/ingest/cancel", asyncHandler(async (req, res) => {
  const runId = req.body?.run_id ? String(req.body.run_id) : undefined;
  res.json({ ok: true, ...(await cancelWebIngest(paths, runId)) });
}));

app.get("/api/rides", asyncHandler(async (_req, res) => {
  const rides = await listRides(pool);
  const catalog = await getDataCatalog(paths);
  const deletedRoutes = await deletedCatalogRouteIds(pool);
  const visibleRides = mergeDiscoveredRides(rides, catalog).filter((ride) => {
    return !(ride.discovered_only && deletedRoutes.has(ride.route_id.toLowerCase()));
  });
  res.json({
    rides: visibleRides,
    reconciliation: {
      scanned_at: catalog.scanned_at,
      truncated: catalog.truncated,
      discovered_reports: catalog.reports.length,
      discovered_sources: catalog.sources.length
    }
  });
}));

app.get("/api/rides/:id/reports", asyncHandler(async (req, res) => {
  const requestedId = String(req.params.id || "");
  const route = await pool.query(
    `SELECT id::text AS route_uuid, route_id, canonical_name
     FROM routes
     WHERE id::text=$1 OR route_id=$1 OR canonical_name=$1
     LIMIT 1`,
    [requestedId]
  );
  const routeIds = new Set<string>(extractRouteIds(requestedId));
  const row = route.rows[0];
  if (row?.route_id) routeIds.add(String(row.route_id).toLowerCase());
  if (row?.canonical_name) for (const id of extractRouteIds(String(row.canonical_name))) routeIds.add(id);
  const catalog = await getDataCatalog(paths);
  const reports = reportsForRide(catalog, [...routeIds]);
  res.json({
    route_uuid: row?.route_uuid || null,
    route_id: row?.route_id || [...routeIds][0] || requestedId,
    requested_id: requestedId,
    reports,
    count: reports.length
  });
}));

app.get("/api/reconciliation/summary", asyncHandler(async (_req, res) => {
  const routes = await pool.query<{ route_id: string }>("SELECT route_id FROM routes");
  const catalog = await getDataCatalog(paths);
  res.json(reconciliationSummary(catalog, routes.rows.map((route) => route.route_id)));
}));

app.get("/api/drive-jobs", asyncHandler(async (_req, res) => {
  const payload = await driveJobsPayload(pool);
  const catalog = await getDataCatalog(paths);
  const jobs = Array.isArray(payload.jobs) ? payload.jobs.map((raw) => {
    const job = raw as Record<string, unknown>;
    const routeId = String(job.route_id || "");
    const driveTime = driveTimeForRoute(catalog, routeId);
    return {
      ...job,
      route_start_wall_time: job.route_start_wall_time || driveTime?.start || "",
      route_end_wall_time: job.route_end_wall_time || driveTime?.end || "",
      route_miles: job.route_miles || driveTime?.mileage || null,
      duration_sec: job.duration_sec || driveTime?.duration_sec || 0
    };
  }) : [];
  res.json({ ...payload, jobs });
}));

app.patch("/api/rides", asyncHandler(async (req, res) => {
  res.json({ ok: true, ...(await patchRides(pool, req.body.rides || [])), rides: await listRides(pool) });
}));

app.delete("/api/rides/:id", asyncHandler(async (req, res) => {
  const confirm = req.query.confirm === "true" || req.body?.confirm === true;
  res.json({ ok: true, ...(await deleteRide(pool, paths, req.params.id, confirm)) });
}));

app.post("/api/rides/:id/review-video", asyncHandler(async (req, res) => {
  res.json(await generateReviewVideo(req.params.id));
}));

app.get("/api/review/jobs", asyncHandler(async (_req, res) => {
  res.json({ jobs: await listReviewJobs(pool) });
}));

app.get("/api/review/jobs/:id/timeline", asyncHandler(async (req, res) => {
  const job = await getReviewJob(pool, req.params.id);
  if (!job) {
    res.status(404).json({ ok: false, error: "review job not found" });
    return;
  }
  res.json(await timelineForRoute(pool, String(job.route_uuid)));
}));

app.get("/api/routes/:id/timeline", asyncHandler(async (req, res) => {
  res.json(await timelineForRoute(pool, req.params.id));
}));

app.get("/api/job-data", asyncHandler(async (req, res) => {
  const data = await jobData(pool, String(req.query.job_id || ""));
  const routeId = typeof data.route === "object" && data.route ? String((data.route as Record<string, unknown>).route_id || "") : "";
  const driveTime = routeId ? driveTimeForRoute(await getDataCatalog(paths), routeId) : null;
  res.json({
    ...data,
    route_start_wall_time: data.route_start_wall_time || driveTime?.start || "",
    route_end_wall_time: data.route_end_wall_time || driveTime?.end || "",
    route_miles: data.route_miles || driveTime?.mileage || null,
    duration_sec: data.duration_sec || driveTime?.duration_sec || 0
  });
}));

app.get("/api/labels", asyncHandler(async (req, res) => {
  res.json(await labelsForJob(pool, String(req.query.job_id || "")));
}));

app.post("/api/labels", asyncHandler(async (req, res) => {
  const body = { ...req.body };
  if (body.job_id && String(body.job_id).startsWith("db:")) {
    const job = await getReviewJob(pool, String(body.job_id));
    if (!job) throw new Error("review job not found");
    body.route_uuid = job.route_uuid;
    body.review_job_id = job.id;
    body.start_sec = body.start_time_sec;
    body.end_sec = body.end_time_sec;
    body.created_by = body.created_by || "manual_labeler";
    body.updated_by = body.updated_by || "manual_labeler";
    body.metadata = { manual_labeler: true };
  }
  const out = await saveLabel(pool, body);
  res.json({ ok: true, label: { ...body, ...out }, ...out });
}));

app.post("/api/delete-label", asyncHandler(async (req, res) => {
  res.json({ ok: true, ...(await deleteLabel(pool, req.body || {})) });
}));

app.post("/api/finish", asyncHandler(async (req, res) => {
  const body = { ...req.body };
  if (body.job_id && String(body.job_id).startsWith("db:")) {
    const job = await getReviewJob(pool, String(body.job_id));
    if (!job) throw new Error("review job not found");
    body.review_job_id = job.id;
    body.route_uuid = job.route_uuid;
    body.finished_by = "manual_labeler";
    body.label_count = Array.isArray(body.labels) ? body.labels.length : 0;
  }
  res.json({ ok: true, review: await finishReview(pool, body), dataset: "drive_db" });
}));

app.post("/api/finish-review", asyncHandler(async (req, res) => {
  res.json({ ok: true, ...(await finishReview(pool, req.body || {})) });
}));

app.get("/api/finished", asyncHandler(async (_req, res) => {
  res.json({ reviews: await finishedReviews(pool), dataset: "drive_db" });
}));

app.get("/api/finished-reviews", asyncHandler(async (_req, res) => {
  res.json({ reviews: await finishedReviews(pool) });
}));

app.post("/api/delete-review-job", asyncHandler(async (req, res) => {
  const job = await getReviewJob(pool, req.body?.job_id || req.body?.review_job_id);
  if (!job) throw new Error("review job not found");
  res.json({ ok: true, ...(await deleteRide(pool, paths, String(job.route_uuid), req.body?.confirm === true)) });
}));

app.post("/api/import-voice", asyncHandler(async (req, res) => {
  const job = await getReviewJob(pool, req.body?.job_id || req.body?.review_job_id);
  if (!job) throw new Error("review job not found");
  const out = await importVoiceIntoDrive(paths, { ...req.body, route_uuid: job.route_uuid, review_job_id: job.id });
  res.json({ ok: true, ...out });
}));

app.get("/api/artifact-proxy/:id", asyncHandler(async (req, res) => {
  const art = await artifactPath(pool, paths, Number(req.params.id));
  res.setHeader("content-type", art.mime);
  if (art.size) res.setHeader("content-length", String(art.size));
  res.sendFile(art.path);
}));

app.head("/api/artifact-proxy/:id", asyncHandler(async (req, res) => {
  const art = await artifactPath(pool, paths, Number(req.params.id));
  res.setHeader("content-type", art.mime);
  if (art.size) res.setHeader("content-length", String(art.size));
  res.end();
}));

app.get("/api/artifacts/:id", asyncHandler(async (req, res) => {
  const art = await artifactPath(pool, paths, Number(req.params.id));
  res.setHeader("content-type", art.mime);
  if (art.size) res.setHeader("content-length", String(art.size));
  res.sendFile(art.path);
}));

app.get("/api/voice/sessions", asyncHandler(async (req, res) => {
  res.json({ sessions: await listVoiceSessions(paths, req.query.include_hidden === "1"), voice_root: paths.voiceSessionRoot });
}));

app.get("/api/sessions", asyncHandler(async (req, res) => {
  res.json({ sessions: await listVoiceSessions(paths, req.query.include_hidden === "1"), voice_root: paths.voiceSessionRoot });
}));

app.get("/api/voice-sessions", asyncHandler(async (_req, res) => {
  const sessions = await listVoiceSessions(paths);
  res.json({
    sessions: sessions.map((s) => ({
      id: s.session_id,
      name: s.display_title,
      created_at: s.started_at_wall,
      ended_at: s.ended_at_wall,
      duration_sec: s.duration_sec,
      bookmark_count: s.transcript_rows,
      audio: s.has_audio,
      needs_transcription: !s.has_transcript && s.has_audio,
      path: s.path
    })),
    voice_root: paths.voiceSessionRoot
  });
}));

app.post("/api/voice/start", asyncHandler(async (req, res) => {
  res.json(await startVoiceSession(paths, req.body || {}));
}));

app.post("/api/start", asyncHandler(async (req, res) => {
  res.json(await startVoiceSession(paths, req.body || {}));
}));

app.post("/api/voice/stop", asyncHandler(async (req, res) => {
  res.json(await stopVoiceSession(paths, String(req.body.session_id || "")));
}));

app.post("/api/stop", asyncHandler(async (req, res) => {
  res.json(await stopVoiceSession(paths, String(req.body.session_id || "")));
}));

app.post("/api/voice/title", asyncHandler(async (req, res) => {
  res.json(await setVoiceTitle(paths, String(req.body.session_id || ""), String(req.body.title || "")));
}));

app.post("/api/title", asyncHandler(async (req, res) => {
  res.json(await setVoiceTitle(paths, String(req.body.session_id || ""), String(req.body.title || "")));
}));

app.post("/api/voice/bookmark", asyncHandler(async (req, res) => {
  res.json(await addVoiceBookmark(paths, req.body || {}));
}));

app.post("/api/bookmark", asyncHandler(async (req, res) => {
  res.json(await addVoiceBookmark(paths, req.body || {}));
}));

app.post("/api/voice/transcribe/status", asyncHandler(async (req, res) => {
  res.json(await transcribeStatus(paths, req.body?.session_id ? String(req.body.session_id) : undefined));
}));

app.post("/api/transcribe/status", asyncHandler(async (req, res) => {
  res.json(await transcribeStatus(paths, req.body?.session_id ? String(req.body.session_id) : undefined));
}));

app.post("/api/voice/transcribe", asyncHandler(async (req, res) => {
  res.json(await transcribeSession(paths, String(req.body.session_id || "")));
}));

app.post("/api/transcribe", asyncHandler(async (req, res) => {
  res.json(await transcribeSession(paths, String(req.body.session_id || "")));
}));

app.post("/api/voice/realtime-preview", asyncHandler(async (req, res) => {
  res.json(await realtimePreview(paths, String(req.body.session_id || "")));
}));

app.post("/api/realtime-preview", asyncHandler(async (req, res) => {
  res.json(await realtimePreview(paths, String(req.body.session_id || "")));
}));

app.delete("/api/voice/sessions/:id", asyncHandler(async (req, res) => {
  res.json({ ok: true, ...(await deleteVoiceSession(paths, req.params.id, req.body?.confirm === true || req.query.confirm === "true")) });
}));

app.get(/^\/manual-labeler$/, (_req, res) => res.redirect("/manual-labeler/"));

app.get(/^\/manual-labeler\/$/, (_req, res) => {
  const indexPath = path.join(paths.labelerOutputRoot, "staging_label_review_ui", "index.html");
  if (!fs.existsSync(indexPath)) {
    res.status(404).send("Manual labeler HTML has not been generated yet.");
    return;
  }
  res.setHeader("cache-control", "no-store");
  res.sendFile(indexPath);
});

if (process.env.NODE_ENV === "production") {
  const clientDist = path.join(projectRoot, "dist", "client");
  app.use(express.static(clientDist));
  app.get("*", (_req, res) => res.sendFile(path.join(clientDist, "index.html")));
} else {
  const { createServer } = await import("vite");
  const vite = await createServer({
    root: path.join(projectRoot, "src", "client"),
    server: { middlewareMode: true },
    appType: "spa"
  });
  app.use(vite.middlewares);
}

app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  sendError(res, err);
});

const server = app.listen(port, bind, () => {
  console.log(`Brickpilot unified UI listening on http://${bind}:${port}`);
  console.log(`Drive data root: ${paths.dataRoot}`);
});

process.on("SIGTERM", async () => {
  server.close();
  await pool.end();
});
