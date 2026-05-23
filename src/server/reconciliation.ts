import fsp from "node:fs/promises";
import path from "node:path";
import type { Pool } from "pg";
import type { BrickpilotPaths } from "./lib";
import type { RideReportFile, RideRow, RideSourceSummary, RideTimeRange } from "../shared/types";

export const ROUTE_ID_RE = /(^|[^0-9a-f])([0-9a-f]{8}--[0-9a-f]{10})(?:--\d+)?(?=$|[^0-9a-f])/gi;
const REPORT_EXTENSIONS = new Set([".md", ".json", ".yaml", ".yml", ".csv", ".txt"]);
const DEFAULT_MAX_DEPTH = 8;
const DEFAULT_MAX_REPORTS = Number(process.env.BRICKPILOT_RECONCILE_MAX_REPORTS || 5000);
const DEFAULT_MAX_ENTRIES = Number(process.env.BRICKPILOT_RECONCILE_MAX_FILES || 50000);
const DEFAULT_SCAN_CONCURRENCY = Number(process.env.BRICKPILOT_RECONCILE_SCAN_CONCURRENCY || 8);
const PREVIEW_BYTES = 16 * 1024;
const CACHE_MS = Number(process.env.BRICKPILOT_RECONCILE_CACHE_MS || process.env.BRICKPILOT_RECONCILIATION_CACHE_MS || 30000);

export interface ScanRoot {
  kind: string;
  path: string;
  exists: boolean;
  files?: number;
}

export interface CatalogReport extends RideReportFile {
  id: string;
  size_bytes: number;
  modified_at: string | null;
}

export interface CatalogSource {
  route_id: string;
  kind: string;
  path: string;
  relative_path: string;
  mtime: string | null;
  modified_at: string | null;
  size_bytes: number;
  file_count: number;
  segment_count: number;
}

export interface DataCatalog {
  data_root: string;
  scan_roots: ScanRoot[];
  reports: CatalogReport[];
  sources: CatalogSource[];
  truncated: boolean;
  scanned_at: string;
}

export interface ReconciliationCatalog extends DataCatalog {
  scanned_files: number;
  discovered_routes: string[];
  roots: Array<{ root: string; exists: boolean; files: number }>;
}

interface ScanOptions {
  maxDepth?: number;
  maxReports?: number;
  maxEntries?: number;
}

let cachedCatalog: { key: string; expires: number; catalog: ReconciliationCatalog } | null = null;

export function extractRouteIds(input: string): string[] {
  const seen = new Set<string>();
  ROUTE_ID_RE.lastIndex = 0;
  for (const match of input.matchAll(ROUTE_ID_RE)) {
    seen.add(match[2].toLowerCase());
  }
  return [...seen].sort();
}

export function isPathInside(root: string, candidate: string): boolean {
  const resolvedRoot = path.resolve(root);
  const resolvedCandidate = path.resolve(candidate);
  return resolvedCandidate === resolvedRoot || resolvedCandidate.startsWith(resolvedRoot + path.sep);
}

export function assertInsideDataRoot(paths: Pick<BrickpilotPaths, "dataRoot">, candidate: string): string {
  const resolved = path.resolve(candidate);
  if (!isPathInside(paths.dataRoot, resolved)) throw new Error("path escapes BrickpilotDriveDB");
  return resolved;
}

export function scanRoots(paths: Pick<BrickpilotPaths, "dataRoot" | "labelerOutputRoot">): ScanRoot[] {
  const dataRoot = paths.dataRoot;
  return [
    { kind: "raw_imports", path: path.join(dataRoot, "imports", "raw", "from_comma"), exists: false },
    { kind: "logdrive_runs", path: path.join(dataRoot, "logdrive_runs"), exists: false },
    { kind: "labeler_outputs", path: paths.labelerOutputRoot || path.join(dataRoot, "labeler_outputs"), exists: false },
    { kind: "analysis_exports", path: path.join(dataRoot, "analysis_exports"), exists: false },
    { kind: "quarantine", path: path.join(dataRoot, "quarantine"), exists: false }
  ];
}

export async function getDataCatalog(paths: BrickpilotPaths): Promise<ReconciliationCatalog> {
  return buildReconciliationCatalog(paths);
}

export async function buildReconciliationCatalog(paths: BrickpilotPaths, force = false): Promise<ReconciliationCatalog> {
  const key = path.resolve(paths.dataRoot);
  const now = Date.now();
  if (!force && cachedCatalog && cachedCatalog.key === key && cachedCatalog.expires > now) return cachedCatalog.catalog;
  const catalog = await scanDataCatalog(paths);
  cachedCatalog = { key, expires: now + CACHE_MS, catalog };
  return catalog;
}

export async function scanDataCatalog(paths: BrickpilotPaths, options: ScanOptions = {}): Promise<ReconciliationCatalog> {
  const roots = scanRoots(paths);
  const reports: CatalogReport[] = [];
  const sourceGroups = new Map<string, CatalogSource>();
  const discoveredRoutes = new Set<string>();
  let entries = 0;
  let reportsRemaining = options.maxReports ?? DEFAULT_MAX_REPORTS;
  let truncated = false;
  const maxDepth = options.maxDepth ?? DEFAULT_MAX_DEPTH;
  const maxEntries = options.maxEntries ?? DEFAULT_MAX_ENTRIES;

  await Promise.all(roots.map(async (root) => {
    root.exists = await dirExistsInside(paths, root.path);
  }));

  const rawRoot = roots.find((root) => root.kind === "raw_imports");
  if (rawRoot?.exists) {
    for (const source of await scanRawImportSources(paths, rawRoot.path, maxEntries)) {
      discoveredRoutes.add(source.route_id);
      sourceGroups.set(`${source.route_id}|${source.path}`, source);
    }
  }

  for (const root of roots.filter((r) => r.exists)) {
    const startEntries = entries;
    if (truncated) break;
    await walkReportFiles(paths, root, root.path, 0, {
      maxDepth,
      maxEntries,
      get maxReports() {
        return reportsRemaining;
      },
      set maxReports(value: number) {
        reportsRemaining = value;
      },
      get entries() {
        return entries;
      },
      set entries(value: number) {
        entries = value;
      },
      get truncated() {
        return truncated;
      },
      set truncated(value: boolean) {
        truncated = value;
      },
      onReport: async (filePath, stat) => {
        const report = await buildReport(paths, root, filePath, stat);
        reports.push(report);
        for (const routeId of report.route_ids) {
          discoveredRoutes.add(routeId);
          const sourcePath = sourceGroupPath(root.path, filePath, routeId);
          const key = `${routeId}|${sourcePath}`;
          if (!sourceGroups.has(key)) {
            sourceGroups.set(key, {
              route_id: routeId,
              kind: root.kind,
              path: sourcePath,
              relative_path: path.relative(paths.dataRoot, sourcePath),
              mtime: report.mtime,
              modified_at: report.mtime,
              size_bytes: report.size,
              file_count: 1,
              segment_count: 0
            });
          } else {
            const current = sourceGroups.get(key)!;
            current.file_count += 1;
            current.size_bytes += report.size;
            current.mtime = latestIso(current.mtime, report.mtime);
            current.modified_at = current.mtime;
          }
        }
      }
    });
    root.files = entries - startEntries;
  }

  reports.sort((a, b) => String(b.mtime || "").localeCompare(String(a.mtime || "")) || a.path.localeCompare(b.path));
  const sources = [...sourceGroups.values()].sort((a, b) => String(b.mtime || "").localeCompare(String(a.mtime || "")) || a.path.localeCompare(b.path));
  return {
    data_root: path.resolve(paths.dataRoot),
    scan_roots: roots,
    reports,
    sources,
    truncated,
    scanned_at: new Date().toISOString(),
    scanned_files: entries,
    discovered_routes: [...discoveredRoutes].sort(),
    roots: roots.map((root) => ({ root: root.path, exists: root.exists, files: root.files || 0 }))
  };
}

export function reportsForRide(catalog: DataCatalog, routeIds: string[]): CatalogReport[];
export function reportsForRide(pool: Pool, paths: BrickpilotPaths, routeOrUuid: string): Promise<CatalogReport[]>;
export function reportsForRide(
  first: DataCatalog | Pool,
  second: string[] | BrickpilotPaths,
  third?: string
): CatalogReport[] | Promise<CatalogReport[]> {
  if (Array.isArray(second)) return reportsForRouteIds(first as DataCatalog, second);
  return reportsForRideFromDb(first as Pool, second as BrickpilotPaths, String(third || ""));
}

export function mergeDiscoveredRides(rides: RideRow[], catalog: DataCatalog): RideRow[] {
  const byRoute = new Map(rides.map((ride) => [ride.route_id.toLowerCase(), ride]));
  const byUuid = new Map(rides.map((ride) => [ride.id.toLowerCase(), ride]));
  const reportsByRoute = new Map<string, CatalogReport[]>();
  const sourcesByRoute = new Map<string, CatalogSource[]>();
  for (const report of catalog.reports) {
    for (const routeId of report.route_ids) {
      reportsByRoute.set(routeId, [...(reportsByRoute.get(routeId) || []), report]);
    }
  }
  for (const source of catalog.sources) {
    sourcesByRoute.set(source.route_id, [...(sourcesByRoute.get(source.route_id) || []), source]);
  }
  const routeIds = new Set<string>([...reportsByRoute.keys(), ...sourcesByRoute.keys()]);

  for (const ride of rides) {
    const routeId = ride.route_id.toLowerCase();
    const reports = reportsByRoute.get(routeId) || [];
    const sources = sourcesByRoute.get(routeId) || [];
    applyDiscovery(ride, reports, sources, false);
  }

  for (const routeId of routeIds) {
    if (byRoute.has(routeId) || byUuid.has(routeId)) continue;
    const reports = reportsByRoute.get(routeId) || [];
    const sources = sourcesByRoute.get(routeId) || [];
    if (!reports.length && !sources.length) continue;
    const row: RideRow = {
      id: `external:${routeId}`,
      route_id: routeId,
      canonical_name: routeId,
      route_label: routeId,
      ride_type: null,
      drive_type: null,
      label_validation: false,
      notes: "",
      branch: null,
      brickpilot_version: null,
      model_bundle: null,
      vehicle: null,
      settings_summary: "",
      settings_detail: null,
      started_at: sources.map((source) => source.mtime).filter(Boolean).sort()[0] || null,
      ended_at: null,
      duration_sec: null,
      mileage: null,
      segment_count: sources.reduce((sum, source) => Math.max(sum, source.segment_count), 0),
      media_status: "missing",
      artifact_count: 0,
      log_artifact_count: 0,
      video_artifact_count: 0,
      playable_video_artifact_count: 0,
      raw_video_artifact_count: 0,
      label_count: 0,
      bookmark_count: 0,
      event_count: 0,
      sample_count: 0,
      video_sync_count: 0,
      metadata_summary: { discovered_only: true }
    };
    applyDiscovery(row, reports, sources, true);
    rides.push(row);
  }

  return rides.sort((a, b) => {
    const am = a.latest_discovered_mtime || a.started_at || "";
    const bm = b.latest_discovered_mtime || b.started_at || "";
    return bm.localeCompare(am);
  });
}

export function augmentRidesWithDiscovered(dbRides: RideRow[], catalog: DataCatalog): RideRow[] {
  return mergeDiscoveredRides(dbRides, catalog);
}

export function reconciliationSummary(catalog: DataCatalog, dbRouteIds?: string[]): Record<string, unknown>;
export function reconciliationSummary(pool: Pool, paths: BrickpilotPaths): Promise<Record<string, unknown>>;
export function reconciliationSummary(
  first: DataCatalog | Pool,
  second: string[] | BrickpilotPaths = []
): Record<string, unknown> | Promise<Record<string, unknown>> {
  if (Array.isArray(second)) return summarizeCatalog(first as DataCatalog, second);
  return summarizeFromDb(first as Pool, second as BrickpilotPaths);
}

async function reportsForRideFromDb(pool: Pool, paths: BrickpilotPaths, routeOrUuid: string): Promise<CatalogReport[]> {
  const routeIds = new Set(extractRouteIds(routeOrUuid));
  const result = await pool.query<{ route_id: string; canonical_name: string | null }>(
    "SELECT route_id, canonical_name FROM routes WHERE id::text=$1 OR route_id=$1 OR canonical_name=$1 LIMIT 1",
    [routeOrUuid]
  );
  if (result.rows[0]?.route_id) routeIds.add(String(result.rows[0].route_id).toLowerCase());
  if (result.rows[0]?.canonical_name) for (const id of extractRouteIds(String(result.rows[0].canonical_name))) routeIds.add(id);
  const catalog = await buildReconciliationCatalog(paths);
  return reportsForRouteIds(catalog, [...routeIds]);
}

function reportsForRouteIds(catalog: DataCatalog, routeIds: string[]): CatalogReport[] {
  const wanted = new Set(routeIds.flatMap((id) => extractRouteIds(id)).concat(routeIds.map((id) => id.toLowerCase())));
  return catalog.reports.filter((report) => report.route_ids.some((id) => wanted.has(id)));
}

async function summarizeFromDb(pool: Pool, paths: BrickpilotPaths): Promise<Record<string, unknown>> {
  const [routesResult, catalog] = await Promise.all([
    pool.query<{ route_id: string }>("SELECT route_id FROM routes"),
    buildReconciliationCatalog(paths)
  ]);
  return summarizeCatalog(catalog, routesResult.rows.map((row) => row.route_id));
}

function summarizeCatalog(catalog: DataCatalog, dbRouteIds: string[] = []): Record<string, unknown> {
  const dbRoutes = new Set(dbRouteIds.map((id) => id.toLowerCase()));
  const linkedReports = catalog.reports.filter((report) => report.route_ids.some((id) => dbRoutes.has(id)));
  const unlinkedReports = catalog.reports.filter((report) => !report.route_ids.length || !report.route_ids.some((id) => dbRoutes.has(id)));
  const linkedSources = catalog.sources.filter((source) => dbRoutes.has(source.route_id));
  const unlinkedSources = catalog.sources.filter((source) => !dbRoutes.has(source.route_id));
  const reportRoutes = new Set(catalog.reports.flatMap((report) => report.route_ids));
  const sourceRoutes = new Set(catalog.sources.map((source) => source.route_id));

  return {
    data_root: catalog.data_root,
    scanned_at: catalog.scanned_at,
    truncated: catalog.truncated,
    scan_roots: catalog.scan_roots,
    counts: {
      db_routes: dbRoutes.size,
      discovered_routes: new Set([...reportRoutes, ...sourceRoutes]).size,
      reports: catalog.reports.length,
      linked_reports: linkedReports.length,
      unlinked_reports: unlinkedReports.length,
      sources: catalog.sources.length,
      linked_sources: linkedSources.length,
      unlinked_sources: unlinkedSources.length,
      routes_with_reports: reportRoutes.size,
      routes_with_sources: sourceRoutes.size
    }
  };
}

async function dirExistsInside(paths: BrickpilotPaths, dirPath: string): Promise<boolean> {
  if (!isPathInside(paths.dataRoot, dirPath)) return false;
  try {
    const stat = await fsp.stat(dirPath);
    return stat.isDirectory();
  } catch {
    return false;
  }
}

async function mapWithConcurrency<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = [];
  let next = 0;
  const workerCount = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: workerCount }, async () => {
    for (;;) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      out[index] = await fn(items[index]);
    }
  }));
  return out;
}

async function scanRawImportSources(paths: BrickpilotPaths, root: string, maxEntries: number): Promise<CatalogSource[]> {
  const candidates: Array<{ route_id: string; sourcePath: string }> = [];
  let dir;
  try {
    dir = await fsp.opendir(assertInsideDataRoot(paths, root));
  } catch {
    return [];
  }
  let scanned = 0;
  for await (const entry of dir) {
    if (scanned++ > maxEntries) break;
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
    const routeIds = extractRouteIds(entry.name);
    if (routeIds.length !== 1 || routeIds[0] !== entry.name.toLowerCase()) continue;
    candidates.push({ route_id: routeIds[0], sourcePath: path.join(root, entry.name) });
  }
  return mapWithConcurrency(candidates, DEFAULT_SCAN_CONCURRENCY, async ({ route_id, sourcePath }) => {
    const stats = await statTree(paths, sourcePath, maxEntries);
    return {
      route_id,
      kind: "raw_imports",
      path: sourcePath,
      relative_path: path.relative(paths.dataRoot, sourcePath),
      mtime: stats.mtime,
      modified_at: stats.mtime,
      size_bytes: stats.sizeBytes,
      file_count: stats.fileCount,
      segment_count: await countSegmentDirs(paths, sourcePath)
    };
  });
}

async function countSegmentDirs(paths: BrickpilotPaths, sourcePath: string): Promise<number> {
  try {
    const segmentsPath = assertInsideDataRoot(paths, path.join(sourcePath, "segments"));
    const dir = await fsp.opendir(segmentsPath);
    let count = 0;
    for await (const entry of dir) {
      if (entry.isDirectory() && !entry.isSymbolicLink() && extractRouteIds(entry.name).length) count += 1;
    }
    return count;
  } catch {
    return 0;
  }
}

async function statTree(paths: BrickpilotPaths, root: string, maxEntries: number): Promise<{ fileCount: number; sizeBytes: number; mtime: string | null }> {
  let fileCount = 0;
  let sizeBytes = 0;
  let newest: string | null = null;
  const stack = [assertInsideDataRoot(paths, root)];
  while (stack.length && fileCount < maxEntries) {
    const current = stack.pop()!;
    let entries;
    try {
      entries = await fsp.readdir(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const entryPath = path.join(current, entry.name);
      if (!isPathInside(paths.dataRoot, entryPath)) continue;
      if (entry.isDirectory()) {
        stack.push(entryPath);
        continue;
      }
      if (!entry.isFile()) continue;
      try {
        const stat = await fsp.stat(entryPath);
        fileCount += 1;
        sizeBytes += stat.size;
        newest = latestIso(newest, stat.mtime.toISOString());
      } catch {
        // Ignore files that disappear during a scan.
      }
      if (fileCount >= maxEntries) break;
    }
  }
  return { fileCount, sizeBytes, mtime: newest };
}

async function walkReportFiles(
  paths: BrickpilotPaths,
  root: ScanRoot,
  dirPath: string,
  depth: number,
  state: {
    maxDepth: number;
    maxReports: number;
    maxEntries: number;
    entries: number;
    truncated: boolean;
    onReport: (filePath: string, stat: { size: number; mtime: Date }) => Promise<void>;
  }
): Promise<void> {
  if (state.truncated || depth > state.maxDepth || !isPathInside(paths.dataRoot, dirPath)) return;
  let entries;
  try {
    entries = await fsp.readdir(assertInsideDataRoot(paths, dirPath), { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (state.truncated || state.entries >= state.maxEntries || state.maxReports <= 0) {
      state.truncated = true;
      return;
    }
    if (entry.isSymbolicLink()) continue;
    state.entries += 1;
    const entryPath = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      if (shouldSkipDir(entry.name)) continue;
      await walkReportFiles(paths, root, entryPath, depth + 1, state);
      continue;
    }
    if (!entry.isFile() || !REPORT_EXTENSIONS.has(path.extname(entry.name).toLowerCase())) continue;
    try {
      const stat = await fsp.stat(entryPath);
      await state.onReport(entryPath, stat);
      state.maxReports -= 1;
    } catch {
      // Ignore files that disappear during a scan.
    }
  }
}

function shouldSkipDir(name: string): boolean {
  return name === ".git" || name === "node_modules" || name === "__pycache__" || name === ".venv" || name === "artifacts" || name === "deleted_artifacts";
}

async function buildReport(paths: BrickpilotPaths, root: ScanRoot, filePath: string, stat: { size: number; mtime: Date }): Promise<CatalogReport> {
  const safePath = assertInsideDataRoot(paths, filePath);
  const preview = await readPreview(safePath);
  const metadata = await reportJsonMetadata(safePath, preview, stat.size);
  const routeIds = [...new Set([...extractRouteIds(`${safePath}\n${preview}`), ...metadata.routeIds])].sort();
  const mtime = stat.mtime.toISOString();
  const size = stat.size;
  return {
    id: Buffer.from(safePath).toString("base64url"),
    title: reportTitle(filePath, preview),
    kind: reportKind(filePath),
    path: safePath,
    relative_path: path.relative(paths.dataRoot, safePath),
    root: root.kind,
    mtime,
    modified_at: mtime,
    size,
    size_bytes: size,
    route_ids: routeIds,
    summary: summarizePreview(preview),
    route_time_ranges: Object.keys(metadata.routeTimeRanges).length ? metadata.routeTimeRanges : undefined
  };
}

async function reportJsonMetadata(filePath: string, preview: string, size: number): Promise<{ routeIds: string[]; routeTimeRanges: Record<string, RideTimeRange> }> {
  if (path.extname(filePath).toLowerCase() !== ".json") return { routeIds: [], routeTimeRanges: {} };
  let text = preview;
  if (size <= 2_000_000) {
    try {
      text = await fsp.readFile(filePath, "utf8");
    } catch {
      text = preview;
    }
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { routeIds: [], routeTimeRanges: {} };
  }
  const routeTimeRanges: Record<string, RideTimeRange> = {};
  const routeIds = new Set<string>();

  function collect(obj: unknown): void {
    if (!obj || typeof obj !== "object") return;
    const rec = obj as Record<string, unknown>;
    const candidates = [
      rec.route_id,
      rec.routeId,
      rec.route_uuid,
      rec.route_label,
      rec.analysis_name,
      typeof rec.route === "object" && rec.route ? (rec.route as Record<string, unknown>).route_id : undefined
    ].flatMap((value) => extractRouteIds(String(value || "")));
    for (const routeId of candidates) {
      routeIds.add(routeId);
      const start = stringValue(rec.route_start_wall_time) || stringValue(rec.drive_start_wall_time) || stringValue(rec.start_wall_time);
      const end = stringValue(rec.route_end_wall_time) || stringValue(rec.drive_end_wall_time) || stringValue(rec.end_wall_time);
      const duration = numberValue(rec.duration_sec);
      const mileage = numberValue(rec.route_miles) ?? numberValue(rec.distance_miles) ?? numberValue(rec.miles);
      if (start || end || duration != null || mileage != null) {
        routeTimeRanges[routeId] = {
          ...(routeTimeRanges[routeId] || {}),
          ...(start ? { start } : {}),
          ...(end ? { end } : {}),
          ...(duration != null ? { duration_sec: duration } : {}),
          ...(mileage != null ? { mileage } : {})
        };
      }
    }
  }

  collect(parsed);
  const root = parsed as Record<string, unknown>;
  if (Array.isArray(root.jobs)) for (const job of root.jobs) collect(job);
  if (Array.isArray(root.routes)) for (const route of root.routes) collect(route);
  return { routeIds: [...routeIds].sort(), routeTimeRanges };
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function numberValue(value: unknown): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

async function readPreview(filePath: string): Promise<string> {
  const handle = await fsp.open(filePath, "r");
  try {
    const buffer = Buffer.alloc(PREVIEW_BYTES);
    const result = await handle.read(buffer, 0, PREVIEW_BYTES, 0);
    return buffer.subarray(0, result.bytesRead).toString("utf8");
  } finally {
    await handle.close();
  }
}

function reportTitle(filePath: string, preview: string): string {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".md") {
    const heading = preview.split(/\r?\n/).find((line) => /^#{1,3}\s+\S/.test(line));
    if (heading) return redactPreviewText(heading.replace(/^#{1,3}\s+/, "").trim()).slice(0, 160);
  }
  if (ext === ".json") {
    try {
      const parsed = JSON.parse(preview);
      if (parsed && typeof parsed === "object") {
        const title = (parsed as Record<string, unknown>).title || (parsed as Record<string, unknown>).name || (parsed as Record<string, unknown>).summary;
        if (typeof title === "string" && title.trim()) return redactPreviewText(title.trim()).slice(0, 160);
      }
    } catch {
      // Partial JSON previews often do not parse; fall back to filename.
    }
  }
  return redactPreviewText(path.basename(filePath).replace(/\.[^.]+$/, "").replaceAll("_", " ")).slice(0, 160);
}

function reportKind(filePath: string): string {
  const base = path.basename(filePath).toLowerCase();
  const ext = path.extname(base).replace(".", "");
  if (base.includes("report")) return "report";
  if (base.includes("summary")) return "summary";
  if (base.includes("manifest") || base.includes("catalog")) return "manifest";
  if (base.includes("queue")) return "queue";
  if (base.includes("labels") || base.includes("label")) return "labels";
  return ext || "text";
}

function summarizePreview(preview: string): string {
  const lines = preview
    .replace(/\0/g, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 8);
  return redactPreviewText(lines.join("\n")).slice(0, 1200);
}

function redactPreviewText(text: string): string {
  const secretKey = "(?:token|password|secret|api[\\s_-]*key|authorization|auth|database[\\s_-]*url|db[\\s_-]*url)";
  return text
    .replace(/\b\d{8,12}:[A-Za-z0-9_-]{24,}\b/g, "[REDACTED_TOKEN]")
    .replace(/\b(?:postgres(?:ql)?|mysql2?|mariadb|mongodb(?:\+srv)?|redis|amqps?|mssql|sqlserver|oracle|couchdb):\/\/[^\s"'<>]+/gi, "[REDACTED_DATABASE_URL]")
    .replace(/\bsqlite:\/\/\/?[^\s"'<>]+/gi, "[REDACTED_DATABASE_URL]")
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [REDACTED]")
    .replace(/\b[A-HJ-NPR-Z0-9]{17}\b/g, "[REDACTED_VIN]")
    .replace(new RegExp(`("(?:[^"]*${secretKey}[^"]*)"\\s*:\\s*)("[^"]*"|[^,}\\s]+)`, "gi"), "$1\"[REDACTED]\"")
    .replace(new RegExp(`(\\b${secretKey}\\b\\s*[:=]\\s*)("[^"]*"|'[^']*'|[^\\s,}]+)`, "gi"), "$1[REDACTED]");
}

function sourceGroupPath(rootPath: string, filePath: string, routeId: string): string {
  const relParts = path.relative(rootPath, filePath).split(path.sep);
  let current = rootPath;
  for (const part of relParts.slice(0, -1)) {
    current = path.join(current, part);
    if (extractRouteIds(part).includes(routeId)) return current;
  }
  return path.dirname(filePath);
}

function latestIso(a: string | null, b: string | null): string | null {
  if (!a) return b;
  if (!b) return a;
  return a > b ? a : b;
}

function applyDiscovery(ride: RideRow, reports: CatalogReport[], sources: CatalogSource[], discoveredOnly: boolean): void {
  const roots = new Set([...reports.map((report) => report.root), ...sources.map((source) => source.kind)]);
  const latest = [...reports.map((report) => report.mtime), ...(sources.map((source) => source.mtime).filter(Boolean) as string[])]
    .sort()
    .at(-1) || null;
  ride.discovered_only = discoveredOnly;
  ride.read_only = discoveredOnly;
  ride.discovered_report_count = reports.length;
  ride.discovered_source_count = sources.length;
  ride.discovered_roots = [...roots].sort();
  ride.latest_discovered_mtime = latest;
  ride.discovered_sources = sources.slice(0, 10).map<RideSourceSummary>((source) => ({
    kind: source.kind,
    path: source.path,
    relative_path: source.relative_path,
    mtime: source.mtime,
    file_count: source.file_count,
    segment_count: source.segment_count,
    size_bytes: source.size_bytes
  }));
  if (discoveredOnly && !ride.segment_count) {
    ride.segment_count = sources.reduce((sum, source) => Math.max(sum, source.segment_count), 0);
  }
  const time = driveTimeForRoute({ reports, sources }, ride.route_id);
  if (time) {
    if (!ride.started_at && time.start) ride.started_at = time.start;
    if (!ride.ended_at && time.end) ride.ended_at = time.end;
    if (!ride.duration_sec && time.duration_sec) ride.duration_sec = time.duration_sec;
    if (!ride.mileage && time.mileage) ride.mileage = time.mileage;
  }
}

export function driveTimeForRoute(catalog: Pick<DataCatalog, "reports" | "sources">, routeId: string): RideTimeRange | null {
  const id = routeId.toLowerCase();
  const ranges = catalog.reports
    .map((report) => report.route_time_ranges?.[id])
    .filter((range): range is RideTimeRange => Boolean(range));
  if (ranges.length) {
    return ranges.reduce<RideTimeRange>((acc, range) => ({
      start: acc.start || range.start,
      end: acc.end || range.end,
      duration_sec: acc.duration_sec ?? range.duration_sec,
      mileage: acc.mileage ?? range.mileage
    }), {});
  }
  const sources = catalog.sources.filter((source) => source.route_id === id && source.mtime).map((source) => source.mtime as string).sort();
  return sources[0] ? { start: sources[0] } : null;
}
