import { describe, expect, test } from "vitest";
import { defaultPaths, parseSimpleToml, progressFromSizes, safeJoin, safeSessionName } from "../src/server/lib";

describe("server utilities", () => {
  test("parses private config without requiring a toml dependency", () => {
    expect(parseSimpleToml('database_url = "postgres://local"\napi_token = "secret"\n')).toEqual({
      database_url: "postgres://local",
      api_token: "secret"
    });
  });

  test("confines artifact paths to the configured root", () => {
    expect(safeJoin("/tmp/brickpilot-artifacts", "sha256/aa/file")).toBe("/tmp/brickpilot-artifacts/sha256/aa/file");
    expect(() => safeJoin("/tmp/brickpilot-artifacts", "../secret")).toThrow(/escapes/);
  });

  test("accepts standalone UI repo data-root env names", () => {
    const prevData = process.env.BRICKPILOT_DATA_ROOT;
    const prevDrive = process.env.BRICKPILOT_DRIVE_DB_ROOT;
    try {
      process.env.BRICKPILOT_DATA_ROOT = "/tmp/brickpilot-data";
      process.env.BRICKPILOT_DRIVE_DB_ROOT = "/tmp/legacy-data";
      expect(defaultPaths().dataRoot).toBe("/tmp/brickpilot-data");
      delete process.env.BRICKPILOT_DATA_ROOT;
      expect(defaultPaths().dataRoot).toBe("/tmp/legacy-data");
    } finally {
      if (prevData === undefined) delete process.env.BRICKPILOT_DATA_ROOT;
      else process.env.BRICKPILOT_DATA_ROOT = prevData;
      if (prevDrive === undefined) delete process.env.BRICKPILOT_DRIVE_DB_ROOT;
      else process.env.BRICKPILOT_DRIVE_DB_ROOT = prevDrive;
    }
  });

  test("rejects unsafe voice session ids", () => {
    expect(safeSessionName("20260515T120000Z-drive")).toBe("20260515T120000Z-drive");
    expect(() => safeSessionName("../drive")).toThrow(/invalid/);
  });

  test("computes repeatable ingest progress", () => {
    expect(progressFromSizes([
      { expectedBytes: 100, copiedBytes: 30 },
      { expectedBytes: 50, copiedBytes: 50 }
    ])).toEqual({
      expectedBytes: 150,
      copiedBytes: 80,
      expectedFiles: 2,
      copiedFiles: 1,
      percent: 53.3,
      complete: false
    });
  });
});
