"""Face recorder: OpenFace action units, head pose and eyelid aperture, no window.

    data/face/<sid>_face.csv     one row per webcam frame OpenFace processed
    data/face/<sid>_face.json    how it was recorded, and the clock anchor

Added 2026-11-10. The face stream was written on 2 Nov inside
``Emotion_gaze_visualization.py``, the Valorant-era overlay: a borderless full-screen
pygame window with the gaze dot, which also needs the Tobii library. Lab PCs skipped it
(``PLAYSMART_SKIP=eda,emotion``) and a player's own PC has no tracker, so no League session
ever got a face stream. This is the same recording without the overlay: it needs a webcam
and OpenFace and nothing else, runs at either tier, and reports progress to the desktop
app while the game is on. The overlay script is unchanged and still used when
``PLAYSMART_OVERLAY=1``.

What is stored (identical to the 2 Nov stream):

* 17 action unit intensities (``AU*_r``, 0-5) and 18 presences (``AU*_c``), head pose
  (``pose_T*`` mm, ``pose_R*`` rad), webcam gaze vectors and angles, tracking
  ``confidence`` / ``success``, and a per-eye eyelid aperture (height / width of the eyelid
  contour; the eye-openness signal the Tobii Pro Spark does not have).
* **No image, no video, no emotion label, no face landmarks.** OpenFace runs without
  ``-simalign``, ``-hogalign``, ``-tracked``, ``-2Dfp``, ``-3Dfp``; the eye-region landmarks
  are reduced to the aperture and dropped unless ``PLAYSMART_FACE_KEEP_LANDMARKS=1``.
* Timing: OpenFace's ``timestamp`` counts from the moment it opened the webcam. The anchor
  is the wall time at which it prints ``Starting tracking``, cross-checked against the
  arrival time of rows (the smallest ``now - timestamp``); both are in the json.
* Load (2026-11-10): OpenFace starts below normal priority and on the last few logical
  processors only (a quarter of the machine, 1 to 4), so the game always gets a core
  first. When the game is busy OpenFace processes fewer frames a second; each row keeps
  the time its frame was captured. ``priority``, ``cpu_cores`` and ``fps`` are in the json.

This measures a person's face. It is off unless ``PLAYSMART_FACE=1`` is set on this PC
(``session.face_enabled``); switch it on only for players who agreed to it.

Environment:
    PLAYSMART_FACE=1                 record the face stream (default off)
    PLAYSMART_FACE_CAM=640x480       webcam capture size
    PLAYSMART_FACE_DEVICE=0          webcam index
    PLAYSMART_FACE_KEEP_LANDMARKS=1  also keep the 56 eye-region landmarks (2D)
    PLAYSMART_OPENFACE_EXE=<path>    FeatureExtraction.exe when it is not in a known place
    PLAYSMART_FACE_CORES=2           logical processors OpenFace may use (default: a quarter,
                                     1-4; ``all`` = no limit)
    PLAYSMART_FACE_PRIORITY=below    below (default) | idle | normal
    PLAYSMART_FACE_MODEL=fast        the lighter CLNF landmark model, about half the CPU

    python src/face_recorder.py            # started by key_listener.py; stops on the stop file
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
POLL_S = 0.5
PROGRESS_EVERY_S = 2.0

# OpenFace's eye model has 28 landmarks per eye: 0-7 pupil boundary, 8-19 eyelid contour,
# 20-27 iris boundary (eye 0 = indices 0-27, eye 1 = 28-55). Index layout from the OpenFace
# source; TESTING.md T4b checks it by comparing aperture dips with AU45 (blink).
EYELID = {"0": range(8, 20), "1": range(36, 48)}


def cam_size() -> tuple:
    raw = os.environ.get("PLAYSMART_FACE_CAM", "640x480").lower().replace(" ", "")
    try:
        w, h = (int(v) for v in raw.split("x"))
        return w, h
    except ValueError:
        print(f"PLAYSMART_FACE_CAM={raw!r} is not WxH - using 640x480")
        return 640, 480


def fast_model(exe: Path) -> "str | None":
    """The lighter landmark model (CLNF), when PLAYSMART_FACE_MODEL=fast and it is installed.

    OpenFace's default is CE-CLM: better landmarks on hard poses, roughly twice the CPU.
    The action units are computed from the aligned face either way. Off by default; for a
    PC where the default cannot hold 10 frames a second inside its core limit.
    """
    if os.environ.get("PLAYSMART_FACE_MODEL", "").strip().lower() != "fast":
        return None
    rel = "model/main_clnf_general.txt"
    if (Path(exe).parent / rel).is_file():
        return rel
    print(f"PLAYSMART_FACE_MODEL=fast, but {rel} is not in the OpenFace folder - using the default model")
    return None


def command(exe: Path, out_dir: Path, sid: str) -> list:
    """The OpenFace command line. Nothing in it writes an image."""
    w, h = cam_size()
    model = fast_model(exe)
    return [str(exe), "-device", os.environ.get("PLAYSMART_FACE_DEVICE", "0").strip() or "0",
            "-cam_width", str(w), "-cam_height", str(h),
            "-aus", "-pose", "-gaze",
            *(["-mloc", model] if model else []),
            "-out_dir", str(out_dir), "-of", f"{sid}_face"]


def openface_env(cores: int) -> dict:
    """OpenFace's environment: its BLAS threads capped to the cores it is allowed."""
    env = dict(os.environ)
    if cores:
        env["OPENBLAS_NUM_THREADS"] = str(cores)
        env["OMP_NUM_THREADS"] = str(cores)
    return env


# ----------------------------------------------------------------------------- reducing
def eyelid_aperture(df, eye: str):
    """Height / width of the eyelid contour of one eye: an eye aspect ratio, distance-free."""
    import pandas as pd
    xs = [f"eye_lmk_x_{i}" for i in EYELID[eye]]
    ys = [f"eye_lmk_y_{i}" for i in EYELID[eye]]
    if not all(c in df for c in xs + ys):
        return pd.Series([float("nan")] * len(df), index=df.index)
    width = df[xs].max(axis=1) - df[xs].min(axis=1)
    height = df[ys].max(axis=1) - df[ys].min(axis=1)
    return (height / width.where(width > 0)).round(4)


def _keep_raw(col: str) -> bool:
    """Columns of OpenFace's CSV worth loading: everything but the 3D eye landmarks."""
    c = col.strip()
    return not c.startswith(("eye_lmk_X_", "eye_lmk_Y_", "eye_lmk_Z_"))


def read_openface_csv(path: Path):
    import pandas as pd
    df = pd.read_csv(path, usecols=_keep_raw, skipinitialspace=True)
    df.columns = df.columns.str.strip()
    return df


def reduce_face(df, anchor_ms: float, keep_landmarks: bool = False):
    """OpenFace's rows as the face stream: wall-clock time, AUs, pose, gaze, aperture."""
    import pandas as pd
    out = pd.DataFrame(index=df.index)
    out["unix_time"] = (anchor_ms + df["timestamp"] * 1000.0).round().astype("int64")
    out["openface_timestamp_s"] = df["timestamp"]
    for col in ("frame", "confidence", "success"):
        if col in df:
            out[col] = df[col]
    out["eye_aperture_0"] = eyelid_aperture(df, "0")
    out["eye_aperture_1"] = eyelid_aperture(df, "1")
    for col in df.columns:
        if col.startswith("eye_lmk_"):
            if keep_landmarks:
                out[col] = df[col]
            continue
        if col.startswith(("gaze_", "pose_", "AU")):
            out[col] = df[col]
    return out


def latest_row(path: Path, cache: dict) -> "dict | None":
    """The last complete row of OpenFace's growing CSV, without parsing the file."""
    header = cache.get("header")
    try:
        with open(path, "rb") as fh:
            if header is None:
                header = [h.strip() for h in fh.readline().decode("utf-8", "replace").strip().split(",")]
                if len(header) < 5:
                    return None
                cache["header"] = header
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            block = min(32768, end)
            fh.seek(end - block)
            lines = [ln for ln in fh.read(block).splitlines() if ln.strip()]
        for raw in reversed(lines[1:] if len(lines) > 1 else []):
            values = raw.decode("utf-8", "replace").split(",")
            if len(values) != len(header):
                continue                      # the row being written right now
            row = {}
            for k, v in zip(header, values):
                try:
                    row[k] = float(v.strip())
                except ValueError:
                    row[k] = v.strip()
            return row
    except (OSError, ValueError):
        return None
    return None


# ----------------------------------------------------------------------------- recorder
class FaceRecorder:
    """Runs OpenFace for one session and turns its output into the face stream."""

    def __init__(self, sid: str, exe: "Path | None" = None, scratch: "Path | None" = None):
        self.sid = sid
        self.exe = exe or session.openface_exe()
        self.scratch = scratch or (REPO / "output" / sid)
        self.raw_csv = self.scratch / f"{sid}_face.csv"
        self.keep_landmarks = os.environ.get("PLAYSMART_FACE_KEEP_LANDMARKS", "0") == "1"
        self.script_start_ms = int(time.time() * 1000)
        self.anchor_ms: "int | None" = None
        self.arrivals: list = []
        self.notes: list = []
        self.proc = None
        self._log = None
        self._cache: dict = {}
        self._recent: list = []          # (wall time, success) of the last polls
        self._frames: list = []          # (wall time, frame number) for the fps estimate
        self._last_progress = 0.0
        self.priority = "normal"
        self.cores = 0
        self.cores_applied = False
        self.model = "default"
        self.state = {"rows": 0, "confidence": None, "success_recent": None, "fps": None}

    def start(self) -> bool:
        if self.exe is None:
            print("!! OpenFace (FeatureExtraction.exe) not found - no face stream this session.")
            print("   Put the OpenFace_2.2.0_win_x64 folder next to the rig, or set PLAYSMART_OPENFACE_EXE.")
            return False
        self.scratch.mkdir(parents=True, exist_ok=True)
        cmd = command(self.exe, self.scratch, self.sid)
        self.model = "clnf" if "-mloc" in cmd else "default"
        # OpenFace is the one heavy thing the rig runs next to the game (2026-11-10): it
        # starts below normal priority, so the game always gets a core before it does, and
        # on the last few logical processors only, so it cannot spread over the machine.
        # The price is its frame rate when the game is busy; its timestamps are real time,
        # so a slower stream is still a correctly timed one.
        self.priority = session.face_priority()
        self.cores = session.face_cores()
        flags = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | session.priority_flags(self.priority)}
                 if os.name == "nt" else {})
        try:
            # stderr to a file: a PIPE nobody reads fills up and stalls OpenFace.
            self._log = open(self.scratch / "openface.log", "w", encoding="utf-8")
            self.proc = subprocess.Popen(cmd, cwd=str(Path(self.exe).parent), stdout=subprocess.PIPE,
                                         stderr=self._log, text=True, bufsize=1, env=openface_env(self.cores), **flags)
        except OSError as exc:
            print(f"!! OpenFace could not start ({exc}) - no face stream this session.")
            return False
        self.cores_applied = session.limit_cores(self.proc.pid, self.cores) if self.cores else False
        threading.Thread(target=self._monitor, daemon=True).start()
        w, h = cam_size()
        limit = (f"{self.cores} of {os.cpu_count()} logical processors" if self.cores_applied
                 else "no core limit" if not self.cores else "core limit not applied")
        print(f"OpenFace: {w}x{h} webcam, -aus -pose -gaze, no images -> {self.sid}_face.csv "
              f"(priority {self.priority}, {limit})")
        return True

    def _monitor(self) -> None:
        """Take the clock anchor from OpenFace's own announcement; keep reading so the pipe never blocks."""
        for line in self.proc.stdout:
            line = line.rstrip()
            if self.anchor_ms is None and "Starting tracking" in line:
                self.anchor_ms = int(time.time() * 1000)
                print(f"OpenFace tracking started at {self.anchor_ms}")
            elif line and len(self.notes) < 200:
                self.notes.append(line)

    def poll(self) -> None:
        """Read the newest row: clock cross-check, tracking state, progress for the app."""
        now = time.time()
        row = latest_row(self.raw_csv, self._cache) if self.raw_csv.is_file() else None
        if row is not None:
            ts, frame = row.get("timestamp"), row.get("frame")
            if isinstance(ts, float):
                self.arrivals.append(now * 1000 - ts * 1000)
            if isinstance(frame, float):
                self.state["rows"] = int(frame)
                self._frames.append((now, frame))
                self._frames = [f for f in self._frames if now - f[0] <= 10.0]
                if len(self._frames) >= 2 and self._frames[-1][0] - self._frames[0][0] >= 2.0:
                    self.state["fps"] = round((self._frames[-1][1] - self._frames[0][1])
                                              / (self._frames[-1][0] - self._frames[0][0]), 1)
            conf = row.get("confidence")
            self.state["confidence"] = round(conf, 2) if isinstance(conf, float) else None
            self._recent.append((now, 1.0 if row.get("success") == 1.0 else 0.0))
            self._recent = [r for r in self._recent if now - r[0] <= 10.0]
            self.state["success_recent"] = round(sum(r[1] for r in self._recent) / len(self._recent), 2)
        if now - self._last_progress >= PROGRESS_EVERY_S:
            self._last_progress = now
            self.write_progress()

    def progress_path(self) -> Path:
        return session.stream_dir("face") / f"{self.sid}_face.progress.json"

    def write_progress(self) -> None:
        alive = self.proc is not None and self.proc.poll() is None
        info = {**self.state, "running": alive, "tracking": self.anchor_ms is not None,
                "updated_ms": int(time.time() * 1000)}
        try:
            p = self.progress_path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(info), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass

    def info(self) -> dict:
        w, h = cam_size()
        return {
            "session_id": self.sid,
            "openface": "2.2.0",
            "arguments": ["-aus", "-pose", "-gaze"],
            "camera_px": [w, h],
            "device": os.environ.get("PLAYSMART_FACE_DEVICE", "0"),
            "landmarks_kept": self.keep_landmarks,
            # how OpenFace was kept out of the game's way; its frame rate depends on both
            "priority": self.priority,
            "cpu_cores": self.cores if self.cores_applied else None,
            "cpu_logical": os.cpu_count(),
            "model": self.model,
            "images_written": False,
            "emotion_labels": None,
            "recorder": "face_recorder.py (no overlay)",
            "anchor_source": None,
            "anchor_unix_ms": None,
            "anchor_check_unix_ms": None,
            "script_start_unix_ms": self.script_start_ms,
            "rows": 0,
        }

    def save(self) -> dict:
        """Write data/face/<sid>_face.csv and .json from OpenFace's raw CSV. Returns the json dict."""
        import pandas as pd
        info = self.info()
        json_path = session.stream_path("face", ext="json", sid=self.sid)
        df = None
        if not self.raw_csv.is_file() or self.raw_csv.stat().st_size == 0:
            info["problem"] = "openface wrote no csv (webcam busy or not found?)"
        else:
            for _ in range(20):                          # the file settles a moment after the stop
                try:
                    df = read_openface_csv(self.raw_csv)
                    break
                except (OSError, pd.errors.ParserError, ValueError):
                    time.sleep(0.25)
            if df is None:
                info["problem"] = "openface csv unreadable"
            elif "timestamp" not in df or df.empty:
                info["problem"] = "openface csv has no rows"
                df = None
        if df is None:
            info["openface_notes"] = self.notes[:50]
            print(f"!! face stream not saved: {info['problem']}")
            try:
                json_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
            except OSError:
                pass
            return info
        # a hard stop can leave a half-written last line: drop rows without a timestamp
        df = df[pd.to_numeric(df["timestamp"], errors="coerce").notna()].reset_index(drop=True)
        # A row cannot arrive before its frame was captured, so every ``now - timestamp`` is
        # an upper bound on the anchor and the smallest one is the tightest. It was the 5th
        # percentile until OpenFace got a core limit (2026-11-10): when it processes slower
        # than the webcam delivers, frames wait in its queue (up to ~6 s) and all but the
        # first rows arrive late, which says nothing about the anchor.
        check = float(min(self.arrivals)) if len(self.arrivals) >= 5 else None
        if self.anchor_ms is not None:
            anchor, source = float(self.anchor_ms), "starting_tracking_line"
        elif check is not None:
            anchor, source = check, "row_arrival_p05"
        else:
            anchor, source = float(self.script_start_ms), "script_start (UNRELIABLE: seconds early)"
        out = reduce_face(df, anchor, self.keep_landmarks)
        path = session.stream_path("face", sid=self.sid)
        out.to_csv(path, index=False)
        span_s = max(float(df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]), 1e-9)
        info.update({
            "anchor_source": source, "anchor_unix_ms": anchor, "anchor_check_unix_ms": check,
            "anchor_check_minus_anchor_ms": (check - anchor) if check is not None else None,
            "rows": int(len(out)),
            "fps": round(len(out) / span_s, 2),
            "success_share": round(float(pd.to_numeric(df["success"], errors="coerce").mean()), 4) if "success" in df else None,
            "confidence_median": round(float(pd.to_numeric(df["confidence"], errors="coerce").median()), 3) if "confidence" in df else None,
            "columns": int(out.shape[1]),
            "au_columns": sorted(c for c in out.columns if c.startswith("AU")),
            "openface_notes": self.notes[:50],
        })
        json_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(f"Face stream saved: {path.name} ({len(out):,} rows, {info['fps']} fps, "
              f"face found in {info['success_share']} of frames, anchor from {source})")
        return info

    def stop(self) -> dict:
        print("Stopping the face recording...")
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
        try:
            info = self.save()
        except Exception as exc:  # noqa: BLE001 - never lose the session over the face stream
            print(f"!! could not save the face stream: {exc}")
            info = {"problem": str(exc)}
        try:
            self.progress_path().unlink()
        except OSError:
            pass
        # The scratch folder holds OpenFace's raw CSV (with the eye landmarks) and its log.
        # The face stream is in data/face now; the raw copy goes.
        if self.scratch.is_dir():
            for _ in range(10):
                try:
                    shutil.rmtree(self.scratch)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                print(f"Could not delete {self.scratch} - still in use")
        return info


def main() -> int:
    sid = session.session_id()
    if not session.face_wanted():
        print("Face stream is off on this PC (PLAYSMART_FACE is not 1) - nothing recorded.")
        return 0
    rec = FaceRecorder(sid)
    if not rec.start():
        return 1
    died_at = None
    try:
        while not session.stop_requested():
            time.sleep(POLL_S)
            rec.poll()
            if rec.proc.poll() is not None and died_at is None:
                died_at = time.time()
                print(f"!! OpenFace exited by itself (code {rec.proc.returncode}) - webcam in use by another "
                      "app, or unplugged. What it recorded so far is kept.")
                rec.write_progress()
    finally:
        info = rec.stop()
    return 0 if info.get("rows") else 1


if __name__ == "__main__":
    sys.exit(main())
