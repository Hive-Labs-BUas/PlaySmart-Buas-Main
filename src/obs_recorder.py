"""Drive OBS Studio's recording from the capture orchestrator.

Why this exists
---------------
Video was the last stream still started and stopped by hand. That is why the
archive holds 364 minutes of footage containing 171 minutes of anything at all,
why several sessions have no video, and why several videos have no session: a
human decided when to press record, and a human decided what to call the file.

OBS Studio 28 and later ship ``obs-websocket`` v5 built in, so the orchestrator
can start and stop the recording on the same signal as every other stream, and
learn the output path from OBS itself instead of guessing which file in the
folder is newest.

The alignment problem, and why ``outputDuration`` solves it
-----------------------------------------------------------
A video file on its own cannot be placed on the game clock. The filesystem tells
you when the file was *written*, not when its first frame was *captured*, and
those differ by however long OBS took to finalise the container. Guessing that
offset is how gaze ends up drawn over the wrong moment.

``GetRecordStatus`` reports ``outputDuration``: how long the recording has been
running, in milliseconds. Read alongside the local clock, that gives frame zero
directly::

    t0 = now - outputDuration

Each sample is bracketed by a clock reading either side of the request, so the
round trip is measured rather than assumed, and the median of several samples is
taken. The result is written next to the video as ``<sid>_video.json``. With that
number, a frame at ``00:04:37`` in the file is at ``t0 + 277_000`` in wall-clock
time, and the gamestate stream converts that to a game time. Without it the video
is just a file.

Setup, once per machine
-----------------------
In OBS: Tools -> WebSocket Server Settings -> Enable. Copy the password and set
``PLAYSMART_OBS_PASSWORD``. Then ``poetry add obsws-python`` (needs Python 3.10,
which the Tobii wheels already pin us to).

Environment:
    PLAYSMART_OBS           "0" disables video entirely.
    PLAYSMART_OBS_HOST      default 127.0.0.1
    PLAYSMART_OBS_PORT      default 4455
    PLAYSMART_OBS_PASSWORD  from OBS's WebSocket settings.

Nothing here is allowed to take the capture down with it. OBS closed, websocket
off, wrong password, library missing - each prints a clear warning and returns
None, and the session records everything else. Video is worth having; it is not
worth losing gaze over.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import time
from pathlib import Path

import session

def _now_ms() -> float:
    """Wall clock in unix milliseconds, matching every other stream."""
    return time.time() * 1000

ENABLED = os.environ.get("PLAYSMART_OBS", "1") != "0"
HOST = os.environ.get("PLAYSMART_OBS_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLAYSMART_OBS_PORT", "4455"))
PASSWORD = os.environ.get("PLAYSMART_OBS_PASSWORD", "")

# How many (now, outputDuration) pairs to take when locating frame zero, and how
# long to keep trying. OBS reports outputDuration 0 for a short window after
# StartRecord, so zero samples are discarded rather than trusted.
OFFSET_SAMPLES = 5
OFFSET_TIMEOUT_S = 6.0
STOP_SETTLE_S = 2.0
# After StopRecord the muxer can hold the file open for several seconds (MP4
# finalisation, a slow disk). Moving it while OBS still has the handle raised
# WinError 32 on 27 Oct 2026 and left the video under OBS's name. Wait for the
# handle to be released and the size to stop changing before claiming the file.
RELEASE_TIMEOUT_S = 30.0


def _client():
    """Connect to OBS, or return None with a reason printed."""
    if not ENABLED:
        return None
    try:
        import obsws_python
    except ImportError:
        print("  !! obsws-python is not installed - no video this session.")
        print("     poetry add obsws-python")
        return None
    # The library logs a full traceback on a refused connection; the one-line
    # message below is the useful part.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    try:
        return obsws_python.ReqClient(host=HOST, port=PORT, password=PASSWORD, timeout=5)
    except Exception as exc:
        print(f"  !! could not reach OBS on {HOST}:{PORT} ({exc}) - no video this session.")
        print("     Is OBS running, with Tools -> WebSocket Server Settings enabled?")
        return None


def _locate_frame_zero(client) -> float | None:
    """Wall-clock time of the recording's first frame, in ms.

    Args:
        client: A connected ReqClient with a recording already running.

    Returns:
        Unix ms of frame zero, or None if OBS never reported a running duration.
    """
    samples: list[float] = []
    deadline = time.time() + OFFSET_TIMEOUT_S
    while len(samples) < OFFSET_SAMPLES and time.time() < deadline:
        before = _now_ms()
        try:
            status = client.get_record_status()
        except Exception as exc:
            print(f"  !! OBS stopped answering while timing the video ({exc})")
            return None
        after = _now_ms()
        duration = getattr(status, "output_duration", 0) or 0
        # Freshly started recordings report 0 for a moment; a zero here would put
        # frame zero at "now" and silently shift the whole video.
        if duration > 0:
            samples.append((before + after) / 2 - duration)
        time.sleep(0.2)
    if not samples:
        print("  !! OBS never reported a recording duration - video cannot be timed")
        return None
    return statistics.median(samples)


def _stats(client) -> dict:
    """OBS's own account of the recording: dropped frames and the video geometry.

    ``GetStats`` gives render/output frame counts and skips; ``GetVideoSettings``
    the base (canvas) and output resolution and the frame rate. Both are
    best-effort - a missing field leaves a key out, nothing fails.
    """
    out: dict = {}
    try:
        st = client.get_stats()
        for key in (
            "output_skipped_frames",
            "output_total_frames",
            "render_skipped_frames",
            "render_total_frames",
            "average_frame_render_time",
            "cpu_usage",
            "available_disk_space",
        ):
            v = getattr(st, key, None)
            if v is not None:
                out[key] = v
    except Exception as exc:
        out["stats_error"] = str(exc)
    try:
        vs = client.get_video_settings()
        for key, name in (
            ("base_width", "canvas_width"),
            ("base_height", "canvas_height"),
            ("output_width", "output_width"),
            ("output_height", "output_height"),
            ("fps_numerator", "fps_numerator"),
            ("fps_denominator", "fps_denominator"),
        ):
            v = getattr(vs, key, None)
            if v is not None:
                out[name] = v
    except Exception as exc:
        out["video_settings_error"] = str(exc)
    return out


def _wait_released(path: Path, timeout: float = RELEASE_TIMEOUT_S) -> bool:
    """True once nothing else holds ``path`` open and its size has settled.

    On Windows an open-for-append fails with a sharing violation while OBS still
    writes the file; on other platforms the size check alone has to do.
    """
    deadline = time.time() + timeout
    last_size, stable_since = -1, time.time()
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        if size != last_size:
            last_size, stable_since = size, time.time()
        released = False
        try:
            with open(path, "ab"):
                released = True
        except OSError:
            released = False
        if released and size > 0 and time.time() - stable_since >= 1.0:
            return True
        time.sleep(0.5)
    return False


def _move_with_retry(source: Path, target: Path, attempts: int = 5) -> bool:
    for i in range(attempts):
        try:
            shutil.move(str(source), str(target))
            return True
        except OSError as exc:
            if i == attempts - 1:
                print(f"  !! could not move the video into place ({exc}); left at {source}")
                return False
            time.sleep(1.0 + i)
    return False


def start(sid: str) -> dict | None:
    """Start an OBS recording for this session.

    Args:
        sid: Session id, used to name the finished file.

    Returns:
        A handle to pass to stop(), or None if video is unavailable.
    """
    client = _client()
    if client is None:
        return None
    try:
        status = client.get_record_status()
        if getattr(status, "output_active", False):
            print("  !! OBS was already recording - stopping that first")
            client.stop_record()
            time.sleep(1.0)
        client.start_record()
    except Exception as exc:
        print(f"  !! OBS refused to start recording ({exc}) - no video this session.")
        return None

    t0 = _locate_frame_zero(client)
    if t0 is None:
        print("  started video (WARNING: unaligned - it cannot be placed on the game clock)")
    else:
        print(f"  started video (frame zero at {t0:.0f})")
    return {"client": client, "t0_unix_ms": t0, "requested_at_ms": _now_ms()}


def stop(sid: str, handle: dict | None) -> Path | None:
    """Stop the recording and file it under the session id.

    OBS returns the path it actually wrote, so the video is claimed by name
    rather than by picking the newest file in a folder - which is how the old
    pipeline attached one session's footage to another session's data.

    Args:
        sid: Session id.
        handle: Whatever start() returned.

    Returns:
        Path to the filed video, or None if there is nothing to file.
    """
    if not handle:
        return None
    client = handle["client"]
    # Recording quality, read before the output closes: dropped frames are the
    # data-quality number for video, and the canvas/output size is what the
    # analysis needs to map screen pixels onto video pixels.
    stats = _stats(client)
    try:
        response = client.stop_record()
        source = Path(getattr(response, "output_path", "") or "")
    except Exception as exc:
        print(f"  !! OBS refused to stop recording ({exc}) - check it by hand")
        return None
    if not source.name:
        print("  !! OBS did not report an output path - find the video by hand")
        return None

    # OBS returns the path as soon as the output closes; the muxer may still be
    # flushing the container for a moment after that.
    deadline = time.time() + STOP_SETTLE_S
    while time.time() < deadline and not source.exists():
        time.sleep(0.2)
    if not source.exists():
        print(f"  !! OBS reported {source} but it is not there")
        return None

    target = session.stream_path("video", ext=source.suffix.lstrip("."), sid=sid)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _wait_released(source):
        print(f"  !! OBS still holds {source.name} after {RELEASE_TIMEOUT_S:.0f}s - trying anyway")
    if not _move_with_retry(source, target):
        target = source

    meta = {
        "session_id": sid,
        "video_file": target.name,
        "t0_unix_ms": handle.get("t0_unix_ms"),
        **stats,
        "note": ("t0_unix_ms is the wall-clock time of the first frame. A frame at "
                 "T seconds into the file is at t0_unix_ms + T*1000. Convert that to "
                 "game time through the gamestate stream, which carries both clocks."),
    }
    (target.parent / f"{sid}_video.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    size = target.stat().st_size / 1e6
    print(f"  video saved: {target.name} ({size:.1f} MB)")
    if handle.get("t0_unix_ms") is None:
        print("  !! this video has no timing anchor - usable to watch, not to align")
    return target
