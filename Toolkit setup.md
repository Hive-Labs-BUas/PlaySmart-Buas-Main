# Toolkit Setup Guide

> Modified 2026-09 (iteration 5, League of Legends port). Setup is now one script; the
> manual steps are kept below for when a step of it fails. The full test sequence that
> proves a capture PC works is in `TESTING.md`.

## Quick path (Windows 10/11)

```powershell
git clone https://github.com/PlaySmart-Buas/PlaySmart-Buas-Main.git C:\League_work\PlaySmart-Buas-Main
cd C:\League_work\PlaySmart-Buas-Main
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` installs Git, Python 3.10, OBS Studio, the VC++ 2017 runtime and GitHub CLI
(winget), Poetry and the project environment, OpenFace 2.2.0 with its models, and creates
`.env` from `.env.example` asking for the OBS and SFTP passwords. Re-run it after any
failure; it skips what is done.

Then, by hand:

1. **Tobii Pro Eye Tracker Manager** — https://www.tobii.com/products/software/applications-and-developer-kits/tobii-pro-eye-tracker-manager — install, plug in the tracker, calibrate the player.
2. **League of Legends** — install, log in, open Practice Tool once.
3. **OBS** — `obs settings\readme.md`, top section: import `PlaySmart_League.json`, enable the WebSocket server, MKV, auto-remux off.
4. `poetry run python src\preflight.py` — every line `ok` or `warn`, nothing `FAIL`.

## Prerequisites (manual path)

- Python **3.10** — https://www.python.org/downloads/release/python-3100/ — 3.10 exactly:
  `tobii-research` ships cp310 wheels only.
- Poetry — `py -3.10 -m pip install --user pipx ; py -3.10 -m pipx install poetry`
- OBS Studio 30 or newer — https://obsproject.com/download
- Tobii Pro Eye Tracker Manager — link above
- Visual C++ 2017 x64 redistributable (OpenFace needs it)
- A connected webcam and a connected Tobii eye tracker
- ffmpeg — **only** if you enable Whisper transcription (`PLAYSMART_TRANSCRIBE=1`):
  `winget install ffmpeg`

## Setting up the Poetry environment

```powershell
cd C:\League_work\PlaySmart-Buas-Main
poetry env use (py -3.10 -c "import sys; print(sys.executable)")
poetry install
poetry run python --version        # 3.10.x
```

`poetry install` installs the capture rig only (about 300 MB). The heavy optional groups:

| group | command | needed for |
|---|---|---|
| transcription | `poetry install --with transcription` | Whisper after a recording (`PLAYSMART_TRANSCRIBE=1`), plus ffmpeg |
| vision | `poetry install --with vision` | `enemy_detection.py`, `inference.py`, `resize_video.py` (iteration 4, Valorant HUD) |
| legacy | `poetry install --with legacy` | packages iteration 4 listed that nothing under `src/` imports |

## Adding OpenFace to the project folder

`setup.ps1` does this. By hand: download `OpenFace_2.2.0_win_x64.zip` from
https://github.com/TadasBaltrusaitis/OpenFace/releases (or the PlaySmart Google Drive),
unzip it so that `OpenFace_2.2.0_win_x64\FeatureExtraction.exe` sits in the repo root, then
**run `download_models.ps1` inside that folder** — the zip does not include the CEN
patch-expert models and `FeatureExtraction.exe` exits immediately without them.

OpenFace produces the **`face` stream** (since 2 Nov 2026; it was called `emotion`):
`data\face\<sid>_face.csv` with the 17 action-unit intensities and 18 presences, head pose,
webcam gaze and a per-eye eyelid aperture, plus `<sid>_face.json` with the achieved fps and
the clock anchor. No emotion label is computed and no face image, video or face landmark is
ever written; OpenFace's scratch folder `output\<sid>\` is deleted when the session ends.

It is **off on the lab PCs** (`PLAYSMART_SKIP=eda,emotion` in `.env`; `emotion` and `face`
both work as the name) until the consent form and the DPO decision cover face measurements
of participants. To record it: remove `emotion` from `PLAYSMART_SKIP`. Knobs in `.env`:
`PLAYSMART_FACE_CAM=640x480` (webcam size; `1280x720` gives better AUs but costs CPU next
to League — `check_session.py` warns below 15 fps), `PLAYSMART_FACE_DEVICE=0`,
`PLAYSMART_FACE_KEEP_LANDMARKS=1` (also keep the 56 eye-region landmarks).

If a PC has no webcam or no OpenFace, set `PLAYSMART_OPENFACE=0` in `.env`: the overlay
then runs gaze-only and there is no face stream.

## Secrets: `.env`

Copy `.env.example` to `.env` and fill in `PLAYSMART_OBS_PASSWORD` (OBS → Tools →
WebSocket Server Settings → Show Connect Info) and `PLAYSMART_SFTP_PASSWORD`. Every
recorder reads `.env` at start (`src/session.py`). `.env` is gitignored; the SFTP password
is no longer in the code.

## Starting the toolkit

1. Webcam, eye tracker and (optionally) the Nuanic ring connected; player calibrated.
2. OBS running with the PlaySmart - League scene collection.
3. `main.bat` — it runs the pre-flight check and then the capture orchestrator.

The capture is automatic: **F7 arms**, the recorders start when a League game starts,
and stop and save when the game ends. **F12 aborts** a running capture. Each session's
files share one id: `data\<stream>\<session_id>_<stream>.<ext>`, with
`data\gamestate\<session_id>_meta.json` carrying the Riot ID, champion, mode and map
from the game client.

| key | action |
|---|---|
| `F7` | arm — wait for a game, record it, stop when it ends |
| `F12` | abort the running capture (streams still save) |

Afterwards: `poetry run python src\check_session.py` says whether the session is usable,
and `poetry run python src\check_video_sync.py` checks the video against the game clock.

## Troubleshooting

`TESTING.md` section 8 has the table. The first stop is always
`poetry run python src\preflight.py`.

> [!TIP]
> Devices connected *before* launching; OBS open *before* pressing F7.

> [!NOTE]
> If Poetry is not found after installation, open a new terminal.

> [!WARNING]
> `poetry install` must run in the folder containing `pyproject.toml`.

## Project structure

```
project/
├── .env                       # passwords and switches (gitignored; see .env.example)
├── .venv/                     # Poetry environment on Python 3.10
├── data/                      # every recording; gitignored, never committed
│   ├── audio  eda  face  gamestate  gaze  input  video
│   └── json/ign_mapping.json  # Riot ID -> participant id; stays on this PC
├── obs settings/
│   ├── PlaySmart_League.json  # scene collection to import
│   └── readme.md
├── OpenFace_2.2.0_win_x64/    # not in git; setup.ps1 downloads it
├── server/python_app/sftp_upload.py
├── src/
│   ├── key_listener.py        # orchestrator (F7 / F12)
│   ├── session.py             # session id, stop signal, .env
│   ├── liveclient_recorder.py # League game state (Live Client Data API)
│   ├── obs_recorder.py        # video via obs-websocket, timing anchor
│   ├── eye_tracking_script.py # gaze (Tobii)
│   ├── Emotion_gaze_visualization.py  # overlay + OpenFace face stream (AUs, pose)
│   ├── keyboard_recording.py  # input
│   ├── microphone_recording.py# audio
│   ├── nuanic_eda.py          # EDA ring
│   ├── pop_up_screen.py       # label + upload
│   ├── preflight.py           # pre-capture checks
│   ├── check_session.py       # is a recorded session usable?
│   ├── check_video_sync.py    # video vs game clock
│   └── enemy_detection.py, inference.py, resize_video.py, merge_datasets.py  # iteration 4
├── main.bat                   # pre-flight, then capture
├── setup.ps1                  # one-command setup
├── TESTING.md                 # setup checks and the test sequence
├── CHANGELOG.md               # what iteration 5 changed and why
├── poetry.lock
└── pyproject.toml
```
