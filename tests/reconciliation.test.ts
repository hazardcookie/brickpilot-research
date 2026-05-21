import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import type { BrickpilotPaths } from "../src/server/lib";
import type { RideRow } from "../src/shared/types";
import {
  assertInsideDataRoot,
  extractRouteIds,
  mergeDiscoveredRides,
  reconciliationSummary,
  reportsForRide,
  scanDataCatalog
} from "../src/server/reconciliation";

let tmpRoot = "";
let paths: BrickpilotPaths;

beforeEach(async () => {
  tmpRoot = await fsp.mkdtemp(path.join(os.tmpdir(), "brickpilot-recon-"));
  paths = {
    repoRoot: path.join(tmpRoot, "repo"),
    toolsRoot: path.join(tmpRoot, "tools"),
    pythonPath: "python3",
    dataRoot: tmpRoot,
    artifactRoot: path.join(tmpRoot, "artifacts"),
    labelerOutputRoot: path.join(tmpRoot, "labeler_outputs"),
    voiceSessionRoot: path.join(tmpRoot, "voice_bookmarks", "sessions"),
    voiceQuarantineRoot: path.join(tmpRoot, "quarantine", "voice_sessions_deleted"),
    dbConfigPath: path.join(tmpRoot, "drive_db.toml")
  };
});

afterEach(async () => {
  if (tmpRoot) await fsp.rm(tmpRoot, { recursive: true, force: true });
});

describe("reconciliation catalog", () => {
  test("extracts base route ids from route and segment names", () => {
    expect(extractRouteIds("logdrive_ui_20260515_validation_0000016f--427a57d416")).toEqual(["0000016f--427a57d416"]);
    expect(extractRouteIds("segments/0000016f--427a57d416--6/qlog.bz2")).toEqual(["0000016f--427a57d416"]);
    expect(extractRouteIds("not-a-route")).toEqual([]);
  });

  test("confines report paths to BrickpilotDriveDB", () => {
    expect(assertInsideDataRoot(paths, path.join(tmpRoot, "logdrive_runs", "report.md"))).toBe(path.join(tmpRoot, "logdrive_runs", "report.md"));
    expect(() => assertInsideDataRoot(paths, path.join(tmpRoot, "..", "outside.md"))).toThrow(/escapes BrickpilotDriveDB/);
  });

  test("catalogs raw sources and linked report metadata without copying data", async () => {
    const route = "0000016f--427a57d416";
    await fsp.mkdir(path.join(tmpRoot, "imports", "raw", "from_comma", route, "segments", `${route}--0`), { recursive: true });
    await fsp.writeFile(path.join(tmpRoot, "imports", "raw", "from_comma", route, "segments", `${route}--0`, "qlog.bz2"), "qlog");
    const reportDir = path.join(tmpRoot, "logdrive_runs", `logdrive_ui_20260515_validation_${route}`);
    await fsp.mkdir(reportDir, { recursive: true });
    await fsp.writeFile(path.join(reportDir, "report.md"), `# Ride report\n\nRoute ${route}\n\nAcceleration notes.`);
    await fsp.mkdir(path.join(tmpRoot, "analysis_exports"), { recursive: true });
    await fsp.writeFile(path.join(tmpRoot, "analysis_exports", "README.md"), "# Export without route\n");

    const catalog = await scanDataCatalog(paths);
    const reports = reportsForRide(catalog, [route]);

    expect(catalog.sources.some((source) => source.route_id === route && source.kind === "raw_imports" && source.segment_count === 1)).toBe(true);
    expect(reports).toHaveLength(1);
    expect(reports[0]).toMatchObject({
      title: "Ride report",
      kind: "report",
      root: "logdrive_runs",
      route_ids: [route]
    });
    expect(reports[0].summary).toContain("Acceleration notes");
    expect(catalog.reports.some((report) => report.route_ids.length === 0 && report.title === "Export without route")).toBe(true);
  });

  test("redacts sensitive-looking report preview values", async () => {
    const route = "0000016f--427a57d416";
    const reportDir = path.join(tmpRoot, "logdrive_runs", `logdrive_ui_${route}`);
    await fsp.mkdir(reportDir, { recursive: true });
    await fsp.writeFile(path.join(reportDir, "report.md"), [
      "# auth = \"heading-secret\"",
      `route ${route}`,
      "token = \"1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\"",
      "api key = abcdefghijklmnop",
      "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
      "database_url = \"mysql://brick:secret@localhost/drive\"",
      "VIN KM8JBDD29NU123456"
    ].join("\n"));

    const catalog = await scanDataCatalog(paths);
    const report = reportsForRide(catalog, [route])[0];

    expect(report.title).toBe("auth = [REDACTED]");
    expect(report.summary).toContain("token = [REDACTED]");
    expect(report.summary).toContain("api key = [REDACTED]");
    expect(report.summary).toContain("Authorization: [REDACTED]");
    expect(report.summary).toContain("database_url = [REDACTED]");
    expect(report.summary).toContain("[REDACTED_VIN]");
    expect(report.title).not.toContain("heading-secret");
    expect(report.summary).not.toContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ");
    expect(report.summary).not.toContain("abcdefghijklmnop");
    expect(report.summary).not.toContain("mysql://brick");
    expect(report.summary).not.toContain("KM8JBDD29NU123456");
  });

  test("shapes rides with DB rows, discovered-only rows, and reconciliation summary", async () => {
    const dbRoute = "0000016d--578c0271ea";
    const discoveredRoute = "0000016f--427a57d416";
    await fsp.mkdir(path.join(tmpRoot, "logdrive_runs", `logdrive_ui_20260514_validation_${dbRoute}`), { recursive: true });
    await fsp.writeFile(path.join(tmpRoot, "logdrive_runs", `logdrive_ui_20260514_validation_${dbRoute}`, "summary.csv"), `route_id,value\n${dbRoute},1\n`);
    await fsp.mkdir(path.join(tmpRoot, "imports", "raw", "from_comma", discoveredRoute, "segments", `${discoveredRoute}--0`), { recursive: true });
    await fsp.writeFile(path.join(tmpRoot, "imports", "raw", "from_comma", discoveredRoute, "segments", `${discoveredRoute}--0`, "rlog.bz2"), "rlog");

    const catalog = await scanDataCatalog(paths);
    const rows = mergeDiscoveredRides([rideFixture(dbRoute)], catalog);
    const dbRow = rows.find((row) => row.route_id === dbRoute)!;
    const discoveredOnly = rows.find((row) => row.route_id === discoveredRoute)!;
    const summary = reconciliationSummary(catalog, [dbRoute]);

    expect(dbRow.discovered_report_count).toBe(1);
    expect(dbRow.discovered_roots).toContain("logdrive_runs");
    expect(discoveredOnly.discovered_only).toBe(true);
    expect(discoveredOnly.read_only).toBe(true);
    expect(discoveredOnly.segment_count).toBe(1);
    expect(summary).toMatchObject({
      counts: {
        db_routes: 1,
        discovered_routes: 2,
        reports: 1,
        linked_reports: 1,
        unlinked_reports: 0,
        sources: 2,
        linked_sources: 1,
        unlinked_sources: 1,
        routes_with_reports: 1,
        routes_with_sources: 2
      }
    });
  });

  test("uses external manifest drive times to enrich sparse DB rides", async () => {
    const route = "0000016f--427a57d416";
    const reportDir = path.join(tmpRoot, "labeler_outputs", "manual_drive_labeler", "jobs", route);
    await fsp.mkdir(reportDir, { recursive: true });
    await fsp.writeFile(path.join(reportDir, "drive_jobs.json"), JSON.stringify({
      jobs: [{
        route_id: route,
        route_start_wall_time: "2026-05-14T21:12:07.739136-04:00",
        route_end_wall_time: "2026-05-14T21:18:41.239136-04:00",
        route_miles: 0.85,
        duration_sec: 393.5
      }]
    }));

    const catalog = await scanDataCatalog(paths);
    const reports = reportsForRide(catalog, [route]);
    const rows = mergeDiscoveredRides([rideFixture(route)], catalog);
    const dbRow = rows.find((row) => row.route_id === route)!;

    expect(reports[0].route_time_ranges?.[route]).toMatchObject({
      start: "2026-05-14T21:12:07.739136-04:00",
      end: "2026-05-14T21:18:41.239136-04:00",
      duration_sec: 393.5,
      mileage: 0.85
    });
    expect(dbRow.started_at).toBe("2026-05-14T21:12:07.739136-04:00");
    expect(dbRow.ended_at).toBe("2026-05-14T21:18:41.239136-04:00");
    expect(dbRow.duration_sec).toBe(393.5);
    expect(dbRow.mileage).toBe(0.85);
  });
});

function rideFixture(routeId: string): RideRow {
  return {
    id: "11111111-1111-1111-1111-111111111111",
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
    started_at: null,
    ended_at: null,
    duration_sec: null,
    mileage: null,
    segment_count: 0,
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
    metadata_summary: {}
  };
}
