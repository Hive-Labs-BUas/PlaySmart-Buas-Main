# OBS for PlaySmart

> Modified 2026-09 (iteration 5). The League section is new; the input-overlay guide
> below it is iteration 4's and still applies if you want the on-screen keyboard/mouse.

## League of Legends (iteration 5) — five minutes, once per PC

Video is now started and stopped by the capture itself (`src/obs_recorder.py`) over
obs-websocket, and the file is named after the session id. OBS only needs to be open,
with the WebSocket server on and one scene that shows the game.

### 1. Import the scene collection

Scene Collection → Import → pick `obs settings\PlaySmart_League.json` → Scene Collection →
**PlaySmart - League**. It contains one video source, **League Game Capture** (Game Capture
of `League of Legends.exe`, matched on the executable), plus default desktop audio and mic.

Why Game Capture of the League window and nothing else: the gaze overlay
(`Emotion_gaze_visualization.py`) is a full-size borderless window. The iteration-4
collection layered a *Window Capture* of that overlay on top of the game, and *Capture
any fullscreen application* latches onto it too. That is how the 2025 archive ended up
with half its video black. The gaze dot can always be re-drawn from `data/gaze/`.

Not included on purpose: the Discord process-audio capture (records other people's voices;
needs their consent) and the input-overlay sources (need the plugin below; input is already
recorded as data by `keyboard_recording.py`).

### 2. WebSocket

Tools → **WebSocket Server Settings** → *Enable WebSocket server*, port `4455`, *Enable
Authentication* on → *Show Connect Info* → copy the password into `.env` as
`PLAYSMART_OBS_PASSWORD` (or let `setup.ps1` ask for it).

### 3. Video

Settings → Video: Base **1920×1080**, Output **1920×1080**, **60** (or 30) FPS, constant.
Not 2260×1080 — that canvas was for the overlays and is what `VideoSettingsOBS.png` shows.

### 4. Recording

Settings → Output → Recording:

| setting | value | why |
|---|---|---|
| Recording Path | `C:\Users\<user>\Videos` | `obs_recorder` moves the file out by the path OBS reports; the old time-based fallback also looks here |
| Recording Format | **MKV** (or *Hybrid MP4* on OBS ≥ 30.2) | survives a crash; plain MP4 does not |
| Encoder | NVENC / hardware if present | x264 at 1080p60 competes with OpenFace for CPU |
| Automatic File Splitting | off | `StopRecord` reports one file |
| Settings → Advanced → *Automatically remux to mp4* | **off** | the remux would run on a file that has already been moved |
| Audio Track | **1 and 2** ticked | track 1 = game sound, track 2 = microphone; see below |

**Audio on separate tracks (added 2026-11-05).** Game sound and the player's voice are
never mixed into one track: Edit → Advanced Audio Properties → *League Game Audio* →
track **1** only; *Mic/Aux* → track **2** only, unmuted; *Desktop Audio* muted (Discord,
music and system sounds stay out). `python src\rig_setup.py` sets all of this over the
websocket and `preflight.py` prints the layout as `obs audio: track 1: League Game Audio,
track 2: Mic/Aux` — a `FAIL` there means two inputs share a track. The rig's own
`data\audio\<sid>_audio.wav` remains the primary voice recording (own timing anchor);
track 2 is the frame-locked copy. `PLAYSMART_OBS_MIC_TRACK=0` in `.env` mutes the mic in
OBS instead. The layout actually recorded is written into `<sid>_video.json`
(`audio_tracks`). Split afterwards with
`ffmpeg -i <sid>_video.mkv -map 0:a:0 game.wav -map 0:a:1 mic.wav`; check a file with
`ffprobe <sid>_video.mkv` (two `Audio:` streams).

Hotkeys for start/stop are no longer needed; F7 in the capture window is the only key.

### 5. Check it

With OBS open: `poetry run python src\preflight.py` prints the OBS version, websocket
version (needs ≥ 5.1, i.e. OBS 29+), canvas size, and whether the current scene has a game
capture. Then `TESTING.md` section 3 has the three smoke tests and section T5 the timing
acceptance test.

---

# OBS Input Overlay Setup Guide

> **Show your mouse and keyboard inputs live on screen while recording in OBS.**
> Based on this [YouTube tutorial](https://www.youtube.com/watch?v=JNNqm3a2oZQ).

---

## Step 1 — Download the Plugin

1. The plugin can be found **[here](https://github.com/univrsal/input-overlay/releases/tag/v4.8)**
2. Assuming you're on **Windows**, download either the **32-bit** or **64-bit** version — it will be a `.zip` file.

---

## Step 2 — Install the Plugin

1. **Right-click** the downloaded `.zip` → **Extract All**.
2. Open the extracted folder and **Shift+select** both the `data` and `obs-plugin` folders.
3. Navigate to:
   ```
   C:\Program Files\OBS Studio\
   ```
4. **Replace** the existing `data` and `obs-plugins` folders with the ones you just downloaded.

---

## Step 3 — Set Up Your Overlays Folder

1. Right-click somewhere convenient → **New Folder**.
2. Name it something like `overlays`.
3. This folder will hold all the **images** for your mouse, keyboard, and controller overlays.

---

## Step 4 — Choose & Extract Your Presets

Navigate back to the downloaded presets folder. Pick what fits your use case:

| Use Case             | Preset to Extract    |
| -------------------- | -------------------- |
| Controller (gamepad) | `gamepad`          |
| Mouse only           | `mouse`            |
| WASD keys only       | `WD` (WASD preset) |
| Full keyboard        | `Cordy`            |

> **Tip:** You can mix and match — extract multiple presets if needed.

Once extracted, **drag everything into your new `overlays` folder**.

---

## Step 5 — Add the Source in OBS Studio

1. In OBS Studio, go to the **Sources** panel.
2. Click the **`+`** button → select **Input Overlay**.
3. Give it a name (e.g., `Mouse Overlay`).

### Configuring the Mouse Overlay

1. Click **Browse** and navigate to your mouse overlay folder.
2. Select the **images** folder first, then go back and grab the **config file**.
3. There are multiple config options — the option **mouse-no-movement** is recommended.
4. Hit **OK**.

---

## Step 6 — Configure OBS settings

Adjust the video settings of OBS as such

![OBS settings](VideoSettingsOBS.png)

Note: Also remember to do the hotkeys for starting and stopping the recording!

---

## Step 7 — Position & Test

- **Drag** the overlay to wherever you want it to appear on screen during recording.
- Click around — you should see the overlay **react in real time** to your inputs! ✅

---

## Step 8 — Add keyboard overlay

Repeat **Step 5** for any additional overlays you want.

For **Valorant** the WASD overlay is recommended.
For **League of Legends** the full keyboard overly is recommended

1. Click **`+`** → **Input Overlay**.
2. Name it (e.g., `WASD Overlay`).
3. Browse to the WASD images folder.
4. Pick the config file that looks best to you.
5. Position it on screen.

---

> [!NOTE]
> If the readme is unclear or it is not workon try following the YouTube tutorial [here](https://www.youtube.com/watch?v=JNNqm3a2oZQ)
