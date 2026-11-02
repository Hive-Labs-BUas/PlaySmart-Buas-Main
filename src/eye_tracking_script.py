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

Modified 2026-11-02:
* `unix_time` is now the Tobii's own `system_time_stamp` converted to wall time with a
  clock offset measured at start (and re-measured at the end, both in the json). The
  callback's arrival time jittered by ~25 ms SD against the tracker clock on the 26 Oct
  session; it is kept as `callback_unix_time` for comparison.
* Per-eye 3D gaze point and gaze origin in the user coordinate system (mm) are kept, not
  just their average: visual angle and angular velocity (I-VT fixation filtering) need
  them. Per-eye pupil validity is kept too.
* Eye openness: the Pro Spark has no openness stream (Spectrum/Fusion only). The
  subscribe call succeeds anyway, so `eye_openness: true` was written to every json while
  the columns stayed empty. The json now says whether any openness sample *arrived*, and
  the columns are dropped when none did.
* Tracker notifications (connection lost/restored, calibration changed, device faults and
  warnings, stream buffer overflow) and stream errors are logged with a timestamp into
  the json, so a gap in the data can be explained instead of guessed at.

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
# TRACKER CLOCK -> WALL CLOCK
# ---------------------------
# The Tobii stamps every sample with its own monotonic clock (us). One offset to the
# PC's wall clock, measured as the median of a burst of paired readings, converts
# it; re-measured at the end to report drift. The callback's own time.time() is
# kept next to it (callback_unix_time) - it is what the archive used, and it is
# the number the ~25 ms jitter was measured on.
def measure_clock_offset_us(n: int = 25):
    """Median of (wall clock us - tracker system clock us) over n paired readings."""
    if use_mock_data or not hasattr(tr, "get_system_time_stamp"):
        return None
    pairs = []
    for _ in range(n):
        a = time.time()
        ts = tr.get_system_time_stamp()
        b = time.time()
        pairs.append(((a + b) / 2) * 1e6 - ts)
    pairs.sort()
    return pairs[len(pairs) // 2]


clock_offset_us = measure_clock_offset_us()
if clock_offset_us is None:
    print("Tracker clock offset: unavailable - unix_time falls back to the callback time")
else:
    print(f"Tracker clock offset: {clock_offset_us / 1e6:.3f} s (wall - tracker)")


def to_unix_ms(system_ts, fallback_ms: int) -> int:
    if clock_offset_us is None or not _finite(system_ts):
        return fallback_ms
    return int(round((float(system_ts) + clock_offset_us) / 1000.0))


# ---------------------------
# NOTIFICATIONS AND STREAM ERRORS (QC log, into <sid>_gaze.json)
# ---------------------------
notifications = []
_NOTIFICATION_NAMES = [n for n in dir(tr) if n.startswith("EYETRACKER_NOTIFICATION_")]


def _note(kind: str, payload) -> None:
    if len(notifications) >= 500:
        return
    try:
        detail = dict(payload) if isinstance(payload, dict) else str(payload)
    except Exception:  # noqa: BLE001
        detail = repr(payload)
    notifications.append({"unix_time": int(time.time() * 1000), "type": kind, "detail": detail})
    print(f"[tobii] {kind}: {detail}", flush=True)


def _make_notification_callback(kind: str):
    return lambda data: _note(kind, data)


def _stream_error_callback(data):
    _note("stream_error", data)


# ---------------------------
# EYE OPENNESS (optional stream, newer SDKs / devices only; NOT the Pro Spark)
# ---------------------------
latest_openness = {"left": None, "right": None, "left_valid": None, "right_valid": None}
openness_supported = False


openness_samples = 0


def openness_callback(data):
    global openness_samples
    openness_samples += 1
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


def _gaze_ucs(gaze_data, side):
    """(x, y, z) of where an eye looks on the screen plane, user coordinates, mm."""
    g = gaze_data.get(f"{side}_gaze_point_in_user_coordinate_system")
    ok = gaze_data.get(f"{side}_gaze_point_validity")
    if not g or (ok is not None and not ok) or not all(_finite(v) for v in g):
        return (None, None, None)
    return tuple(float(v) for v in g)


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
        lgx = lgy = lgz = rgx = rgy = rgz = None
        left_pupil_valid = right_pupil_valid = False
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
        lgx, lgy, lgz = _gaze_ucs(gaze_data, "left")
        rgx, rgy, rgz = _gaze_ucs(gaze_data, "right")
        left_pupil_valid = bool(gaze_data.get('left_pupil_validity', False))
        right_pupil_valid = bool(gaze_data.get('right_pupil_validity', False))
        device_ts = gaze_data.get('device_time_stamp')
        system_ts = gaze_data.get('system_time_stamp')

    callback_unix_time = unix_time
    unix_time = to_unix_ms(system_ts, callback_unix_time)

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

    # Average pupil (NaN when the eye is not tracked; the Tobii also flags it)
    left_pupil = float(left_pupil) if (_finite(left_pupil) and (left_pupil_valid or use_mock_data)) else None
    right_pupil = float(right_pupil) if (_finite(right_pupil) and (right_pupil_valid or use_mock_data)) else None
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
        # 2 Nov 2026: per-eye 3D gaze point and origin (user coordinate system, mm),
        # per-eye pupil validity, and the callback's own clock for comparison
        "callback_unix_time": callback_unix_time,
        "left_gaze_ucs_x": lgx, "left_gaze_ucs_y": lgy, "left_gaze_ucs_z": lgz,
        "right_gaze_ucs_x": rgx, "right_gaze_ucs_y": rgy, "right_gaze_ucs_z": rgz,
        "left_origin_x": lox, "left_origin_y": loy, "left_origin_z": loz,
        "right_origin_x": rox, "right_origin_y": roy, "right_origin_z": roz,
        "left_pupil_valid": left_pupil_valid,
        "right_pupil_valid": right_pupil_valid,
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
    # Every notification the installed SDK knows about, plus stream errors. Cheap,
    # silent when nothing happens, and the only way to tell "tracker dropped off
    # the USB" from "player left the room" afterwards.
    _subscribed_notifications = []
    for name in _NOTIFICATION_NAMES:
        try:
            my_eyetracker.subscribe_to(getattr(tr, name),
                                       _make_notification_callback(name[len("EYETRACKER_NOTIFICATION_"):].lower()),
                                       as_dictionary=True)
            _subscribed_notifications.append(name)
        except Exception:
            pass
    if hasattr(tr, "EYETRACKER_STREAM_ERRORS"):
        try:
            my_eyetracker.subscribe_to(tr.EYETRACKER_STREAM_ERRORS, _stream_error_callback, as_dictionary=True)
        except Exception:
            pass
    print(f"Notifications: {len(_subscribed_notifications)} kinds subscribed")


def tracker_info():
    """What we know about the tracker and the screen, for <sid>_gaze.json."""
    info = {
        "screen_px": [screen_width, screen_height],
        "mock": use_mock_data,
        # True only when the device actually sent openness samples. The subscribe
        # call succeeds on a Pro Spark, which has no such stream.
        "eye_openness": bool(openness_supported and openness_samples > 0),
        "eye_openness_subscribed": openness_supported,
        "eye_openness_samples": openness_samples,
        "sdk_version": getattr(tr, "__version__", None),
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
    clock_offset_end_us = measure_clock_offset_us() if my_eyetracker is not None else None
    if my_eyetracker is not None:
        my_eyetracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, gaze_data_callback)
        if openness_supported:
            try:
                my_eyetracker.unsubscribe_from(tr.EYETRACKER_EYE_OPENNESS_DATA, openness_callback)
            except Exception:
                pass
        for name in _NOTIFICATION_NAMES:
            try:
                my_eyetracker.unsubscribe_from(getattr(tr, name))
            except Exception:
                pass
        if hasattr(tr, "EYETRACKER_STREAM_ERRORS"):
            try:
                my_eyetracker.unsubscribe_from(tr.EYETRACKER_STREAM_ERRORS)
            except Exception:
                pass

    if not gaze_data_list:
        print("No data recorded.")
    else:
        # Save CSV
        csv_path = str(session.stream_path("gaze"))
        df = pd.DataFrame(gaze_data_list)
        if openness_samples == 0:
            # No device-side openness: do not ship two empty columns per sample.
            df = df.drop(columns=[c for c in ("left_openness", "right_openness") if c in df])
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
                # unix_time = (system_time_stamp + clock_offset_start_us) / 1000
                "clock_source": "tracker system clock" if clock_offset_us is not None else "callback time",
                "clock_offset_start_us": clock_offset_us,
                "clock_offset_end_us": clock_offset_end_us,
                "clock_drift_ms": (round((clock_offset_end_us - clock_offset_us) / 1000.0, 2)
                                   if clock_offset_us is not None and clock_offset_end_us is not None else None),
                "callback_jitter_ms_sd": (round(float((df["unix_time"] - df["callback_unix_time"]).std()), 2)
                                          if clock_offset_us is not None else None),
                "notifications": notifications,
                # samples per second actually written, against the tracker's rate:
                # with invalid rows kept this should match frequency_hz
                "samples_per_s": (round(len(df) * 1000 / max(1, int(df["unix_time"].iloc[-1] - df["unix_time"].iloc[0])), 1)
                                  if len(df) > 1 else None),
            })
            json_path = session.stream_path("gaze", ext="json")
            json_path.write_text(json.dumps(info, indent=2, default=str), encoding="utf-8")
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