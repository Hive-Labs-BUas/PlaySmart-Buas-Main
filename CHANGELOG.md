# Changelog

All changes to the capture toolkit, with the reason for each. Apache 2.0 asks that
modified files say so; every file touched carries a `Modified 2026-09` / `Added 2026-09`
notice at the top, and this file is the index.

## Iteration 5 — 2026-09 — League of Legends port of the capture rig

Team: Hristiyan Georgiev (BUas ADS&AI year 3) with two teammates; mentor Bram Heijligers;
infrastructure Uther Tlas. Branch `feature/league-collection`, based on `Dev`.

### Why

An audit of the 2024–25 recordings (four capture PCs, 19.6 GB, Nov 2024 – Nov 2025)
found 364 minutes of video with 171 minutes of content, several 44-byte WAV files, gaze
files 2.6 hours away from the session named on them, and eleven identical merged CSVs on
one machine. Each traced to a mechanism, not to bad luck: recorders were killed with
`terminate()` before they wrote; files were paired afterwards by "newest file in the
folder"; video was started by hand and captured the wrong window. The changes below
remove those mechanisms rather than patch around them.

### Capture — what a session now is

| file | change | reason |
|---|---|---|
| `src/session.py` **new** | One session id per game, passed to every recorder through the environment; every stream names its file `<sid>_<stream>.<ext>` at creation. A stop *signal* (`stop_requested()`) the recorders poll. Loads `.env`. | Ends the newest-file pairing that attached one session's data to another. Gives the buffering recorders a way to be *asked* to stop. |
| `src/key_listener.py` rewritten | Capture brackets the game, not the operator: F7 arms, the recorders start when League's local API appears and stop when it disappears. Stop = request → wait per recorder → terminate only on timeout. End-of-session report per stream; a clean exit with an empty file is reported as such. `PLAYSMART_SKIP` drops absent sensors from both the start list and the expectations. | `terminate()` two seconds after F12 is what truncated gaze, EDA and audio. With automatic stopping there is no F12 to hook, so graceful stop was a prerequisite. A report line that is always red gets ignored. |
| `src/liveclient_recorder.py` **new** | Polls League's Live Client Data API (`127.0.0.1:2999`) at 2 Hz: game state with both `unix_time_ms` and `game_time_s` per row, events deduplicated on `EventID`, `<sid>_meta.json` with Riot ID, champion, mode, map, bots. | The clock mapping is what lets a gaze sample be placed at "14:32 into the game". No key, no rate limit; answers in Practice Tool and customs, which Match-V5 does not. Replaces the free-text game dropdown that produced nine spellings of two game names. |
| `src/obs_recorder.py` **new** | Starts/stops OBS over obs-websocket 5 on the same signal as every other stream. Frame zero located from `GetRecordStatus.outputDuration` (median of bracketed samples, zero-duration samples discarded); written to `<sid>_video.json`. File claimed by the path `StopRecord` reports and moved to `data/video/<sid>_video.<ext>`. Degrades to "no video" on every failure path without touching the other streams. `PLAYSMART_OBS=0` disables. | Video was the last stream started by hand and named by OBS. A file's mtime says when it was written, not when frame zero was captured; without `t0` the video cannot be placed on the game clock. |
| `src/eye_tracking_script.py` | Session-named file, stop signal, save in `finally`. A missing tracker refuses to record; `PLAYSMART_MOCK_GAZE=1` writes rows flagged `gaze_valid=False`. Two commented-out legacy copies (295 lines) removed. | The old fallback wrote a whole session of random "gaze" indistinguishable from real data. |
| `src/Emotion_gaze_visualization.py` | Session-named file, stop signal. Emotion readout drawn at 1080p (was hardcoded `y=1400`, off-screen). Reads only the tail of OpenFace's growing CSV (was re-parsing the whole file twice a second). Survives a missing OpenFace binary as a gaze-only overlay; `PLAYSMART_OPENFACE=0` makes that deliberate. | The overlay crashing took the gaze dot — the operator's only live confirmation the tracker works — down with it. |
| `src/keyboard_recording.py`, `src/microphone_recording.py`, `src/nuanic_eda.py` | Session-named files, stop signal. Whisper transcription off unless `PLAYSMART_TRANSCRIBE=1`. | The audio `SoundFile` context must close to finalise the WAV header — the 44-byte files are exactly an unfinalised header. Whisper ran inline right before the kill and never completed. |
| `src/pop_up_screen.py` rewritten | Collects the files carrying this session's id and uploads them; labels come from `<sid>_meta.json`. Participant pseudonym minted per Riot ID in `data/json/ign_mapping.json`, which stays local. Video: uploads what `obs_recorder` filed; the time-based claim from `~/Videos` is only the fallback. | No more guessing which files belong together, no more free-text game names. **Deliberate divergence from `Dev`:** `Dev`'s version uploads `ign_mapping.json` to the server; this version does not, because that file is the only re-identification key and must not travel with the biometric data. To be confirmed with Bram. |

### Quality control — new

| file | what it answers |
|---|---|
| `src/check_session.py` | Is this session usable? Clock mapping (tolerant of Practice Tool's +30 s skips; fails only on a clock running backwards or a > 30 s blind gap), per-stream coverage of the game window, gaze validity and on-screen share, minimap share, emotion label distribution, APM. Exit 1 on problems. |
| `src/check_video_sync.py` | Does the video sit on the game clock? Prints video positions and the HUD clock each should show, for checking by eye. |
| `src/preflight.py` | Is everything the rig needs there, before F7? Python, packages, `.env`, disk, display, tracker, OpenFace + models, mic, ring, OBS (version, canvas, scene, recording dir), League API, SFTP reachability. |
| `src/_stub_recorder.py` | Stand-in recorder for `key_listener.py --dry-run`: buffers and writes only when asked to stop, so a kill instead of a wait shows up as a missing file. |

### Setup — from an afternoon to one command

| file | change |
|---|---|
| `setup.ps1` **new** | winget installs (Git, Python 3.10, OBS, VC++ 2017, gh), Poetry on 3.10, `poetry install`, OpenFace 2.2.0 + CEN models, `.env` from `.env.example`. Idempotent. |
| `pyproject.toml` | Python pinned `>=3.10,<3.11` (`tobii-research` is cp310-only). Dependencies split: the main group is exactly what the capture imports (43 packages, ~300 MB); Torch/Whisper (`transcription`), OpenCV/YOLO (`vision`) and nine packages nothing imports (`legacy`) are optional groups. `obsws-python` added. License field corrected to Apache-2.0 to match `LICENSE`. |
| `poetry.lock` | Regenerated against the groups (on Linux; the lock is platform-independent). |
| `main.bat` | Runs `preflight.py`, then the orchestrator. |
| `.env.example` **new**, `.gitignore` | Secrets and switches in `.env`. `.gitignore` now covers `data/`, `output/`, media files, `.env`, caches. |
| `obs settings/PlaySmart_League.json` **new**, `obs settings/readme.md` | A 1920×1080 scene collection with one Game Capture of `League of Legends.exe`. The League setup (websocket, video, recording) is the readme's top section; the iteration-4 overlay guide is kept below it. |
| `Toolkit setup.md`, `TESTING.md` **new**, `README.md` | Setup rewritten around `setup.ps1`; test sequence T1–T8 with pass conditions; README points here. |

### Security and privacy

- **`server/python_app/sftp_upload.py`**: the SFTP password was a literal in this public
  repository. It now comes from `PLAYSMART_SFTP_PASSWORD` (`.env`), and the uploader
  keeps files local with a clear message when it is unset. The old password is in git
  history forever and must be rotated on the server.
- `data/json/ign_mapping.json` and `date_game_count.json` were tracked despite `/data`
  being ignored (force-added). Untracked; `ign_mapping.json` is the re-identification key.
- Three `.pyc` files were tracked. Untracked.
- The scene collection deliberately has no Discord process-audio capture: recording other
  people's voices needs their consent.

### Verified

- 10 Sep 2026, home PC, Practice Tool: clock slope 0.999986, drift 3 ms over 101 s,
  events captured retroactively from the cumulative payload.
- 14 Sep 2026, lab PC_1, Practice Tool: audio 9.71 MB (was 44 bytes), gaze 5,804 samples
  at 56.8 Hz 100 % valid, input 980 events, game state 161 rows, 11 events; a dead
  recorder correctly reported.
- 20 Sep 2026, no hardware: all files byte-compile and pass `ruff check .` (the CI);
  `key_listener.py --dry-run --once` passes; `obs_recorder.py` against a mock OBS:
  `t0` within 15 ms, right file claimed with a newer decoy present, all four failure
  paths degrade cleanly; `poetry lock` + `poetry install` of the main group on 3.10 in
  23 s + 5 s.
- **21 Sep 2026, HIVE pc03, set up from a bare Windows install with `setup.ps1`:** first
  full-stack capture — session `20260921-153638-ae18ce`, a real CLASSIC game on Summoner's
  Rift (~18 min). Gaze 64,022 samples at 60 Hz, 100 % valid (Tobii Pro Spark); audio 104.7 MB;
  input, game state, events and meta; **video 57.5 MB started, stopped and filed by
  `obs_recorder` on the session id**. Upload correctly withheld with no SFTP password.
  Emotion wrote nothing despite OpenFace being present (undiagnosed); EDA absent (no ring).
- **Not yet verified:** `check_video_sync` (T5) on that session, EDA end to end, the emotion
  stream on the new setup.

### Replay API spike (22 Sep 2026) — `tools/replay/`

Riot's official Replay API renders a recorded game with fog of war off and the interface
reduced to the minimap, at an exact frame rate. `minimap_match.py` reads champion
positions off those frames (Data Dragon portrait templates, static-background exclusion,
one champion per icon). On a 30 s / 61-frame test at 3440×1440: 9 of 10 champions
placed correctly per frame, 44 game units per pixel. This replaces the planned
minimap-CV-on-screen-recording route and the dead `.rofl` parser. See `tools/replay/README.md`.

### Game identity and audio timing (23 Sep 2026)

| file | change | reason |
|---|---|---|
| `src/lcu.py` **new** | Reads the League client's lockfile and asks its local API (read-only, best-effort) for the game id, platform, queue, PUUID and client patch. `python src/lcu.py` prints them. | The Live Client Data API has no game id, so nothing linked a session to its replay, its Match-V5 record, or the other four players' sessions of the same game. |
| `src/liveclient_recorder.py` | `<sid>_meta.json` gains `game_id`, `platform_id`, `queue_id`, `puuid`, `client_game_version`, `identity_source`, filled in a background thread so the recording never waits on the client. | `<platform>-<game_id>.rofl` is the replay's file name; the patch decides whether it can still be rendered. |
| `src/microphone_recording.py` | Writes `<sid>_audio.json`: wall-clock time of the first sample (`t0_unix_ms`), frames written, end time, and the number of input overflows. | A WAV has no timestamps; without an anchor the audio could not be placed on the game clock. Overflows would shift everything after them, so they are counted. |
| `src/check_session.py` | Reports whether the session has a game id (or is Practice Tool, which has no replay) and whether the audio is anchored. | |

Rendering the replay, reading positions and merging all streams per game run after the
session, in the team repository's `playsmart` package (`playsmart postgame`). The
prototype in `tools/replay/` stays for reference; the package version adds temporal
gating and the fog-on pass.

### Keep everything the sources already send (25 Sep 2026) — collection wrap-up

Before the rig is frozen: every session should carry everything the Live Client, the
League client and the Tobii already deliver, so nothing has to be wished for later.
None of this needs new hardware; the self-labels are a questionnaire and belong on
the consent form. **Untested on the lab hardware** at the time of writing — run T2
and T4 once after pulling.

| file | change | reason |
|---|---|---|
| `src/liveclient_recorder.py` | Gamestate rows gain `move_speed`, `attack_range`, `attack_speed`, `attack_damage`, `ability_power`, `armor`, `magic_resist`, `ability_haste`, `tenacity`, `ability_levels` (Q/W/E/R), `items`, `items_usable`. New **`<sid>_players.csv`**: every player's level, death state, scores and items per poll (champion + team, no Riot IDs). Meta gains `skin_id`/`skin_name`, `summoner_spells`, `runes`, `abilities`, the same per roster entry, the client context below, and the per-PC measurements (`minimap_rect_px`, `gaze_validation`). | Movement speed and ranges at 2 Hz are what a threat model needs; Match-V5 has them once a minute. `items_usable` says whether the escape item was off cooldown. The skin decides the minimap icon. |
| `src/lcu.py` | `client_context()`: game settings (resolution, `MinimapScale`, `GlobalScale`), input settings (key bindings), queue context (solo queue / custom / ranked), tier per ranked queue. `end_of_game_stats()`: the post-game stats block, scrubbed of names, with a `game_id` check. `scrub_names()`. | Replaces the 14 % × 25 % minimap guess with the client's own numbers; the input log cannot say which key was a ping without the bindings; the post-game block exists for Practice Tool and customs, which Match-V5 never records. |
| `src/pop_up_screen.py` | After the recorders stop: `<sid>_eog.json` (post-game stats, waits up to `PLAYSMART_EOG_WAIT_S`, default 45 s), the self-label dialog, **`<sid>_manifest.json`** (size and SHA-256 of every file of the session) and one line in **`data/sessions.csv`**. All before the upload; all best-effort. | A dataset that can be checked file by file on the server or a handover drive; an index of what exists without walking folders. |
| `src/self_labels.py` **new** | One dialog after each game: the player's own deaths (from the events stream) with *knew / didn't look / didn't know / not sure* each, one ordinal question (calmer / same / more tilted than the last game), a free note. `<sid>_labels.json`. `PLAYSMART_SELF_LABELS=0` disables; skipped without a display; never blocks the upload past 3 minutes. | The analysis sorts deaths into the same three buckets from fog, positions and gaze; the player's own answer one minute later is the cheapest second label there is. A second label, not ground truth. |
| `src/eye_tracking_script.py` | Per sample: `left_valid`, `right_valid`, `head_x_mm`, `head_y_mm`, `head_dist_mm` (gaze origin in user coordinates), `device_time_stamp`, `system_time_stamp`, `left_openness`, `right_openness` (eye-openness stream, when the SDK and device provide it). New `<sid>_gaze.json`: tracker model, serial, frequency, display size in mm, screen px, sample count, valid share, median head distance. | Head distance turns pixel error into degrees; eye openness gives blink rate; the tracker's own timestamps let a dropped sample be told from a slow callback. All of it was in the callback already. |
| `src/gaze_validation.py` **new** | Once per player setup, after the Tobii calibration: five dots, 1.5 s each → accuracy (mean offset) and precision (RMS) in px and degrees, to `data/gaze/validation_<stamp>.json`; then two SPACE presses on the minimap's corners → `data/json/minimap_rect.json`. Both are copied into every later session's meta. | "Was the player looking at the minimap" is only as good as the accuracy number, and nobody had one. The minimap rectangle depends on resolution and scale. |
| `src/obs_recorder.py` | `<sid>_video.json` gains OBS's `GetStats` (skipped/total frames, render time, CPU) and `GetVideoSettings` (canvas and output size, fps). | Dropped frames are the data-quality number for video; the canvas size maps screen pixels onto video pixels for the camera-box reader in the team repository. |
| `src/check_session.py` | Reports queue, tier, skin, client settings, whether the minimap rectangle and a gaze validation are on record, and whether the post-game stats, self-labels and manifest exist. | |
| `.env.example` | `PLAYSMART_SELF_LABELS`, `PLAYSMART_EOG_WAIT_S`; `PLAYSMART_SKIP=eda,emotion` recommended for the lab PCs. | EDA was never verified end to end; emotion produced 99 % Neutral in the archive and is the one stream with an AI Act Art. 5(1)(f) question. Off by decision until both change. |

The merge in the team repository reads all of it (`_players.csv`, the new gamestate
columns, `minimap_rect_px`, `input_settings` for pings, `_gaze.json`), and derives
pings, minimap clicks, voice activity and the fight start per death from streams that
were already recorded.

### Upload: keep, off or move (28 Sep 2026)

| file | change | reason |
|--|--|--|
| `server/python_app/sftp_upload.py` rewritten | `PLAYSMART_UPLOAD=keep\|off\|move` (default `keep`: upload and keep the local copy; `move` deletes it only after paramiko has confirmed the remote size). One SSH connection per session (`SftpSession`, `upload_files()`), ten-second connect timeout, remote paths built with `posixpath`, files sent as `<name>.part` and renamed on completion, a file already on the server with the same size is skipped, missing remote directories are created when the sftp user may. `upload_file_to_sftp()` kept for old callers. | The old file built `/data/gaze\<file>` remote paths on Windows, opened a connection per file, hung indefinitely off the VPN, and left the "keep local" behaviour to a commented-out `os.remove`. The `.part` rename keeps the server's cron pipeline from merging a half-uploaded video. |
| `src/pop_up_screen.py` | `upload_session_files()`: one connection, a one-line summary (`4 uploaded (local copies kept)`), every outcome appended to **`data/upload_log.csv`**, and the retry command printed when something did not go. | The lab PCs are not always on the BUas network when a game ends. |
| `src/upload_session.py` **new** | `<sid>…`, `--pending` (every session with a file not logged as uploaded) or `--all`. Safe to re-run. | Send what stayed local, later, without re-recording anything. |
| `src/preflight.py` `--fix-env` / `--env-only`; `setup.ps1` step 4 | `.env` is untracked, so a `git pull` never brings new keys to a PC set up earlier. Pre-flight now diffs `.env` against `.env.example`: missing keys → warn (`--fix-env` appends them, values untouched); a `# comment` after a value → **FAIL**, because the loader keeps it as part of the value (`PLAYSMART_SKIP=eda   # ring` skipped nothing on a lab PC, 28 Sep). `setup.ps1` runs the sync when `.env` exists. `.env.example` has no trailing comments any more. | Every PC set up before a change gets the change without hand-editing `.env`. |
| `tests/test_sftp_upload.py` **new** | Nine tests against an in-process paramiko SFTP server: keep, move, exists, resend on change, unwritable directory, off, unreachable, env parsing, legacy wrapper. `poetry run pytest tests/`. | The uploader is the one part of the rig that cannot be exercised on the lab PC without the server. |

### `PLAYSMART_GAME` — keep `Dev` usable for Valorant (30 Sep 2026)

| file | change | reason |
|--|--|--|
| `src/session.py` `game()`, `src/key_listener.py`, `src/pop_up_screen.py`, `src/preflight.py`, `src/liveclient_recorder.py`, `.env.example` | `PLAYSMART_GAME=league` (default) is the iteration-5 behaviour. Any other value is the manual F7/F12 bracket: no wait for League's API, no game-state stream, F12 ends the session cleanly (not an abort), pre-flight skips the League check, `sessions.csv` and the manifest carry the game name. | The org's other rigs capture Valorant from `Dev`. Without this switch a merge would have made them wait forever for a League API that never appears. They still gain the graceful stop (their 44-byte WAVs), session ids, the OBS anchor, pre-flight and the upload modes. Untested on a Valorant PC - T1 + one F7/F12 game there before relying on it. |

### After the first full merge of the 28 Sep sessions (14 Oct 2026)

Three customs, five rigs, every stream merged in the analysis toolkit; the capture
audit is in the team repo (`first-merge-2026-09-28-sessions`). What it found on the
rig side, and the change for each:

- `src/eye_tracking_script.py` — **gaze validity was never recorded.** The Tobii marks
  an untracked eye with NaN coordinates, the `None in (...)` check let them through,
  `int(nan * width)` raised inside the SDK callback and the sample vanished: 2-15 % of
  samples missing, every written row "valid". Validity now comes from the tracker's
  flags, NaN is treated as missing, invalid samples are written with empty
  coordinates and `gaze_valid=False`, one tracked eye is used when the other is lost.
  Also an on-screen **head-distance warning** (50-75 cm; one player sat at 79 cm) and
  `samples_per_s` / `head_distance_warnings` in `<sid>_gaze.json`.
- `src/liveclient_recorder.py` — `wait_for_game` accepts only a live game (champion
  present, clock advancing or on the loading screen): the post-game screen produced
  a 58-row session with an empty champion. `late_start_s` in the meta with a loud
  console warning. `minimap_rect_px` read from `<data>/json/` (it was read from the
  working directory, so a measured rectangle never reached the meta) and otherwise
  **estimated from the client's MinimapScale**, fitted on five measured rigs (within a
  pixel or two). The game is declared over 5 s after the API stops answering, not after
  ten timed-out polls (audio and video ran 25 s past the end).
- `src/key_listener.py` — **auto re-arm** after every game (`PLAYSMART_REARM=0` to
  disable); one rig was armed late for game 2 and not at all for game 3. End-of-session
  report prints late start, rectangle source and the game id to download.
- `src/preflight.py` — new checks: PC clock vs time server (rigs were 2-5 s apart),
  League minimap scale against the rig standard (0.3-1.5 across rigs), minimap
  rectangle measured, OBS 1920x1080 at 60 fps (one rig recorded 1280x720 at 30), and a
  line naming the streams this rig will not record (no emotion on 28 Sep).
- `src/apply_rig_settings.py` — new: writes the rig standard (MinimapScale 1.0) into
  `game.cfg` with a backup, client closed.
- `tests/test_liveclient_guards.py`, `tests/test_rig_settings.py` — new.

### Not changed, on purpose

- `auto_merge.py` / the server pipeline (separate repository): `AUTO_ALIGN_EMOTION` should
  become `False` now that the streams share a wall clock, and `eda` needs adding to
  `collect_files()`. `/data/gamestate/` needs creating in the SFTP container.
- `src/tobii_research.py` (a copy of the package's import shim), `enemy_detection.py`,
  `inference.py`, `merge_datasets.py`, `resize_video.py`, `nuanic_rings.py`, the
  notebooks: untouched, now behind optional dependency groups.
- Both `eye_tracking_script.py` and `Emotion_gaze_visualization.py` subscribe to the
  same Tobii. Untested whether that costs samples (`TESTING.md` T8).
