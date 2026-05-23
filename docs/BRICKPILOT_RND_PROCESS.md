# Brickpilot R&D Process

This repo owns the local data, label, ML, and analysis loop for Brickpilot. Raw
routes, qlogs, video, audio, SQLite/Postgres data, and generated reports stay
outside git under `~/BrickpilotDriveDB` unless explicitly exported as a small
sanitized artifact.

## Current Repos

- `brickpilot-research`: local UI, DriveDB tools, label normalization, ML, CAN
  analysis, event cards, and process docs.
- `brickpilot`: deployable openpilot/sunnypilot fork used for staging and comma
  installs.

Use `BRICKPILOT_REPO_ROOT=/Users/brick/comma-dev/brickpilot` and
`BRICKPILOT_PYTHON=/Users/brick/.config/brickpilot/python` for tools that need
the runtime checkout.

## 0.5.8 Tooling Optimization Audit

The 0.5.8 research tooling pass is recorded in
`docs/BRICKPILOT_OPTIMIZATION_AUDIT.md`, with benchmark commands, output parity
checks, and verification results. The key measured result was reducing the
voice-labeler fast-CV run from 1166.71s to 400.50s on the real local DriveDB
route set, a 65.7% wall-time reduction and 2.91x speedup with byte-for-byte
parity for the generated CSV/JSON/JSONL analysis artifacts.

Keep the conservative worker defaults unless memory pressure says otherwise:
use `--train-workers 1 --cv-workers 1` for serial parity debugging, or the
default 4-worker path for normal M4 MacBook Pro R&D runs.

## Standard Post-Drive Flow

1. Ingest the route through the UI or ingest scripts.
2. Import voice bookmarks into the manual labeler.
3. Run voice normalization and finish review if the user asks Codex to do it:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/normalize_review_voice_labels.py \
  <route-id> [<route-id> ...] \
  --config /Users/brick/.config/brickpilot/drive_db.toml \
  --output-dir ~/BrickpilotDriveDB/analysis_exports/<normalization-name> \
  --finish
```

Use `--alpha-long on/off` when the user explicitly says the route metadata is
stale. The normalizer records the trusted setting into route metadata and tags
labels with `alpha_long_on` or `alpha_long_off`.

4. Run preliminary route analysis:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/analyze_test_route_prelim.py <route-id> \
  --config /Users/brick/.config/brickpilot/drive_db.toml \
  --output-dir ~/BrickpilotDriveDB/analysis_exports/prelim_<name>_<route-id>
```

5. Generate stop-stack event cards:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/analyze_stop_stack_events.py \
  ~/BrickpilotDriveDB/analysis_exports/prelim_<name>_<route-id> \
  --route-id <route-id>
```

6. Retrain or cross-check weak labels after reviewed routes are folded in:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/brick/comma-dev/brickpilot-research \
/Users/brick/.config/brickpilot/python \
  scripts/drive_tests/train_voice_labeler_0325.py \
  --config /Users/brick/.config/brickpilot/drive_db.toml \
  --stamp <stamp> \
  --fast-cross-validation \
  --cv-progress \
  --cv-max-targets 50
```

The weak labeler is an inspection and ranking tool, not ground truth. Voice
review labels are stronger than predictions, and physical guards should win over
model confidence for stationary, stop-complete, missed-stop, and follow/pacing
claims.

## Label Normalization Rules

The normalizer turns messy spoken phrases into multiple atomic labels. Mixed
phrases are intentionally split by meaning, for example:

- "good rolling follow" -> `rolling_follow_good`, `pacing_good`
- "overbraked rolling traffic" -> `overbraked_rolling_traffic`,
  `unnecessary_braking`, `braking_early`
- "follow distance too far" -> `follow_distance_too_far`,
  `follow_distance_bad`
- "driver brake needed" -> `driver_brake_intervention`,
  `human_intervention_brake`, `braking_bad`
- "foot off gas" -> `driver_no_gas`, `phev_regen_coast`,
  `regen_light_coast`

The default alignment uses the voice alignment calibrator plus label-family
windows. State labels get tighter windows; stop, lead, and pacing labels keep
enough context to preserve the event phase.

## 0.5.6 Alpha ON/OFF Routes

The 0.5.6 comparison routes were normalized and finalized with trusted settings:

| Route | Trusted Setting | Atomic Labels | Voice Bookmarks | Notes |
| --- | --- | ---: | ---: | --- |
| `00000207--a8c307d230` | Alpha Long ON | 60 | 27 | Brickpilot active stop stack and pacing complaints/good spots |
| `00000209--f9ffcb0581` | Alpha Long OFF | 118 | 60 | Native/SCC reference with much better subjective pacing/rolling stops |

Generated reports:

- `~/BrickpilotDriveDB/analysis_exports/prelim_057_alpha_on_00000207--a8c307d230`
- `~/BrickpilotDriveDB/analysis_exports/prelim_057_alpha_off_00000209--f9ffcb0581`

Key 0.5.6 Alpha ON telemetry:

- `longitudinalAssistActive` nonzero around 4.1%.
- `stopActive` nonzero around 0.8%.
- active stop assist was geometry-valid: required decel valid for all active
  samples in the event-card pass.
- Active stop source mix was lead-heavy: about 93.6% lead and 6.4%
  creep/final-hold.
- Active stop reason mix was mostly planner-debt and final-stop-commit, with
  smaller controller-underbrake and brake-debt shares.

Key Alpha OFF/native reference telemetry:

- Brickpilot live assist was inactive, as expected.
- Route had more stopped/low-speed exposure and more brake pedal activity.
- User-reported subjective pacing/final-stop feel was much better, especially
  with aggressive following distance.
- CAN brake-domain bytes `0x065.b3/b9/b11/b12/b14`, `0x0FA.b4/b7`, and
  `0x0BA.b14` remain high-priority context candidates. `0x0BA.b14` is not pure
  stationary/Auto Hold; treat it as brake/hold context unless speed/standstill
  confirms stationary.

## 0.5.7 Build Direction

0.5.7 is the native-pacing mimic build. It should not raise braking authority.
It keeps the 0.5.6 final-stop arbitration and adds a small signed lead-pacing
path for Alpha Long ON:

- If the radar lead is rolling and the gap is too far, add a small positive
  accel nudge so Alpha Long ON feels less like stock distance 4.
- If the radar lead is close or mildly closing, add a small negative pacing nudge
  without entering full final-stop commitment.
- If a true stop is urgent, final-stop or urgent stop logic keeps priority.
- Driver gas/brake, steering override, high lateral demand, PHEV brake/regen
  context, radar/model mismatch, DEC/SCC turns, and invalid states remain hard
  blocks.
- New telemetry fields in `brickpilotShadow`: `leadPacingMode`,
  `leadPacingTargetGap`, `leadPacingGapError`, `leadPacingVRel`,
  `leadPacingAssistDelta`, and `leadPacingJerkLimited`.

0.5.7 should be judged by fewer `follow_distance_too_far`, `pacing_bad`,
`pacing_bursty`, `driver_gas_after_brake`, and `overbraked_rolling_traffic`
labels, without increasing `driver_brake_intervention`, `missed_stop`, or
invalid active stop assist.

## 0.5.7 Post-Drive Read

Three routes are the current pacing reference set:

| Route | Build | Alpha Long | Model | Label state |
| --- | --- | --- | --- | --- |
| `0000020c--59eadd34bc` | 0.5.6 | OFF | nnv2 | telemetry-only normal drive |
| `0000020f--2ecc9b6e08` | 0.5.6 | ON | nnv2 | telemetry-only normal drive |
| `00000213--ab5b813126` | 0.5.7 | ON | WMI V12 | reviewed 48-label test drive |

Trust the user-reported settings for these routes. Ingest metadata can reflect
the latest comma settings rather than the per-route settings.

The key 0.5.7 finding is that lead-pacing context is being classified but not
yet acted on:

- `leadPacingMode` was nonzero on about 25% of the 0.5.7 route.
- `leadPacingAssistDelta` stayed at 0.0 for the route.
- Active stop assist was lead-backed and valid, but mostly came from
  `planner_debt`, `regen_light_not_enough`, and `urgent_brake_recovery`.
- 0.5.7 did not materially use broad `final_stop_commit` on the reviewed route,
  which helps avoid overcommit but leaves the drive feeling unlike native SCC.

The 0.5.8 R&D direction should be a shadow/route-sweep over target-gap,
closing-speed, and gating thresholds that makes small signed pacing deltas live
in narrow conditions. Do not raise stop authority as the default next move.
Success is a native-like pacing shape: closer useful following, fewer
`pacing_bad`/`follow_distance_too_far` labels, fewer driver interventions, and
no missed-stop regression.

## 0.5.8 Implementation Read

0.5.8 is the native rolling-lead micro-pacing build:

- No lateral changes.
- No stop-authority increase.
- No broader `final_stop_commit`.
- No CAN candidate promoted to brake authority.
- Live control stays in the existing post-planner/pre-LongControl Brickpilot
  shaping path.

The implemented 0.5.8 live policy uses a speed-shaped target gap and deadband,
then applies only small signed deltas in valid rolling-lead contexts:

- Positive pacing cap: `+0.10 m/s^2`.
- Negative/coast pacing cap: `-0.16 m/s^2`.
- Valid lead geometry: 6-70 m, ego speed at least 2 m/s, lead speed at least
  2 m/s.
- High-speed taper: begins above 18 m/s, zero above 28 m/s.
- Entry hold: 0.35 s before first nonzero live pacing.
- Rate limits: slower positive ramp, faster negative/release ramp.

Hard live blocks:

- driver gas, brake, or steering override
- stop-stack priority, final-stop commit, urgent TTC, or active stop assist
- high lateral demand or steering guard suppression
- clear `0x065` brake-blend context
- stationary/Auto Hold context
- DEC/SCC turn context, radar/model mismatch, FCW/model-brake context

PHEV CAN handling in 0.5.8:

- `0x065.b3/b9/b11+b12/b14` forms the clear brake-blend context used as a live
  pacing veto.
- `0x0FA.b4` is logged and read as signed/wrapped energy context, not as raw
  unsigned brake magnitude.
- `0x0FA.b7` is light regen/energy context. It may soften positive pacing or
  block it when paired with actual deceleration, but it is not enough by itself
  to prove braking.
- `0x0BA.b14` remains conditional context only and should not be treated as a
  pure stationary/Auto Hold signal without low-speed/standstill confirmation.

New `brickpilotShadow` telemetry for audit:

- `leadPacingPolicyVersion`
- `leadPacingRawDelta`
- `leadPacingLiveDelta`
- `leadPacingDeltaAfterRateLimit`
- `leadPacingGateMask`
- `leadPacingBlockReason`
- `leadPacingVLead`
- `leadPacingTtc`
- `leadPacingModeAge`
- `leadPacingBrakeBlendContext`
- `leadPacingRegenSoftContext`
- `leadPacingRegenHardContext`
- `leadPacingStopPriority`
- `leadPacingDriverOverride`
- `leadPacingHighLatDemand`
- `leadPacingAppliedToATarget`

The preflight script is
`scripts/drive_tests/sweep_lead_pacing_058.py`. It compares installable and
shadow pacing variants across the current Alpha Long ON/OFF and 0.5.7 WMI
routes. The current installable candidate passes the intended micro-pacing
range on the lead-active Alpha ON routes:

| Route | Setting | Active fraction of eligible rolling samples | Read |
| --- | --- | ---: | --- |
| `0000020f--2ecc9b6e08` | 0.5.6 Alpha ON, nnv2 | 0.174 | within target |
| `00000213--ab5b813126` | 0.5.7 Alpha ON, WMI V12 | 0.144 | within target; fixes 444 of 2995 prior mode/nonzero-gap samples |
| `00000207--a8c307d230` | 0.5.6 Alpha ON, WMI-ish reviewed | 0.150 | within target |

The Alpha Long OFF reference prelims show no Brickpilot rolling-pacing eligible
samples, which is expected because the native/OEM path is the reference shape
rather than Brickpilot control output.

## 0.6.0 Target

0.6.0 should be a Stop Stack release, not a knob release:

- native-reference comparison reports for Alpha ON/OFF and stock/base paths
- validated event cards for bad stops, rolling follow, and final stops
- shadow low-speed following profiles
- CAN route cards for brake pedal, regen coast, Auto Hold, creep, and engine
  state transitions
- hard weak-label post-filters for stationary and stop-complete semantics
- phase-aware steering monitor retained as a regression guard, not a primary
  tuning target

The safety boundary stays boring: normal LongControl path, bounded deltas,
driver override blocks, valid source gates, no panda/opendbc safety bypasses,
and no broad no-lead stoplight heroics.
