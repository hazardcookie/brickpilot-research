#!/usr/bin/env python3
"""Large-scale VM replay steering variant sweep.

Generates a 100k-scale candidate set, scores it with a compiled local C++
engine over the latest VM controlsd replay series, and writes ranked artifacts.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import html
import json
import math
import os
import random
import shutil
import subprocess
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BRICKPILOT_ANALYSIS_ROOT", Path.home() / "BrickpilotDriveDB" / "analysis_exports"))
DEFAULT_REPLAY_DIR = Path(os.environ.get("BRICKPILOT_REPLAY_DIR", DEFAULT_OUTPUT_ROOT / "vm_controlsd_replay_steering_latest"))
DEFAULT_PREVIOUS_DIR = Path(os.environ.get("BRICKPILOT_PREVIOUS_SWEEP_DIR", DEFAULT_OUTPUT_ROOT / "vm_replay_steering_variant_sweep_20260518T051800Z"))
DEFAULT_LAB_RATIO = 0.20


@dataclass(frozen=True)
class Candidate:
  name: str
  family: str
  unsafe_lab_only: bool
  unsafe_reason: str
  alpha: float
  reversal_scale: float
  zero_hold_frames: int
  zero_cross_raw: float
  zero_cross_smooth: float
  rate_limit_per_sec: float | None
  second_alpha: float | None
  deadband: float
  torque_scale: float
  lookahead_frames: int
  hold_after_reversal_frames: int


def as_float(value: Any, default: float = 0.0) -> float:
  try:
    ret = float(value)
  except (TypeError, ValueError):
    return default
  return ret if math.isfinite(ret) else default


def as_bool(value: Any) -> bool:
  return str(value).strip().lower() in ("1", "true", "yes")


def candidate_key(candidate: Candidate) -> tuple[Any, ...]:
  data = asdict(candidate)
  data.pop("name", None)
  data.pop("unsafe_reason", None)
  return tuple(sorted(data.items()))


def parse_optional_float(value: str | None) -> float | None:
  if value is None or str(value).strip() == "":
    return None
  return as_float(value)


def previous_candidates(path: Path) -> list[Candidate]:
  csv_path = path / "candidate_scores.csv"
  if not csv_path.exists():
    return []
  out: list[Candidate] = []
  with csv_path.open(newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
      out.append(Candidate(
        name=row.get("candidate") or row.get("name") or f"previous_{len(out)}",
        family=row.get("family") or "previous",
        unsafe_lab_only=as_bool(row.get("unsafe_lab_only")),
        unsafe_reason=row.get("unsafe_reason") or "",
        alpha=as_float(row.get("alpha"), 1.0),
        reversal_scale=as_float(row.get("reversal_scale"), 1.0),
        zero_hold_frames=int(as_float(row.get("zero_hold_frames"), 0.0)),
        zero_cross_raw=as_float(row.get("zero_cross_raw"), 0.0),
        zero_cross_smooth=as_float(row.get("zero_cross_smooth"), 0.0),
        rate_limit_per_sec=parse_optional_float(row.get("rate_limit_per_sec")),
        second_alpha=parse_optional_float(row.get("second_alpha")),
        deadband=as_float(row.get("deadband"), 0.0),
        torque_scale=as_float(row.get("torque_scale"), 1.0),
        lookahead_frames=int(as_float(row.get("lookahead_frames"), 0.0)),
        hold_after_reversal_frames=int(as_float(row.get("hold_after_reversal_frames"), 0.0)),
      ))
  return out


def generate_candidates(total: int, previous_dir: Path, lab_ratio: float = DEFAULT_LAB_RATIO,
                        seed: int = 404004, name_prefix: str = "road") -> list[Candidate]:
  if total < 10:
    raise ValueError("candidate total must be at least 10")
  lab_ratio = min(0.95, max(0.0, lab_ratio))
  lab_target = int(round(total * lab_ratio))
  road_target = total - lab_target - 1
  rng = random.Random(seed)
  candidates: list[Candidate] = [
    Candidate("baseline_replay_0_4_4", "baseline", False, "", 1.0, 1.0, 0, 0.0, 0.0, None, None, 0.0, 1.0, 0, 0),
  ]
  seen = {candidate_key(candidates[0])}
  road_count = 0
  lab_count = 0

  for prev in previous_candidates(previous_dir):
    key = candidate_key(prev)
    if key in seen:
      continue
    if prev.unsafe_lab_only:
      if lab_count >= lab_target:
        continue
      lab_count += 1
    else:
      if prev.family == "baseline" or road_count >= road_target:
        continue
      road_count += 1
    seen.add(key)
    candidates.append(prev)

  def add(candidate: Candidate) -> bool:
    nonlocal road_count, lab_count
    key = candidate_key(candidate)
    if key in seen:
      return False
    if candidate.unsafe_lab_only:
      if lab_count >= lab_target:
        return False
      lab_count += 1
    else:
      if road_count >= road_target:
        return False
      road_count += 1
    seen.add(key)
    candidates.append(candidate)
    return True

  road_idx = 0
  while road_count < road_target:
    alpha = rng.uniform(0.035, 0.38)
    if rng.random() < 0.58:
      alpha = rng.choice([0.055, 0.07, 0.085, 0.10, 0.115, 0.13, 0.16, 0.20, 0.26])
    reversal = rng.uniform(0.12, 1.18)
    hold = rng.choice([0, 1, 2, 3, 4, 5, 6, 8, 10, 12])
    zero_raw = rng.uniform(0.20, 0.78)
    zero_smooth = min(zero_raw, rng.uniform(0.18, 0.70))
    rate: float | None
    if rng.random() < 0.22:
      rate = None
    else:
      rate = rng.uniform(0.75, 6.5)
    second: float | None
    if rng.random() < 0.38:
      second = None
    else:
      second = rng.uniform(0.10, 0.62)
    deadband = rng.choice([0.0, rng.uniform(0.004, 0.035), rng.uniform(0.035, 0.085)])
    scale = rng.uniform(0.88, 1.08)
    hold_after = rng.choice([0, 0, 0, 1, 2, 3])
    if add(Candidate(
      name=f"{name_prefix}_{road_idx:07d}",
      family="road_budget_causal",
      unsafe_lab_only=False,
      unsafe_reason="",
      alpha=alpha,
      reversal_scale=reversal,
      zero_hold_frames=hold,
      zero_cross_raw=zero_raw,
      zero_cross_smooth=zero_smooth,
      rate_limit_per_sec=rate,
      second_alpha=second,
      deadband=deadband,
      torque_scale=scale,
      lookahead_frames=0,
      hold_after_reversal_frames=hold_after,
    )):
      road_idx += 1

  lab_idx = 0
  while lab_count < lab_target:
    alpha = rng.choice([rng.uniform(0.005, 0.08), rng.uniform(0.60, 1.35)])
    reversal = rng.uniform(0.02, 1.8)
    hold = rng.choice([0, 4, 8, 12, 18, 24, 32, 40])
    zero_raw = rng.uniform(0.35, 1.20)
    zero_smooth = rng.uniform(0.30, 1.10)
    rate = None if rng.random() < 0.16 else rng.choice([rng.uniform(0.04, 0.70), rng.uniform(7.0, 18.0)])
    second = None if rng.random() < 0.20 else rng.uniform(0.02, 0.90)
    deadband = rng.uniform(0.0, 0.28)
    scale = rng.choice([rng.uniform(0.45, 0.84), rng.uniform(1.11, 1.75), rng.uniform(0.70, 1.35)])
    lookahead = rng.choice([0, 1, 2, 5, 10, 20, 40, 60])
    hold_after = rng.choice([0, 2, 5, 8, 12, 18])
    reasons: list[str] = []
    if alpha < 0.035 or (rate is not None and rate < 0.50):
      reasons.append("excessive lag/rate clamp")
    if lookahead > 0:
      reasons.append("noncausal lookahead")
    if scale < 0.85 or scale > 1.10:
      reasons.append("torque scaling outside road budget")
    if hold >= 18 or deadband > 0.12:
      reasons.append("large hold/deadband")
    if not reasons:
      reasons.append("lab-only safety-margin violation")
    if add(Candidate(
      name=f"lab_{name_prefix}_{lab_idx:07d}",
      family="lab_break_safety",
      unsafe_lab_only=True,
      unsafe_reason="; ".join(reasons),
      alpha=alpha,
      reversal_scale=reversal,
      zero_hold_frames=hold,
      zero_cross_raw=zero_raw,
      zero_cross_smooth=zero_smooth,
      rate_limit_per_sec=rate,
      second_alpha=second,
      deadband=deadband,
      torque_scale=scale,
      lookahead_frames=lookahead,
      hold_after_reversal_frames=hold_after,
    )):
      lab_idx += 1

  return candidates[:total]


def write_candidates_tsv(path: Path, candidates: list[Candidate]) -> None:
  fields = list(asdict(candidates[0]).keys())
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore")
    writer.writeheader()
    for candidate in candidates:
      row = asdict(candidate)
      row["rate_limit_per_sec"] = "" if candidate.rate_limit_per_sec is None else candidate.rate_limit_per_sec
      row["second_alpha"] = "" if candidate.second_alpha is None else candidate.second_alpha
      writer.writerow(row)


def write_samples_tsv(replay_dir: Path, path: Path) -> dict[str, Any]:
  input_path = replay_dir / "controlsd_series.csv"
  route_ids: list[str] = []
  route_map: dict[str, int] = {}
  count = 0
  active = 0
  with input_path.open(newline="", encoding="utf-8") as inf, path.open("w", newline="", encoding="utf-8") as outf:
    reader = csv.DictReader(inf)
    writer = csv.writer(outf, delimiter="\t")
    writer.writerow(["route_idx", "route_id", "segment_index", "t_sec", "active", "torque_output", "desired_lateral_accel"])
    for row in reader:
      if row.get("source") != "replay_0_4_4":
        continue
      route_id = row["route_id"]
      if route_id not in route_map:
        route_map[route_id] = len(route_ids)
        route_ids.append(route_id)
      is_active = as_bool(row.get("active"))
      writer.writerow([
        route_map[route_id],
        route_id,
        row["segment_index"],
        as_float(row.get("t_sec")),
        1 if is_active else 0,
        as_float(row.get("torque_output")),
        as_float(row.get("desired_lateral_accel")),
      ])
      count += 1
      active += int(is_active)
  return {"samples": count, "active_samples": active, "route_ids": route_ids}


CPP_SOURCE = r'''
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

struct Sample {
  int route_idx;
  int segment_index;
  double t;
  bool active;
  double raw;
  double desired;
  int seg_key;
};

struct Candidate {
  std::string name;
  std::string family;
  bool unsafe;
  std::string unsafe_reason;
  double alpha;
  double reversal_scale;
  int zero_hold_frames;
  double zero_cross_raw;
  double zero_cross_smooth;
  bool has_rate;
  double rate_limit;
  bool has_second;
  double second_alpha;
  double deadband;
  double torque_scale;
  int lookahead_frames;
  int hold_after_reversal_frames;
};

struct Metrics {
  long samples = 0;
  double rate_p95 = 0.0;
  double jerk_p95 = 0.0;
  double sign_changes_per_min = 0.0;
  double delta_rms = 0.0;
  double delta_p95 = 0.0;
  double high_delta_rms = 0.0;
  double learning_score = 0.0;
  double road_score = 0.0;
  double rate_gain = 0.0;
  double jerk_gain = 0.0;
  double sign_gain = 0.0;
};

static std::vector<std::string> split_tab(const std::string &line) {
  std::vector<std::string> out;
  std::string cur;
  std::stringstream ss(line);
  while (std::getline(ss, cur, '\t')) out.push_back(cur);
  if (!line.empty() && line.back() == '\t') out.push_back("");
  return out;
}

static double to_double(const std::string &s, double def=0.0) {
  if (s.empty()) return def;
  char *end = nullptr;
  double v = std::strtod(s.c_str(), &end);
  if (end == s.c_str() || !std::isfinite(v)) return def;
  return v;
}

static int to_int(const std::string &s, int def=0) {
  return static_cast<int>(std::llround(to_double(s, def)));
}

static bool to_bool(const std::string &s) {
  return s == "1" || s == "True" || s == "true";
}

static double percentile(std::vector<double> &values, double q) {
  if (values.empty()) return 0.0;
  size_t idx = static_cast<size_t>(std::llround((q / 100.0) * static_cast<double>(values.size() - 1)));
  if (idx >= values.size()) idx = values.size() - 1;
  std::nth_element(values.begin(), values.begin() + idx, values.end());
  return values[idx];
}

static std::vector<Sample> read_samples(const std::string &path, std::vector<int> &seg_end) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("failed to open samples");
  std::string line;
  std::getline(f, line);
  std::vector<Sample> samples;
  std::unordered_map<std::string, int> seg_keys;
  while (std::getline(f, line)) {
    auto parts = split_tab(line);
    if (parts.size() < 7) continue;
    std::string seg_name = parts[1] + "--" + parts[2];
    auto it = seg_keys.find(seg_name);
    if (it == seg_keys.end()) {
      int next = static_cast<int>(seg_keys.size());
      it = seg_keys.emplace(seg_name, next).first;
    }
    samples.push_back({
      to_int(parts[0]),
      to_int(parts[2]),
      to_double(parts[3]),
      to_int(parts[4]) != 0,
      to_double(parts[5]),
      to_double(parts[6]),
      it->second
    });
  }
  seg_end.assign(samples.size(), 0);
  size_t start = 0;
  while (start < samples.size()) {
    size_t end = start + 1;
    while (end < samples.size() && samples[end].seg_key == samples[start].seg_key) end++;
    for (size_t i = start; i < end; i++) seg_end[i] = static_cast<int>(end);
    start = end;
  }
  return samples;
}

static std::vector<Candidate> read_candidates(const std::string &path) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("failed to open candidates");
  std::string line;
  std::getline(f, line);
  std::vector<Candidate> out;
  while (std::getline(f, line)) {
    auto p = split_tab(line);
    if (p.size() < 15) continue;
    Candidate c;
    c.name = p[0];
    c.family = p[1];
    c.unsafe = to_bool(p[2]);
    c.unsafe_reason = p[3];
    c.alpha = to_double(p[4], 1.0);
    c.reversal_scale = to_double(p[5], 1.0);
    c.zero_hold_frames = to_int(p[6]);
    c.zero_cross_raw = to_double(p[7]);
    c.zero_cross_smooth = to_double(p[8]);
    c.has_rate = !p[9].empty();
    c.rate_limit = to_double(p[9]);
    c.has_second = !p[10].empty();
    c.second_alpha = to_double(p[10]);
    c.deadband = to_double(p[11]);
    c.torque_scale = to_double(p[12], 1.0);
    c.lookahead_frames = to_int(p[13]);
    c.hold_after_reversal_frames = to_int(p[14]);
    out.push_back(c);
  }
  return out;
}

static Metrics score_candidate(const std::vector<Sample> &samples, const std::vector<int> &seg_end,
                               const Candidate &c, const Metrics *baseline) {
  bool initialized = false;
  double smooth = 0.0;
  double second = 0.0;
  int zero_hold = 0;
  int reversal_hold = 0;
  int current_seg = -1;
  bool prev_active = false;
  double prev_t = 0.0;
  double prev_out = 0.0;
  bool have_prev_rate = false;
  double prev_rate = 0.0;
  int prev_sign = 0;
  int sign_changes = 0;
  double duration = 0.0;
  double delta_sq = 0.0;
  double high_delta_sq = 0.0;
  long high_count = 0;
  std::vector<double> rates;
  std::vector<double> jerks;
  std::vector<double> delta_abs;
  rates.reserve(samples.size() / 2);
  jerks.reserve(samples.size() / 2);
  delta_abs.reserve(samples.size() / 2);

  auto reset = [&]() {
    initialized = false;
    smooth = 0.0;
    second = 0.0;
    zero_hold = 0;
    reversal_hold = 0;
  };

  for (size_t i = 0; i < samples.size(); i++) {
    const Sample &s = samples[i];
    if (s.seg_key != current_seg) {
      current_seg = s.seg_key;
      reset();
      prev_active = false;
      have_prev_rate = false;
      prev_sign = 0;
    }
    double out = s.raw;
    if (c.family != "baseline") {
      int look = static_cast<int>(i) + c.lookahead_frames;
      if (look >= seg_end[i]) look = seg_end[i] - 1;
      double raw = samples[look].raw * c.torque_scale;
      if (!s.active) {
        reset();
        out = s.raw;
      } else if (reversal_hold > 0) {
        reversal_hold--;
        initialized = true;
        out = smooth;
      } else {
        if (zero_hold > 0) {
          if (std::abs(raw) <= c.zero_cross_raw) {
            zero_hold--;
            smooth = 0.0;
            second = 0.0;
            initialized = true;
            out = 0.0;
            goto scored;
          }
          zero_hold = 0;
        }
        if (!initialized) {
          initialized = true;
          smooth = raw;
          second = raw;
          out = raw;
        } else {
          bool weak_zero_cross = raw * smooth < 0.0 && std::abs(raw) <= c.zero_cross_raw && std::abs(smooth) <= c.zero_cross_smooth;
          if (weak_zero_cross) {
            zero_hold = c.zero_hold_frames;
            reversal_hold = c.hold_after_reversal_frames;
            smooth = 0.0;
            second = 0.0;
            out = 0.0;
          } else {
            double alpha = c.alpha;
            if (raw * smooth < 0.0) alpha *= c.reversal_scale;
            double next_value = smooth + alpha * (raw - smooth);
            if (c.has_rate) {
              double dt = (i + 1 < samples.size() && samples[i + 1].seg_key == s.seg_key) ? samples[i + 1].t - s.t : 0.0;
              if (dt > 0.0 && dt <= 0.55) {
                double limit = c.rate_limit * dt;
                if (next_value > smooth + limit) next_value = smooth + limit;
                if (next_value < smooth - limit) next_value = smooth - limit;
              }
            }
            if (std::abs(next_value) < c.deadband && std::abs(raw) < c.zero_cross_raw) next_value = 0.0;
            smooth = next_value;
            if (c.has_second) {
              second += c.second_alpha * (smooth - second);
              out = second;
            } else {
              out = smooth;
            }
          }
        }
      }
    }
scored:
    if (!s.active) {
      prev_active = false;
      have_prev_rate = false;
      continue;
    }
    double delta = out - s.raw;
    delta_sq += delta * delta;
    delta_abs.push_back(std::abs(delta));
    if (std::abs(s.desired) >= 0.75 || std::abs(s.raw) >= 0.65) {
      high_delta_sq += delta * delta;
      high_count++;
    }
    int sign = out > 1e-6 ? 1 : (out < -1e-6 ? -1 : 0);
    if (prev_active) {
      double dt = s.t - prev_t;
      if (dt > 0.0 && dt <= 0.55) {
        double rate = (out - prev_out) / dt;
        rates.push_back(std::abs(rate));
        duration += dt;
        if (have_prev_rate) jerks.push_back(std::abs((rate - prev_rate) / dt));
        prev_rate = rate;
        have_prev_rate = true;
        if (sign && prev_sign && sign != prev_sign) sign_changes++;
      } else {
        have_prev_rate = false;
      }
    }
    if (sign) prev_sign = sign;
    prev_active = true;
    prev_t = s.t;
    prev_out = out;
  }
  Metrics m;
  m.samples = static_cast<long>(delta_abs.size());
  m.rate_p95 = percentile(rates, 95.0);
  m.jerk_p95 = percentile(jerks, 95.0);
  m.sign_changes_per_min = duration > 1e-9 ? static_cast<double>(sign_changes) * 60.0 / duration : 0.0;
  m.delta_rms = m.samples > 0 ? std::sqrt(delta_sq / static_cast<double>(m.samples)) : 0.0;
  m.delta_p95 = percentile(delta_abs, 95.0);
  m.high_delta_rms = high_count > 0 ? std::sqrt(high_delta_sq / static_cast<double>(high_count)) : 0.0;
  if (baseline != nullptr) {
    m.rate_gain = (baseline->rate_p95 - m.rate_p95) / std::max(baseline->rate_p95, 1e-9);
    m.jerk_gain = (baseline->jerk_p95 - m.jerk_p95) / std::max(baseline->jerk_p95, 1e-9);
    m.sign_gain = (baseline->sign_changes_per_min - m.sign_changes_per_min) / std::max(baseline->sign_changes_per_min, 1e-9);
    m.learning_score = 120.0 * m.rate_gain + 90.0 * m.jerk_gain + 24.0 * m.sign_gain - 55.0 * m.delta_rms - 20.0 * m.high_delta_rms;
    m.road_score = 120.0 * m.rate_gain + 90.0 * m.jerk_gain + 24.0 * m.sign_gain - 95.0 * m.delta_rms - 50.0 * m.delta_p95 - 50.0 * m.high_delta_rms;
    if (c.unsafe) m.road_score -= 250.0;
  }
  return m;
}

int main(int argc, char **argv) {
  if (argc < 4) {
    std::cerr << "usage: scorer samples.tsv candidates.tsv scores.tsv\n";
    return 2;
  }
  std::vector<int> seg_end;
  auto samples = read_samples(argv[1], seg_end);
  auto candidates = read_candidates(argv[2]);
  Candidate baseline_candidate;
  baseline_candidate.name = "baseline";
  baseline_candidate.family = "baseline";
  baseline_candidate.unsafe = false;
  Metrics baseline = score_candidate(samples, seg_end, baseline_candidate, nullptr);
  std::ofstream out(argv[3]);
  out << "candidate\tfamily\tunsafe_lab_only\tunsafe_reason\tlearning_score\troad_score\trate_gain\tjerk_gain\tsign_gain\trate_p95_abs\tjerk_p95_abs\tsign_changes_per_min\ttorque_delta_rms\ttorque_delta_p95_abs\thigh_demand_delta_rms\tsamples\talpha\treversal_scale\tzero_hold_frames\tzero_cross_raw\tzero_cross_smooth\trate_limit_per_sec\tsecond_alpha\tdeadband\ttorque_scale\tlookahead_frames\thold_after_reversal_frames\n";
  out << std::fixed << std::setprecision(8);
  for (size_t i = 0; i < candidates.size(); i++) {
    const auto &c = candidates[i];
    Metrics m = score_candidate(samples, seg_end, c, &baseline);
    out << c.name << '\t' << c.family << '\t' << (c.unsafe ? "True" : "False") << '\t' << c.unsafe_reason << '\t'
        << m.learning_score << '\t' << m.road_score << '\t' << m.rate_gain << '\t' << m.jerk_gain << '\t'
        << m.sign_gain << '\t' << m.rate_p95 << '\t' << m.jerk_p95 << '\t' << m.sign_changes_per_min << '\t'
        << m.delta_rms << '\t' << m.delta_p95 << '\t' << m.high_delta_rms << '\t' << m.samples << '\t'
        << c.alpha << '\t' << c.reversal_scale << '\t' << c.zero_hold_frames << '\t' << c.zero_cross_raw << '\t'
        << c.zero_cross_smooth << '\t' << (c.has_rate ? std::to_string(c.rate_limit) : "") << '\t'
        << (c.has_second ? std::to_string(c.second_alpha) : "") << '\t' << c.deadband << '\t' << c.torque_scale << '\t'
        << c.lookahead_frames << '\t' << c.hold_after_reversal_frames << '\n';
    if ((i + 1) % 10000 == 0) {
      std::cerr << "[" << (i + 1) << "/" << candidates.size() << "]\n";
    }
  }
  std::ofstream meta(std::string(argv[3]) + ".baseline.json");
  meta << "{\n"
       << "  \"samples\": " << baseline.samples << ",\n"
       << "  \"rate_p95_abs\": " << baseline.rate_p95 << ",\n"
       << "  \"jerk_p95_abs\": " << baseline.jerk_p95 << ",\n"
       << "  \"sign_changes_per_min\": " << baseline.sign_changes_per_min << "\n"
       << "}\n";
  return 0;
}
'''


def compile_engine(out_dir: Path) -> Path:
  source = out_dir / "large_scorer.cpp"
  binary = out_dir / "large_scorer"
  source.write_text(CPP_SOURCE, encoding="utf-8")
  compiler = shutil.which("clang++") or shutil.which("g++") or shutil.which("c++")
  if compiler is None:
    raise SystemExit("no C++ compiler found")
  subprocess.run([compiler, "-O3", "-std=c++17", str(source), "-o", str(binary)], check=True)
  return binary


def run_label(candidate_count: int) -> str:
  if candidate_count % 1_000_000 == 0:
    return f"{candidate_count // 1_000_000}m"
  if candidate_count % 1000 == 0:
    return f"{candidate_count // 1000}k"
  return str(candidate_count)


def previous_summary(path: Path) -> dict[str, Any]:
  manifest_path = path / "run_manifest.json"
  if not manifest_path.exists():
    return {}
  try:
    return json.loads(manifest_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return {}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
  fields: list[str] = []
  for row in rows:
    for key in row:
      if key not in fields:
        fields.append(key)
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)


def stream_score_summary(raw_scores_tsv: Path, csv_path: Path, previous_ids: set[str], top_n: int = 80) -> dict[str, Any]:
  top_road: list[tuple[float, int, dict[str, str]]] = []
  top_lab: list[tuple[float, int, dict[str, str]]] = []
  counts = {"total": 0, "road": 0, "lab": 0}
  previous_rows: dict[str, dict[str, str]] = {}
  fieldnames: list[str] | None = None
  idx = 0
  with raw_scores_tsv.open(newline="", encoding="utf-8") as inf, csv_path.open("w", newline="", encoding="utf-8") as outf:
    reader = csv.DictReader(inf, delimiter="\t")
    fieldnames = list(reader.fieldnames or [])
    writer = csv.DictWriter(outf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in reader:
      writer.writerow(row)
      counts["total"] += 1
      candidate = row.get("candidate") or ""
      unsafe = row.get("unsafe_lab_only") == "True"
      is_baseline = row.get("family") == "baseline"
      if candidate in previous_ids:
        previous_rows[candidate] = dict(row)
      if unsafe:
        counts["lab"] += 1
        score = as_float(row.get("learning_score"))
        heapq.heappush(top_lab, (score, idx, dict(row)))
        if len(top_lab) > top_n:
          heapq.heappop(top_lab)
      elif not is_baseline:
        counts["road"] += 1
        score = as_float(row.get("road_score"))
        heapq.heappush(top_road, (score, idx, dict(row)))
        if len(top_road) > top_n:
          heapq.heappop(top_road)
      idx += 1

  previous_ranks: dict[str, int | None] = {}
  previous_thresholds: dict[str, tuple[str, bool, float]] = {}
  for candidate, row in previous_rows.items():
    unsafe = row.get("unsafe_lab_only") == "True"
    field = "learning_score" if unsafe else "road_score"
    previous_thresholds[candidate] = (field, unsafe, as_float(row.get(field)))
  if previous_thresholds:
    greater_counts = {candidate: 0 for candidate in previous_thresholds}
    with raw_scores_tsv.open(newline="", encoding="utf-8") as inf:
      reader = csv.DictReader(inf, delimiter="\t")
      for row in reader:
        unsafe = row.get("unsafe_lab_only") == "True"
        if row.get("family") == "baseline":
          continue
        for candidate, (field, want_unsafe, threshold) in previous_thresholds.items():
          if unsafe == want_unsafe and as_float(row.get(field)) > threshold:
            greater_counts[candidate] += 1
    previous_ranks = {candidate: greater_counts[candidate] + 1 for candidate in previous_thresholds}
  for candidate in previous_ids:
    previous_ranks.setdefault(candidate, None)

  return {
    **counts,
    "top_road": [entry[2] for entry in sorted(top_road, key=lambda item: item[0], reverse=True)],
    "top_lab": [entry[2] for entry in sorted(top_lab, key=lambda item: item[0], reverse=True)],
    "previous_rows": previous_rows,
    "previous_ranks": previous_ranks,
    "fieldnames": fieldnames or [],
  }


def svg_bar(path: Path, title: str, labels: list[str], values: list[float], width: int = 1160, height: int = 620) -> None:
  height = max(height, 84 + 28 * len(labels))
  max_value = max((abs(v) for v in values), default=1.0)
  lines = [
    f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
    "<rect width='100%' height='100%' fill='#071018'/>",
    f"<text x='24' y='38' fill='#eaf4ff' font-family='Arial' font-size='22' font-weight='700'>{html.escape(title)}</text>",
  ]
  for idx, (label, value) in enumerate(zip(labels, values)):
    y = 70 + idx * 28
    bar = 0 if max_value <= 0 else abs(value) / max_value * 560
    color = "#6ee7a8" if value >= 0 else "#ff8a7a"
    lines.append(f"<text x='24' y='{y + 14}' fill='#d7e7f7' font-family='Arial' font-size='11'>{html.escape(label[:60])}</text>")
    lines.append(f"<rect x='470' y='{y}' width='{bar:.1f}' height='17' rx='3' fill='{color}'/>")
    lines.append(f"<text x='{480 + bar:.1f}' y='{y + 13}' fill='#d7e7f7' font-family='Arial' font-size='11'>{value:.3f}</text>")
  lines.append("</svg>\n")
  path.write_text("\n".join(lines), encoding="utf-8")


def write_report(path: Path, summary: dict[str, Any], baseline: dict[str, Any],
                 previous: dict[str, Any], previous_dir: Path, args: argparse.Namespace) -> None:
  road_ranked = summary["top_road"]
  lab_ranked = summary["top_lab"]
  prev_road = (previous.get("top_road_candidate") or {}).get("candidate")
  prev_lab = (previous.get("top_lab_candidate") or {}).get("candidate")
  lines = [
    f"# Brickpilot {run_label(args.candidates).upper()} VM Replay Steering Variant Sweep",
    "",
    f"Generated: `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
    "",
    "This is a large post-process sweep over the real Linux VM `controlsd` replay series. It carries forward previous road-budget candidates, then expands the search. Lab-only boundary breakers can be excluded from new candidate generation once their ceiling is understood while still being kept as ranked historical references.",
    "",
    "## Scope",
    "",
    f"- Input replay dir: `{args.replay_dir}`",
    f"- Previous comparison dir: `{previous_dir}`",
    f"- Candidate policies scored: {summary['total']}",
    f"- Road-budget candidates scored: {summary['road']}",
    f"- New lab-only safety-breaking candidates scored: {summary['lab']}",
    f"- Requested lab-only ratio: `{args.lab_ratio}`",
    f"- Baseline active samples: {baseline.get('samples')}",
    f"- Baseline torque-rate p95: `{baseline.get('rate_p95_abs')}`",
    f"- Baseline jerk p95: `{baseline.get('jerk_p95_abs')}`",
    f"- Baseline sign changes/min: `{baseline.get('sign_changes_per_min')}`",
    "",
    "## Previous Winners In This Field",
    "",
    f"- Previous best road candidate `{prev_road}` road-score rank: `{summary['previous_ranks'].get(prev_road) if prev_road else None}`.",
    f"- Previous best lab candidate `{prev_lab}` learning-score rank in this run: `{summary['previous_ranks'].get(prev_lab) if prev_lab else None}`.",
    f"- Previous best lab candidate remains historical reference from `{previous_dir}` if new lab candidates are excluded.",
    "",
    "## Top Road-Budget Candidates",
    "",
    "| Rank | Candidate | Road score | Rate gain | Jerk gain | Sign gain | Delta RMS | High-demand delta RMS |",
    "|---:|---|---:|---:|---:|---:|---:|---:|",
  ]
  for idx, row in enumerate(road_ranked[:30], start=1):
    lines.append(
      f"| {idx} | `{row['candidate']}` | {row['road_score']} | {row['rate_gain']} | {row['jerk_gain']} | "
      f"{row['sign_gain']} | {row['torque_delta_rms']} | {row['high_demand_delta_rms']} |"
    )
  if lab_ranked:
    lines.extend([
      "",
      "## Top New Lab-Only Boundary Breakers",
      "",
      "| Rank | Candidate | Learning score | Reason | Rate gain | Jerk gain | Sign gain | Delta RMS |",
      "|---:|---|---:|---|---:|---:|---:|---:|",
    ])
    for idx, row in enumerate(lab_ranked[:30], start=1):
      lines.append(
        f"| {idx} | `{row['candidate']}` | {row['learning_score']} | {row['unsafe_reason']} | "
        f"{row['rate_gain']} | {row['jerk_gain']} | {row['sign_gain']} | {row['torque_delta_rms']} |"
      )
  elif previous.get("top_lab_candidate"):
    lab_best = previous["top_lab_candidate"]
    lines.extend([
      "",
      "## Historical Lab-Only Boundary",
      "",
      f"- No new lab-only candidates were generated in this run. Previous lab ceiling remains `{lab_best.get('candidate')}`.",
      f"- Previous lab learning score: `{lab_best.get('learning_score')}`.",
      f"- Previous lab gains: rate `{lab_best.get('rate_gain')}`, jerk `{lab_best.get('jerk_gain')}`, sign `{lab_best.get('sign_gain')}`.",
    ])
  best = road_ranked[0]
  lines.extend([
    "",
    "## Read",
    "",
    f"- Best road-budget candidate: `{best['candidate']}`.",
    f"- It gets rate gain `{best['rate_gain']}`, jerk gain `{best['jerk_gain']}`, sign gain `{best['sign_gain']}`, torque delta RMS `{best['torque_delta_rms']}`.",
    "- If lab-only lookahead candidates dominate, the road-safe translation is upstream path/curvature smoothing or model-output conditioning, not future-looking actuator output.",
    "- If road-budget winners cluster around low alpha plus second-stage smoothing, the road-safe translation is stronger causal texture filtering than 0.4.4, with careful high-demand escape rules.",
    "",
    "## Files",
    "",
    "- `candidate_scores.csv`",
    "- `candidate_scores.raw.tsv`",
    "- `candidates.tsv`",
    "- `samples.tsv`",
    "- `road_score_chart.svg`",
    "- `run_manifest.json`",
  ])
  if lab_ranked:
    lines.insert(-1, "- `lab_learning_score_chart.svg`")
  path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--replay-dir", type=Path, default=DEFAULT_REPLAY_DIR)
  parser.add_argument("--previous-dir", type=Path, default=DEFAULT_PREVIOUS_DIR)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--out", type=Path, default=None)
  parser.add_argument("--candidates", type=int, default=100000)
  parser.add_argument("--lab-ratio", type=float, default=DEFAULT_LAB_RATIO)
  parser.add_argument("--seed", type=int, default=404004)
  parser.add_argument("--name-prefix", default="road100k")
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  label = run_label(args.candidates)
  out_dir = args.out or args.output_root / f"vm_replay_steering_variant_sweep_{label}_{timestamp}"
  out_dir.mkdir(parents=True, exist_ok=True)
  previous = previous_summary(args.previous_dir)
  previous_ids = {
    candidate.get("candidate")
    for candidate in (previous.get("top_road_candidate"), previous.get("top_lab_candidate"))
    if isinstance(candidate, dict) and candidate.get("candidate")
  }
  candidates = generate_candidates(args.candidates, args.previous_dir, args.lab_ratio, args.seed, args.name_prefix)
  candidates_tsv = out_dir / "candidates.tsv"
  samples_tsv = out_dir / "samples.tsv"
  raw_scores_tsv = out_dir / "candidate_scores.raw.tsv"
  print(f"writing {len(candidates)} candidates", flush=True)
  write_candidates_tsv(candidates_tsv, candidates)
  sample_meta = write_samples_tsv(args.replay_dir, samples_tsv)
  engine = compile_engine(out_dir)
  print("running compiled scorer", flush=True)
  subprocess.run([str(engine), str(samples_tsv), str(candidates_tsv), str(raw_scores_tsv)], check=True)
  print("streaming ranked summary", flush=True)
  summary = stream_score_summary(raw_scores_tsv, out_dir / "candidate_scores.csv", previous_ids)
  baseline = json.loads((Path(str(raw_scores_tsv) + ".baseline.json")).read_text(encoding="utf-8"))
  road_ranked = summary["top_road"]
  lab_ranked = summary["top_lab"]
  svg_bar(out_dir / "road_score_chart.svg", f"Top road-budget candidates in {label} sweep",
          [row["candidate"] for row in road_ranked[:30]], [float(row["road_score"]) for row in road_ranked[:30]])
  if lab_ranked:
    svg_bar(out_dir / "lab_learning_score_chart.svg", f"Top lab-only boundary breakers in {label} sweep",
            [row["candidate"] for row in lab_ranked[:30]], [float(row["learning_score"]) for row in lab_ranked[:30]])
  manifest = {
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "replay_dir": str(args.replay_dir),
    "previous_dir": str(args.previous_dir),
    "output_dir": str(out_dir),
    "candidate_count": summary["total"],
    "road_budget_count": summary["road"],
    "unsafe_lab_only_count": summary["lab"],
    "lab_ratio": args.lab_ratio,
    "seed": args.seed,
    "name_prefix": args.name_prefix,
    "sample_meta": sample_meta,
    "baseline": baseline,
    "top_road_candidate": road_ranked[0],
    "top_lab_candidate": lab_ranked[0] if lab_ranked else previous.get("top_lab_candidate"),
    "previous_ranks": summary["previous_ranks"],
  }
  (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
  write_report(out_dir / "report.md", summary, baseline, previous, args.previous_dir, args)
  latest = args.output_root / f"vm_replay_steering_variant_sweep_{label}_latest"
  if latest.exists() or latest.is_symlink():
    latest.unlink()
  latest.symlink_to(out_dir.name if out_dir.parent == args.output_root else out_dir, target_is_directory=True)
  print(out_dir)
  print(json.dumps({
    "candidate_count": summary["total"],
    "road_budget_count": summary["road"],
    "unsafe_lab_only_count": summary["lab"],
    "top_road_candidate": road_ranked[0],
    "top_lab_candidate": lab_ranked[0] if lab_ranked else previous.get("top_lab_candidate"),
    "previous_ranks": summary["previous_ranks"],
  }, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
