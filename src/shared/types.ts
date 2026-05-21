export type MediaStatus = "ready" | "raw video only" | "needs transcode" | "missing";

export interface RideRow {
  id: string;
  route_id: string;
  canonical_name: string | null;
  route_label: string | null;
  ride_type: string | null;
  drive_type: string | null;
  label_validation: boolean;
  notes: string;
  branch: string | null;
  brickpilot_version: string | null;
  model_bundle: string | null;
  vehicle: string | null;
  settings_summary: string;
  settings_detail: RideSettingsDetail | null;
  started_at: string | null;
  ended_at: string | null;
  duration_sec: number | null;
  mileage: number | null;
  segment_count: number;
  media_status: MediaStatus;
  artifact_count: number;
  log_artifact_count: number;
  video_artifact_count: number;
  playable_video_artifact_count: number;
  raw_video_artifact_count: number;
  label_count: number;
  bookmark_count: number;
  event_count: number;
  sample_count: number;
  video_sync_count: number;
  metadata_summary: Record<string, unknown>;
  discovered_only?: boolean;
  read_only?: boolean;
  discovered_report_count?: number;
  discovered_source_count?: number;
  discovered_roots?: string[];
  latest_discovered_mtime?: string | null;
  discovered_sources?: RideSourceSummary[];
}

export interface RideSettingsDetail {
  status: string;
  captured_at: string | null;
  params_dir: string | null;
  settings_count: number;
  raw_param_count: number;
  artifact_id: number | null;
  error?: string | null;
  software: Record<string, unknown>;
  settings: Record<string, string>;
}

export interface RideSourceSummary {
  kind: string;
  path: string;
  relative_path: string;
  mtime: string | null;
  file_count: number;
  segment_count: number;
  size_bytes: number;
}

export interface RideReportFile {
  title: string;
  kind: string;
  path: string;
  relative_path: string;
  root: string;
  mtime: string;
  size: number;
  route_ids: string[];
  summary: string;
  route_time_ranges?: Record<string, RideTimeRange>;
}

export interface RideTimeRange {
  start?: string;
  end?: string;
  duration_sec?: number;
  mileage?: number;
}

export interface ReviewJob {
  id: number;
  job_id: string;
  route_uuid: string;
  route_id: string;
  route_label: string;
  ride_type: string;
  status: string;
  version: number;
  duration_sec: number | null;
  started_at: string | null;
  ended_at: string | null;
  media_status: MediaStatus;
  sample_count: number;
  bookmark_count: number;
  video_artifact_count: number;
  playable_video_artifact_count: number;
  raw_video_artifact_count: number;
}

export interface VoiceSession {
  session_id: string;
  display_title: string;
  custom_title?: string;
  started_at_wall?: string;
  ended_at_wall?: string;
  duration_sec?: number;
  recording: boolean;
  has_audio: boolean;
  has_transcript: boolean;
  transcript_rows: number;
  audio_chunks: number;
  path?: string;
}

export interface IngestProgress {
  expectedBytes: number;
  copiedBytes: number;
  expectedFiles: number;
  copiedFiles: number;
  percent: number;
  complete: boolean;
}

export type IngestRideType = "normal drive" | "test drive" | "label validation";
export type IngestConnection = "wifi" | "usb";
export type IngestRunStatus = "discovering" | "copying" | "importing" | "complete" | "error" | "canceled";

export interface IngestCandidate {
  route_id: string;
  segment_count: number;
  segments: number[];
  updated_at: string | null;
  log_file_count: number;
  video_file_count: number;
  total_file_count: number;
  log_bytes: number;
  video_bytes: number;
  total_bytes: number;
  reason: string;
}

export interface IngestCopyFile {
  rel: string;
  remote: string;
  dest: string;
  tmp: string;
  expectedBytes: number;
  remoteMtime?: number;
  kind: "logs" | "video";
}

export interface IngestRun {
  run_id: string;
  route_id: string;
  route_key: string;
  ride_type: IngestRideType;
  connection: IngestConnection;
  include_video: boolean;
  status: IngestRunStatus;
  started_at: string;
  finished_at?: string;
  message?: string;
  files: IngestCopyFile[];
  import_result?: Record<string, unknown>;
}

export interface MlOverview {
  generated_at: string;
  data_root: string;
  repo_root: string;
  tools_root: string;
  counts: {
    routes: number;
    labeled_routes: number;
    labels: number;
    bookmarks: number;
    route_samples: number;
    can_frames: number;
    events: number;
    artifacts: number;
    review_jobs: number;
    discovered_reports: number;
    discovered_sources: number;
  };
  progress: Array<{ label: string; value: number; total: number }>;
  review_status: Array<{ status: string; count: number }>;
  model_coverage: Array<{ model_bundle: string; routes: number; labels: number; bookmarks: number; samples: number; events: number }>;
  recent_routes: Array<{ route_id: string; label: string; started_at: string | null; model_bundle: string | null; brickpilot_version: string | null; segment_count: number }>;
  recent_reports: Array<{ title: string; kind: string; relative_path: string; mtime: string | null; size_bytes: number; route_ids: string[]; summary: string }>;
  latest_voice_labeler_run: MlVoiceLabelerRun | null;
}

export interface MlVoiceLabelerRun {
  title: string;
  analysis_name: string;
  created_at: string | null;
  output_dir: string;
  relative_path: string;
  report_path: string | null;
  test_route: string;
  validation_routes: string[];
  human_validation_route: string | null;
  voice_atomic_labels: number;
  canonical_label_count: number;
  trained_target_count: number;
  model_feature_count: number;
  can_candidate_count: number;
  test_prediction_count: number;
  test_prediction_all_count: number;
  test_high_confidence_count: number;
  mean_auc: number | null;
  test_duration_sec: number;
  label_family_counts: Array<{ family: string; count: number }>;
  top_labels: Array<{ label: string; family: string; polarity: string; count: number }>;
  route_label_counts: Array<{
    route_id: string;
    role: string;
    duration_sec: number;
    atomic_label_count: number;
    voice_bookmark_count: number;
    labels_per_min: number;
  }>;
  prediction_target_counts: Array<{ target: string; count: number; max_score: number; total_duration_sec: number }>;
  prediction_timeline: Array<{
    target: string;
    family: string;
    start_sec: number;
    end_sec: number;
    peak_score: number;
    rank: number;
  }>;
  auc_distribution: Array<{ bucket: string; count: number }>;
  auc_by_target: Array<{ target: string; mean_auc: number; eval_count: number }>;
  can_address_summary: Array<{
    bus: number;
    address_hex: string;
    count: number;
    max_abs_effect: number;
    top_target: string;
  }>;
  can_target_summary: Array<{ target: string; count: number; max_abs_effect: number }>;
  top_predictions: Array<{
    rank: number;
    target: string;
    start_sec: number;
    end_sec: number;
    peak_score: number;
    duration_sec: number;
    reason: string;
  }>;
  top_can_candidates: Array<{
    target: string;
    bus: number;
    address_hex: string;
    byte_index: number;
    stat: string;
    effect: number;
    interpretation_status: string;
  }>;
}
