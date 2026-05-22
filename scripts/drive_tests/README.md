# Drive-Test Analysis Harness

This harness compares local comma/sunnypilot route logs across driving models and settings without modifying any driving or control code. It reads route segments through `openpilot.tools.lib.logreader.LogReader`, stores metrics in SQLite/CSV, writes a Markdown report, and generates PNG plots.

VINs are redacted in generated outputs. `carFw` is not dumped; reports only include high-level firmware counts/markers.

## Route Catalog

Edit `scripts/drive_tests/route_catalog.yaml` to add routes, notes, settings, warmup skip time, and thresholds. The default `warmup_skip_sec` is 90 seconds so each drive can be scored as both all-data and post-calibration warmup.

The North Nevada route is weighted lower by default because it includes an intentionally aggressive low-speed neighborhood turn.

## Commands

Authenticate if needed:

```bash
python3 tools/lib/auth.py
```

Run all analysis:

```bash
cd /path/to/brickpilot-research
source .venv/bin/activate
python3 scripts/drive_tests/analyze_routes.py \
  --catalog scripts/drive_tests/route_catalog.yaml \
  --out ~/BrickpilotDriveDB/analysis_exports/drive_tests \
  --max-segments 80
```

Generate report:

```bash
python3 scripts/drive_tests/report.py \
  --db ~/BrickpilotDriveDB/analysis_exports/drive_tests/results.sqlite \
  --out ~/BrickpilotDriveDB/analysis_exports/drive_tests/report.md
```

Generate plots:

```bash
python3 scripts/drive_tests/plots.py \
  --db ~/BrickpilotDriveDB/analysis_exports/drive_tests/results.sqlite \
  --out ~/BrickpilotDriveDB/analysis_exports/drive_tests/plots
```

Sweep Brickpilot longitudinal policy candidates over existing prelim exports:

```bash
python3 scripts/drive_tests/sweep_longitudinal_candidates.py
```

The sweep writes ranked candidate CSVs, a Markdown report, and an SVG chart under
`~/BrickpilotDriveDB/analysis_exports/longitudinal_candidate_sweep_*`.
It is a qlog/shadow-sample policy proxy, not a full process replay, and is meant
to rank installable candidates before promoting them into a comma build.

Normalize imported voice bookmarks into reviewed DriveDB labels:

```bash
PYTHONPATH=/path/to/brickpilot-research python3 \
  scripts/drive_tests/normalize_review_voice_labels.py <route-id> \
  --config ~/.config/brickpilot/drive_db.toml \
  --output-dir ~/BrickpilotDriveDB/analysis_exports/voice_review_normalization_<stamp> \
  --finish
```

Use repeated `--alpha-long on/off` values when route metadata is stale and the
driver explicitly identifies the settings. The normalizer applies the voice
alignment calibrator, splits mixed phrases into atomic labels, tags label phase
and family, and can finish the manual review job after writing labels.

Generate stop-stack/event-card reports from a preliminary route export:

```bash
PYTHONPATH=/path/to/brickpilot-research python3 \
  scripts/drive_tests/analyze_stop_stack_events.py \
  ~/BrickpilotDriveDB/analysis_exports/prelim_<stamp>_<route-id> \
  --route-id <route-id>
```

The event-card report includes active stop source/reason/mode crosstabs,
required-decel validity, final-stop blocked reasons, and current lead-pacing
telemetry when present in `brickpilotShadow`.

Compare one Alpha Long ON route against one Alpha Long OFF/native reference:

```bash
python3 scripts/drive_tests/compare_alpha_long_native_pacing.py \
  --alpha-on ~/BrickpilotDriveDB/analysis_exports/prelim_alpha_on_<route-id> \
  --alpha-off ~/BrickpilotDriveDB/analysis_exports/prelim_alpha_off_<route-id> \
  --output-dir ~/BrickpilotDriveDB/analysis_exports/alpha_long_native_compare_<stamp>
```

This writes a small Markdown decision report plus CSVs for telemetry and labels.
It is intended for native/SCC mimic work such as 0.5.7.

Sweep Brickpilot lateral steering smoothness candidates over DriveDB qlogs:

```bash
python3 scripts/drive_tests/sweep_lateral_steering_candidates.py
```

The lateral sweep scores output-smoothing candidates against observed torque
rate, torque jerk, steering weak-label windows, and command-delta distortion. It also
mines steering-label CAN correlations and writes a Markdown R&D report plus CSV
tables under `~/BrickpilotDriveDB/analysis_exports/lateral_steering_sweep_*`.
This is a command-space digital-mile proxy; it ranks candidates for later road
A/B testing and does not prove full `controlsd` replay behavior.

## Voice Bookmarks

For local, in-drive spoken/manual bookmarks that can be imported into the generated manual labeler drafts, see [`VOICE_BOOKMARKS.md`](VOICE_BOOKMARKS.md):

```bash
python3 scripts/drive_tests/voice_bookmarker.py record --name commute-test
python3 scripts/drive_tests/voice_bookmarker.py import <voice-session-dir> --labeler-dir <manual_drive_labeler-dir>
```

The helper stays local-only and supports manual transcript fallback when STT/audio dependencies are absent.

## Outputs

The analyzer creates:

- `~/BrickpilotDriveDB/analysis_exports/drive_tests/results.sqlite`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/summary.csv`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/segment_summary.csv`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/lateral_events.csv`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/pinned_bursts.csv`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/acceleration_events.csv`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/stop_go_events.csv`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/report.md`
- `~/BrickpilotDriveDB/analysis_exports/drive_tests/plots/*.png`

## Segment Discovery

For each route, the analyzer tries segment `0..N` until `missing_stop_after` consecutive missing logs are found, or `--max-segments` is reached. It tries rlogs first. By default it uses the LogReader `/a` selector as a qlog fallback when an rlog is missing and records a warning because qlog-derived metrics are lower fidelity.

Disable qlog fallback with:

```bash
python3 scripts/drive_tests/analyze_routes.py --no-qlog-fallback
```

## Metrics

The harness records all-data and post-warmup metrics for each route and segment. It also splits route-level metrics by speed regime:

- `stopped`: below 1 mph
- `low_neighborhood`: 1 to 20 mph
- `neighborhood`: 20 to 35 mph
- `backroad`: 35 to 55 mph
- `highway`: above 55 mph

Turn demand is binned by absolute desired lateral acceleration:

- `straightish`: below 0.25
- `mild_curve`: 0.25 to 0.75
- `medium_curve`: 0.75 to 1.25
- `hard_curve`: above 1.25

Pinned lateral output uses `abs(torqueState.output) >= 0.98`. Pinned bursts are grouped when consecutive pinned samples are separated by no more than 0.20 seconds.

Longitudinal catch-up events start when clean longActive, no-lead-relaxed samples have a set-speed deficit of at least 5 mph while already moving above 3 mph. Stop-go events start below 0.5 mph or at standstill and end above 5 mph.
