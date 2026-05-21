import { describe, expect, test } from "vitest";
import { labelerBookmarksFromTimeline, patchRides, selectReviewVideo, videoSyncForSelectedVideo } from "../src/server/db";

describe("ride patching", () => {
  test("preserves omitted route fields on partial edits", async () => {
    const { updateArgs } = await runPatch({ notes: "new note" });
    expect(updateArgs[1]).toBe("existing label");
    expect(updateArgs[2]).toBe("normal drive");
    expect(JSON.parse(String(updateArgs[3]))).toMatchObject({
      notes: "new note",
      ride_type: "normal drive"
    });
  });

  test("canonicalizes ride type values written from the rides table", async () => {
    const { updateArgs } = await runPatch({ ride_type: "test", drive_type: "test" });
    expect(updateArgs[2]).toBe("test drive");
    expect(JSON.parse(String(updateArgs[3]))).toMatchObject({
      ride_type: "test drive"
    });
  });

  test("review checkbox keeps test drives as test-drive review jobs", async () => {
    const { insertArgs } = await runPatch({ ride_type: "test", drive_type: "test", label_validation: true });
    expect(insertArgs?.[2]).toBe("test drive");
    expect(insertArgs?.[3]).toBe("existing label");
  });
});

describe("job video sync", () => {
  test("uses a one-to-one sync for selected full-drive MP4s", () => {
    const sync = videoSyncForSelectedVideo(
      { id: "5487", kind: "full_drive_video", mime_type: "video/mp4", artifact_path: "sha256/aa/file" },
      {
        video_sync: [
          { artifact_id: "raw0", segment_index: 0, route_start_sec: 0, video_start_sec: 0, duration_sec: 60 },
          { artifact_id: "raw1", segment_index: 1, route_start_sec: 60, video_start_sec: 0, duration_sec: 60 }
        ]
      },
      420
    );

    expect(sync).toEqual([{
      artifact_id: "5487",
      segment_index: 0,
      route_start_sec: 0,
      video_start_sec: 0,
      duration_sec: 420,
      source_path: "sha256/aa/file",
      confidence: 1
    }]);
  });

  test("treats full-drive route-artifact roles as playable review video", () => {
    const video = selectReviewVideo([
      { id: "10", kind: "qcamera", role: "qcamera", mime_type: "video/mp2t", size_bytes: "1000" },
      { id: "11", kind: "qcamera", role: "full_drive_video", mime_type: "video/mp4", size_bytes: "2000" }
    ] as never);

    expect(video?.id).toBe("11");
    expect(videoSyncForSelectedVideo(video, { video_sync: [] }, 123)).toEqual([{
      artifact_id: "11",
      segment_index: 0,
      route_start_sec: 0,
      video_start_sec: 0,
      duration_sec: 123,
      source_path: undefined,
      confidence: 1
    }]);
  });
});

describe("manual labeler bookmarks", () => {
  test("keeps usable DB bookmark rows unchanged", () => {
    const bookmarks = labelerBookmarksFromTimeline([
      { id: "b1", t_sec: 10, text: "lazy accel", tags: ["accel"] }
    ] as never, [
      { id: "e1", t: 20, type: "userBookmark", severity: "bookmark", summary: "userBookmark" }
    ] as never, 60);

    expect(bookmarks).toEqual([{
      id: "b1",
      t: 10,
      end_t: undefined,
      type: "bookmark",
      label: "lazy accel",
      reason: "lazy accel",
      tags: ["accel"]
    }]);
  });

  test("uses timestamped bookmark events when imported bookmark rows are all zero", () => {
    const bookmarks = labelerBookmarksFromTimeline([
      { id: "b1", t_sec: 0, text: "bookmarkButton", tags: [] },
      { id: "b2", t_sec: 0, text: "userBookmark", tags: [] }
    ] as never, [
      { id: "e1", t: 99.977, type: "bookmarkButton", severity: "bookmark", summary: "bookmarkButton" },
      { id: "e2", t: 136.631, type: "userBookmark", severity: "bookmark", summary: "userBookmark" }
    ] as never, 180);

    expect(bookmarks.map((bookmark) => bookmark.t)).toEqual([99.977, 136.631]);
    expect(bookmarks.map((bookmark) => bookmark.id)).toEqual(["event:e1", "event:e2"]);
  });
});

async function runPatch(patch: Record<string, unknown>): Promise<{ updateArgs: unknown[]; insertArgs: unknown[] | null }> {
  let updateArgs: unknown[] | null = null;
  let insertArgs: unknown[] | null = null;
  const client = {
    async query(sql: string, args?: unknown[]) {
      if (sql === "BEGIN" || sql === "COMMIT" || sql === "ROLLBACK") return { rows: [], rowCount: 0 };
      if (sql.includes("INSERT INTO review_inboxes")) return { rows: [{ id: "7" }], rowCount: 1 };
      if (sql.includes("SELECT metadata_jsonb, route_label, drive_type, route_id FROM routes")) {
        return {
          rows: [{
            metadata_jsonb: { notes: "old note", ride_type: "normal drive" },
            route_label: "existing label",
            drive_type: "normal drive",
            route_id: "0000016f--427a57d416"
          }],
          rowCount: 1
        };
      }
      if (sql.includes("UPDATE routes")) {
        updateArgs = args || [];
        return { rows: [], rowCount: 1 };
      }
      if (sql.includes("INSERT INTO review_jobs")) {
        insertArgs = args || [];
        return { rows: [], rowCount: 1 };
      }
      return { rows: [], rowCount: 0 };
    },
    release() {}
  };

  await patchRides({ connect: async () => client } as never, [{ id: "route-uuid", ...patch }] as never);
  if (!updateArgs) throw new Error("UPDATE routes was not called");
  return { updateArgs, insertArgs };
}
