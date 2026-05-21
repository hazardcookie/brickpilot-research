# Brickpilot Voice Bookmarks (Phase 1)

`voice_bookmarker.py` is a local-only helper for capturing spoken/manual drive notes and importing them into the generated `manual_drive_labeler` draft labels.

It is meant for the MacBook Pro workflow, but it has no cloud dependency and can run anywhere the repo is present. If a local audio backend is missing, it still records timestamped typed/manual bookmarks and accepts a transcript import later.

## Record during a drive

```bash
cd /path/to/brickpilot-research
python3 scripts/drive_tests/voice_bookmarker.py record --name commute-test
```

Behavior:

- Starts local mic capture to `mic.wav` when `ffmpeg` (macOS `avfoundation`) or `sox/rec` is available.
- Creates `~/BrickpilotDriveDB/voice_bookmarks/<timestamp>_<name>/`.
- While running, type a short label and press Enter whenever something happens.
- Blank Enter creates a plain `bookmark` row.
- `/note text` appends session notes; `/q` stops.

Useful options:

```bash
# text/bookmark only, no mic attempt
python3 scripts/drive_tests/voice_bookmarker.py record --name test --no-audio

# specify a macOS ffmpeg avfoundation device string
python3 scripts/drive_tests/voice_bookmarker.py record --backend ffmpeg --device ':0'
```

## Manual transcript fallback

If STT is unavailable, edit a timestamped text file by hand:

```text
00:12.5 late brake behind lead
01:03 nice smooth turn
90.0 engine kicked on / regen changed
```

Convert it to editable JSONL:

```bash
python3 scripts/drive_tests/voice_bookmarker.py transcribe-template \
  ~/BrickpilotDriveDB/voice_bookmarks/20260513_210000_commute-test \
  --input /tmp/drive-transcript.txt
```

The importer accepts `.txt`, `.json`, or `.jsonl` transcripts. Plain text rows support `MM:SS text`, `HH:MM:SS text`, or `seconds text`.

## Import into manual_drive_labeler drafts

First build the manual labeler for the route as usual:

```bash
python3 scripts/drive_tests/manual_drive_labeler.py --route latest --no-video
```

Then import the voice bookmarks into the selected/default job:

```bash
python3 scripts/drive_tests/voice_bookmarker.py import \
  ~/BrickpilotDriveDB/voice_bookmarks/20260513_210000_commute-test \
  --labeler-dir ~/BrickpilotDriveDB/labeler_outputs/manual_drive_labeler_<route_slug>
```

This appends rows to the target job's `drive_labels.jsonl` using the same draft shape as the UI (`job_id`, `route_id`, `start_time_sec`, `end_time_sec`, `label`, `severity`, `tags`, `notes`). It also writes `voice_bookmark_import_manifest.json` next to the draft file.

Options:

- `--job-id <id>`: import into a specific inbox job from `drive_jobs.json`.
- `--offset-sec <seconds>`: shift all bookmarks if the recorder started before/after the route clock.
- `--window-sec <seconds>`: label window centered on each bookmark (default 3s).
- `--dry-run`: print the import manifest without writing drafts.

## Privacy

- Audio, transcripts, manifests, and labels stay local.
- No raw route logs/audio are uploaded by this tool.
- Generated artifacts are intended to live under `~/BrickpilotDriveDB/`, not in this repo.
