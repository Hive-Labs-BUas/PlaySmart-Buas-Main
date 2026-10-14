"""Gaze recorder: Tobii samples to data/gaze/<session_id>_gaze.csv.

Modified 2026-09 (iteration 5, League of Legends port):
* file named from the shared session id (session.py) instead of a local timestamp;
* stops on the orchestrator's stop signal and saves in `finally` — no more truncated
  files from being terminated;
* a missing tracker refuses to record; PLAYSMART_MOCK_GAZE=1 writes mock rows flagged
  gaze_valid=False for plumbing tests only;
* two commented-out legacy copies of this script removed (they are in git history).

Modified 2026-10-14: validity is taken from the Tobii's own flags, and a sample the
tracker could not place (a blink, a look away) is written as a row with empty
coordinates and gaze_valid=False instead of being dropped. The Tobii reports an
untracked eye as NaN, not None; the old `None in (...)` test let those through,
`int(nan * width)` then raised inside the callback and the SDK swallowed the error,
so every invalid sample silently vanished and every written row said valid. The
28 Sep 2026 sessions are 100 % "valid" with 2-15 % of samples missing for that reason.
Also warns on-screen when the player sits outside 50-75 cm from the tracker.

Modified 2026-09-25: keeps more of what the Tobii already sends - per-eye validity,
the gaze origin in user coordinates (head position and distance to the screen), the
tracker's own timestamps, and eye openness when the SDK/device provide it (blink
rate). Writes `<sid>_gaze.json` with the tracker, its frequency and the display
geometry, so gaze accuracy can be stated in degrees, not just pixels.
"""

import math
import time
import os
import sys
import json
import random
from pathlib import Path
import pandas as pd
import screeninfo
import tobii_research as tr
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session

# ---------------------------
# SCREEN SETUP
# ---------------------------
screen = screeninfo.get_monitors()[0]
screen_width, screen_height = screen.width, screen.height
screen_resolution = f"{screen_width}x{screen_height}p"

# ---------------------------
# FOLDER SETUP
# ---------------------------
data_folder = 'data/gaze'
os.makedirs(data_folder, exist_ok=True)

gaze_data_list = []

# ---------------------------
# EYE TRACKER SETUP
# ---------------------------
eyetrackers = tr.find_all_eyetrackers()
my_eyetracker = eyetrackers[0] if eyetrackers else None

# Previously this fell back to random mock data when no tracker was found, and
# wrote a whole session of it to disk indistinguishable from real gaze. Refuse
# instead: a missing tracker is a setup problem to fix before recording, not
# something to paper over. PLAYSMART_MOCK_GAZE=1 re-enables it for plumbing
# tests, and marks every row gaze_valid=False so it can never be mistaken for
# real data.
use_mock_data = os.environ.get("PLAYSMART_MOCK_GAZE", "0") == "1"

if my_eyetracker is None and not use_mock_data:
    print("No eye tracker found. Refusing to record fabricated gaze data.")
    print("Connect the Tobii and try again, or set PLAYSMART_MOCK_GAZE=1 to test plumbing.")
    sys.exit(1)

if use_mock_data:
    print("PLAYSMART_MOCK_GAZE=1 — writing MOCK gaze, every row flagged gaze_valid=False")
else:
    print(f"Connected to: {my_eyetracker.model}")

# ---------------------------
# SMOOTHED GAZE
# ---------------------------
smoothed_x, smoothed_y = screen_width // 2, screen_height // 2

# ---------------------------
# BASELINE SETTINGS
# ---------------------------
baseline_samples = 60
baseline_pupil_values = []
baseline_pupil = None

# ---------------------------
# DELTA TRACKING (NEW)
# ---------------------------
previous_avg_pupil = None

# ---------------------------
# HEAD DISTANCE WARNING
# ---------------------------
# The Tobii Pro Spark tracks from about 45 to 85 cm; accuracy is best in the
# middle. Pc_3's player on 28 Sep 2026 sat at ~79 cm. Warn (on this console and in
# the session json) when the rolling median leaves the comfortable band.
HEAD_MIN_MM, HEAD_MAX_MM = 500.0, 750.0
HEAD_WARN_EVERY_S = 15.0
_head_window = []
_head_last_warn = 0.0
_head_warnings = 0


def _finite(v):
    """True for a real number the tracker actually measured (not None, not NaN)."""
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _head_check(head_dist, unix_time):
    global _head_last_warn, _head_warnings
    if not _finite(head_dist):
        return
    _head_window.append(float(head_dist))
    if len(_head_window) > 600:          # ~10 s at 60 Hz
        del _head_window[:-600]
    if unix_time / 1000 - _head_last_warn < HEAD_WARN_EVERY_S or len(_head_window) < 300:
        return
    med = sorted(_head_window)[len(_head_window) // 2]
    if med < HEAD_MIN_MM or med > HEAD_MAX_MM:
        _head_last_warn = unix_time / 1000
        _head_warnings += 1
        where = "too close" if med < HEAD_MIN_MM else "too far"
        print(f"!! head distance {med / 10:.0f} cm - {where} (keep 50-75 cm from the screen)",
              flush=True)


# ---------------------------
# EYE OPENNESS (optional stream, newer SDKs / devices only)
# ---------------------------
latest_openness = {"left": None, "right": None, "left_valid": None, "right_valid": None}
openness_supported = False


def openness_callback(data):
    latest_openness["left"] = data.get("left_eye_openness_value")
    latest_openness["right"] = data.get("right_eye_openness_value")
    latest_openness["left_valid"] = data.get("left_eye_validity")
    latest_openness["right_valid"] = data.get("right_eye_validity")


def _origin(gaze_data, side):
    """(x, y, z) of an eye in the user coordinate system, mm; None when not tracked."""
    o = gaze_data.get(f"{side}_gaze_origin_in_user_coordinate_system")
    ok = gaze_data.get(f"{side}_gaze_origin_validity")
    if not o or (ok is not None and not ok) or not all(_finite(v) for v in o):
        return (None, None, None)
    return tuple(float(v) for v in o)


# ---------------------------
# MOCK DATA
# ---------------------------
def generate_mock_gaze():
    return random.uniform(0.1, 0.8), random.uniform(0.1, 0.8)

def generate_mock_pupil():
    return random.uniform(2.5, 4.5)

# ---------------------------
# CALLBACK
# ---------------------------
def gaze_data_callback(gaze_data):
    global smoothed_x, smoothed_y
    global baseline_pupil, baseline_pupil_values
    global previous_avg_pupil

    unix_time = int(time.time() * 1000)

    # Gaze + pupil
    if use_mock_data:
        left_gaze_x, left_gaze_y = generate_mock_gaze()
        right_gaze_x, right_gaze_y = generate_mock_gaze()
        left_pupil = generate_mock_pupil()
        right_pupil = generate_mock_pupil()
        left_valid = right_valid = False
        lox = loy = loz = rox = roy = roz = None
        device_ts = system_ts = None
    else:
        left_gaze_x, left_gaze_y = gaze_data.get('left_gaze_point_on_display_area', (None, None))
        right_gaze_x, right_gaze_y = gaze_data.get('right_gaze_point_on_display_area', (None, None))
        left_pupil = gaze_data.get('left_pupil_diameter', None)
        right_pupil = gaze_data.get('right_pupil_diameter', None)
        left_valid = gaze_data.get('left_gaze_point_validity')
        right_valid = gaze_data.get('right_gaze_point_validity')
        lox, loy, loz = _origin(gaze_data, "left")
        rox, roy, roz = _origin(gaze_data, "right")
        device_ts = gaze_data.get('device_time_stamp')
        system_ts = gaze_data.get('system_time_stamp')

    # Head position: the mean of the two eye origins, in the tracker's user
    # coordinate system (mm; z is the distance from the screen plane). Free with
    # every sample, and what turns a pixel error into a visual-angle error.
    zs = [z for z in (loz, roz) if z is not None]
    head_dist = sum(zs) / len(zs) if zs else None
    xs = [v for v in (lox, rox) if v is not None]
    ys = [v for v in (loy, roy) if v is not None]
    head_x = sum(xs) / len(xs) if xs else None
    head_y = sum(ys) / len(ys) if ys else None

    # Average gaze.
    #
    # This used to substitute generate_mock_gaze() whenever the tracker lost the
    # eyes — a blink, a glance away, glasses, head movement — writing random
    # coordinates into screen_x/screen_y that were indistinguishable from real
    # gaze downstream. Worse, the fabricated values were uniform over 0.1-0.8,
    # a box that excludes the bottom-right minimap, so tracking loss showed up
    # as artificially low minimap attention.
    #
    # Now a dropout is recorded as a dropout: coordinates are None and
    # gaze_valid is False, so analysis can exclude those samples instead of
    # silently averaging noise into the result.
    #
    # 14 Oct 2026: the Tobii marks an untracked eye with validity False and NaN
    # coordinates, never None. Use its flags, and treat NaN as missing, so an
    # invalid sample is written as such instead of raising inside this callback
    # (the SDK swallows the exception and the sample is lost without a trace).
    left_ok = bool(left_valid) if left_valid is not None else _finite(left_gaze_x)
    right_ok = bool(right_valid) if right_valid is not None else _finite(right_gaze_x)
    left_ok = left_ok and _finite(left_gaze_x) and _finite(left_gaze_y)
    right_ok = right_ok and _finite(right_gaze_x) and _finite(right_gaze_y)
    if not left_ok:
        left_gaze_x = left_gaze_y = None
    if not right_ok:
        right_gaze_x = right_gaze_y = None
    gaze_valid = (not use_mock_data) and (left_ok or right_ok)

    if left_ok and right_ok:
        avg_x = (left_gaze_x + right_gaze_x) / 2
        avg_y = (left_gaze_y + right_gaze_y) / 2
    elif left_ok:
        avg_x, avg_y = left_gaze_x, left_gaze_y   # one eye is better than a gap
    elif right_ok:
        avg_x, avg_y = right_gaze_x, right_gaze_y
    else:
        avg_x = avg_y = None
    if avg_x is not None:
        smoothed_x = int(avg_x * screen_width)
        smoothed_y = int(avg_y * screen_height)
    else:
        smoothed_x = smoothed_y = None

    # Average pupil (NaN when the eye is not tracked)
    left_pupil = float(left_pupil) if _finite(left_pupil) else None
    right_pupil = float(right_pupil) if _finite(right_pupil) else None
    if left_pupil is not None and right_pupil is not None:
        avg_pupil = (left_pupil + right_pupil) / 2
    else:
        avg_pupil = left_pupil if left_pupil is not None else right_pupil

    _head_check(head_dist, unix_time)

    pupil_dilation = None
    pupil_delta_change = None

    if avg_pupil is not None:
        # ---------------------------
        # BASELINE BUILDING
        # ---------------------------
        if len(baseline_pupil_values) < baseline_samples:
            baseline_pupil_values.append(avg_pupil)

        elif baseline_pupil is None:
            baseline_pupil = sum(baseline_pupil_values) / len(baseline_pupil_values)
            print(f"Baseline established: {baseline_pupil:.2f} mm")

        # ---------------------------
        # BASELINE DILATION
        # ---------------------------
        if baseline_pupil is not None:
            pupil_dilation = avg_pupil - baseline_pupil

        # ---------------------------
        # DELTA CHANGE (NEW)
        # ---------------------------
        if previous_avg_pupil is not None:
            pupil_delta_change = avg_pupil - previous_avg_pupil

        previous_avg_pupil = avg_pupil

    # ---------------------------
    # STORE DATA
    # ---------------------------
    gaze_data_list.append({
        "unix_time": unix_time,
        "gaze_valid": gaze_valid,
        "left_gaze_x": left_gaze_x,
        "left_gaze_y": left_gaze_y,
        "right_gaze_x": right_gaze_x,
        "right_gaze_y": right_gaze_y,
        "screen_x": smoothed_x,
        "screen_y": smoothed_y,
        "left_pupil_diameter": left_pupil,
        "right_pupil_diameter": right_pupil,
        "avg_pupil_diameter": avg_pupil,        # absolute
        "baseline_pupil": baseline_pupil,
        "pupil_dilation": pupil_dilation,       # vs baseline
        "pupil_delta_change": pupil_delta_change,  # NEW
        "screen_resolution": screen_resolution,
        # 25 Sep 2026: kept from the same sample
        "left_valid": left_ok,
        "right_valid": right_ok,
        "head_x_mm": head_x,
        "head_y_mm": head_y,
        "head_dist_mm": head_dist,
        "device_time_stamp": device_ts,
        "system_time_stamp": system_ts,
        "left_openness": latest_openness["left"],
        "right_openness": latest_openness["right"],
    })

# Subscribe
if not use_mock_data:
    my_eyetracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, gaze_data_callback, as_dictionary=True)
    # Eye openness is a separate stream (tobii_research >= 1.10, and only on devices
    # that measure it). Absent, it costs nothing: the columns stay empty.
    try:
        my_eyetracker.subscribe_to(tr.EYETRACKER_EYE_OPENNESS_DATA, openness_callback,
                                   as_dictionary=True)
        openness_supported = True
        print("Eye openness stream: on")
    except Exception as exc:
        print(f"Eye openness stream: not available ({exc})")


def tracker_info():
    """What we know about the tracker and the screen, for <sid>_gaze.json."""
    info = {
        "screen_px": [screen_width, screen_height],
        "mock": use_mock_data,
        "eye_openness": openness_supported,
    }
    if my_eyetracker is None:
        return info
    for attr in ("model", "serial_number", "device_name", "firmware_version", "runtime_version"):
        try:
            info[attr] = getattr(my_eyetracker, attr)
        except Exception:
            pass
    try:
        info["frequency_hz"] = my_eyetracker.get_gaze_output_frequency()
    except Exception:
        pass
    try:
        area = my_eyetracker.get_display_area()
        info["display_mm"] = [round(area.width, 1), round(area.height, 1)]
        info["display_corners_mm"] = {
            "top_left": list(area.top_left), "top_right": list(area.top_right),
            "bottom_left": list(area.bottom_left),
        }
    except Exception:
        pass
    return info

# ---------------------------
# MAIN LOOP
# ---------------------------
print("Recording... stops when the game ends, or on F12.")

try:
    # session.stop_requested() covers both the orchestrator's stop signal and a
    # manual F12. With automatic start/stop there is no key press to wait for,
    # and this loop exiting normally is what reaches the to_csv() in `finally`.
    while not session.stop_requested():
        if use_mock_data:
            gaze_data_callback({})
        time.sleep(1/60)

# ---------------------------
# SAVE + PLOT
# ---------------------------
finally:
    if my_eyetracker is not None:
        my_eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
        if openness_supported:
            try:
                my_eyetracker.unsubscribe_from(tr.EYETRACKER_EYE_OPENNESS_DATA, openness_callback)
            except Exception:
                pass

    if not gaze_data_list:
        print("No data recorded.")
    else:
        # Save CSV
        csv_path = str(session.stream_path("gaze"))
        df = pd.DataFrame(gaze_data_list)
        df.to_csv(csv_path, index=False)

        valid = df["gaze_valid"].mean() if "gaze_valid" in df else float("nan")
        print(f"CSV saved: {csv_path}  ({len(df):,} samples, {valid:.1%} valid)")

        # Tracker and screen geometry, once per session, next to the samples.
        try:
            info = tracker_info()
            info.update({
                "session_id": session.session_id(),
                "samples": int(len(df)),
                "valid_share": round(float(valid), 4) if valid == valid else None,
                "head_dist_mm_median": (round(float(df["head_dist_mm"].dropna().median()), 1)
                                        if df["head_dist_mm"].notna().any() else None),
                "head_distance_warnings": _head_warnings,
                "head_distance_band_mm": [HEAD_MIN_MM, HEAD_MAX_MM],
                # samples per second actually written, against the tracker's rate:
                # with invalid rows kept this should match frequency_hz
                "samples_per_s": (round(len(df) * 1000 / max(1, int(df["unix_time"].iloc[-1] - df["unix_time"].iloc[0])), 1)
                                  if len(df) > 1 else None),
            })
            json_path = session.stream_path("gaze", ext="json")
            json_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
            print(f"Tracker info saved: {json_path}")
        except Exception as exc:
            print(f"Could not write the tracker info: {exc}")

        # ---------------------------
        # SAVE GRAPH (DILATION)
        # ---------------------------
        df_dilation = df.dropna(subset=["pupil_dilation"])

        if not df_dilation.empty:
            time_sec = (df_dilation["unix_time"] - df_dilation["unix_time"].iloc[0]) / 1000

            plt.figure()
            plt.plot(time_sec, df_dilation["pupil_dilation"])
            plt.xlabel("Time (seconds)")
            plt.ylabel("Pupil Dilation (mm)")
            plt.title("Pupil Dilation Over Time")
            plt.grid()

            image_path = os.path.join(data_folder, f'{session.session_id()}_pupil_dilation.png')
            plt.savefig(image_path)
            plt.close()

            print(f"Dilation graph saved: {image_path}")
        else:
            print("No dilation data (record longer or reduce baseline_samples).")

        # ---------------------------
        # SAVE GRAPH (ABSOLUTE SIZE)
        # ---------------------------
        df_abs = df.dropna(subset=["avg_pupil_diameter"])

        if not df_abs.empty:
            time_sec = (df_abs["unix_time"] - df_abs["unix_time"].iloc[0]) / 1000

            plt.figure()
            plt.plot(time_sec, df_abs["avg_pupil_diameter"])
            plt.xlabel("Time (seconds)")
            plt.ylabel("Pupil Diameter (mm)")
            plt.title("Absolute Pupil Diameter Over Time")
            plt.grid()

            image_path = os.path.join(data_folder, f'{session.session_id()}_pupil_absolute.png')
            plt.savefig(image_path)
            plt.close()

            print(f"Absolute graph saved: {image_path}")
        else:
            print("No absolute pupil data.")