# Brickpilot Research Optimization Audit

Date: 2026-05-23
Branch: `optimization`
Target: `main`
Version label: `0.5.8`

## Scope

This audit covers the R&D optimization pass for `brickpilot-research`, focused
on the local tools and UI stack used for route ingest, DriveDB reconciliation,
voice-label ML, CAN candidate analysis, and dashboard serving.

The original performance symptom was that long-running ML/CV data crunching
reported a maxed CPU while the machine looked mostly idle. The root cause was
not lack of hardware headroom. Several hot paths were single Python workers or
serial scans, so one core could be saturated while the rest of the M4 remained
available.

## Changes Audited

- Updated the research UI/version metadata from `0.5.6` to `0.5.8`.
- Reworked CAN feature aggregation in
  `scripts/drive_tests/train_voice_labeler_0325.py` from per-window lists of
  byte samples to streaming count/sum/min/max accumulators.
- Added conservative multi-process target training with `--train-workers`.
- Added conservative multi-process leave-one-route-out CV with `--cv-workers`.
- Replaced pairwise AUC loops with rank-based AUC that preserves tie behavior.
- Added stage timing output with `--timings`.
- Optimized catalog reconciliation by indexing reports and sources by route
  before merging rides.
- Added concurrent raw-import stat scanning for catalog builds.
- Added `npm run bench:catalog` for repeatable UI/catalog scan timing.
- Fixed `tools/verify_split_tools.py` path ordering so this repo's
  `scripts.drive_tests` package wins even when `PYTHONPATH` already contains
  the repo.

## Benchmark Summary

Benchmarks used the real local Brickpilot DriveDB route set and the same
voice-labeler command shape:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/train_voice_labeler_0325.py \
  --config /Users/brick/.config/brickpilot/drive_db.toml \
  --stamp <stamp> \
  --fast-cross-validation \
  --cv-progress \
  --cv-max-targets 16
```

| Run | Workers | Real Time | Improvement vs Baseline |
| --- | ---: | ---: | ---: |
| Baseline before optimization | serial | 1166.71s | baseline |
| Streaming CAN + parallel CV | train serial, CV 4 | 744.49s | 36.2% faster |
| Final optimized pass | train 4, CV 4 | 400.50s | 65.7% faster |

The final pass is 2.91x faster than baseline by wall time.

Final optimized stage timing:

| Stage | Seconds | Share of Final Runtime |
| --- | ---: | ---: |
| build_windows | 27.03 | 6.7% |
| select_model_features | 10.43 | 2.6% |
| train_models | 183.09 | 45.7% |
| cross_validate | 170.78 | 42.6% |
| score_predictions | 0.77 | 0.2% |
| write_artifacts | 7.58 | 1.9% |

The largest single improvement was setup work before CV: the old run took
roughly 12 minutes before CV progress appeared, while the optimized CAN/window
setup finished in 27.03 seconds.

Catalog scan benchmark:

```bash
npm run bench:catalog
```

Measured result after this pass: 1831ms over `/Users/brick/BrickpilotDriveDB`,
with 5000 reports, 2768 sources, 25377 scanned files, and truncation at the
configured report cap.

## Output Parity

The optimized final run matched the baseline byte-for-byte for the generated
data artifacts that drive downstream analysis:

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

Headline counts also matched:

- 129 CV rows
- 120 trained targets
- 768 all test-route predictions
- 90 review predictions
- 1178 CAN candidate rows

`report.md` and `run_summary.json` intentionally differed because they include
timestamps, output paths, worker counts, and timing metadata.

## Verification

Validation run for this pass:

```bash
npm test
npm run typecheck
npm run build
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python -m pytest scripts/drive_tests/tests
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
BRICKPILOT_TOOLS_ROOT=/Users/brick/comma-dev/brickpilot-research \
BRICKPILOT_REPO_ROOT=/Users/brick/comma-dev/brickpilot \
BRICKPILOT_DATA_ROOT=/Users/brick/BrickpilotDriveDB \
BRICKPILOT_DRIVE_DB_CONFIG=/Users/brick/.config/brickpilot/drive_db.toml \
/Users/brick/.config/brickpilot/python tools/verify_split_tools.py
npm run bench:catalog
```

Results:

- Vitest: 25 passed.
- TypeScript: clean.
- Vite build: clean.
- Python drive-test suite: 56 passed.
- Split-tools verification: passed.
- Catalog benchmark: completed in 1831ms.
- UI served from this repo on `http://127.0.0.1:8792` and LAN
  `http://192.168.1.88:8792` during review.

## Tradeoffs And Follow-Up

- Parallel training/CV uses more memory while workers are live. The default is
  intentionally conservative at 4 workers to fit the 24 GB MacBook Pro without
  consuming all headroom.
- Serial parity remains available with `--train-workers 1 --cv-workers 1`.
- The final remaining runtime is dominated by target training and CV scoring.
  Further optimization should profile feature-vector storage and model training
  reuse before changing model behavior.
- No generated raw data, private DB dumps, route video, or local analysis
  artifacts were added to git.
