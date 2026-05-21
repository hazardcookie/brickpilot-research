import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type {
  IngestCandidate,
  IngestConnection,
  IngestProgress,
  IngestRideType,
  IngestRun,
  MediaStatus,
  MlOverview,
  MlVoiceLabelerRun,
  ReviewJob,
  RideRow,
  VoiceSession
} from "../../shared/types";
import "./styles.css";

type Tab = "voice" | "labeler" | "ml" | "ingest" | "rl" | "rides";
type RideReport = {
  title: string;
  kind: string;
  path: string;
  relative_path?: string;
  size?: number;
  size_bytes?: number;
  mtime?: string | null;
  modified_at?: string | null;
  route_id?: string | null;
  summary?: string;
};
type ReconciliationSummary = {
  counts?: {
    db_routes?: number;
    discovered_routes?: number;
    linked_reports?: number;
    unlinked_reports?: number;
    sources?: number;
  };
  truncated?: boolean;
  roots?: Array<{ root: string; exists: boolean; file_count?: number }>;
};

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...options,
    headers: {
      ...(options?.body instanceof Blob ? {} : { "content-type": "application/json" }),
      ...(options?.headers || {})
    }
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data as T;
}

function fmtDate(value?: string | null): string {
  if (!value) return "";
  return new Date(value).toLocaleString();
}

function fmtShortDate(value?: string | null): string {
  if (!value) return "drive time unknown";
  return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function fmtTimeRange(start?: string | null, end?: string | null): string {
  if (!start) return "drive time unknown";
  const s = new Date(start);
  const e = end ? new Date(end) : null;
  const time = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  return e ? `${time.format(s)}-${time.format(e)}` : time.format(s);
}

function fmtDuration(value?: number | null): string {
  if (!value) return "";
  const m = Math.floor(value / 60);
  const s = Math.round(value % 60);
  return m ? `${m}m ${s}s` : `${s}s`;
}

function fmtBytes(value?: number | null): string {
  if (!value) return "";
  const mb = value / 1_000_000;
  return mb > 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

function fmtCount(value?: number | null): string {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function fmtScore(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "";
  return `${(value * 100).toFixed(1)}%`;
}

function fmtClockSeconds(value?: number | null): string {
  const total = Math.max(0, Math.round(Number(value || 0)));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function fmtRouteWindow(start?: number | null, end?: number | null): string {
  return `${fmtClockSeconds(start)}-${fmtClockSeconds(end)}`;
}

function labelText(value: string): string {
  return String(value || "").replaceAll("_", " ");
}

function clipText(value: string, max = 110): string {
  return value.length > max ? `${value.slice(0, max - 3)}...` : value;
}

const FAMILY_COLORS: Record<string, string> = {
  phev: "#3fb950",
  longitudinal: "#d29922",
  other: "#8b949e",
  drive_control: "#58a6ff",
  lateral: "#f778ba",
  accessory: "#a371f7",
  quality: "#56d4dd"
};

function familyColor(family?: string): string {
  return FAMILY_COLORS[String(family || "")] || "#8b949e";
}

function MiniBarChart({
  rows,
  maxValue,
  valueLabel
}: {
  rows: Array<{ label: string; value: number; detail?: string; family?: string }>;
  maxValue?: number;
  valueLabel?: (value: number) => string;
}) {
  const inferredMax = rows.reduce((best, row) => Math.max(best, row.value), 0);
  const max = Math.max(1, maxValue ?? inferredMax);
  return (
    <div className="miniBarList">
      {rows.map((row) => {
        const pct = Math.max(2, Math.min(100, (row.value / max) * 100));
        return (
          <div className="miniBarRow" key={`${row.label}-${row.detail || ""}`}>
            <div>
              <strong>{labelText(row.label)}</strong>
              <span>{row.detail || valueLabel?.(row.value) || fmtCount(row.value)}</span>
            </div>
            <div className="miniTrack">
              <span style={{ width: `${pct}%`, background: familyColor(row.family) }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function FamilyStack({ rows }: { rows: MlVoiceLabelerRun["label_family_counts"] }) {
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  if (!total) return <p className="muted">No family counts found.</p>;
  return (
    <>
      <div className="familyStack" aria-label="Voice label family mix">
        {rows.map((row) => (
          <span
            key={row.family}
            style={{ width: `${Math.max(2, (row.count / total) * 100)}%`, background: familyColor(row.family) }}
            title={`${labelText(row.family)} ${row.count}`}
          />
        ))}
      </div>
      <div className="familyLegend">
        {rows.map((row) => (
          <span key={row.family}><i style={{ background: familyColor(row.family) }} />{labelText(row.family)} · {fmtCount(row.count)}</span>
        ))}
      </div>
    </>
  );
}

function PredictionTimeline({ run }: { run: MlVoiceLabelerRun }) {
  const duration = Math.max(1, run.test_duration_sec || Math.max(...run.prediction_timeline.map((row) => row.end_sec), 1));
  const familyOrder = ["phev", "longitudinal", "drive_control", "lateral", "accessory", "quality", "other"];
  const families = familyOrder.filter((family) => run.prediction_timeline.some((row) => row.family === family));
  const lanes = families.length ? families : ["other"];
  const laneHeight = 18;
  const top = 22;
  const width = 1000;
  const height = top + lanes.length * laneHeight + 28;
  const ticks = [0, duration * 0.25, duration * 0.5, duration * 0.75, duration];
  return (
    <div className="timelineWrap">
      <svg className="predictionTimeline" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Held-out test route prediction timeline">
        <line x1="0" x2={width} y1={height - 20} y2={height - 20} />
        {ticks.map((tick) => {
          const x = (tick / duration) * width;
          return (
            <g key={tick}>
              <line className="tick" x1={x} x2={x} y1="10" y2={height - 16} />
              <text x={Math.min(width - 42, x + 4)} y={height - 4}>{fmtClockSeconds(tick)}</text>
            </g>
          );
        })}
        {lanes.map((family, index) => {
          const y = top + index * laneHeight;
          return (
            <g key={family}>
              <text className="laneLabel" x="4" y={y + 12}>{labelText(family)}</text>
              <line className="laneLine" x1="118" x2={width} y1={y + 8} y2={y + 8} />
            </g>
          );
        })}
        {run.prediction_timeline.map((row) => {
          const lane = Math.max(0, lanes.indexOf(row.family));
          const x = Math.max(118, (row.start_sec / duration) * width);
          const w = Math.max(3, ((row.end_sec - row.start_sec) / duration) * width);
          const y = top + lane * laneHeight + 2;
          return (
            <rect
              key={`${row.rank}-${row.target}-${row.start_sec}`}
              x={x}
              y={y}
              width={Math.min(width - x, w)}
              height="12"
              rx="2"
              fill={familyColor(row.family)}
              opacity={0.45 + Math.min(0.5, row.peak_score * 0.45)}
            >
              <title>{labelText(row.target)} {fmtRouteWindow(row.start_sec, row.end_sec)} · {fmtScore(row.peak_score)}</title>
            </rect>
          );
        })}
      </svg>
    </div>
  );
}

function shortCommit(value?: unknown): string {
  const text = String(value || "");
  return text.length > 12 ? text.slice(0, 12) : text;
}

function brickpilotVersionLabel(value?: string | null): string {
  const text = String(value || "").trim();
  if (!text) return "version unknown";
  return /brickpilot/i.test(text) ? text : `Brickpilot ${text}`;
}

function settingsLines(ride: RideRow): string[] {
  const detail = ride.settings_detail;
  if (!detail) return [];
  const rows = Object.entries(detail.settings || {}).sort(([a], [b]) => a.localeCompare(b));
  return rows.map(([key, value]) => `${key}=${value}`);
}

function rideDisplayName(ride: RideRow): string {
  const base = ride.route_label || ride.canonical_name || ride.route_id;
  if (base && base !== ride.route_id) return base;
  const parts = [ride.route_id];
  if (ride.model_bundle) parts.push(ride.model_bundle);
  if (ride.brickpilot_version) parts.push(brickpilotVersionLabel(ride.brickpilot_version));
  return parts.join(" · ");
}

function normalizeRideType(value?: string | null): string {
  const v = String(value || "").trim().toLowerCase().replaceAll("_", " ").replaceAll("-", " ");
  if (v.includes("label") || v.includes("validation")) return "label validation";
  if (v.includes("test")) return "test drive";
  return "normal drive";
}

function mediaTone(status: MediaStatus): string {
  return status.replaceAll(" ", "-");
}

function App() {
  const [tab, setTab] = useState<Tab>("voice");
  return (
    <main>
      <header className="topbar">
        <div className="brand">
          <strong>Brickpilot</strong>
          <span>0.3.25.0</span>
        </div>
        <nav aria-label="Brickpilot tools">
          {[
            ["voice", "Voice Bookmarks"],
            ["labeler", "Manual Labeler"],
            ["ml", "ML"],
            ["ingest", "Ingest"],
            ["rl", "RL"],
            ["rides", "Rides"]
          ].map(([id, label]) => (
            <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id as Tab)}>
              {label}
            </button>
          ))}
        </nav>
      </header>
      {tab === "voice" && <VoiceBookmarks />}
      {tab === "labeler" && <ManualLabeler />}
      {tab === "ml" && <MlProgress />}
      {tab === "ingest" && <IngestPage />}
      {tab === "rl" && <RlQueue />}
      {tab === "rides" && <RidesTable />}
    </main>
  );
}

function VoiceBookmarks() {
  const [session, setSession] = useState<Record<string, unknown> | null>(null);
  const [sessions, setSessions] = useState<VoiceSession[]>([]);
  const [title, setTitle] = useState("");
  const [bookmarkText, setBookmarkText] = useState("");
  const [status, setStatus] = useState("Idle");
  const [recHealth, setRecHealth] = useState("Idle · captured 0 · saved 0 · pending 0");
  const [micLevel, setMicLevel] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunkIndex = useRef(0);
  const started = useRef(0);
  const uploadPromises = useRef<Promise<void>[]>([]);
  const uploadErrors = useRef<string[]>([]);
  const stats = useRef({ captured: 0, saved: 0, failed: 0 });
  const micFrame = useRef<number | null>(null);
  const micContext = useRef<AudioContext | null>(null);
  const recording = useRef(false);

  async function refresh() {
    const out = await api<{ sessions: VoiceSession[] }>("/api/voice/sessions");
    setSessions(out.sessions || []);
  }

  useEffect(() => {
    refresh().catch((err) => setStatus(err.message));
    return () => {
      stopMicMeter();
      stream.current?.getTracks().forEach((track) => track.stop());
      recording.current = false;
    };
  }, []);

  function updateRecHealth(extra = "") {
    const pending = uploadPromises.current.length;
    const failed = stats.current.failed ? ` · retries ${stats.current.failed}` : "";
    setRecHealth(`${recording.current ? "Recording" : "Idle"} · captured ${stats.current.captured} · saved ${stats.current.saved} · pending ${pending}${failed}${extra ? ` · ${extra}` : ""}`);
  }

  function stopMicMeter() {
    if (micFrame.current) window.cancelAnimationFrame(micFrame.current);
    micFrame.current = null;
    const ctx = micContext.current;
    micContext.current = null;
    if (ctx && ctx.state !== "closed") void ctx.close().catch(() => undefined);
    setMicLevel(0);
  }

  function startMicMeter(input: MediaStream) {
    stopMicMeter();
    const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextCtor) return;
    const ctx = new AudioContextCtor();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.72;
    ctx.createMediaStreamSource(input).connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    micContext.current = ctx;
    const tick = () => {
      analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) {
        const centered = (sample - 128) / 128;
        sum += centered * centered;
      }
      const rms = Math.sqrt(sum / samples.length);
      const level = Math.max(0, Math.min(1, rms * 5));
      setMicLevel((prev) => Math.abs(prev - level) > 0.015 ? level : prev);
      micFrame.current = window.requestAnimationFrame(tick);
    };
    tick();
  }

  function queueChunk(sessionId: string, blob: Blob, type: string) {
    const index = chunkIndex.current++;
    stats.current.captured += 1;
    const promise = fetch(`/api/voice/chunk?session_id=${encodeURIComponent(sessionId)}&index=${index}`, {
      method: "POST",
      headers: {
        "content-type": type || "audio/webm",
        "x-client-wall": new Date().toISOString(),
        "x-client-session-sec": String((performance.now() - started.current) / 1000)
      },
      body: blob
    }).then(async (res) => {
      if (!res.ok) throw new Error(await res.text());
      stats.current.saved += 1;
      updateRecHealth();
    }).catch((err) => {
      stats.current.failed += 1;
      const message = err instanceof Error ? err.message : String(err);
      uploadErrors.current.push(message);
      setStatus(`Audio chunk upload failed: ${message}`);
      updateRecHealth();
      throw err;
    }).finally(() => {
      uploadPromises.current = uploadPromises.current.filter((item) => item !== promise);
      updateRecHealth();
    });
    uploadPromises.current.push(promise);
    updateRecHealth();
  }

  async function waitForUploads() {
    while (uploadPromises.current.length) {
      await Promise.allSettled(uploadPromises.current);
    }
    if (uploadErrors.current.length) {
      throw new Error(`${uploadErrors.current.length} audio chunk upload(s) failed; session left open so you can retry or delete it.`);
    }
  }

  async function startRide() {
    if (session) return;
    setStatus("Requesting microphone");
    let created: Record<string, unknown> | null = null;
    try {
      created = await api<Record<string, unknown>>("/api/voice/start", {
        method: "POST",
        body: JSON.stringify({ ride_type: "label validation", title })
      });
      setSession(created);
      started.current = performance.now();
      uploadPromises.current = [];
      uploadErrors.current = [];
      stats.current = { captured: 0, saved: 0, failed: 0 };
      recording.current = true;
      updateRecHealth("mic opening");
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: true });
      startMicMeter(stream.current);
      recorder.current = new MediaRecorder(stream.current);
      chunkIndex.current = 0;
      recorder.current.ondataavailable = (event) => {
        if (!event.data.size || !created?.session_id) return;
        queueChunk(String(created.session_id), event.data, event.data.type);
      };
      recorder.current.start(3000);
      setStatus("Recording");
      updateRecHealth("mic open");
    } catch (err) {
      stopMicMeter();
      stream.current?.getTracks().forEach((track) => track.stop());
      stream.current = null;
      recorder.current = null;
      recording.current = false;
      if (created?.session_id) {
        await api<Record<string, unknown>>("/api/voice/stop", {
          method: "POST",
          body: JSON.stringify({ session_id: created.session_id, error: err instanceof Error ? err.message : String(err) })
        }).catch(() => undefined);
      }
      setSession(null);
      updateRecHealth("start failed");
      setStatus(`Start failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function stopRide() {
    if (!session?.session_id) return;
    setStatus("Saving session");
    if (recorder.current && recorder.current.state !== "inactive") {
      await new Promise<void>((resolve) => {
        if (!recorder.current) return resolve();
        recorder.current.onstop = () => resolve();
        recorder.current.requestData();
        recorder.current.stop();
      });
    }
    try {
      await waitForUploads();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
      return;
    }
    stopMicMeter();
    stream.current?.getTracks().forEach((track) => track.stop());
    const stopped = await api<Record<string, unknown>>("/api/voice/stop", {
      method: "POST",
      body: JSON.stringify({ session_id: session.session_id })
    });
    setSession(null);
    recorder.current = null;
    stream.current = null;
    recording.current = false;
    updateRecHealth("saved");
    setStatus(`Saved ${stopped.display_title || stopped.session_id}`);
    await refresh();
  }

  async function newSession() {
    if (session) await stopRide();
    setSession(null);
    setTitle("");
    setBookmarkText("");
    setStatus("New session ready");
  }

  async function addBookmark() {
    if (!session?.session_id || !bookmarkText.trim()) return;
    const t = (performance.now() - started.current) / 1000;
    await api("/api/voice/bookmark", {
      method: "POST",
      body: JSON.stringify({ session_id: session.session_id, text: bookmarkText, t_session_start_sec: t, t_session_end_sec: t })
    });
    setBookmarkText("");
    setStatus("Bookmark saved");
  }

  async function deleteSession(id: string) {
    if (!window.confirm(`Delete voice session ${id}? It will move to recoverable quarantine outside the repo.`)) return;
    await api(`/api/voice/sessions/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    await refresh();
  }

  async function transcribe(id: string) {
    setStatus(`Checking final transcription readiness for ${id}`);
    const readiness = await api<Record<string, unknown>>("/api/voice/transcribe/status", { method: "POST", body: JSON.stringify({ session_id: id }) });
    if (readiness.recording) {
      setStatus("Stop Ride before transcribing so final audio chunks are flushed.");
      return;
    }
    if (["no_backend", "model_required", "model_not_found"].includes(String(readiness.status || ""))) {
      setStatus(String(readiness.message || readiness.status));
      return;
    }
    setStatus(`Running final Transcribe with ${String(readiness.selected_backend || "best backend")}`);
    const out = await api<Record<string, unknown>>("/api/voice/transcribe", { method: "POST", body: JSON.stringify({ session_id: id }) });
    setStatus(`${out.status || "done"}: rows ${out.appended_rows ?? 0}, filtered ${out.filtered_rows ?? 0}, replaced ${out.replaced_rows ?? 0}`);
    await refresh();
  }

  return (
    <section className="panel voice">
      <div className="recorder">
        <div>
          <label>Ride title</label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} disabled={Boolean(session)} placeholder="Label validation drive" />
        </div>
        <div className="controls">
          <button onClick={startRide} disabled={Boolean(session)}>Start Ride</button>
          <button onClick={stopRide} disabled={!session}>Stop Ride</button>
          <button onClick={newSession}>New Session</button>
        </div>
        <div>
          <label>Bookmark</label>
          <div className="inline">
            <input value={bookmarkText} onChange={(e) => setBookmarkText(e.target.value)} disabled={!session} placeholder="Say or type a label" />
            <button onClick={addBookmark} disabled={!session || !bookmarkText.trim()}>Add</button>
          </div>
        </div>
        <div className="micPanel">
          <div
            className="micMeter"
            aria-label="Microphone input level"
            style={{
              "--mic-scale": String(1 + micLevel * 0.38),
              "--mic-opacity": String(0.24 + micLevel * 0.58)
            } as React.CSSProperties}
          >
            <span className="micPulse" />
            <svg className="micIcon" aria-hidden="true" viewBox="0 0 24 24">
              <path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3Z" />
              <path d="M19 11a7 7 0 0 1-14 0" />
              <path d="M12 18v4" />
              <path d="M8 22h8" />
            </svg>
          </div>
          <div>
            <strong>Mic input</strong>
            <span>{recHealth}</span>
          </div>
        </div>
        <output>{status}</output>
      </div>
      <div className="sessions">
        <div className="sectionHead">
          <h2>Drive Sessions</h2>
          <button onClick={refresh}>Refresh</button>
        </div>
        {sessions.length === 0 ? <p className="muted">No drive sessions yet.</p> : sessions.map((s) => (
          <article className="session" key={s.session_id}>
            <div>
              <strong>{s.display_title}</strong>
              <span>{fmtDate(s.started_at_wall)} {fmtDuration(s.duration_sec)} {s.transcript_rows} bookmarks {s.audio_chunks} chunks</span>
            </div>
            <div className="rowActions">
              <button onClick={() => transcribe(s.session_id)} disabled={s.recording}>Transcribe</button>
              <button className="danger" onClick={() => deleteSession(s.session_id)}>Delete</button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function ManualLabeler() {
  const [jobs, setJobs] = useState<ReviewJob[]>([]);
  useEffect(() => {
    api<{ jobs: ReviewJob[] }>("/api/review/jobs").then((out) => setJobs(out.jobs || [])).catch(() => setJobs([]));
  }, []);
  return (
    <section className="labelerWrap">
      <div className="statusStrip">
        <span>{jobs.length} review jobs</span>
      </div>
      <iframe title="Manual Labeler" src="/manual-labeler/" />
    </section>
  );
}

function RlQueue() {
  return (
    <section className="panel">
      <div className="sectionHead">
        <h2>RL</h2>
      </div>
      <table>
        <thead>
          <tr><th>Question</th><th>Route</th><th>Status</th><th>Created</th></tr>
        </thead>
        <tbody>
          <tr><td colSpan={4} className="empty">No DB-backed RL questions queued.</td></tr>
        </tbody>
      </table>
    </section>
  );
}

function MlProgress() {
  const [overview, setOverview] = useState<MlOverview | null>(null);
  const [status, setStatus] = useState("Loading ML overview");

  async function refresh() {
    setStatus("Loading ML overview");
    try {
      const out = await api<MlOverview>("/api/ml/overview");
      setOverview(out);
      setStatus("");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const counts = overview?.counts;
  const voiceRun = overview?.latest_voice_labeler_run;
  return (
    <section className="panel mlPage">
      <div className="sectionHead">
        <div>
          <h2>ML Progress</h2>
          <p className="subtle">Read-only view of DB coverage, recovered analysis, and model-tuning evidence.</p>
        </div>
        <button onClick={refresh}>Refresh</button>
      </div>
      {status ? <output>{status}</output> : null}
      {overview && counts ? (
        <>
          <div className="metricGrid">
            {[
              ["Routes", counts.routes],
              ["Human labels", counts.labels],
              ["Voice marks", counts.bookmarks],
              ["Route samples", counts.route_samples],
              ["CAN frames", counts.can_frames],
              ["Events", counts.events],
              ["Artifacts", counts.artifacts],
              ["Reports", counts.discovered_reports]
            ].map(([label, value]) => (
              <article className="metricCard" key={label}>
                <span>{label}</span>
                <strong>{fmtCount(Number(value))}</strong>
              </article>
            ))}
          </div>
          {voiceRun ? (
            <section className="subPanel voiceRunPanel">
              <div className="voiceRunHeader">
                <div>
                  <h3>0.3.25 Voice Labeler Run</h3>
                  <p className="subtle">
                    {fmtDate(voiceRun.created_at)} · held-out test {voiceRun.test_route} · {voiceRun.validation_routes.length} validation routes
                  </p>
                </div>
                <span className="chip">{voiceRun.human_validation_route ? `Human reference ${voiceRun.human_validation_route}` : "No human reference route"}</span>
              </div>
              <div className="statStrip">
                {[
                  ["Voice atoms", fmtCount(voiceRun.voice_atomic_labels)],
                  ["Canonical labels", fmtCount(voiceRun.canonical_label_count)],
                  ["Targets trained", fmtCount(voiceRun.trained_target_count)],
                  ["Mean holdout AUC", voiceRun.mean_auc == null ? "n/a" : voiceRun.mean_auc.toFixed(3)],
                  ["Test candidates", fmtCount(voiceRun.test_prediction_count)],
                  ["CAN candidates", fmtCount(voiceRun.can_candidate_count)]
                ].map(([label, value]) => (
                  <span className="statPill" key={label}><strong>{value}</strong>{label}</span>
                ))}
              </div>
              <div className="visualGrid">
                <section className="chartPanel wide">
                  <div className="chartHead">
                    <h3>Held-Out Test Route Timeline</h3>
                    <span>{fmtDuration(voiceRun.test_duration_sec)} · {fmtCount(voiceRun.prediction_timeline.length)} candidate intervals</span>
                  </div>
                  <PredictionTimeline run={voiceRun} />
                </section>
                <section className="chartPanel">
                  <div className="chartHead">
                    <h3>Voice Label Families</h3>
                    <span>{fmtCount(voiceRun.voice_atomic_labels)} normalized atoms</span>
                  </div>
                  <FamilyStack rows={voiceRun.label_family_counts} />
                </section>
                <section className="chartPanel">
                  <div className="chartHead">
                    <h3>Top Canonical Labels</h3>
                    <span>highest-volume voice signals</span>
                  </div>
                  <MiniBarChart
                    rows={voiceRun.top_labels.slice(0, 9).map((row) => ({
                      label: row.label,
                      value: row.count,
                      detail: `${fmtCount(row.count)} · ${labelText(row.family)}`,
                      family: row.family
                    }))}
                  />
                </section>
                <section className="chartPanel">
                  <div className="chartHead">
                    <h3>Route Label Density</h3>
                    <span>validation narration intensity</span>
                  </div>
                  <MiniBarChart
                    rows={voiceRun.route_label_counts.map((row) => ({
                      label: row.route_id.replace(/^00000/, ""),
                      value: row.labels_per_min,
                      detail: `${row.role.replaceAll("_", " ")} · ${row.atomic_label_count} atoms · ${row.voice_bookmark_count} marks`,
                      family: row.role === "human_validation" ? "quality" : row.role === "test" ? "other" : "phev"
                    }))}
                    valueLabel={(value) => `${value.toFixed(1)} atoms/min`}
                  />
                </section>
                <section className="chartPanel">
                  <div className="chartHead">
                    <h3>Validation AUC Bands</h3>
                    <span>{voiceRun.mean_auc == null ? "mean n/a" : `mean ${voiceRun.mean_auc.toFixed(3)}`}</span>
                  </div>
                  <MiniBarChart
                    rows={voiceRun.auc_distribution.map((row) => ({ label: row.bucket, value: row.count, family: "drive_control" }))}
                    valueLabel={(value) => `${fmtCount(value)} evals`}
                  />
                </section>
                <section className="chartPanel">
                  <div className="chartHead">
                    <h3>Test Prediction Targets</h3>
                    <span>curated candidates by type</span>
                  </div>
                  <MiniBarChart
                    rows={voiceRun.prediction_target_counts.slice(0, 9).map((row) => ({
                      label: row.target,
                      value: row.count,
                      detail: `${fmtCount(row.count)} intervals · max ${fmtScore(row.max_score)}`,
                      family: row.target.includes("regen") || row.target.includes("engine") || row.target.includes("eco") ? "phev" : row.target.includes("brake") || row.target.includes("stop") ? "longitudinal" : "other"
                    }))}
                  />
                </section>
                <section className="chartPanel">
                  <div className="chartHead">
                    <h3>PHEV CAN Address Leads</h3>
                    <span>correlation strength by address</span>
                  </div>
                  <MiniBarChart
                    rows={voiceRun.can_address_summary.slice(0, 9).map((row) => ({
                      label: `bus ${row.bus} · ${row.address_hex}`,
                      value: row.max_abs_effect,
                      detail: `${row.count} rows · ${labelText(row.top_target)}`,
                      family: "phev"
                    }))}
                    maxValue={1}
                    valueLabel={(value) => value.toFixed(3)}
                  />
                </section>
              </div>
              <div className="voiceRunGrid">
                <div>
                  <h3>Test Route Review Candidates</h3>
                  <table className="compactTable predictionTable">
                    <thead><tr><th>Rank</th><th>Target</th><th>Window</th><th>Score</th><th>Why</th></tr></thead>
                    <tbody>
                      {voiceRun.top_predictions.map((row) => (
                        <tr key={`${row.rank}-${row.target}-${row.start_sec}`}>
                          <td>{row.rank}</td>
                          <td>{labelText(row.target)}</td>
                          <td>{fmtRouteWindow(row.start_sec, row.end_sec)}</td>
                          <td>{fmtScore(row.peak_score)}</td>
                          <td className="reasonCell">{clipText(row.reason)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div>
                  <h3>PHEV CAN Signal Leads</h3>
                  <p className="subtle tableNote">Candidate correlations only; DBC semantics are not decoded yet.</p>
                  <table className="compactTable canLeadTable">
                    <thead><tr><th>Target</th><th>CAN</th><th>Byte</th><th>Effect</th><th>Status</th></tr></thead>
                    <tbody>
                      {voiceRun.top_can_candidates.map((row) => (
                        <tr key={`${row.target}-${row.bus}-${row.address_hex}-${row.byte_index}-${row.stat}`}>
                          <td>{labelText(row.target)}</td>
                          <td>bus {row.bus} · {row.address_hex}</td>
                          <td>{row.byte_index} {row.stat}</td>
                          <td>{row.effect.toFixed(3)}</td>
                          <td>{labelText(row.interpretation_status || "candidate_correlation_not_dbc_semantics")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              <code className="runPath">{voiceRun.report_path || voiceRun.relative_path}</code>
            </section>
          ) : null}
          <div className="mlGrid">
            <section className="subPanel">
              <h3>Coverage</h3>
              <div className="progressList">
                {overview.progress.map((item) => {
                  const pct = item.total > 0 ? Math.round((item.value / item.total) * 100) : 0;
                  return (
                    <div className="progressRow" key={item.label}>
                      <div>
                        <strong>{item.label}</strong>
                        <span>{fmtCount(item.value)} / {fmtCount(item.total)} · {pct}%</span>
                      </div>
                      <div className="bar"><span style={{ width: `${Math.min(100, pct)}%` }} /></div>
                    </div>
                  );
                })}
              </div>
            </section>
            <section className="subPanel">
              <h3>Review Queue</h3>
              <div className="statusChips">
                {overview.review_status.length ? overview.review_status.map((row) => (
                  <span className="chip" key={row.status}>{row.status}: {fmtCount(row.count)}</span>
                )) : <span className="muted">No review jobs found.</span>}
              </div>
              <h3>Recent Routes</h3>
              <div className="compactList">
                {overview.recent_routes.map((route) => (
                  <article key={route.route_id}>
                    <strong>{route.route_id}</strong>
                    <span>{fmtDate(route.started_at)} · {route.model_bundle || "model unknown"} · {route.brickpilot_version || "version unknown"}</span>
                  </article>
                ))}
              </div>
            </section>
          </div>
          <section className="subPanel">
            <h3>Model Coverage</h3>
            <div className="tableScroller">
              <table className="compactTable">
                <thead><tr><th>Model</th><th>Routes</th><th>Labels</th><th>Bookmarks</th><th>Samples</th><th>Events</th></tr></thead>
                <tbody>
                  {overview.model_coverage.map((row) => (
                    <tr key={row.model_bundle}>
                      <td>{row.model_bundle}</td>
                      <td>{fmtCount(row.routes)}</td>
                      <td>{fmtCount(row.labels)}</td>
                      <td>{fmtCount(row.bookmarks)}</td>
                      <td>{fmtCount(row.samples)}</td>
                      <td>{fmtCount(row.events)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="subPanel">
            <h3>Recent ML / CAN / Replay Outputs</h3>
            <div className="reportList">
              {overview.recent_reports.map((report) => (
                <article className="reportItem" key={report.relative_path}>
                  <div>
                    <strong>{report.title}</strong>
                    <span>{report.kind} · {fmtDate(report.mtime)} · {fmtBytes(report.size_bytes)} · {report.route_ids.length ? `${report.route_ids.length} route(s)` : "unlinked"}</span>
                    {report.summary ? <p>{report.summary}</p> : null}
                    <code>{report.relative_path}</code>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </>
      ) : null}
    </section>
  );
}

function IngestPage() {
  const [candidates, setCandidates] = useState<IngestCandidate[]>([]);
  const [selected, setSelected] = useState("");
  const [rideType, setRideType] = useState<IngestRideType>("normal drive");
  const [connection, setConnection] = useState<IngestConnection>("wifi");
  const [includeVideo, setIncludeVideo] = useState(false);
  const [activeRun, setActiveRun] = useState<IngestRun | null>(null);
  const [history, setHistory] = useState<IngestRun[]>([]);
  const [progress, setProgress] = useState<IngestProgress | null>(null);
  const [status, setStatus] = useState("");

  async function refreshCandidates() {
    setStatus(`Checking comma over ${connection}`);
    try {
      const out = await api<{ candidates: IngestCandidate[] }>(`/api/ingest/candidates?connection=${encodeURIComponent(connection)}`);
      setCandidates(out.candidates || []);
      setSelected((prev) => out.candidates?.some((candidate) => candidate.route_id === prev) ? prev : out.candidates?.[0]?.route_id || "");
      setStatus(out.candidates?.length ? "" : "No comma route candidates found.");
    } catch (err) {
      setStatus(`Comma route scan failed over ${connection}: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function refreshProgress() {
    try {
      const out = await api<{ activeRun: IngestRun | null; history: IngestRun[]; progress: IngestProgress }>("/api/ingest/progress");
      setActiveRun(out.activeRun);
      setHistory(out.history || []);
      setProgress(out.progress);
    } catch {
      // Keep the ingest screen usable even if no state file exists yet.
    }
  }

  useEffect(() => {
    refreshCandidates();
    refreshProgress();
    const timer = window.setInterval(refreshProgress, 2000);
    return () => window.clearInterval(timer);
  }, [connection]);

  async function startIngest() {
    if (!selected) return;
    setStatus(`Starting ${connection} ingest for ${selected}`);
    const out = await api<{ run: IngestRun }>("/api/ingest/start", {
      method: "POST",
      body: JSON.stringify({ route_id: selected, ride_type: rideType, connection, include_video: includeVideo })
    });
    setActiveRun(out.run);
    setStatus("Ingest started");
    await refreshProgress();
  }

  async function cancelIngest() {
    if (!activeRun) return;
    setStatus(`${busy ? "Canceling" : "Clearing"} ingest for ${activeRun.route_id}`);
    await api("/api/ingest/cancel", {
      method: "POST",
      body: JSON.stringify({ run_id: activeRun.run_id })
    });
    setStatus(busy ? "Ingest canceled" : "Ingest cleared");
    await refreshProgress();
  }

  const selectedCandidate = candidates.find((candidate) => candidate.route_id === selected);
  const busy = activeRun ? ["discovering", "copying", "importing"].includes(activeRun.status) : false;
  const copiedFiles = progress?.copiedFiles ?? 0;
  const expectedFiles = progress?.expectedFiles ?? 0;
  const percent = progress?.percent ?? 0;

  return (
    <section className="panel ingestPage">
      <div className="sectionHead">
        <div>
          <h2>Ingest</h2>
          <p className="subtle">Direct comma route import. Logs are always copied; video is optional for any ride type.</p>
        </div>
        <div className="inline">
          <button onClick={refreshCandidates}>Refresh Routes</button>
          {activeRun ? <button className="secondary" onClick={cancelIngest}>{busy ? "Cancel Ingest" : "Clear"}</button> : null}
          <button onClick={startIngest} disabled={!selected || busy}>Start Ingest</button>
        </div>
      </div>
      {status ? <output>{status}</output> : null}
      <div className="ingestGrid">
        <section className="subPanel">
          <h3>Ride Type</h3>
          <label>
            <span className="labelText">Connection</span>
            <select value={connection} onChange={(e) => setConnection(e.target.value as IngestConnection)} disabled={busy}>
              <option value="wifi">Wi-Fi</option>
              <option value="usb">USB</option>
            </select>
          </label>
          <label>
            <span className="labelText">Ride type</span>
            <select value={rideType} onChange={(e) => setRideType(e.target.value as IngestRideType)}>
              <option value="normal drive">normal</option>
              <option value="test drive">test</option>
              <option value="label validation">label validation</option>
            </select>
          </label>
          <label className="checkboxLine">
            <input type="checkbox" checked={includeVideo} onChange={(e) => setIncludeVideo(e.target.checked)} disabled={busy} />
            <span>Ingest route video</span>
          </label>
          <p className="subtle">
            {includeVideo ? "Copies qlog/rlog plus qcamera/fcamera/ecamera/dcamera video. Use this when visual review or RL video work matters." : "Copies qlog/rlog only for fast post-drive import. Label-validation routes still enter the review inbox without video."}
          </p>
          {selectedCandidate ? (
            <article className="candidateSummary">
              <strong>{selectedCandidate.route_id}</strong>
              <span>{selectedCandidate.segment_count} segment(s) · {selectedCandidate.log_file_count} log · {selectedCandidate.video_file_count} video</span>
              <span>{fmtBytes(includeVideo ? selectedCandidate.log_bytes + selectedCandidate.video_bytes : selectedCandidate.log_bytes)} selected for copy</span>
            </article>
          ) : null}
        </section>
        <section className="subPanel">
          <h3>Progress</h3>
          {activeRun ? (
            <div className="ingestProgress">
              <div className="progressRow">
                <div>
                  <strong>{activeRun.route_id}</strong>
                  <span>{activeRun.status} · {activeRun.connection || "wifi"} · {activeRun.include_video ? "logs+video" : "logs only"} · {copiedFiles}/{expectedFiles} file(s) · {fmtBytes(progress?.copiedBytes)}/{fmtBytes(progress?.expectedBytes)}</span>
                </div>
                <div className="bar"><span style={{ width: `${Math.min(100, percent)}%` }} /></div>
              </div>
              {activeRun.message ? <p className="warn">{activeRun.message}</p> : null}
              <div className="fileProgress">
                {activeRun.files.slice(0, 12).map((file) => (
                  <span key={file.rel}>{file.kind}: {file.rel.split("/").slice(-2).join("/")} · {fmtBytes(file.expectedBytes)}</span>
                ))}
                {activeRun.files.length > 12 ? <span>{activeRun.files.length - 12} more file(s)</span> : null}
              </div>
            </div>
          ) : <p className="muted">No active ingest.</p>}
        </section>
      </div>
      <section className="subPanel">
        <h3>Comma Route Candidates</h3>
        <div className="candidateList">
          {candidates.map((candidate) => (
            <label className={`candidateItem ${selected === candidate.route_id ? "selected" : ""}`} key={candidate.route_id}>
              <input type="radio" checked={selected === candidate.route_id} onChange={() => setSelected(candidate.route_id)} />
              <div>
                <strong>{candidate.route_id}</strong>
                <span>{fmtDate(candidate.updated_at)} · segments {candidate.segments[0]}-{candidate.segments[candidate.segments.length - 1]} · {fmtBytes(candidate.total_bytes)}</span>
                <span>{candidate.reason}</span>
              </div>
            </label>
          ))}
          {!candidates.length ? <p className="empty">No route candidates loaded.</p> : null}
        </div>
      </section>
      <section className="subPanel">
        <h3>Recent Web Ingest Runs</h3>
        <div className="compactList">
          {history.length ? history.map((run) => (
            <article key={run.run_id}>
              <strong>{run.route_id}</strong>
              <span>{run.status} · {run.connection || "wifi"} · {run.ride_type} · {run.include_video ? "video" : "logs only"} · {fmtDate(run.started_at)}</span>
            </article>
          )) : <p className="muted">No completed web ingest runs yet.</p>}
        </div>
      </section>
    </section>
  );
}

function RidesTable() {
  const [rides, setRides] = useState<RideRow[]>([]);
  const [edits, setEdits] = useState<Record<string, Partial<RideRow>>>({});
  const [status, setStatus] = useState("");
  const [notesRide, setNotesRide] = useState<RideRow | null>(null);
  const [notesDraft, setNotesDraft] = useState("");
  const [reportsRide, setReportsRide] = useState<RideRow | null>(null);
  const [reports, setReports] = useState<RideReport[]>([]);
  const [reportsStatus, setReportsStatus] = useState("");
  const [reconciliation, setReconciliation] = useState<ReconciliationSummary | null>(null);
  const dirty = useMemo(() => Object.keys(edits).length, [edits]);

  async function refresh() {
    const out = await api<{ rides: RideRow[] }>("/api/rides");
    setRides(out.rides || []);
    setEdits({});
    api<ReconciliationSummary>("/api/reconciliation/summary").then(setReconciliation).catch(() => setReconciliation(null));
  }

  useEffect(() => {
    refresh().catch((err) => setStatus(err.message));
  }, []);

  function value<T extends keyof RideRow>(ride: RideRow, key: T): RideRow[T] {
    return (edits[ride.id]?.[key] as RideRow[T] | undefined) ?? ride[key];
  }

  function edit(id: string, patch: Partial<RideRow>) {
    setEdits((prev) => ({ ...prev, [id]: { ...(prev[id] || {}), ...patch } }));
  }

  async function save() {
    const payload = Object.entries(edits).map(([id, patch]) => ({ id, ...patch }));
    const out = await api<{ updated: number; rides: RideRow[] }>("/api/rides", { method: "PATCH", body: JSON.stringify({ rides: payload }) });
    setRides(out.rides || []);
    setEdits({});
    setStatus(`Saved ${out.updated} ride edit(s)`);
  }

  async function deleteRide(ride: RideRow) {
    if (!window.confirm(`Delete ride ${ride.route_id}? Unshared artifacts will move to deleted_artifacts outside the repo.`)) return;
    await api(`/api/rides/${encodeURIComponent(ride.id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    await refresh();
  }

  function openNotes(ride: RideRow) {
    setNotesRide(ride);
    setNotesDraft(String(value(ride, "notes") || ""));
  }

  function saveNotes() {
    if (!notesRide) return;
    edit(notesRide.id, { notes: notesDraft });
    setNotesRide(null);
  }

  async function openReports(ride: RideRow) {
    setReportsRide(ride);
    setReports([]);
    setReportsStatus("Loading reports");
    try {
      const out = await api<{ reports: RideReport[] }>(`/api/rides/${encodeURIComponent(ride.id)}/reports`);
      setReports(out.reports || []);
      setReportsStatus("");
    } catch (err) {
      setReportsStatus(err instanceof Error ? err.message : String(err));
    }
  }

  async function makeReviewVideo(ride: RideRow) {
    setStatus(`Generating review video for ${ride.route_id}`);
    await api(`/api/rides/${encodeURIComponent(ride.id)}/review-video`, { method: "POST", body: JSON.stringify({}) });
    await refresh();
    setStatus(`Review video ready for ${ride.route_id}`);
  }

  return (
    <section className="panel rides">
      <div className="sectionHead sticky">
        <div>
          <h2>Rides</h2>
          <p className="subtle">
            {rides.length} visible ride(s)
            {reconciliation?.counts?.db_routes ? ` · ${reconciliation.counts.db_routes} DB ride(s)` : ""}
            {reconciliation?.counts?.discovered_routes ? ` · ${reconciliation.counts.discovered_routes} discovered route id(s)` : ""}
            {reconciliation?.counts?.linked_reports ? ` · ${reconciliation.counts.linked_reports} linked report(s)` : ""}
            {reconciliation?.counts?.unlinked_reports ? ` · ${reconciliation.counts.unlinked_reports} unlinked report(s)` : ""}
            {reconciliation?.truncated ? " · scan truncated" : ""}
          </p>
        </div>
        <div className="inline">
          {dirty ? <span className="dirty">{dirty} unsaved</span> : null}
          <button onClick={refresh}>Refresh</button>
          <button onClick={save} disabled={!dirty}>Save</button>
        </div>
      </div>
      {status && <output>{status}</output>}
      <div className="ridesScroller">
        <table className="ridesTable">
          <colgroup>
            <col className="rideCol" />
            <col className="typeCol" />
            <col className="validationCol" />
            <col className="timeCol" />
            <col className="distanceCol" />
            <col className="mediaCol" />
            <col className="softwareCol" />
            <col className="dataCol" />
            <col className="notesCol" />
            <col className="actionsCol" />
          </colgroup>
          <thead>
            <tr>
              <th>Ride</th>
              <th>Drive Type</th>
              <th>Review</th>
              <th>Drive Time</th>
              <th>Distance</th>
              <th>Media</th>
              <th>Software</th>
              <th>Data</th>
              <th>Notes</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rides.map((ride) => {
              const currentType = normalizeRideType(value(ride, "ride_type") || value(ride, "drive_type"));
              const currentNotes = String(value(ride, "notes") || "");
              const readOnly = Boolean(ride.read_only);
              return (
                <tr key={ride.id} className={edits[ride.id] ? "editedRow" : ""}>
                  <td className="rideCell">
                    <code>{ride.route_id}</code>
                    <span>{rideDisplayName(ride)}</span>
                    {ride.vehicle ? <small>{ride.vehicle}</small> : null}
                    {ride.discovered_only ? <small>discovered outside DB</small> : null}
                  </td>
                  <td>
                    <select
                      value={currentType}
                      disabled={readOnly}
                      onChange={(e) => edit(ride.id, { ride_type: e.target.value, drive_type: e.target.value })}
                    >
                      <option value="normal drive">normal</option>
                      <option value="test drive">test</option>
                      <option value="label validation">label validation</option>
                    </select>
                  </td>
                  <td>
                    <label className="checkRow">
                      <input
                        type="checkbox"
                        disabled={readOnly}
                        checked={Boolean(value(ride, "label_validation"))}
                        onChange={(e) => edit(ride.id, { label_validation: e.target.checked })}
                      />
                      <span>review</span>
                    </label>
                  </td>
                  <td>
                    <strong>{fmtShortDate(ride.started_at)}</strong>
                    <span className="stackText">{fmtTimeRange(ride.started_at, ride.ended_at)}</span>
                    <span className="stackText">{fmtDuration(ride.duration_sec) || "duration unknown"}</span>
                  </td>
                  <td>
                    <strong>{ride.mileage ? `${ride.mileage.toFixed(2)} mi` : "miles unknown"}</strong>
                    <span className="stackText">{ride.segment_count || 0} segment(s)</span>
                  </td>
                  <td>
                    <span className={`pill ${mediaTone(ride.media_status)}`}>{ride.media_status}</span>
                    <span className="stackText">{ride.video_artifact_count} video · {ride.log_artifact_count} log</span>
                    {ride.discovered_source_count ? <span className="stackText">{ride.discovered_source_count} discovered source(s)</span> : null}
                    {ride.media_status === "raw video only" && <div className="miniAction"><button onClick={() => makeReviewVideo(ride)}>Make MP4</button></div>}
                  </td>
                  <td className="softwareCell">
                    <span>{brickpilotVersionLabel(ride.brickpilot_version)}</span>
                    <small>{ride.branch || "branch unknown"}</small>
                    <small>{ride.model_bundle || "model unknown"}</small>
                    {ride.settings_detail ? (
                      <details className="settingsDetails">
                        <summary>{ride.settings_detail.settings_count || 0} settings · {ride.settings_detail.status}</summary>
                        <div className="settingsMeta">
                          {ride.settings_detail.captured_at ? <span>captured {fmtDate(ride.settings_detail.captured_at)}</span> : null}
                          {ride.settings_detail.raw_param_count ? <span>{ride.settings_detail.raw_param_count} raw param files archived</span> : null}
                          {ride.settings_detail.software.commit ? <span>commit {shortCommit(ride.settings_detail.software.commit)}</span> : null}
                          {ride.settings_detail.error ? <span className="warnText">{ride.settings_detail.error}</span> : null}
                        </div>
                        <pre>{settingsLines(ride).join("\n") || ride.settings_summary || "No text settings captured."}</pre>
                      </details>
                    ) : (
                      <small className="warnText">settings missing</small>
                    )}
                  </td>
                  <td>
                    <strong>{ride.artifact_count} artifacts</strong>
                    <span className="stackText">{ride.label_count} labels · {ride.bookmark_count} marks</span>
                    <span className="stackText">{ride.sample_count} samples · {ride.event_count} events</span>
                    {ride.discovered_report_count ? <span className="stackText">{ride.discovered_report_count} report(s)</span> : null}
                  </td>
                  <td className="notesCell">
                    {readOnly ? (
                      <span className="stackText">source only</span>
                    ) : currentNotes ? (
                      <button className="linkButton notePreview" onClick={() => openNotes(ride)}>{currentNotes}</button>
                    ) : (
                      <button className="linkButton" onClick={() => openNotes(ride)}>Add note</button>
                    )}
                  </td>
                  <td className="actionsCell">
                    {!readOnly ? <button onClick={() => window.open(`/manual-labeler/?route_id=${encodeURIComponent(ride.route_id)}`, "_blank")}>Labeler</button> : null}
                    <button onClick={() => openReports(ride)}>Reports</button>
                    {!readOnly ? <button className="danger" onClick={() => deleteRide(ride)}>Delete</button> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {notesRide && (
        <div className="modalBackdrop" role="dialog" aria-modal="true" aria-label="Ride notes">
          <div className="modal notesModal">
            <div className="sectionHead">
              <div>
                <h2>Ride Notes</h2>
                <p className="subtle">{notesRide.route_id}</p>
              </div>
              <button onClick={() => setNotesRide(null)}>Close</button>
            </div>
            <textarea className="notesEditor" value={notesDraft} onChange={(e) => setNotesDraft(e.target.value)} placeholder="Add notes for this ride" />
            <div className="modalActions">
              <button onClick={() => setNotesDraft("")}>Clear</button>
              <button onClick={saveNotes}>Apply</button>
            </div>
          </div>
        </div>
      )}
      {reportsRide && (
        <div className="modalBackdrop" role="dialog" aria-modal="true" aria-label="Ride reports">
          <div className="modal reportsModal">
            <div className="sectionHead">
              <div>
                <h2>Ride Reports</h2>
                <p className="subtle">{reportsRide.route_id}</p>
              </div>
              <button onClick={() => setReportsRide(null)}>Close</button>
            </div>
            {reportsStatus ? <output>{reportsStatus}</output> : null}
            {!reportsStatus && reports.length === 0 ? <p className="empty">No linked analysis reports found yet.</p> : null}
            <div className="reportList">
              {reports.map((report) => (
                <article className="reportItem" key={report.path}>
                  <div>
                    <strong>{report.title}</strong>
                    <span>{report.kind} · {fmtDate(report.modified_at || report.mtime)} · {fmtBytes(report.size_bytes ?? report.size)}</span>
                    {report.summary ? <p>{report.summary}</p> : null}
                      <code>{report.relative_path || report.path}</code>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
