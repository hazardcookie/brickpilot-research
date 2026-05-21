import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import type { Pool, PoolClient, QueryResultRow } from "pg";
import pg from "pg";
import type { BrickpilotPaths } from "./lib";
import { readTomlFile, safeJoin } from "./lib";
import type { MediaStatus, ReviewJob, RideRow } from "../shared/types";

const { Pool: PgPool } = pg;
type Queryable = Pick<Pool, "query"> | Pick<PoolClient, "query">;

export async function createPool(paths: BrickpilotPaths): Promise<Pool> {
  const cfg = fs.existsSync(paths.dbConfigPath) ? await readTomlFile(paths.dbConfigPath) : {};
  const connectionString = process.env.BRICKPILOT_DATABASE_URL || cfg.database_url;
  if (!connectionString) {
    throw new Error(`missing database_url in ${paths.dbConfigPath}`);
  }
  return new PgPool({
    connectionString,
    max: Number(process.env.BRICKPILOT_UI_DB_POOL || 8)
  }) as Pool;
}

function row<T extends QueryResultRow>(row: T): T {
  return row;
}

export async function refreshTimelineStats(pool: Pool): Promise<void> {
  await pool.query("ANALYZE route_samples");
  await pool.query("ANALYZE can_frames_sampled");
  await pool.query("ANALYZE events");
  await pool.query("ANALYZE video_sync_segments");
}

async function ensureDeletedRouteTombstones(db: Queryable): Promise<void> {
  await db.query(
    `CREATE TABLE IF NOT EXISTS deleted_route_tombstones (
       route_id text PRIMARY KEY,
       route_uuid uuid,
       reason text,
       deleted_at timestamptz NOT NULL DEFAULT now(),
       deleted_by text
     )`
  );
}

export async function deletedCatalogRouteIds(pool: Pool): Promise<Set<string>> {
  const exists = await pool.query("SELECT to_regclass('public.deleted_route_tombstones') AS table_name");
  if (!exists.rows[0]?.table_name) return new Set();
  const result = await pool.query("SELECT route_id FROM deleted_route_tombstones");
  return new Set(result.rows.map((r) => String(r.route_id).toLowerCase()));
}

async function tombstoneDeletedRoute(db: Queryable, routeId: string, routeUuid: string | null, deletedBy: string): Promise<void> {
  const normalizedRouteId = routeId.replace(/^external:/, "").trim();
  if (!normalizedRouteId) return;
  await ensureDeletedRouteTombstones(db);
  await db.query(
    `INSERT INTO deleted_route_tombstones(route_id, route_uuid, reason, deleted_by)
     VALUES($1,$2,'deleted from web UI',$3)
     ON CONFLICT(route_id) DO UPDATE
       SET route_uuid=COALESCE(EXCLUDED.route_uuid, deleted_route_tombstones.route_uuid),
           reason=EXCLUDED.reason,
           deleted_at=now(),
           deleted_by=EXCLUDED.deleted_by`,
    [normalizedRouteId, routeUuid, deletedBy]
  );
}

function asJson(value: unknown): Record<string, unknown> {
  if (!value) return {};
  if (typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return typeof parsed === "object" && parsed && !Array.isArray(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }
  return {};
}

function asArray(value: unknown): string[] {
  if (!value) return [];
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch {
      return value ? [value] : [];
    }
  }
  return [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return asJson(value);
}

function stringValue(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  return String(value);
}

function settingsFromMetadata(metadata: Record<string, unknown>): Record<string, string> {
  const raw = asRecord(metadata.settings || metadata.params || metadata.settings_snapshot || metadata.openpilot_params);
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (value === null || value === undefined) continue;
    out[key] = typeof value === "string" ? value : JSON.stringify(value);
  }
  return out;
}

function modelBundleFromSettings(settings: Record<string, string>): string | null {
  const raw = settings.ModelManager_ActiveBundle || settings.ModelManager_ActiveModel || settings.ModelManager_Selector || settings.ModelRunner || settings.Model;
  if (!raw || raw === "[redacted]") return null;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const direct = stringValue(parsed.displayName) || stringValue(parsed.internalName) || stringValue(parsed.name) || stringValue(parsed.model);
    if (direct) return direct;
    const models = Array.isArray(parsed.models) ? parsed.models : [];
    const first = models[0] as Record<string, unknown> | undefined;
    const artifact = asRecord(first?.artifact);
    return stringValue(artifact.fileName);
  } catch {
    return raw.slice(0, 160);
  }
}

function settingsDetailFromMetadata(metadata: Record<string, unknown>) {
  const settings = settingsFromMetadata(metadata);
  const software = asRecord(metadata.software);
  const manifest = asRecord(metadata.settings_manifest);
  if (!Object.keys(settings).length && !Object.keys(manifest).length && !Object.keys(software).length) return null;
  return {
    status: String(manifest.status || "ok"),
    captured_at: stringValue(manifest.captured_at),
    params_dir: stringValue(manifest.params_dir),
    settings_count: Number(manifest.settings_count || Object.keys(settings).length || 0),
    raw_param_count: Number(manifest.raw_param_count || 0),
    artifact_id: manifest.artifact_id == null ? null : Number(manifest.artifact_id),
    error: stringValue(manifest.error),
    software,
    settings
  };
}

function settingsSummary(settings: Record<string, string>): string {
  const preferred = [
    "ExperimentalMode",
    "DynamicExperimentalControl",
    "AlphaLongitudinalEnabled",
    "LongitudinalPersonality",
    "HyundaiLongitudinalTuning",
    "SccSmoother",
    "SccSmootherSlowOnCurves",
    "ModelManager_ActiveBundle",
    "ModelManager_ActiveModel"
  ];
  const parts: string[] = [];
  for (const key of preferred) {
    const value = settings[key];
    if (!value) continue;
    const compact = key === "ModelManager_ActiveBundle" ? (modelBundleFromSettings(settings) || value) : value;
    parts.push(`${key}=${compact}`);
  }
  if (!parts.length) {
    for (const [key, value] of Object.entries(settings).slice(0, 10)) parts.push(`${key}=${value}`);
  }
  return parts.join(", ").slice(0, 500);
}

function numberOrNull(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function finiteNumber(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function mediaStatus(value: unknown): MediaStatus {
  if (value === "ready" || value === "raw video only" || value === "needs transcode" || value === "missing") return value;
  return "missing";
}

function normalizedKind(row: QueryResultRow | Record<string, unknown>): string {
  return String(row.kind || row.role || "").toLowerCase();
}

function normalizedRole(row: QueryResultRow | Record<string, unknown>): string {
  return String(row.role || "").toLowerCase();
}

function normalizedMime(row: QueryResultRow | Record<string, unknown>): string {
  return String(row.mime_type || "").toLowerCase();
}

function hasArtifactBytes(row: QueryResultRow | Record<string, unknown>): boolean {
  return Number(row.size_bytes || 0) > 0 || row.size_bytes == null;
}

function isPlayableVideo(row: QueryResultRow | Record<string, unknown>): boolean {
  const kind = normalizedKind(row);
  const role = normalizedRole(row);
  const mime = normalizedMime(row);
  return ["full_drive_video", "clip"].includes(kind) || ["full_drive_video", "clip"].includes(role) || mime === "video/mp4";
}

function isRawCameraVideo(row: QueryResultRow | Record<string, unknown>): boolean {
  const kind = normalizedKind(row);
  const role = normalizedRole(row);
  return ["qcamera", "fcamera", "ecamera", "dcamera", "camera"].includes(kind) ||
    ["qcamera", "fcamera", "ecamera", "dcamera", "camera"].includes(role);
}

export function selectReviewVideo(artifacts: QueryResultRow[]): QueryResultRow | undefined {
  const sized = artifacts.filter(hasArtifactBytes);
  return sized.find((artifact) => isPlayableVideo(artifact) && ["full_drive_video", "clip"].includes(normalizedKind(artifact))) ||
    sized.find((artifact) => isPlayableVideo(artifact)) ||
    sized.find((artifact) => isRawCameraVideo(artifact));
}

const playableVideoPredicate = (a = "a", ra = "ra") =>
  `((${a}.kind IN ('full_drive_video','clip') OR ${ra}.role IN ('full_drive_video','clip') OR ${a}.mime_type='video/mp4') AND COALESCE(${a}.size_bytes,0)>0)`;

const rawVideoPredicate = (a = "a", ra = "ra") =>
  `((${a}.kind IN ('qcamera','fcamera','ecamera','dcamera','camera') OR ${ra}.role IN ('qcamera','fcamera','ecamera','dcamera','camera') OR lower(COALESCE(${a}.artifact_path,'')) LIKE '%camera%' OR lower(COALESCE(${a}.artifact_path,'')) LIKE '%.hevc' OR lower(COALESCE(${a}.artifact_path,'')) LIKE '%.ts') AND COALESCE(${a}.size_bytes,0)>0)`;

const reviewVideoPredicate = (a = "a", ra = "ra") => `(${playableVideoPredicate(a, ra)} OR ${rawVideoPredicate(a, ra)})`;
const logArtifactPredicate = (a = "a") => `(${a}.kind IN ('qlog','rlog') AND COALESCE(${a}.size_bytes,0)>0)`;
const reviewableRouteTypePredicate = (r = "r") =>
  `(COALESCE(${r}.drive_type,'') IN ('label validation','test drive') OR COALESCE(${r}.metadata_jsonb->>'ride_type','') IN ('label validation','test drive'))`;
const routeReviewTypeExpression = (r = "r") =>
  `CASE WHEN COALESCE(${r}.drive_type, ${r}.metadata_jsonb->>'ride_type', '')='test drive' OR COALESCE(${r}.metadata_jsonb->>'ride_type','')='test drive' THEN 'test drive' ELSE 'label validation' END`;
const reviewableTimelinePredicate = (r = "r") =>
  `(EXISTS (SELECT 1 FROM route_samples rs WHERE rs.route_uuid=${r}.id) OR EXISTS (SELECT 1 FROM bookmarks b WHERE b.route_uuid=${r}.id AND b.deleted_at IS NULL) OR EXISTS (SELECT 1 FROM events e WHERE e.route_uuid=${r}.id) OR EXISTS (SELECT 1 FROM video_sync_segments vs WHERE vs.route_uuid=${r}.id))`;

export async function getLogdriveInboxId(pool: Pool | PoolClient): Promise<number> {
  const result = await pool.query<{ id: string }>(
    `INSERT INTO review_inboxes(name,purpose)
     VALUES($1,$2)
     ON CONFLICT(name) DO UPDATE SET purpose=COALESCE(EXCLUDED.purpose, review_inboxes.purpose)
     RETURNING id`,
    ["logdrive_label_validation", "Brickpilot post-drive label validation"]
  );
  return Number(result.rows[0].id);
}

export async function ensureDynamicReviewJobs(pool: Pool | PoolClient): Promise<void> {
  const inboxId = await getLogdriveInboxId(pool);
  const videoPred = reviewVideoPredicate();
  const logPred = logArtifactPredicate();
  const timelinePred = reviewableTimelinePredicate();
  const rideTypeExpr = routeReviewTypeExpression("r");
  await pool.query(
    `INSERT INTO review_jobs(inbox_id, route_uuid, legacy_job_id, status, selected, ride_type, route_label, sort_order, updated_by)
     SELECT $1, r.id, NULL, 'pending', true, ${rideTypeExpr},
            COALESCE(r.route_label, r.canonical_name, r.route_id), NULL, 'node_dynamic_inbox'
     FROM routes r
     WHERE r.route_id <> '0000015c--c960fea484'
       AND EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${logPred})
       AND (${reviewableRouteTypePredicate("r")} OR EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${videoPred}))
       AND ${timelinePred}
       AND NOT EXISTS (SELECT 1 FROM review_jobs j WHERE j.inbox_id=$1 AND j.route_uuid=r.id AND j.legacy_job_id IS NULL)
     ON CONFLICT(inbox_id, route_uuid) WHERE legacy_job_id IS NULL DO NOTHING`,
    [inboxId]
  );
}

export async function listReviewJobs(pool: Pool): Promise<ReviewJob[]> {
  await ensureDynamicReviewJobs(pool);
  const inboxId = await getLogdriveInboxId(pool);
  const videoPred = reviewVideoPredicate();
  const playablePred = playableVideoPredicate();
  const rawPred = rawVideoPredicate();
  const logPred = logArtifactPredicate();
  const timelinePred = reviewableTimelinePredicate();
  const rideTypeExpr = routeReviewTypeExpression("r");
  const result = await pool.query(
    `SELECT j.id, ('db:' || j.id::text) AS job_id, j.route_uuid, r.route_id,
            COALESCE(NULLIF(r.route_label, ''), NULLIF(j.route_label, ''), r.canonical_name, r.route_id) AS route_label,
            COALESCE(NULLIF(j.ride_type, ''), ${rideTypeExpr}) AS ride_type,
            j.status, j.version, r.started_at, r.ended_at,
            COALESCE(r.duration_sec, (SELECT SUM(COALESCE(s.duration_sec, 60)) FROM route_segments s WHERE s.route_uuid=r.id)) AS duration_sec,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${videoPred}) AS video_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${playablePred}) AS playable_video_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${rawPred}) AS raw_video_artifact_count,
            CASE
              WHEN EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${playablePred}) THEN 'ready'
              WHEN EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${rawPred}) THEN 'raw video only'
              ELSE 'missing'
            END AS media_status,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${logPred}) AS log_artifact_count,
            (SELECT COUNT(*) FROM video_sync_segments vs WHERE vs.route_uuid=r.id) AS video_sync_count,
            (SELECT COUNT(*) FROM route_samples rs WHERE rs.route_uuid=r.id) AS sample_count,
            (SELECT COUNT(*) FROM bookmarks b WHERE b.route_uuid=r.id AND b.deleted_at IS NULL) AS bookmark_count
     FROM review_jobs j
     JOIN routes r ON r.id=j.route_uuid
     WHERE j.inbox_id=$1
       AND COALESCE(j.status, '') NOT LIKE 'superseded%'
       AND COALESCE(r.route_id, '') <> '0000015c--c960fea484'
       AND (j.legacy_job_id IS NOT NULL OR (
         EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${logPred})
         AND (${reviewableRouteTypePredicate("r")} OR COALESCE(j.ride_type,'') IN ('label validation','test drive') OR COALESCE(j.selected,false)=true OR EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${videoPred}))
         AND ${timelinePred}
       ))
     ORDER BY COALESCE(j.sort_order, 1000000), j.created_at DESC, r.started_at DESC NULLS LAST, j.id DESC`,
    [inboxId]
  );
  return result.rows.map((r) => ({
    id: Number(r.id),
    job_id: String(r.job_id),
    route_uuid: String(r.route_uuid),
    route_id: String(r.route_id),
    route_label: String(r.route_label || r.route_id),
    ride_type: String(r.ride_type || "label validation"),
    status: String(r.status || "pending"),
    version: Number(r.version || 1),
    duration_sec: numberOrNull(r.duration_sec),
    started_at: r.started_at ? new Date(r.started_at).toISOString() : null,
    ended_at: r.ended_at ? new Date(r.ended_at).toISOString() : null,
    media_status: mediaStatus(r.media_status),
    sample_count: Number(r.sample_count || 0),
    bookmark_count: Number(r.bookmark_count || 0),
    video_artifact_count: Number(r.video_artifact_count || 0),
    playable_video_artifact_count: Number(r.playable_video_artifact_count || 0),
    raw_video_artifact_count: Number(r.raw_video_artifact_count || 0)
  }));
}

export async function listRides(pool: Pool): Promise<RideRow[]> {
  await ensureDynamicReviewJobs(pool);
  const videoPred = reviewVideoPredicate();
  const playablePred = playableVideoPredicate();
  const rawPred = rawVideoPredicate();
  const logPred = logArtifactPredicate();
  const result = await pool.query(
    `WITH mile_calc AS (
       SELECT route_uuid,
              SUM(((speed_mph + next_speed_mph) / 2.0) * LEAST(GREATEST(next_t_sec - t_sec, 0), 5) / 3600.0) AS miles
       FROM (
         SELECT route_uuid, t_sec, speed_mph,
                LEAD(t_sec) OVER (PARTITION BY route_uuid ORDER BY t_sec) AS next_t_sec,
                LEAD(speed_mph) OVER (PARTITION BY route_uuid ORDER BY t_sec) AS next_speed_mph
         FROM route_samples
         WHERE speed_mph IS NOT NULL
       ) s
       WHERE next_t_sec IS NOT NULL AND next_speed_mph IS NOT NULL
       GROUP BY route_uuid
     )
     SELECT r.*,
            EXISTS (
              SELECT 1 FROM review_jobs j
              JOIN review_inboxes i ON i.id=j.inbox_id
              WHERE j.route_uuid=r.id AND i.name='logdrive_label_validation' AND COALESCE(j.status,'') NOT LIKE 'superseded%'
            ) AS label_validation,
            (SELECT COUNT(*) FROM route_artifacts ra WHERE ra.route_uuid=r.id) AS artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${logPred}) AS log_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${videoPred}) AS video_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${playablePred}) AS playable_video_artifact_count,
            (SELECT COUNT(*) FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${rawPred}) AS raw_video_artifact_count,
            CASE
              WHEN EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${playablePred}) THEN 'ready'
              WHEN EXISTS (SELECT 1 FROM route_artifacts ra JOIN artifacts a ON a.id=ra.artifact_id WHERE ra.route_uuid=r.id AND ${rawPred}) THEN 'raw video only'
              ELSE 'missing'
            END AS media_status,
            (SELECT COUNT(*) FROM labels l WHERE l.route_uuid=r.id AND l.deleted_at IS NULL) AS label_count,
            (SELECT COUNT(*) FROM bookmarks b WHERE b.route_uuid=r.id AND b.deleted_at IS NULL) AS bookmark_count,
            (SELECT COUNT(*) FROM events e WHERE e.route_uuid=r.id) AS event_count,
            (SELECT COUNT(*) FROM route_samples rs WHERE rs.route_uuid=r.id) AS sample_count,
            (SELECT COUNT(*) FROM video_sync_segments vs WHERE vs.route_uuid=r.id) AS video_sync_count,
            COALESCE(mc.miles, 0) AS mileage
     FROM routes r
     LEFT JOIN mile_calc mc ON mc.route_uuid=r.id
     ORDER BY r.updated_at DESC, r.started_at DESC NULLS LAST
     LIMIT 500`
  );
  return result.rows.map((r) => {
    const metadata = asJson(r.metadata_jsonb);
    const settings = settingsFromMetadata(metadata);
    const settingsDetail = settingsDetailFromMetadata(metadata);
    const software = asRecord(metadata.software);
    const notes = typeof metadata.notes === "string" ? metadata.notes : "";
    const branch = stringValue(r.branch) || stringValue(software.branch) || settings.GitBranch || null;
    const brickpilotVersion = stringValue(r.brickpilot_version) || stringValue(software.brickpilot_version) || stringValue(software.version) || settings.BrickpilotVersion || null;
    const modelBundle = stringValue(r.model_bundle) || stringValue(software.model_bundle) || modelBundleFromSettings(settings);
    return {
      id: String(r.id),
      route_id: String(r.route_id),
      canonical_name: r.canonical_name,
      route_label: r.route_label,
      ride_type: (metadata.ride_type as string | undefined) || r.drive_type || null,
      drive_type: r.drive_type,
      label_validation: Boolean(r.label_validation),
      notes,
      branch,
      brickpilot_version: brickpilotVersion,
      model_bundle: modelBundle,
      vehicle: r.vehicle,
      settings_summary: settingsSummary(settings),
      settings_detail: settingsDetail,
      started_at: r.started_at ? new Date(r.started_at).toISOString() : null,
      ended_at: r.ended_at ? new Date(r.ended_at).toISOString() : null,
      duration_sec: numberOrNull(r.duration_sec),
      mileage: numberOrNull(r.mileage),
      segment_count: Number(r.segment_count || 0),
      media_status: mediaStatus(r.media_status),
      artifact_count: Number(r.artifact_count || 0),
      log_artifact_count: Number(r.log_artifact_count || 0),
      video_artifact_count: Number(r.video_artifact_count || 0),
      playable_video_artifact_count: Number(r.playable_video_artifact_count || 0),
      raw_video_artifact_count: Number(r.raw_video_artifact_count || 0),
      label_count: Number(r.label_count || 0),
      bookmark_count: Number(r.bookmark_count || 0),
      event_count: Number(r.event_count || 0),
      sample_count: Number(r.sample_count || 0),
      video_sync_count: Number(r.video_sync_count || 0),
      metadata_summary: summarizeMetadata(metadata)
    };
  });
}

function summarizeMetadata(metadata: Record<string, unknown>): Record<string, unknown> {
  const hidden = new Set(["raw", "raw_json", "telemetry", "samples", "can", "can_frames", "settings", "params", "settings_snapshot"]);
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(metadata)) {
    if (hidden.has(key) || /token|password|secret|vin/i.test(key)) continue;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean" || value == null) out[key] = value;
    else if (Array.isArray(value)) out[key] = `${value.length} item(s)`;
    else if (typeof value === "object") out[key] = "summary object";
  }
  return out;
}

function canonicalDriveType(value: unknown): string | null {
  const normalized = String(value ?? "").trim().toLowerCase().replaceAll("_", " ").replaceAll("-", " ");
  if (!normalized) return null;
  if (normalized.includes("label") || normalized.includes("validation")) return "label validation";
  if (normalized.includes("test")) return "test drive";
  return "normal drive";
}

export async function patchRides(pool: Pool, rides: Array<Partial<RideRow> & { id: string }>): Promise<{ updated: number }> {
  const client = await pool.connect();
  let updated = 0;
  try {
    await client.query("BEGIN");
    const inboxId = await getLogdriveInboxId(client);
    for (const ride of rides) {
      const current = await client.query<{ metadata_jsonb: unknown; route_label: string | null; drive_type: string | null; route_id: string }>(
        "SELECT metadata_jsonb, route_label, drive_type, route_id FROM routes WHERE id=$1 FOR UPDATE",
        [ride.id]
      );
      if (!current.rowCount) continue;
      const currentRow = current.rows[0];
      const metadata = asJson(currentRow.metadata_jsonb);
      const nextRouteLabel = ride.route_label !== undefined ? ride.route_label : currentRow.route_label;
      const requestedDriveType = ride.drive_type !== undefined ? ride.drive_type : ride.ride_type !== undefined ? ride.ride_type : undefined;
      const nextDriveType = requestedDriveType !== undefined ? canonicalDriveType(requestedDriveType) : currentRow.drive_type;
      if (ride.notes !== undefined) metadata.notes = ride.notes;
      if (ride.ride_type !== undefined || ride.drive_type !== undefined) metadata.ride_type = nextDriveType;
      await client.query(
        `UPDATE routes
         SET route_label=$2, drive_type=$3, metadata_jsonb=$4::jsonb, updated_at=now()
         WHERE id=$1`,
        [ride.id, nextRouteLabel ?? null, nextDriveType ?? null, JSON.stringify(metadata)]
      );
      if (ride.label_validation === true) {
        const reviewRideType = canonicalDriveType(nextDriveType || currentRow.drive_type) || "label validation";
        await client.query(
          `INSERT INTO review_jobs(inbox_id, route_uuid, legacy_job_id, status, selected, ride_type, route_label, updated_by)
           VALUES($1,$2,NULL,'pending',true,$3,$4,'rides_table')
           ON CONFLICT(inbox_id, route_uuid) WHERE legacy_job_id IS NULL
           DO UPDATE SET status='pending', selected=true, ride_type=EXCLUDED.ride_type, route_label=EXCLUDED.route_label, updated_at=now(), updated_by='rides_table'`,
          [inboxId, ride.id, reviewRideType, nextRouteLabel || ride.route_id || currentRow.route_id || null]
        );
      } else if (ride.label_validation === false) {
        await client.query(
          `UPDATE review_jobs
           SET status='superseded_by_user', selected=false, version=version+1, updated_at=now(), updated_by='rides_table'
           WHERE inbox_id=$1 AND route_uuid=$2 AND legacy_job_id IS NULL`,
          [inboxId, ride.id]
        );
      }
      updated += 1;
    }
    await client.query("COMMIT");
    return { updated };
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}

export async function timelineForRoute(pool: Pool, routeUuid: string): Promise<Record<string, unknown>> {
  const canRowsPromise = (async (): Promise<QueryResultRow[]> => {
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SET LOCAL statement_timeout = '3000ms'");
      await client.query("SET LOCAL plan_cache_mode = force_custom_plan");
      const canRows = await client.query(
        `WITH sample_times AS (
           SELECT DISTINCT t_sec
           FROM can_frames_sampled
           WHERE route_uuid=$1
           ORDER BY t_sec
           LIMIT 3000
         )
         SELECT c.t_sec AS t, c.bus, c.address, c.data_hex, c.name, c.hint, c.is_unknown
         FROM can_frames_sampled c
         JOIN sample_times st ON st.t_sec=c.t_sec
         WHERE c.route_uuid=$1
         ORDER BY c.t_sec, c.address, c.bus`,
        [routeUuid]
      );
      await client.query("COMMIT");
      return canRows.rows;
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      console.warn(`CAN timeline load skipped for ${routeUuid}: ${err instanceof Error ? err.message : String(err)}`);
      return [];
    } finally {
      client.release();
    }
  })();
  const [route, labels, bookmarks, artifacts, samples, canRows, events, videoSync] = await Promise.all([
    pool.query("SELECT * FROM routes WHERE id=$1", [routeUuid]),
    pool.query("SELECT * FROM labels WHERE route_uuid=$1 AND deleted_at IS NULL ORDER BY start_sec", [routeUuid]),
    pool.query("SELECT * FROM bookmarks WHERE route_uuid=$1 AND deleted_at IS NULL ORDER BY t_sec", [routeUuid]),
    pool.query(
      `SELECT a.id,a.kind,a.mime_type,a.size_bytes,a.artifact_path,ra.role,ra.start_sec,ra.duration_sec
       FROM artifacts a JOIN route_artifacts ra ON ra.artifact_id=a.id
       WHERE ra.route_uuid=$1
       ORDER BY CASE a.kind WHEN 'full_drive_video' THEN 0 WHEN 'clip' THEN 1 WHEN 'qcamera' THEN 2 WHEN 'fcamera' THEN 3 WHEN 'ecamera' THEN 4 ELSE 9 END,
                COALESCE(ra.start_sec,0), a.id`,
      [routeUuid]
    ),
    pool.query(
      "SELECT t_sec AS t, speed_mph, set_speed_mph, a_ego_mps2, gas_pressed, brake_pressed, lead_status, lead_d_rel_m, lead_v_rel_mps FROM route_samples WHERE route_uuid=$1 ORDER BY t_sec LIMIT 20000",
      [routeUuid]
    ),
    canRowsPromise,
    pool.query("SELECT id, source, event_type AS type, t_sec AS t, end_t_sec AS end_t, severity, summary FROM events WHERE route_uuid=$1 ORDER BY t_sec", [routeUuid]),
    pool.query("SELECT artifact_id, segment_index, route_start_sec, video_start_sec, duration_sec, source_path, confidence FROM video_sync_segments WHERE route_uuid=$1 ORDER BY route_start_sec, segment_index", [routeUuid])
  ]);
  const can: Array<{ t: unknown; frames: unknown[] }> = [];
  for (const r of canRows) {
    const last = can[can.length - 1];
    if (!last || last.t !== r.t) can.push({ t: r.t, frames: [] });
    can[can.length - 1].frames.push({ src: r.bus, addr: r.address, data: r.data_hex, name: r.name, hint: r.hint, unknown: r.is_unknown });
  }
  return {
    route: route.rows[0] ? row(route.rows[0]) : null,
    labels: labels.rows,
    bookmarks: bookmarks.rows,
    artifacts: artifacts.rows,
    telemetry: { samples: samples.rows },
    can: { samples: can },
    events: events.rows,
    video_sync: videoSync.rows
  };
}

export async function getReviewJob(pool: Pool, jobId: string | number): Promise<QueryResultRow | null> {
  const id = typeof jobId === "string" && jobId.startsWith("db:") ? Number(jobId.slice(3)) : Number(jobId);
  if (!Number.isFinite(id)) return null;
  const result = await pool.query(
    `SELECT j.*, r.route_id, r.canonical_name, r.started_at, r.ended_at, r.duration_sec,
            COALESCE(NULLIF(r.route_label, ''), NULLIF(j.route_label, ''), r.canonical_name, r.route_id) AS effective_route_label
     FROM review_jobs j JOIN routes r ON r.id=j.route_uuid
     WHERE j.id=$1`,
    [id]
  );
  return result.rows[0] || null;
}

function durationFromTimeline(job: QueryResultRow, timeline: Record<string, unknown>): number {
  const values: number[] = [Number(job.duration_sec || 0)];
  const artifacts = (timeline.artifacts as QueryResultRow[] | undefined) || [];
  for (const a of artifacts) values.push(Number(a.start_sec || 0) + Number(a.duration_sec || 0));
  const sync = (timeline.video_sync as QueryResultRow[] | undefined) || [];
  for (const v of sync) values.push(Number(v.route_start_sec || 0) + Number(v.duration_sec || 0));
  const samples = ((timeline.telemetry as { samples?: QueryResultRow[] } | undefined)?.samples) || [];
  for (const s of samples) values.push(Number(s.t || 0));
  const events = (timeline.events as QueryResultRow[] | undefined) || [];
  for (const e of events) values.push(Number(e.end_t || e.t || 0));
  const bookmarks = (timeline.bookmarks as QueryResultRow[] | undefined) || [];
  for (const b of bookmarks) values.push(Number(b.end_sec || b.t_sec || 0));
  return Math.max(0, ...values.filter((v) => Number.isFinite(v)));
}

export function videoSyncForSelectedVideo(video: QueryResultRow | undefined, timeline: Record<string, unknown>, durationSec: number): QueryResultRow[] {
  const sync = ((timeline.video_sync as QueryResultRow[] | undefined) || []).filter((row) => Number(row.duration_sec) > 0);
  if (!video) return sync;
  if (isPlayableVideo(video)) {
    const duration = Number(video.duration_sec) > 0 ? Number(video.duration_sec) : durationSec;
    return [{
      artifact_id: video.id,
      segment_index: 0,
      route_start_sec: Number(video.start_sec || 0),
      video_start_sec: 0,
      duration_sec: duration,
      source_path: video.artifact_path,
      confidence: 1
    }];
  }
  return sync.filter((row) => !row.artifact_id || String(row.artifact_id) === String(video.id));
}

function milesFromSamples(samples: QueryResultRow[]): number | null {
  let total = 0;
  let prev: { t: number; mph: number } | null = null;
  for (const sample of samples) {
    const t = Number(sample.t ?? sample.t_sec);
    const mph = Number(sample.speed_mph);
    if (!Number.isFinite(t) || !Number.isFinite(mph)) continue;
    if (prev) {
      const dt = Math.max(0, Math.min(5, t - prev.t));
      total += ((prev.mph + mph) / 2) * dt / 3600;
    }
    prev = { t, mph };
  }
  return total > 0 ? Math.round(total * 100) / 100 : null;
}

function labelFromDb(label: QueryResultRow, reviewJobId: number): Record<string, unknown> {
  return {
    id: label.id,
    version: label.version,
    review_job_id: label.review_job_id,
    route_uuid: label.route_uuid,
    job_id: `db:${reviewJobId}`,
    route_id: label.route_uuid,
    label_kind: label.label_kind || "drive",
    label: label.label || "other",
    severity: label.severity || "reviewed",
    start_time_sec: label.start_sec || 0,
    end_time_sec: label.end_sec || label.start_sec || 0,
    notes: label.notes || "",
    tags: asArray(label.tags)
  };
}

function eventFromDb(event: QueryResultRow): Record<string, unknown> {
  return {
    id: event.id,
    t: event.t || event.t_sec || 0,
    end_t: event.end_t || event.end_t_sec,
    type: event.type || event.event_type || event.source || "event",
    label: event.summary || event.type || event.event_type || "event",
    reason: event.summary || "",
    severity: event.severity || "",
    raw: event.raw_jsonb
  };
}

function bookmarkTimeFromRow(row: QueryResultRow | Record<string, unknown>): number | null {
  return finiteNumber(row.t_sec ?? row.t ?? row.start_sec ?? row.route_time_sec);
}

function bookmarkEndFromRow(row: QueryResultRow | Record<string, unknown>): number | null {
  return finiteNumber(row.end_sec ?? row.end_t ?? row.end_t_sec);
}

function isUsableTimelineTime(value: number | null, _durationSec: number): value is number {
  return value !== null && value >= 0;
}

function isBookmarkEvent(event: QueryResultRow | Record<string, unknown>): boolean {
  const severity = String(event.severity || "").toLowerCase();
  const type = String(event.type || event.event_type || "").toLowerCase();
  const summary = String(event.summary || "").toLowerCase();
  return severity === "bookmark" || type.includes("bookmark") || summary.includes("bookmark");
}

function bookmarkEventLabel(event: QueryResultRow | Record<string, unknown>): string {
  const label = String(event.summary || event.type || event.event_type || event.source || "bookmark").trim();
  if (/^bookmarkButton$/i.test(label)) return "bookmark button";
  if (/^userBookmark$/i.test(label)) return "user bookmark";
  return label || "bookmark";
}

export function labelerBookmarksFromTimeline(
  bookmarks: QueryResultRow[],
  events: QueryResultRow[],
  durationSec: number
): Record<string, unknown>[] {
  return labelerBookmarkStateFromTimeline(bookmarks, events, durationSec).bookmarks;
}

function labelerBookmarkStateFromTimeline(
  bookmarks: QueryResultRow[],
  events: QueryResultRow[],
  durationSec: number
): { bookmarks: Record<string, unknown>[]; derivedFromEvents: boolean } {
  const validBookmarks = bookmarks.flatMap((bookmark) => {
    const t = bookmarkTimeFromRow(bookmark);
    if (!isUsableTimelineTime(t, durationSec)) return [];
    return [{
      id: bookmark.id,
      t,
      end_t: bookmarkEndFromRow(bookmark) ?? undefined,
      type: "bookmark",
      label: bookmark.text || bookmark.label || bookmark.source || "bookmark",
      reason: bookmark.text || bookmark.reason || "",
      tags: asArray(bookmark.tags)
    }];
  });

  const zeroishCount = validBookmarks.filter((bookmark) => Number(bookmark.t || 0) <= 0.001).length;
  const eventBookmarks = events.flatMap((event) => {
    const t = bookmarkTimeFromRow(event);
    if (!isBookmarkEvent(event) || !isUsableTimelineTime(t, durationSec)) return [];
    const label = bookmarkEventLabel(event);
    return [{
      id: `event:${event.id ?? `${t}:${label}`}`,
      t,
      end_t: bookmarkEndFromRow(event) ?? undefined,
      type: "bookmark",
      label,
      reason: label,
      tags: event.source ? [String(event.source)] : []
    }];
  });
  const timedEventBookmarks = eventBookmarks.filter((bookmark) => Number(bookmark.t || 0) > 0.001);
  const degenerateBookmarkRows = validBookmarks.length > 1 &&
    zeroishCount / validBookmarks.length > 0.8 &&
    timedEventBookmarks.length > 0;
  const derivedFromEvents = eventBookmarks.length > 0 && (degenerateBookmarkRows || validBookmarks.length === 0);

  return {
    bookmarks: (derivedFromEvents ? eventBookmarks : validBookmarks)
      .sort((a, b) => Number(a.t || 0) - Number(b.t || 0)),
    derivedFromEvents
  };
}

export async function jobData(pool: Pool, jobId: string): Promise<Record<string, unknown>> {
  const job = await getReviewJob(pool, jobId);
  if (!job) throw new Error("unknown DB job");
  const reviewJobId = Number(job.id);
  const timeline = await timelineForRoute(pool, String(job.route_uuid));
  const artifacts = (timeline.artifacts as QueryResultRow[] | undefined) || [];
  const video = selectReviewVideo(artifacts);
  const videoPlayable = video ? isPlayableVideo(video) : false;
  const routeRow = timeline.route as QueryResultRow | undefined;
  const routeMetadata = asJson(routeRow?.metadata_jsonb);
  const videoStatus = video ? (videoPlayable ? "ok" : "raw video only; make MP4 review video first") :
    routeMetadata.web_ingest_include_video === false ? "logs only; video not ingested" : "missing video artifact";
  const labels = ((timeline.labels as QueryResultRow[] | undefined) || [])
    .filter((x) => !x.review_job_id || Number(x.review_job_id) === reviewJobId)
    .map((x) => labelFromDb(x, reviewJobId));
  const samples = ((timeline.telemetry as { samples?: QueryResultRow[] } | undefined)?.samples) || [];
  const durationSec = durationFromTimeline(job, timeline);
  const events = ((timeline.events as QueryResultRow[] | undefined) || []);
  const bookmarkState = labelerBookmarkStateFromTimeline((timeline.bookmarks as QueryResultRow[] | undefined) || [], events, durationSec);
  const eventsForLabeler = bookmarkState.derivedFromEvents ? events.filter((event) => !isBookmarkEvent(event)) : events;
  return {
    route: {
      route_id: job.route_id,
      route_uuid: job.route_uuid,
      route_label: job.effective_route_label
    },
    duration_sec: durationSec,
    route_start_wall_time: job.started_at || "",
    route_end_wall_time: job.ended_at || "",
    route_miles: milesFromSamples(samples),
    video_status: videoStatus,
    video_url: video && videoPlayable ? `/api/artifact-proxy/${video.id}` : "",
    video_sync: videoSyncForSelectedVideo(video, timeline, durationSec),
    video_fps: 20,
    events: eventsForLabeler.map(eventFromDb),
    bookmarks: bookmarkState.bookmarks,
    labels,
    artifacts,
    telemetry: { samples },
    radar: { samples: [] },
    can: timeline.can || { samples: [] }
  };
}

export async function driveJobsPayload(pool: Pool): Promise<Record<string, unknown>> {
  const jobs = await listReviewJobs(pool);
  return {
    jobs: jobs.map((j) => ({
      job_id: j.job_id,
      db: true,
      review_job_id: j.id,
      job_version: j.version,
      inbox_name: "logdrive_label_validation",
      route_uuid: j.route_uuid,
      route_id: j.route_id,
      route_label: j.route_label,
      analysis_name: "logdrive_label_validation",
      ride_type: j.ride_type,
      ride_metadata: j.ride_type,
      duration_sec: j.duration_sec || 0,
      route_start_wall_time: j.started_at || "",
      route_end_wall_time: j.ended_at || "",
      media_status: j.media_status,
      data_path: `/api/job-data?job_id=${encodeURIComponent(j.job_id)}`
    })),
    default_job_id: jobs[0]?.job_id || "",
    source: "node-db"
  };
}

export async function labelsForJob(pool: Pool, jobId: string): Promise<Record<string, unknown[]>> {
  const data = await jobData(pool, jobId);
  const labels = (data.labels as Record<string, unknown>[] | undefined) || [];
  return {
    drive: labels.filter((label) => (label.label_kind || "drive") === "drive"),
    phev: labels.filter((label) => label.label_kind === "phev")
  };
}

export async function saveLabel(pool: Pool, data: Record<string, unknown>): Promise<{ id: number }> {
  const expected = data.expected_version;
  const labelId = data.id ? Number(data.id) : null;
  const tags = asArray(data.tags);
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    let id = labelId;
    if (id) {
      const current = await client.query("SELECT version FROM labels WHERE id=$1 AND deleted_at IS NULL", [id]);
      if (!current.rowCount) throw new Error("label not found");
      if (expected == null || Number(current.rows[0].version) !== Number(expected)) throw new Error("version_conflict");
      await client.query(
        `UPDATE labels
         SET label=$2, severity=$3, start_sec=$4, end_sec=$5, notes=$6, tags=$7, version=version+1, updated_at=now(), updated_by=$8
         WHERE id=$1`,
        [id, data.label, data.severity, data.start_sec, data.end_sec, data.notes, tags, data.updated_by || "brickpilot-ui"]
      );
      await client.query("INSERT INTO label_events(label_id,review_job_id,action,payload_jsonb,created_by) VALUES($1,$2,'update',$3::jsonb,$4)", [id, data.review_job_id, JSON.stringify(data), data.updated_by || "brickpilot-ui"]);
    } else {
      const identity = String(data.label_identity_hash || crypto.createHash("sha256").update(JSON.stringify(data)).digest("hex"));
      const created = await client.query<{ id: string }>(
        `INSERT INTO labels(route_uuid,review_job_id,label_identity_hash,label_kind,label,severity,start_sec,end_sec,notes,tags,created_by,metadata_jsonb)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
         ON CONFLICT(label_identity_hash) DO UPDATE SET updated_at=now()
         RETURNING id`,
        [data.route_uuid, data.review_job_id, identity, data.label_kind, data.label, data.severity, data.start_sec, data.end_sec, data.notes, tags, data.created_by || "brickpilot-ui", JSON.stringify(data.metadata || {})]
      );
      id = Number(created.rows[0].id);
      await client.query("INSERT INTO label_events(label_id,review_job_id,action,payload_jsonb,created_by) VALUES($1,$2,'create',$3::jsonb,$4)", [id, data.review_job_id, JSON.stringify(data), data.created_by || "brickpilot-ui"]);
    }
    await client.query("COMMIT");
    return { id: id || 0 };
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}

export async function deleteLabel(pool: Pool, data: Record<string, unknown>): Promise<{ deleted: true }> {
  const id = Number(data.id);
  const current = await pool.query("SELECT version FROM labels WHERE id=$1 AND deleted_at IS NULL", [id]);
  if (!current.rowCount) throw new Error("label not found");
  if (data.expected_version == null || Number(current.rows[0].version) !== Number(data.expected_version)) throw new Error("version_conflict");
  await pool.query("UPDATE labels SET deleted_at=now(), deleted_by=$2, version=version+1 WHERE id=$1", [id, data.deleted_by || "brickpilot-ui"]);
  await pool.query("INSERT INTO label_events(label_id,review_job_id,action,payload_jsonb,created_by) VALUES($1,$2,'delete',$3::jsonb,$4)", [id, data.review_job_id, JSON.stringify(data), data.deleted_by || "brickpilot-ui"]);
  return { deleted: true };
}

export async function finishReview(pool: Pool, data: Record<string, unknown>): Promise<{ finished: true }> {
  const jobId = Number(data.review_job_id);
  const current = await pool.query("SELECT version FROM review_jobs WHERE id=$1", [jobId]);
  if (!current.rowCount) throw new Error("review job not found");
  if (data.expected_version == null || Number(current.rows[0].version) !== Number(data.expected_version)) throw new Error("version_conflict");
  await pool.query("INSERT INTO finished_reviews(review_job_id,route_uuid,finished_by,label_count,notes,snapshot_jsonb) VALUES($1,$2,$3,$4,$5,$6::jsonb)", [jobId, data.route_uuid, data.finished_by || "brickpilot-ui", data.label_count, data.notes, JSON.stringify(data)]);
  await pool.query("UPDATE review_jobs SET status='finished', version=version+1, updated_at=now(), updated_by=$2 WHERE id=$1", [jobId, data.finished_by || "brickpilot-ui"]);
  await pool.query("INSERT INTO label_events(review_job_id,action,payload_jsonb,created_by) VALUES($1,'finish_review',$2::jsonb,$3)", [jobId, JSON.stringify(data), data.finished_by || "brickpilot-ui"]);
  return { finished: true };
}

export async function finishedReviews(pool: Pool): Promise<QueryResultRow[]> {
  const result = await pool.query(
    `SELECT f.*, j.legacy_job_id, j.ride_type, COALESCE(j.route_label,r.route_label,r.canonical_name,r.route_id) AS route_label, r.route_id
     FROM finished_reviews f
     LEFT JOIN review_jobs j ON j.id=f.review_job_id
     LEFT JOIN routes r ON r.id=f.route_uuid
     ORDER BY f.finished_at DESC
     LIMIT 200`
  );
  return result.rows;
}

export async function artifactPath(pool: Pool, paths: BrickpilotPaths, artifactId: number): Promise<{ path: string; mime: string; size: number }> {
  const result = await pool.query("SELECT artifact_path,mime_type,size_bytes FROM artifacts WHERE id=$1", [artifactId]);
  if (!result.rowCount) throw new Error("artifact not found");
  const artifactRel = String(result.rows[0].artifact_path);
  const full = safeJoin(paths.artifactRoot, artifactRel);
  return {
    path: full,
    mime: result.rows[0].mime_type || "application/octet-stream",
    size: Number(result.rows[0].size_bytes || 0)
  };
}

export async function deleteRide(pool: Pool, paths: BrickpilotPaths, routeUuid: string, confirm: boolean): Promise<Record<string, unknown>> {
  if (!confirm) throw new Error("confirmation_required");
  const client = await pool.connect();
  const movedFiles: string[] = [];
  try {
    await client.query("BEGIN");
    const requestedRouteId = routeUuid.replace(/^external:/, "");
    const route = await client.query("SELECT id, route_id FROM routes WHERE id::text=$1 OR route_id=$1 FOR UPDATE", [routeUuid]);
    if (!route.rowCount) {
      await tombstoneDeletedRoute(client, requestedRouteId, null, "brickpilot-ui");
      await client.query("COMMIT");
      return {
        deleted: true,
        route_uuid: "",
        route_id: requestedRouteId,
        discovered_only: true,
        artifact_count: 0,
        orphan_artifacts_deleted: 0,
        files_moved_to: "",
        moved_file_count: 0
      };
    }
    const routeId = String(route.rows[0].id);
    const visibleRouteId = String(route.rows[0].route_id || requestedRouteId);
    await tombstoneDeletedRoute(client, visibleRouteId, routeId, "brickpilot-ui");
    const artRows = await client.query<{ id: string; artifact_path: string }>(
      `SELECT DISTINCT a.id, a.artifact_path
       FROM artifacts a JOIN route_artifacts ra ON ra.artifact_id=a.id
       WHERE ra.route_uuid=$1`,
      [routeId]
    );
    const orphanIds: number[] = [];
    for (const art of artRows.rows) {
      const refs = await client.query(
        `SELECT
           (SELECT COUNT(*) FROM route_artifacts WHERE artifact_id=$1 AND route_uuid<>$2) AS route_refs,
           (SELECT COUNT(*) FROM bookmarks WHERE artifact_id=$1 AND (route_uuid IS NULL OR route_uuid<>$2)) AS bookmark_refs,
           (SELECT COUNT(*) FROM video_sync_segments WHERE artifact_id=$1 AND route_uuid<>$2) AS video_refs,
           (SELECT COUNT(*) FROM input_set_members WHERE artifact_id=$1 AND (route_uuid IS NULL OR route_uuid<>$2)) AS input_refs,
           (SELECT COUNT(*)
              FROM analysis_outputs ao
              JOIN analysis_runs ar ON ar.id=ao.analysis_run_id
             WHERE ao.artifact_id=$1 AND (ar.route_uuid IS NULL OR ar.route_uuid<>$2)) AS analysis_output_refs,
           (SELECT COUNT(*) FROM analysis_runs WHERE report_artifact_id=$1 AND (route_uuid IS NULL OR route_uuid<>$2)) AS analysis_report_refs`,
        [art.id, routeId]
      );
      const refCount = Object.values(refs.rows[0]).reduce<number>((sum, value) => sum + Number(value || 0), 0);
      if (refCount === 0) orphanIds.push(Number(art.id));
    }
    const deletedDir = path.join(paths.artifactRoot, "deleted_artifacts", new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z"), routeId);
    for (const art of artRows.rows) {
      if (!orphanIds.includes(Number(art.id))) continue;
      const src = safeJoin(paths.artifactRoot, art.artifact_path);
      if (!fs.existsSync(src)) continue;
      const dst = safeJoin(deletedDir, art.artifact_path);
      await fsp.mkdir(path.dirname(dst), { recursive: true });
      await fsp.rename(src, dst);
      movedFiles.push(dst);
    }
    await client.query("DELETE FROM label_events WHERE label_id IN (SELECT id FROM labels WHERE route_uuid=$1)", [routeId]);
    await client.query("DELETE FROM label_events WHERE review_job_id IN (SELECT id FROM review_jobs WHERE route_uuid=$1)", [routeId]);
    await client.query("DELETE FROM finished_reviews WHERE route_uuid=$1", [routeId]);
    await client.query("DELETE FROM input_set_members WHERE route_uuid=$1", [routeId]);
    await client.query("DELETE FROM labels WHERE route_uuid=$1", [routeId]);
    await client.query("DELETE FROM bookmarks WHERE route_uuid=$1", [routeId]);
    await client.query("UPDATE analysis_runs SET route_uuid=NULL WHERE route_uuid=$1", [routeId]);
    await client.query("UPDATE ingest_items SET route_uuid=NULL WHERE route_uuid=$1", [routeId]);
    await client.query("DELETE FROM review_jobs WHERE route_uuid=$1", [routeId]);
    for (const aid of orphanIds) {
      await client.query("DELETE FROM source_paths WHERE artifact_id=$1", [aid]);
      await client.query("DELETE FROM legacy_files WHERE artifact_id=$1", [aid]);
      await client.query("UPDATE ingest_items SET artifact_id=NULL WHERE artifact_id=$1", [aid]);
      await client.query("UPDATE analysis_runs SET report_artifact_id=NULL WHERE report_artifact_id=$1", [aid]);
      await client.query("DELETE FROM input_set_members WHERE artifact_id=$1", [aid]);
      await client.query("DELETE FROM analysis_outputs WHERE artifact_id=$1", [aid]);
      await client.query("DELETE FROM ml_run_outputs WHERE artifact_id=$1", [aid]);
    }
    await client.query("DELETE FROM routes WHERE id=$1", [routeId]);
    for (const aid of orphanIds) await client.query("DELETE FROM artifacts WHERE id=$1", [aid]);
    await client.query("COMMIT");
    return {
      deleted: true,
      route_uuid: routeId,
      route_id: visibleRouteId,
      artifact_count: artRows.rowCount,
      orphan_artifacts_deleted: orphanIds.length,
      files_moved_to: movedFiles.length ? deletedDir : "",
      moved_file_count: movedFiles.length
    };
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}
