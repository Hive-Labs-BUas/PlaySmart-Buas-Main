"""Gaze overlay and OpenFace face recorder to data/face/<session_id>_face.csv.

Modified 2026-09 (iteration 5): file named from the shared session id; stops on the
orchestrator's stop signal; only the tail of OpenFace's growing CSV is read each tick; a
missing OpenFace binary (or PLAYSMART_OPENFACE=0) leaves this running as a gaze-only
overlay instead of taking the gaze dot down with it.

Rewritten 2026-11-02: the *face* stream replaces the *emotion* stream.

* OpenFace runs with ``-aus -pose -gaze`` and everything it measures is kept: 17 action
  unit intensities (``AU*_r``, 0-5) and 18 presences (``AU*_c``), head pose
  (``pose_T*`` mm, ``pose_R*`` rad), webcam gaze vectors and angles, tracking
  ``confidence``/``success``, and a per-eye eyelid aperture derived from the eye-region
  landmarks (the eye-openness signal the Tobii Pro Spark does not provide).
* The seven-label ``map_emotion()`` classifier is gone. It returned Neutral on ~95 % of
  frames and the label is the one thing in this stack that the AI Act (Art. 5(1)(f),
  emotion inference in education) is about. Raw facial measurements are kept; what they
  mean is decided in analysis, per player, relative to that player's own baseline.
* **No face images or video are ever written.** OpenFace is run without ``-simalign``,
  ``-hogalign`` and ``-tracked``, and the 68 face landmarks (``-2Dfp``/``-3Dfp``) are
  not requested because they describe the face's geometry. The eye-region landmarks are
  reduced to the aperture number and dropped unless PLAYSMART_FACE_KEEP_LANDMARKS=1.
* Timing fixed. OpenFace's ``timestamp`` counts from the moment the webcam was opened,
  which is after its models load; the old code added it to the *script's* start time,
  so every face row was stamped seconds early. The anchor is now taken when OpenFace
  prints ``Starting tracking`` and cross-checked against the arrival time of rows
  during the session; both numbers go into ``<sid>_face.json``.
* The overlay's readout shows face tracking confidence and achieved fps, not an emotion.

Environment:
    PLAYSMART_OPENFACE=0             gaze overlay only, no webcam, no face stream
    PLAYSMART_FACE_CAM=640x480       webcam capture size (OpenFace's default; a larger
                                     face gives better AUs but costs CPU next to League)
    PLAYSMART_FACE_DEVICE=0          webcam index
    PLAYSMART_FACE_KEEP_LANDMARKS=1  also keep the 56 eye-region landmarks (2D + 3D)
    PLAYSMART_OPENFACE_EXE=<path>    FeatureExtraction.exe elsewhere than OpenFace_2.2.0_win_x64/
"""

import sys
import os
import math
import json
import shutil
import pygame
import tobii_research as tr
import screeninfo
import keyboard
import subprocess
import pandas as pd
import time
from pathlib import Path
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session

# Paths and constants
REPO = Path(__file__).resolve().parent.parent
openface_executable = os.environ.get("PLAYSMART_OPENFACE_EXE") or str(REPO / 'OpenFace_2.2.0_win_x64' / 'FeatureExtraction.exe')
sid = session.session_id()
# One scratch folder per session so a stale CSV from a crashed run can never be
# mistaken for this session's output.
output_dir = REPO / 'output' / sid
circle_radius = 50
outline_thickness = 3

ENABLE_OPENFACE = os.environ.get("PLAYSMART_OPENFACE", "1") == "1"
KEEP_LANDMARKS = os.environ.get("PLAYSMART_FACE_KEEP_LANDMARKS", "0") == "1"
FACE_DEVICE = os.environ.get("PLAYSMART_FACE_DEVICE", "0").strip() or "0"


def _cam_size() -> tuple:
    raw = os.environ.get("PLAYSMART_FACE_CAM", "640x480").lower().replace(" ", "")
    try:
        w, h = (int(v) for v in raw.split("x"))
        return w, h
    except ValueError:
        print(f"PLAYSMART_FACE_CAM={raw!r} is not WxH - using 640x480")
        return 640, 480


CAM_W, CAM_H = _cam_size()

script_start_ms = int(time.time() * 1000)
print(f"Script started at: {script_start_ms} (unix ms)")

# Initialize Pygame for visualization
pygame.init()
clock = pygame.time.Clock()
screen = screeninfo.get_monitors()[0]
screen_width, screen_height = screen.width, screen.height
window = pygame.display.set_mode((screen_width, screen_height), pygame.NOFRAME)
pygame.display.set_caption("Gaze and Face Overlay")

# Eye Tracker setup
eyetrackers = tr.find_all_eyetrackers()
if not eyetrackers:
    print("No eye trackers found. Running without gaze tracking.")
    my_eyetracker = None
else:
    my_eyetracker = eyetrackers[0]

# Define smoothing parameters
alpha = 0.1
smoothed_x, smoothed_y = screen_width // 2, screen_height // 2


def _finite(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def gaze_data_callback(gaze_data):
    """Move the dot. Author: Thomas Pichardo; NaN-guarded 2026-11.

    The Tobii reports an untracked eye as NaN. ``int(nan)`` raised inside this
    callback, the SDK swallowed it, and the dot froze on every blink.
    """
    global smoothed_x, smoothed_y
    left_x, left_y = gaze_data.get('left_gaze_point_on_display_area', (None, None))
    right_x, right_y = gaze_data.get('right_gaze_point_on_display_area', (None, None))
    xs = [v for v in (left_x, right_x) if _finite(v)]
    ys = [v for v in (left_y, right_y) if _finite(v)]
    if not xs or not ys:
        return
    x_screen = int(sum(xs) / len(xs) * screen_width)
    y_screen = int(sum(ys) / len(ys) * screen_height)
    smoothed_x = alpha * x_screen + (1 - alpha) * smoothed_x
    smoothed_y = alpha * y_screen + (1 - alpha) * smoothed_y


# ---------------------------------------------------------------------------
# OpenFace
# ---------------------------------------------------------------------------
process = None
openface_log = None
face_csv = output_dir / f"{sid}_face.csv"

# Timing. `anchor_ms` is the unix time OpenFace's `timestamp` column counts from.
# Primary estimate: when "Starting tracking" is printed (OpenFace opens the webcam
# and sets its clock zero just before). Cross-check: every tick the overlay reads
# the newest row, so (now - timestamp) is anchor + processing + read latency; its
# 5th percentile over the session is a second estimate, biased late by the
# per-frame latency only. Both are written to the json; the primary is used when
# it exists, the cross-check otherwise.
anchor_ms = None          # from "Starting tracking"
_arrivals = []            # (now_ms - timestamp_ms) per tick
_openface_lines = []      # anything else OpenFace says, for the json

if ENABLE_OPENFACE:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        openface_executable,
        '-device', FACE_DEVICE,
        '-cam_width', str(CAM_W), '-cam_height', str(CAM_H),
        '-aus', '-pose', '-gaze',
        '-out_dir', str(output_dir),
        '-of', f"{sid}_face",
    ]
    try:
        # stderr goes to a file: a PIPE nobody reads fills up and stalls OpenFace.
        openface_log = open(output_dir / "openface.log", "w", encoding="utf-8")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=openface_log,
                                   text=True, bufsize=1)
    except OSError as exc:
        # OpenFace is not installed on every lab machine. An uncaught
        # FileNotFoundError here used to take the whole overlay down with it,
        # which also cost the on-screen gaze dot - the operator's only live
        # confirmation that the tracker is working at all. Face is optional;
        # the dot is not. Carry on without it.
        print("!" * 68)
        print(f"!!  OpenFace could not start: {exc}")
        print(f"!!  looked for: {openface_executable}")
        print("!!  Continuing as a gaze overlay only - no face stream this session.")
        print("!!  Set PLAYSMART_OPENFACE=0 to make this the intended behaviour.")
        print("!" * 68, flush=True)
        ENABLE_OPENFACE = False
        process = None

if ENABLE_OPENFACE:
    def monitor_openface():
        """Take the clock anchor from OpenFace's own announcement; keep reading so the pipe never blocks."""
        global anchor_ms
        for line in process.stdout:
            line = line.rstrip()
            if anchor_ms is None and "Starting tracking" in line:
                anchor_ms = int(time.time() * 1000)
                print(f"OpenFace tracking started at: {anchor_ms}")
            elif line and len(_openface_lines) < 200:
                _openface_lines.append(line)

    threading.Thread(target=monitor_openface, daemon=True).start()
    print(f"OpenFace: {CAM_W}x{CAM_H} on device {FACE_DEVICE}, -aus -pose -gaze -> {face_csv.name}")
elif os.environ.get("PLAYSMART_OPENFACE", "1") == "0":
    print("PLAYSMART_OPENFACE=0 - gaze overlay only, no webcam and no face stream.")

# Load calibration if available
if my_eyetracker:
    try:
        calibration_data = my_eyetracker.retrieve_calibration_data()
        if calibration_data:
            my_eyetracker.apply_calibration_data(calibration_data)
            print("Calibration loaded.")
    except Exception as exc:  # noqa: BLE001 - the dot matters more than the reload
        print(f"Calibration not reloaded ({exc}); the tracker keeps its current one.")
    my_eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)


# ---------------------------------------------------------------------------
# Reducing OpenFace's CSV to the face stream
# ---------------------------------------------------------------------------
# OpenFace's eye model has 28 landmarks per eye: 0-7 pupil boundary, 8-19 eyelid
# contour, 20-27 iris boundary (eye 0 = indices 0-27, eye 1 = 28-55). The eyelid
# aperture below is (vertical extent / horizontal extent) of the contour, an eye
# aspect ratio independent of distance to the camera. The index layout is taken
# from the OpenFace source, not its documentation; `check_session.py` verifies it
# indirectly by comparing aperture dips with AU45 (blink).
EYELID = {"0": range(8, 20), "1": range(36, 48)}


def eyelid_aperture(df: pd.DataFrame, eye: str) -> pd.Series:
    xs = [f"eye_lmk_x_{i}" for i in EYELID[eye]]
    ys = [f"eye_lmk_y_{i}" for i in EYELID[eye]]
    if not all(c in df for c in xs + ys):
        return pd.Series([float("nan")] * len(df), index=df.index)
    width = df[xs].max(axis=1) - df[xs].min(axis=1)
    height = df[ys].max(axis=1) - df[ys].min(axis=1)
    return (height / width.where(width > 0)).round(4)


def read_openface_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    return df


def reduce_face(df: pd.DataFrame, anchor: float) -> pd.DataFrame:
    """Keep everything OpenFace measured except the eye landmarks (reduced to an aperture)."""
    out = pd.DataFrame(index=df.index)
    out["unix_time"] = (anchor + df["timestamp"] * 1000.0).round().astype("int64")
    out["openface_timestamp_s"] = df["timestamp"]
    for col in ("frame", "confidence", "success"):
        if col in df:
            out[col] = df[col]
    out["eye_aperture_0"] = eyelid_aperture(df, "0")
    out["eye_aperture_1"] = eyelid_aperture(df, "1")
    keep_prefixes = ("gaze_", "pose_", "AU")
    for col in df.columns:
        if col.startswith("eye_lmk_"):
            if KEEP_LANDMARKS:
                out[col] = df[col]
            continue
        if col.startswith(keep_prefixes):
            out[col] = df[col]
    return out


def save_face_data() -> dict:
    """Write data/face/<sid>_face.csv and <sid>_face.json. Returns the json dict."""
    info = {
        "session_id": sid,
        "openface": "2.2.0",
        "arguments": ["-aus", "-pose", "-gaze"],
        "camera_px": [CAM_W, CAM_H],
        "device": FACE_DEVICE,
        "landmarks_kept": KEEP_LANDMARKS,
        "images_written": False,
        "emotion_labels": None,
        "anchor_source": None,
        "anchor_unix_ms": None,
        "anchor_check_unix_ms": None,
        "script_start_unix_ms": script_start_ms,
        "rows": 0,
    }
    if not ENABLE_OPENFACE:
        return info
    if not face_csv.is_file() or face_csv.stat().st_size == 0:
        print(f"OpenFace wrote no CSV at {face_csv} - no face stream this session.")
        info["problem"] = "no openface csv"
        return info
    # OpenFace flushes on exit; give the file a moment after terminate().
    for _ in range(20):
        try:
            df = read_openface_csv(face_csv)
            break
        except (OSError, pd.errors.ParserError, ValueError):
            time.sleep(0.25)
    else:
        info["problem"] = "openface csv unreadable"
        return info
    if "timestamp" not in df or df.empty:
        info["problem"] = "openface csv has no rows"
        return info

    check = None
    if len(_arrivals) >= 5:
        check = float(pd.Series(_arrivals).quantile(0.05))
    if anchor_ms is not None:
        anchor, source = float(anchor_ms), "starting_tracking_line"
    elif check is not None:
        anchor, source = check, "row_arrival_p05"
    else:
        anchor, source = float(script_start_ms), "script_start (UNRELIABLE: seconds early)"
    info.update({"anchor_source": source, "anchor_unix_ms": anchor,
                 "anchor_check_unix_ms": check,
                 "anchor_check_minus_anchor_ms": (check - anchor) if check is not None else None})

    out = reduce_face(df, anchor)
    path = session.stream_path("face")
    out.to_csv(path, index=False)
    span_s = max((df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]), 1e-9)
    info.update({
        "rows": int(len(out)),
        "fps": round(len(out) / span_s, 2),
        "success_share": round(float(pd.to_numeric(df.get("success"), errors="coerce").mean()), 4)
        if "success" in df else None,
        "confidence_median": round(float(pd.to_numeric(df.get("confidence"), errors="coerce").median()), 3)
        if "confidence" in df else None,
        "columns": int(out.shape[1]),
        "openface_notes": _openface_lines[:50],
    })
    session.stream_path("face", ext="json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"Face stream saved: {path}  ({len(out):,} rows, {info['fps']} fps, "
          f"success {info['success_share']}, anchor from {source})")
    return info


_stopped = threading.Event()
_stop_lock = threading.Lock()
_stop_done = False


def stop_recording():
    """Stop OpenFace and write the face stream, exactly once.

    Called from the stop-watcher thread and again from the main loop's ``finally``;
    the lock makes the second caller wait for the first to finish, so the scratch
    folder is not deleted under a save that is still running.
    """
    global _stop_done
    with _stop_lock:
        if _stop_done:
            return
        _stopped.set()
        _save_and_stop()
        _stop_done = True


def _save_and_stop():
    print('Stopping face recording...')
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    if openface_log is not None:
        try:
            openface_log.close()
        except OSError:
            pass
    try:
        save_face_data()
    except Exception as exc:  # noqa: BLE001 - never lose the session over the json
        print(f"!! could not save the face stream: {exc}")
    print('Face recording stopped.')


def listen_for_stop():
    """Watch the shared stop signal (stop file, or F12 for a manual abort)."""
    while not _stopped.is_set():
        if session.stop_requested():
            stop_recording()
            return
        time.sleep(0.25)


threading.Thread(target=listen_for_stop, daemon=True).start()

# ---------------------------------------------------------------------------
# Live readout: face tracking state, not an emotion
# ---------------------------------------------------------------------------
face_update_interval = 0.5
last_face_check_time = time.time()
readout_rect = (10, screen_height - 60, 420, 32)
face_state = {"confidence": None, "success": None, "fps": None, "rows": 0}
_last_rows = (time.time(), 0)


def latest_face_row(path, _cache={}):
    """Read only the last row of OpenFace's CSV (the whole file was re-parsed twice a second before)."""
    header = _cache.get(path)
    try:
        with open(path, 'rb') as fh:
            if header is None:
                header = [h.strip() for h in fh.readline().decode('utf-8', 'replace').strip().split(',')]
                _cache[path] = header
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            block = min(16384, end)
            fh.seek(end - block)
            lines = [ln for ln in fh.read(block).splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        values = lines[-1].decode('utf-8', 'replace').split(',')
        if len(values) != len(header):
            return None
        row = {}
        for k, v in zip(header, values):
            try:
                row[k] = float(v.strip())
            except ValueError:
                row[k] = v.strip()
        return row
    except (OSError, ValueError):
        return None


def poll_face():
    global _last_rows
    row = latest_face_row(face_csv) if face_csv.is_file() else None
    if row is None:
        return
    now = time.time()
    ts = row.get("timestamp")
    if _finite(ts):
        _arrivals.append(now * 1000 - float(ts) * 1000)
    face_state["confidence"] = row.get("confidence")
    face_state["success"] = row.get("success")
    frame = row.get("frame")
    if _finite(frame):
        t_prev, f_prev = _last_rows
        if now - t_prev >= 5:
            face_state["fps"] = (float(frame) - f_prev) / (now - t_prev)
            _last_rows = (now, float(frame))


# Visualization loop
try:
    font = pygame.font.SysFont('Arial', 24)
    while not _stopped.is_set():
        pygame.event.pump()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                stop_recording()

        window.fill((0, 0, 0))
        pygame.draw.circle(window, (255, 255, 255), (int(smoothed_x), int(smoothed_y)),
                           circle_radius, outline_thickness)

        if ENABLE_OPENFACE:
            ok = face_state["success"] == 1.0
            conf = face_state["confidence"]
            fps = face_state["fps"]
            colour = (80, 200, 80) if ok else (200, 80, 80)
            text = "face: " + ("tracking" if ok else "no face") + \
                   (f"  conf {conf:.2f}" if _finite(conf) else "") + \
                   (f"  {fps:.0f} fps" if _finite(fps) else "")
            pygame.draw.rect(window, colour, readout_rect, 0)
            window.blit(font.render(text, True, (0, 0, 0)), (readout_rect[0] + 10, readout_rect[1] + 4))

        pygame.display.flip()
        clock.tick(60)

        if ENABLE_OPENFACE and time.time() - last_face_check_time > face_update_interval:
            poll_face()
            last_face_check_time = time.time()

        if keyboard.is_pressed("f12"):
            break
finally:
    stop_recording()
    if my_eyetracker is not None:
        try:
            my_eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
        except Exception:
            pass
    pygame.quit()

    # The scratch folder holds OpenFace's raw CSV (with the eye landmarks), its
    # details file and its log. The face stream has been written to data/face; the
    # raw copy goes. Nothing in it is an image, but it is still not kept twice.
    if output_dir.is_dir():
        for attempt in range(10):
            try:
                shutil.rmtree(output_dir)
                break
            except (PermissionError, OSError):
                time.sleep(0.5)
        else:
            print(f"Could not delete {output_dir} - still in use")
    print("Gaze and face overlay stopped.")
