# Brickpilot Research Optimization R&D

This branch keeps the 0.6.0 tooling work measurable: optimize only when output
parity can be tested, and benchmark with real local data before claiming a win.

The full project audit record for this pass is
`docs/BRICKPILOT_OPTIMIZATION_AUDIT.md`.

## Current Hotspots

- `scripts/drive_tests/train_voice_labeler_0325.py` was mostly single-core.
  Activity Monitor can show low total CPU while one Python worker is pegged.
- CAN feature construction stored every sampled byte in per-window Python lists.
  On large route sets this wastes memory and burns CPU before CV even starts.
- Leave-one-route-out CV was serial by route, even when the machine has idle
  performance cores.
- The research UI catalog merge repeatedly filtered the full report/source list
  for each ride. That output was correct, but the cost grows with every new
  route and analysis export.

## Changes In This Branch

- CAN feature aggregation now uses running count/sum/min/max accumulators. It
  writes the same feature names and values: `*_count`, `*_mean`, and `*_range`.
- AUC calculation now uses rank statistics instead of positive-by-negative
  pairwise loops. Ties still count as half wins, matching the old definition.
- Target model training can run in parallel with `--train-workers`. By default
  the CLI uses the same conservative local worker count as CV.
- CV can run route holdouts in parallel with `--cv-workers`. By default the CLI
  uses `BRICKPILOT_CV_WORKERS` or a conservative local core count. Use
  `--train-workers 1 --cv-workers 1` for serial parity debugging.
- Catalog raw-import scans can stat route directories concurrently. Set
  `BRICKPILOT_RECONCILE_SCAN_CONCURRENCY=1` to force serial catalog scanning.
- Ride/catalog reconciliation now indexes reports and sources by route before
  merging, avoiding repeated full-catalog filters.

## Benchmarks

Measured on the local M4 MacBook Pro against the real Brickpilot DriveDB route
set, using `--fast-cross-validation --cv-max-targets 16`:

| Run | Workers | Real Time | Key Output Counts |
| --- | ---: | ---: | --- |
| Baseline before this branch | serial | 1166.71s | 129 CV rows, 120 trained targets, 768 all predictions, 90 review predictions, 1178 CAN candidates |
| Optimized CAN + parallel CV | train serial, CV 4 | 744.49s | same counts |
| Final optimized pass | train 4, CV 4 | 400.50s | same counts |

Final optimized stage timings:

| Stage | Seconds |
| --- | ---: |
| build_windows | 27.03 |
| select_model_features | 10.43 |
| train_models | 183.09 |
| cross_validate | 170.78 |
| score_predictions | 0.77 |
| write_artifacts | 7.58 |

Byte-for-byte parity was checked for:

- `can_signal_candidates.csv`
- `model_eval_leave_one_route.csv`
- `model_feature_summary.csv`
- `route_summary.csv`
- `test_route_predictions.csv`
- `test_route_predictions_all.csv`
- `training_windows.csv`
- `voice_label_corpus.csv`
- `voice_label_corpus.jsonl`
- `voice_label_taxonomy.json`

The reports and run summaries intentionally differ because they include output
paths, timestamps, worker counts, and benchmark timings.

Catalog scan:

```bash
npm run bench:catalog
```

Voice-labeler ML/CV baseline or optimized run:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/train_voice_labeler_0325.py \
  --config /Users/brick/.config/brickpilot/drive_db.toml \
  --stamp <benchmark-stamp> \
  --fast-cross-validation \
  --cv-progress \
  --cv-max-targets 16 \
  --timings
```

Serial parity check:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/train_voice_labeler_0325.py \
  --config /Users/brick/.config/brickpilot/drive_db.toml \
  --stamp <serial-stamp> \
  --fast-cross-validation \
  --cv-progress \
  --cv-max-targets 16 \
  --train-workers 1 \
  --cv-workers 1
```

## Verification Rules

- Compare generated CSV/JSON outputs for the same route set before treating a
  speedup as valid.
- Keep `--cv-workers 1` available for exact serial debugging.
- Run the TypeScript and Python tests after each optimization pass.
- Serve the UI from this repo on a separate port for review before merge.
