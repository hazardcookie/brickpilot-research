import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import type { Pool } from "pg";
import type { BrickpilotPaths } from "./lib";
import { getDataCatalog } from "./reconciliation";
import type {
  MlAlphaComparison,
  MlCanAnalysis,
  MlDriveAnalysisRun,
  MlDriveTrendPoint,
  MlOverview,
  MlVoiceLabelerRun
} from "../shared/types";

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === "," && !inQuotes) {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  return fields;
}

function parseCsv(input: string): Array<Record<string, string>> {
  const lines = input.split(/\r?\n/).filter((line) => line.trim().length > 0);
  if (!lines.length) return [];
  const header = parseCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = parseCsvLine(line);
    const row: Record<string, string> = {};
    header.forEach((key, index) => {
      row[key] = values[index] || "";
    });
    return row;
  });
}

async function readJsonFile<T>(filePath: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await fsp.readFile(filePath, "utf8")) as T;
  } catch {
    return fallback;
  }
}

async function readCsvFile(filePath: string): Promise<Array<Record<string, string>>> {
  try {
    return parseCsv(await fsp.readFile(filePath, "utf8"));
  } catch {
    return [];
  }
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fsp.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function analysisDirs(paths: BrickpilotPaths, match: RegExp): Promise<Array<{ name: string; dir: string; mtimeMs: number }>> {
  const analysisRoot = path.join(paths.dataRoot, "analysis_exports");
  try {
    const dirents = (await fsp.readdir(analysisRoot, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory() && match.test(entry.name));
    const rows = await Promise.all(dirents.map(async (entry) => {
      const dir = path.join(analysisRoot, entry.name);
      const stat = await fsp.stat(dir);
      return { name: entry.name, dir, mtimeMs: stat.mtimeMs };
    }));
    return rows.sort((a, b) => b.mtimeMs - a.mtimeMs || b.name.localeCompare(a.name));
  } catch {
    return [];
  }
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    const text = String(value ?? "").trim();
    if (text) return text;
  }
  return null;
}

function relativeToDataRoot(paths: BrickpilotPaths, filePath: string): string {
  const rel = path.relative(paths.dataRoot, filePath);
  if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) return filePath;
  return rel;
}

function reportPathFor(paths: BrickpilotPaths, runDir: string): string | null {
  const reportPath = path.join(runDir, "report.md");
  return fsPathExists(reportPath) ? relativeToDataRoot(paths, reportPath) : null;
}

function fsPathExists(filePath: string): boolean {
  try {
    return fs.existsSync(filePath);
  } catch {
    return false;
  }
}

function labelFamily(taxonomy: Record<string, unknown>, target: string): string {
  const labels = taxonomy.labels as Record<string, { family?: string }> | undefined;
  const direct = labels?.[target]?.family;
  if (direct) return direct;
  if (target.startsWith("group_phev") || /^phev_|ev_light_|ice_engine_|eco_mode|electric_mode|automatic_mode|hybrid_mode|sport_mode|power_meter_/.test(target)) return "phev";
  if (target.includes("brake") || target.includes("stop") || target.startsWith("accel") || target === "driver_gas" || target === "driver_brake") return "longitudinal";
  if (target.startsWith("steering") || target.startsWith("turn_signal")) return "lateral";
  if (target.startsWith("quality") || target === "smooth_driving") return "quality";
  if (target.startsWith("gear") || ["set_speed", "comma_engaged", "comma_disengaged", "cruise_button_press"].includes(target)) return "drive_control";
  if (/parking|hvac|charger|headlight/.test(target)) return "accessory";
  return "other";
}

function aucBucket(value: number): string {
  if (value >= 0.9) return "0.90-1.00";
  if (value >= 0.8) return "0.80-0.89";
  if (value >= 0.7) return "0.70-0.79";
  if (value >= 0.6) return "0.60-0.69";
  return "<0.60";
}

async function latestVoiceLabelerRun(paths: BrickpilotPaths): Promise<MlVoiceLabelerRun | null> {
  const entries = await analysisDirs(paths, /^ml_.*voice_labeler/i);

  for (const entry of entries) {
    const runDir = entry.dir;
    const summary = await readJsonFile<Record<string, unknown>>(path.join(runDir, "run_summary.json"), {});
    if (!Object.keys(summary).length) continue;
    const taxonomy = await readJsonFile<Record<string, unknown>>(path.join(runDir, "voice_label_taxonomy.json"), {});

    const evalRows = await readCsvFile(path.join(runDir, "model_eval_leave_one_route.csv"));
    const aucValues = evalRows
      .map((row) => String(row.auc ?? "").trim())
      .filter((value) => value.length > 0)
      .map((value) => Number(value))
      .filter((value) => Number.isFinite(value));
    const meanAuc = aucValues.length ? aucValues.reduce((sum, value) => sum + value, 0) / aucValues.length : null;
    const aucDistributionMap = new Map<string, number>([
      ["0.90-1.00", 0],
      ["0.80-0.89", 0],
      ["0.70-0.79", 0],
      ["0.60-0.69", 0],
      ["<0.60", 0]
    ]);
    const aucTargetMap = new Map<string, { sum: number; count: number }>();
    for (const row of evalRows) {
      const aucText = String(row.auc ?? "").trim();
      if (!aucText) continue;
      const auc = Number(aucText);
      if (!Number.isFinite(auc)) continue;
      aucDistributionMap.set(aucBucket(auc), (aucDistributionMap.get(aucBucket(auc)) || 0) + 1);
      const target = String(row.target || "");
      const current = aucTargetMap.get(target) || { sum: 0, count: 0 };
      current.sum += auc;
      current.count += 1;
      aucTargetMap.set(target, current);
    }
    const aucByTarget = Array.from(aucTargetMap.entries())
      .map(([target, value]) => ({ target, mean_auc: value.sum / value.count, eval_count: value.count }))
      .sort((a, b) => b.mean_auc - a.mean_auc)
      .slice(0, 12);

    const predictionRows = await readCsvFile(path.join(runDir, "test_route_predictions.csv"));
    const topPredictions = predictionRows
      .sort((a, b) => num(a.rank) - num(b.rank))
      .slice(0, 8)
      .map((row) => ({
        rank: num(row.rank),
        target: String(row.target || ""),
        start_sec: num(row.start_sec),
        end_sec: num(row.end_sec),
        peak_score: num(row.peak_score),
        duration_sec: num(row.duration_sec),
        reason: String(row.reason || "")
      }));
    const predictionTargetMap = new Map<string, { count: number; max_score: number; total_duration_sec: number }>();
    for (const row of predictionRows) {
      const target = String(row.target || "");
      const current = predictionTargetMap.get(target) || { count: 0, max_score: 0, total_duration_sec: 0 };
      current.count += 1;
      current.max_score = Math.max(current.max_score, num(row.peak_score));
      current.total_duration_sec += num(row.duration_sec);
      predictionTargetMap.set(target, current);
    }
    const predictionTargetCounts = Array.from(predictionTargetMap.entries())
      .map(([target, value]) => ({ target, ...value }))
      .sort((a, b) => b.count - a.count || b.max_score - a.max_score)
      .slice(0, 12);
    const predictionTimeline = predictionRows
      .map((row) => ({
        target: String(row.target || ""),
        family: labelFamily(taxonomy, String(row.target || "")),
        start_sec: num(row.start_sec),
        end_sec: num(row.end_sec),
        peak_score: num(row.peak_score),
        rank: num(row.rank)
      }))
      .sort((a, b) => a.start_sec - b.start_sec || a.rank - b.rank)
      .slice(0, 140);

    const canRows = await readCsvFile(path.join(runDir, "can_signal_candidates.csv"));
    const phevTargets = /^(phev_|group_phev|ev_light_|ice_engine_|eco_mode|electric_mode|automatic_mode|hybrid_mode|sport_mode|power_meter_)/;
    const canSource = canRows.filter((row) => phevTargets.test(String(row.target || "")));
    const topCanCandidates = (canSource.length ? canSource : canRows)
      .sort((a, b) => Math.abs(num(b.effect)) - Math.abs(num(a.effect)))
      .slice(0, 8)
      .map((row) => ({
        target: String(row.target || ""),
        bus: num(row.bus),
        address_hex: String(row.address_hex || ""),
        byte_index: num(row.byte_index),
        stat: String(row.stat || ""),
        effect: num(row.effect),
        interpretation_status: String(row.interpretation_status || "candidate_correlation_not_dbc_semantics")
      }));
    const canAddressMap = new Map<string, { bus: number; address_hex: string; count: number; max_abs_effect: number; top_target: string }>();
    const canTargetMap = new Map<string, { count: number; max_abs_effect: number }>();
    for (const row of canSource.length ? canSource : canRows) {
      const bus = num(row.bus);
      const addressHex = String(row.address_hex || "");
      const target = String(row.target || "");
      const absEffect = Math.abs(num(row.effect));
      const key = `${bus}-${addressHex}`;
      const address = canAddressMap.get(key) || { bus, address_hex: addressHex, count: 0, max_abs_effect: 0, top_target: target };
      address.count += 1;
      if (absEffect > address.max_abs_effect) {
        address.max_abs_effect = absEffect;
        address.top_target = target;
      }
      canAddressMap.set(key, address);
      const targetCurrent = canTargetMap.get(target) || { count: 0, max_abs_effect: 0 };
      targetCurrent.count += 1;
      targetCurrent.max_abs_effect = Math.max(targetCurrent.max_abs_effect, absEffect);
      canTargetMap.set(target, targetCurrent);
    }
    const canAddressSummary = Array.from(canAddressMap.values())
      .sort((a, b) => b.max_abs_effect - a.max_abs_effect || b.count - a.count)
      .slice(0, 12);
    const canTargetSummary = Array.from(canTargetMap.entries())
      .map(([target, value]) => ({ target, ...value }))
      .sort((a, b) => b.max_abs_effect - a.max_abs_effect || b.count - a.count)
      .slice(0, 12);

    const labels = taxonomy.labels as Record<string, { count?: number; family?: string; polarity?: string }> | undefined;
    const topLabels = Object.entries(labels || {})
      .map(([label, value]) => ({
        label,
        family: String(value.family || labelFamily(taxonomy, label)),
        polarity: String(value.polarity || ""),
        count: num(value.count)
      }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 14);
    const familyEntries = Object.entries(taxonomy.families as Record<string, Record<string, number>> | undefined || {});
    const labelFamilyCounts = familyEntries
      .map(([family, values]) => ({ family, count: Object.values(values || {}).reduce((sum, value) => sum + num(value), 0) }))
      .sort((a, b) => b.count - a.count);

    const routeLabelCounts = (await readCsvFile(path.join(runDir, "route_summary.csv")))
      .map((row) => {
        const duration = num(row.duration_sec);
        const atomicCount = num(row.atomic_label_count);
        return {
          route_id: String(row.route_id || ""),
          role: String(row.role || ""),
          duration_sec: duration,
          atomic_label_count: atomicCount,
          voice_bookmark_count: num(row.voice_bookmark_count),
          labels_per_min: duration > 0 ? atomicCount / (duration / 60) : 0
        };
      });
    const routes = summary.routes as Record<string, { duration_sec?: number }> | undefined;
    const testDuration = num(routes?.[String(summary.test_route || summary.route_id || "")]?.duration_sec) || num((routeLabelCounts.find((row) => row.route_id === String(summary.test_route || summary.route_id || "")) || {}).duration_sec);

    const reportPath = path.join(runDir, "report.md");
    return {
      title: String(summary.title || "Voice labeler ML run"),
      analysis_name: String(summary.analysis_name || "ml_0325_voice_labeler"),
      created_at: typeof summary.created_at === "string" ? summary.created_at : null,
      output_dir: typeof summary.output_dir === "string" ? summary.output_dir : runDir,
      relative_path: relativeToDataRoot(paths, runDir),
      report_path: (await fileExists(reportPath)) ? relativeToDataRoot(paths, reportPath) : null,
      test_route: String(summary.test_route || summary.route_id || ""),
      validation_routes: Array.isArray(summary.validation_routes) ? summary.validation_routes.map(String) : [],
      human_validation_route: typeof summary.human_validation_route === "string" ? summary.human_validation_route : null,
      voice_atomic_labels: num(summary.voice_atomic_labels),
      canonical_label_count: num(summary.canonical_label_count),
      trained_target_count: num(summary.trained_target_count),
      model_feature_count: num(summary.model_feature_count),
      can_candidate_count: num(summary.can_candidate_count),
      test_prediction_count: num(summary.test_prediction_count),
      test_prediction_all_count: num(summary.test_prediction_all_count),
      test_high_confidence_count: num(summary.test_high_confidence_count),
      mean_auc: meanAuc,
      test_duration_sec: testDuration,
      label_family_counts: labelFamilyCounts,
      top_labels: topLabels,
      route_label_counts: routeLabelCounts,
      prediction_target_counts: predictionTargetCounts,
      prediction_timeline: predictionTimeline,
      auc_distribution: Array.from(aucDistributionMap.entries()).map(([bucket, count]) => ({ bucket, count })),
      auc_by_target: aucByTarget,
      can_address_summary: canAddressSummary,
      can_target_summary: canTargetSummary,
      top_predictions: topPredictions,
      top_can_candidates: topCanCandidates
    };
  }

  return null;
}

function driveTrendFromSummary(summary: Record<string, unknown>, createdAt: string | null): MlDriveTrendPoint | null {
  const route = summary.route as Record<string, unknown> | undefined;
  const routeCounts = summary.route_counts as Record<string, unknown> | undefined;
  const routeId = firstString(summary.route_id, route?.route_id);
  if (!routeId) return null;
  return {
    route_id: routeId,
    brickpilot_version: firstString(route?.brickpilot_version, (route?.software as Record<string, unknown> | undefined)?.brickpilot_version),
    model_bundle: firstString(route?.model_bundle),
    created_at: createdAt,
    duration_sec: num(route?.duration_sec),
    sample_count: num(routeCounts?.samples),
    speed_avg_mph: num(routeCounts?.speed_avg_mph),
    speed_max_mph: num(routeCounts?.speed_max_mph),
    stopped_frac: num(routeCounts?.stopped_frac),
    low_speed_frac: num(routeCounts?.low_speed_frac),
    brake_pressed_frac: num(routeCounts?.brake_pressed_frac),
    gas_pressed_frac: num(routeCounts?.gas_pressed_frac),
    all_predictions: num(summary.all_predictions),
    review_predictions: Array.isArray(summary.review_predictions) ? summary.review_predictions.length : num(summary.review_predictions)
  };
}

async function latestDriveAnalysis(paths: BrickpilotPaths): Promise<MlDriveAnalysisRun | null> {
  const entries = await analysisDirs(paths, /^prelim_/i);
  for (const entry of entries) {
    const summary = await readJsonFile<Record<string, unknown>>(path.join(entry.dir, "run_summary.json"), {});
    const route = summary.route as Record<string, unknown> | undefined;
    const routeCounts = summary.route_counts as Record<string, unknown> | undefined;
    const routeId = firstString(summary.route_id, route?.route_id);
    if (!routeId || !Object.keys(summary).length) continue;

    const timelineRows = await readCsvFile(path.join(entry.dir, "prediction_timeline.csv"));
    const labelSummaryRows = await readCsvFile(path.join(entry.dir, "prediction_label_summary.csv"));
    const shadowRows = await readCsvFile(path.join(entry.dir, "brickpilot_shadow_summary.csv"));
    const topPredictionSource = Array.isArray(summary.top_prediction_labels)
      ? summary.top_prediction_labels as Array<Record<string, unknown>>
      : labelSummaryRows;
    const shadowLabels: Record<string, string> = {
      nearStandstill: "Near standstill",
      standstill: "Full stop",
      longitudinalAssistActive: "Assist active",
      longitudinalAssistShadowCandidate: "Assist opportunity",
      stopActive: "Stop assist active",
      stopShadowCandidate: "Stop opportunity",
      brakePressed: "Brake pressed",
      gasPressed: "Gas pressed"
    };
    const shadowMetrics = shadowRows
      .filter((row) => row.field in shadowLabels)
      .map((row) => ({
        field: String(row.field || ""),
        label: shadowLabels[String(row.field || "")],
        value: num(row.true_frac || row.nonzero_frac || row.mean),
        count: num(row.true_count || row.count)
      }))
      .slice(0, 8);

    const reportPath = path.join(entry.dir, "report.md");
    return {
      title: `${firstString(route?.brickpilot_version, (route?.software as Record<string, unknown> | undefined)?.brickpilot_version) || "Latest"} post-drive analysis`,
      analysis_name: entry.name,
      created_at: firstString(summary.created_utc) || new Date(entry.mtimeMs).toISOString(),
      relative_path: relativeToDataRoot(paths, entry.dir),
      report_path: fs.existsSync(reportPath) ? relativeToDataRoot(paths, reportPath) : null,
      route_id: routeId,
      route_label: firstString(route?.route_label),
      brickpilot_version: firstString(route?.brickpilot_version, (route?.software as Record<string, unknown> | undefined)?.brickpilot_version),
      model_bundle: firstString(route?.model_bundle),
      duration_sec: num(route?.duration_sec),
      segment_count: num(route?.segment_count),
      sample_count: num(routeCounts?.samples),
      test_windows: num(summary.test_windows),
      all_predictions: num(summary.all_predictions),
      review_predictions: Array.isArray(summary.review_predictions) ? summary.review_predictions.length : num(summary.review_predictions),
      trained_targets: num(summary.trained_targets),
      speed_avg_mph: num(routeCounts?.speed_avg_mph),
      speed_max_mph: num(routeCounts?.speed_max_mph),
      stopped_frac: num(routeCounts?.stopped_frac),
      low_speed_frac: num(routeCounts?.low_speed_frac),
      brake_pressed_frac: num(routeCounts?.brake_pressed_frac),
      gas_pressed_frac: num(routeCounts?.gas_pressed_frac),
      shadow_metrics: shadowMetrics,
      top_prediction_labels: topPredictionSource
        .slice(0, 14)
        .map((row) => {
          const target = String(row.target || "");
          return {
            target,
            family: labelFamily({}, target),
            intervals: num(row.intervals),
            seconds: num(row.seconds),
            best_peak_score: num(row.best_peak_score),
            best_start_sec: num(row.best_start_sec),
            best_end_sec: num(row.best_end_sec),
            best_reason: String(row.best_reason || "")
          };
        }),
      prediction_timeline: timelineRows
        .map((row) => {
          const target = String(row.target || "");
          return {
            target,
            family: labelFamily({}, target),
            start_sec: num(row.start_sec),
            end_sec: num(row.end_sec),
            peak_score: num(row.peak_score),
            rank: num(row.rank)
          };
        })
        .sort((a, b) => a.start_sec - b.start_sec || a.rank - b.rank)
        .slice(0, 180)
    };
  }
  return null;
}

async function driveAnalysisTrend(paths: BrickpilotPaths): Promise<MlDriveTrendPoint[]> {
  const entries = (await analysisDirs(paths, /^prelim_/i)).slice(0, 12);
  const points: MlDriveTrendPoint[] = [];
  for (const entry of entries) {
    const summary = await readJsonFile<Record<string, unknown>>(path.join(entry.dir, "run_summary.json"), {});
    const point = driveTrendFromSummary(summary, firstString(summary.created_utc) || new Date(entry.mtimeMs).toISOString());
    if (point) points.push(point);
  }
  return points.reverse();
}

function alphaGroupLabel(value: string): string {
  if (/off|native|reference/i.test(value)) return "Native / OFF";
  if (/_on_|brickpilot|stop/i.test(value)) return "Alpha Long ON";
  return value.replaceAll("_", " ");
}

async function latestAlphaComparison(paths: BrickpilotPaths): Promise<MlAlphaComparison | null> {
  const entries = await analysisDirs(paths, /^alpha_long_on_off_compare_/i);
  for (const entry of entries) {
    const summary = await readJsonFile<Record<string, unknown>>(path.join(entry.dir, "run_summary.json"), {});
    const groupRows = await readCsvFile(path.join(entry.dir, "alpha_on_off_group_means.csv"));
    const routeRows = await readCsvFile(path.join(entry.dir, "alpha_on_off_route_metrics.csv"));
    if (!groupRows.length && !routeRows.length) continue;
    const stopBuckets = (await readCsvFile(path.join(entry.dir, "alpha_on_off_stop_buckets.csv")))
      .map((row) => ({
        context: String(row.context || ""),
        comparison_group: String(row.comparison_group || ""),
        samples: num(row.samples),
        frac: num(row.frac)
      }))
      .filter((row) => row.samples > 0)
      .sort((a, b) => b.samples - a.samples)
      .slice(0, 12);
    const activeStopReasons = (await readCsvFile(path.join(entry.dir, "alpha_on_off_active_stop_reasons.csv")))
      .map((row) => ({
        value: String(row.value || ""),
        comparison_group: String(row.comparison_group || ""),
        samples: num(row.samples),
        frac: num(row.frac)
      }))
      .filter((row) => row.samples > 0)
      .sort((a, b) => b.samples - a.samples)
      .slice(0, 12);
    const reportPath = path.join(entry.dir, "report.md");
    return {
      title: "Alpha Long ON/OFF comparison",
      created_at: new Date(entry.mtimeMs).toISOString(),
      relative_path: relativeToDataRoot(paths, entry.dir),
      report_path: fs.existsSync(reportPath) ? relativeToDataRoot(paths, reportPath) : null,
      routes: Array.isArray(summary.routes) ? summary.routes.length : routeRows.length,
      groups: groupRows.map((row) => {
        const group = String(row.comparison_group || "");
        return {
          comparison_group: group,
          label: alphaGroupLabel(group),
          routes: num(row.routes),
          stopped_frac: num(row.stopped_frac_mean),
          low_speed_frac: num(row.low_speed_frac_mean),
          lead_frac: num(row.lead_frac_mean),
          brake_pressed_frac: num(row.brake_pressed_frac_mean),
          gas_pressed_frac: num(row.gas_pressed_frac_mean),
          assist_active_frac: num(row.longitudinalAssistActive_nonzero_frac_mean),
          stop_active_frac: num(row.stopActive_nonzero_frac_mean),
          planner_debt: num(row.stopPlannerDebt_mean_mean),
          controller_debt: num(row.stopControllerDebt_mean_mean),
          brake_debt: num(row.stopBrakeDebt_mean_mean),
          good_stop_labels: num(row.good_stop_labels_mean),
          bad_brake_labels: num(row.bad_brake_labels_mean),
          driver_intervention_labels: num(row.driver_brake_intervention_labels_mean),
          stop_complete_labels: num(row.stop_complete_labels_mean),
          stop_go_bad_labels: num(row.stop_go_bad_labels_mean),
          unnecessary_braking_labels: num(row.unnecessary_braking_labels_mean)
        };
      }),
      route_metrics: routeRows.slice(0, 10).map((row) => ({
        route_id: String(row.route_id || ""),
        version: String(row.version || ""),
        comparison_group: String(row.comparison_group || ""),
        note: String(row.note || ""),
        duration_sec: num(row.duration_sec),
        avg_speed_mph: num(row.avg_speed_mph),
        stopped_frac: num(row.stopped_frac),
        low_speed_frac: num(row.low_speed_frac),
        lead_frac: num(row.lead_frac),
        brake_pressed_frac: num(row.brake_pressed_frac),
        gas_pressed_frac: num(row.gas_pressed_frac),
        assist_active_frac: num(row.longitudinalAssistActive_nonzero_frac),
        stop_active_frac: num(row.stopActive_nonzero_frac),
        good_stop_labels: num(row.good_stop_labels),
        bad_brake_labels: num(row.bad_brake_labels),
        driver_brake_intervention_labels: num(row.driver_brake_intervention_labels),
        stop_complete_labels: num(row.stop_complete_labels),
        stop_go_bad_labels: num(row.stop_go_bad_labels),
        unnecessary_braking_labels: num(row.unnecessary_braking_labels)
      })),
      stop_buckets: stopBuckets,
      active_stop_reasons: activeStopReasons
    };
  }
  return null;
}

async function latestCanAnalysis(paths: BrickpilotPaths): Promise<MlCanAnalysis | null> {
  const entries = await analysisDirs(paths, /^phev_can_/i);
  for (const entry of entries) {
    const summary = await readJsonFile<Record<string, unknown>>(path.join(entry.dir, "run_summary.json"), {});
    if (!Object.keys(summary).length) continue;
    const effectsSource = Array.isArray(summary.top_effects)
      ? summary.top_effects as Array<Record<string, unknown>>
      : await readCsvFile(path.join(entry.dir, "label_candidate_effects.csv"));
    const routeCandidates = (await readCsvFile(path.join(entry.dir, "route_candidate_summary.csv")))
      .map((row) => ({
        field: String(row.field || ""),
        count: num(row.count),
        mean: num(row.mean),
        max: num(row.max),
        match_frac: num(row.fa_b4_bus0_bus130_match_frac)
      }))
      .filter((row) => row.field)
      .sort((a, b) => b.count - a.count)
      .slice(0, 10);
    const reportPath = path.join(entry.dir, "report.md");
    return {
      title: "PHEV CAN signal analysis",
      created_at: firstString(summary.created_utc) || new Date(entry.mtimeMs).toISOString(),
      relative_path: relativeToDataRoot(paths, entry.dir),
      report_path: fs.existsSync(reportPath) ? relativeToDataRoot(paths, reportPath) : null,
      routes: Array.isArray(summary.routes) ? summary.routes.length : num(summary.routes),
      frame_rows: num(summary.frame_rows),
      decoded_field_rows: num(summary.decoded_field_rows),
      route_summary_rows: num(summary.route_summary_rows),
      label_effect_rows: num(summary.label_effect_rows),
      test_interval_rows: num(summary.test_interval_rows),
      top_effects: effectsSource.slice(0, 10).map((row) => ({
        target: String(row.target || ""),
        field: String(row.field || ""),
        pos_count: num(row.pos_count),
        background_count: num(row.background_count),
        pos_mean: num(row.pos_mean),
        background_mean: num(row.background_mean),
        effect: num(row.effect),
        abs_effect: num(row.abs_effect),
        interpretation_status: String(row.interpretation_status || "")
      })),
      route_candidates: routeCandidates
    };
  }
  return null;
}

export async function getMlOverview(pool: Pool, paths: BrickpilotPaths): Promise<MlOverview> {
  const [
    counts,
    reviewStatus,
    modelCoverage,
    recentRoutes,
    catalog,
    voiceLabelerRun,
    driveAnalysis,
    alphaComparison,
    canAnalysis,
    analysisTrend
  ] = await Promise.all([
    pool.query(`
      SELECT
        (SELECT COUNT(*) FROM routes) AS routes,
        (SELECT COUNT(DISTINCT route_uuid) FROM labels WHERE deleted_at IS NULL) AS labeled_routes,
        (SELECT COUNT(*) FROM labels WHERE deleted_at IS NULL) AS labels,
        (SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL) AS bookmarks,
        (SELECT COUNT(*) FROM route_samples) AS route_samples,
        (SELECT COUNT(*) FROM can_frames_sampled) AS can_frames,
        (SELECT COUNT(*) FROM events) AS events,
        (SELECT COUNT(*) FROM artifacts) AS artifacts,
        (SELECT COUNT(*) FROM review_jobs WHERE COALESCE(status, '') NOT LIKE 'superseded%') AS review_jobs,
        (SELECT COUNT(*) FROM routes WHERE model_bundle IS NOT NULL AND model_bundle <> '') AS routes_with_model,
        (SELECT COUNT(*) FROM routes WHERE brickpilot_version IS NOT NULL AND brickpilot_version <> '') AS routes_with_version
    `),
    pool.query("SELECT COALESCE(status, 'pending') AS status, COUNT(*) AS count FROM review_jobs GROUP BY COALESCE(status, 'pending') ORDER BY count DESC"),
    pool.query(`
      WITH base AS (
        SELECT id, COALESCE(NULLIF(model_bundle, ''), 'unknown model') AS model_bundle
        FROM routes
      ),
      label_counts AS (
        SELECT route_uuid, COUNT(*) AS labels FROM labels WHERE deleted_at IS NULL GROUP BY route_uuid
      ),
      bookmark_counts AS (
        SELECT route_uuid, COUNT(*) AS bookmarks FROM bookmarks WHERE deleted_at IS NULL GROUP BY route_uuid
      ),
      sample_counts AS (
        SELECT route_uuid, COUNT(*) AS samples FROM route_samples GROUP BY route_uuid
      ),
      event_counts AS (
        SELECT route_uuid, COUNT(*) AS events FROM events GROUP BY route_uuid
      )
      SELECT b.model_bundle,
             COUNT(*) AS routes,
             COALESCE(SUM(l.labels), 0) AS labels,
             COALESCE(SUM(bm.bookmarks), 0) AS bookmarks,
             COALESCE(SUM(s.samples), 0) AS samples,
             COALESCE(SUM(e.events), 0) AS events
      FROM base b
      LEFT JOIN label_counts l ON l.route_uuid=b.id
      LEFT JOIN bookmark_counts bm ON bm.route_uuid=b.id
      LEFT JOIN sample_counts s ON s.route_uuid=b.id
      LEFT JOIN event_counts e ON e.route_uuid=b.id
      GROUP BY b.model_bundle
      ORDER BY routes DESC, labels DESC
      LIMIT 20
    `),
    pool.query(`
      SELECT route_id, COALESCE(route_label, canonical_name, route_id) AS label, started_at,
             model_bundle, brickpilot_version, segment_count
      FROM routes
      ORDER BY COALESCE(started_at, updated_at) DESC NULLS LAST
      LIMIT 8
    `),
    getDataCatalog(paths),
    latestVoiceLabelerRun(paths),
    latestDriveAnalysis(paths),
    latestAlphaComparison(paths),
    latestCanAnalysis(paths),
    driveAnalysisTrend(paths)
  ]);

  const countRow = counts.rows[0] || {};
  const reports = catalog.reports
    .filter((report) => /ml|rnd|replay|can|analysis|tuning|sweep|candidate|matrix|phev/i.test(`${report.relative_path} ${report.title} ${report.kind}`))
    .slice(0, 18)
    .map((report) => ({
      title: report.title,
      kind: report.kind,
      relative_path: report.relative_path,
      mtime: report.mtime,
      size_bytes: report.size_bytes,
      route_ids: report.route_ids,
      summary: report.summary
    }));

  const routeCount = num(countRow.routes);
  const labeledRoutes = num(countRow.labeled_routes);
  const routesWithModel = num(countRow.routes_with_model);
  const routesWithVersion = num(countRow.routes_with_version);

  return {
    generated_at: new Date().toISOString(),
    data_root: paths.dataRoot,
    repo_root: paths.repoRoot,
    tools_root: paths.toolsRoot,
    counts: {
      routes: routeCount,
      labeled_routes: labeledRoutes,
      labels: num(countRow.labels),
      bookmarks: num(countRow.bookmarks),
      route_samples: num(countRow.route_samples),
      can_frames: num(countRow.can_frames),
      events: num(countRow.events),
      artifacts: num(countRow.artifacts),
      review_jobs: num(countRow.review_jobs),
      discovered_reports: catalog.reports.length,
      discovered_sources: catalog.sources.length
    },
    progress: [
      { label: "Routes with human labels", value: labeledRoutes, total: routeCount },
      { label: "Routes with model metadata", value: routesWithModel, total: routeCount },
      { label: "Routes with Brickpilot version", value: routesWithVersion, total: routeCount },
      { label: "Linked reports visible to UI", value: catalog.reports.filter((report) => report.route_ids.length).length, total: Math.max(catalog.reports.length, 1) }
    ],
    review_status: reviewStatus.rows.map((row) => ({ status: String(row.status), count: num(row.count) })),
    model_coverage: modelCoverage.rows.map((row) => ({
      model_bundle: String(row.model_bundle),
      routes: num(row.routes),
      labels: num(row.labels),
      bookmarks: num(row.bookmarks),
      samples: num(row.samples),
      events: num(row.events)
    })),
    recent_routes: recentRoutes.rows.map((row) => ({
      route_id: String(row.route_id),
      label: String(row.label || row.route_id),
      started_at: row.started_at ? new Date(row.started_at).toISOString() : null,
      model_bundle: row.model_bundle || null,
      brickpilot_version: row.brickpilot_version || null,
      segment_count: num(row.segment_count)
    })),
    recent_reports: reports,
    latest_voice_labeler_run: voiceLabelerRun,
    latest_drive_analysis: driveAnalysis,
    latest_alpha_comparison: alphaComparison,
    latest_can_analysis: canAnalysis,
    analysis_trend: analysisTrend
  };
}
