import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test } from "vitest";
import type { BrickpilotPaths } from "../src/server/lib";
import {
  cancelWebIngest,
  canonicalIncludeVideo,
  canonicalIngestConnection,
  destinationForRemoteFile,
  getWebIngestProgress,
  ingestFileKind,
  safeRouteDirName,
  settingsManifestDestination,
  settingsSnapshotRemoteCommand,
  shouldCopyForRideType
} from "../src/server/ingest";

const paths: BrickpilotPaths = {
  repoRoot: "/tmp/brickpilot",
  toolsRoot: "/tmp/brickpilot-research",
  pythonPath: "python3",
  dataRoot: "/tmp/BrickpilotDriveDB",
  artifactRoot: "/tmp/BrickpilotDriveDB/artifacts",
  labelerOutputRoot: "/tmp/BrickpilotDriveDB/labeler_outputs",
  voiceSessionRoot: "/tmp/BrickpilotDriveDB/voice_bookmarks/sessions",
  voiceQuarantineRoot: "/tmp/BrickpilotDriveDB/quarantine/voice_sessions_deleted",
  dbConfigPath: "/tmp/brickpilot/drive_db.toml"
};

describe("web ingest policy", () => {
  test("classifies comma log and camera files distinctly", () => {
    expect(ingestFileKind("rlog.zst")).toBe("logs");
    expect(ingestFileKind("qlog.bz2")).toBe("logs");
    expect(ingestFileKind("fcamera.hevc")).toBe("video");
    expect(ingestFileKind("ecamera.hevc")).toBe("video");
    expect(ingestFileKind("qcamera.ts")).toBe("video");
    expect(ingestFileKind("params.json")).toBe("ignore");
  });

  test("video copying is an explicit ingest option for every ride type", () => {
    for (const rideType of ["normal drive", "test drive", "label validation"] as const) {
      expect(shouldCopyForRideType("rlog.zst", rideType)).toBe(true);
      expect(shouldCopyForRideType("qlog.zst", rideType)).toBe(true);
      expect(shouldCopyForRideType("fcamera.hevc", rideType)).toBe(false);
      expect(shouldCopyForRideType("qcamera.ts", rideType)).toBe(false);
      expect(shouldCopyForRideType("fcamera.hevc", rideType, true)).toBe(true);
      expect(shouldCopyForRideType("qcamera.ts", rideType, "yes")).toBe(true);
    }
  });

  test("canonicalizes the include-video toggle", () => {
    expect(canonicalIncludeVideo(true)).toBe(true);
    expect(canonicalIncludeVideo("include video")).toBe(true);
    expect(canonicalIncludeVideo("off")).toBe(false);
    expect(canonicalIncludeVideo(undefined)).toBe(false);
  });

  test("canonicalizes ingest connection selection", () => {
    expect(canonicalIngestConnection("usb")).toBe("usb");
    expect(canonicalIngestConnection("USB tether")).toBe("usb");
    expect(canonicalIngestConnection("wifi")).toBe("wifi");
    expect(canonicalIngestConnection(undefined)).toBe("wifi");
  });

  test("destinations are confined to BrickpilotDriveDB raw imports", () => {
    const dest = destinationForRemoteFile(paths, "0000016f--427a57d416", "0000016f--427a57d416--2", "rlog.zst");
    expect(dest).toBe(path.join(paths.dataRoot, "imports/raw/from_comma/0000016f--427a57d416/segments/0000016f--427a57d416--2/rlog.zst"));
    expect(() => safeRouteDirName("../../bad")).toThrow(/invalid/);
  });

  test("settings snapshots are stored at the route root for DB import", () => {
    expect(settingsManifestDestination(paths, "00000189--6fb7c85de1")).toBe(
      path.join(paths.dataRoot, "imports/raw/from_comma/00000189--6fb7c85de1/settings_manifest.json")
    );
    const command = settingsSnapshotRemoteCommand("00000189--6fb7c85de1", "label validation");
    expect(command).toContain("BRICKPILOT_ROUTE_ID='00000189--6fb7c85de1'");
    expect(command).toContain("BRICKPILOT_INGEST_CONNECTION='wifi'");
    expect(command).toContain("/data/params/d");
    expect(command).toContain("params_raw");
    expect(command).toContain("sensitive_param_redacted");
    expect(command).toContain("CUSTOM_BRAND_VERSION");
    expect(command).toContain("BRICKPILOT_LONGITUDINAL_VERSION");
    expect(command).toContain("openpilot_version");
    expect(command).toContain("git\", \"rev-parse\", \"--abbrev-ref\"");
  });

  test("cancel clears active ingest state and removes partial files", async () => {
    const dataRoot = await fsp.mkdtemp(path.join(os.tmpdir(), "brickpilot-ingest-"));
    const testPaths = { ...paths, dataRoot };
    const tmp = path.join(dataRoot, "imports/raw/from_comma/route-a/segments/route-a--0/rlog.zst.part");
    await fsp.mkdir(path.dirname(tmp), { recursive: true });
    await fsp.writeFile(tmp, "partial");
    await fsp.mkdir(path.join(dataRoot, "logdrive_runs"), { recursive: true });
    await fsp.writeFile(
      path.join(dataRoot, "logdrive_runs", "web-ingest-state.json"),
      JSON.stringify({
        activeRun: {
          run_id: "run-1",
          route_id: "route-a",
          route_key: "route-a",
          ride_type: "normal drive",
          include_video: false,
          status: "copying",
          started_at: "2026-05-16T21:00:00.000Z",
          files: [{ rel: "route-a/rlog.zst", remote: "/remote/rlog.zst", dest: tmp.replace(/\.part$/, ""), tmp, expectedBytes: 8, kind: "logs" }]
        },
        history: []
      }, null, 2)
    );

    const result = await cancelWebIngest(testPaths, "run-1");
    const progress = await getWebIngestProgress(testPaths);

    await expect(fsp.stat(tmp)).rejects.toMatchObject({ code: "ENOENT" });
    expect(result.canceled).toBe(true);
    expect(result.run?.status).toBe("canceled");
    expect(progress.activeRun).toBeNull();
    expect(progress.history.at(-1)?.status).toBe("canceled");
  });
});
