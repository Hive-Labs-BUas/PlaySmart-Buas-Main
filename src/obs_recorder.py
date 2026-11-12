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
    PLAYSMART_OBS_FIT       "0": do not resize OBS's canvas to the screen before a recording.
    PLAYSMART_GAME_AUDIO    "off": do not write the game sound as its own WAV after the game.
    PLAYSMART_FFMPEG        path to ffmpeg(.exe) for that step (else imageio-ffmpeg, else PATH).

Modified 2026-11-10
-------------------
**The recording follows the screen it is made on.** ``rig_setup.py`` set OBS's canvas and
the game capture's size once, when the rig started. On 9 Nov a lab screen had been left at
1920x1080 with OBS still on a 2560x1440 canvas, and the game ended up in a corner of the
frame. ``start()`` now calls ``fit()`` first: canvas = output = the screen as it is *now*,
and the game capture stretched over the whole canvas whatever size the game renders at.
The geometry at the start and at the end of the recording goes into ``<sid>_video.json``
(``screen_px_start``, ``game_source_px_start/_end``, ``frame_in_video``,
``geometry_changed``), next to ``display_watch.py``'s second-by-second log.

**Game sound as its own file.** Bram asked for three files per session: the microphone
(``audio/<sid>_audio.wav``, the rig's own recorder), the game sound without any voice, and
the video. The game sound already sits alone on recording track 1 (``rig_setup.py``), so
``extract_game_audio()`` copies that track out of the finished recording into
``gameaudio/<sid>_gameaudio.wav`` (48 kHz, 16-bit, as OBS mixed it) with a
``_gameaudio.json`` carrying the same ``t0_unix_ms`` as the video: sample n is at
``t0 + n / samplerate``. The video keeps both tracks.

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
import subprocess
import time
from pathlib import Path

import session

SCENE = "PlaySmart - League"
SOURCE = "League Game Capture"
AUDIO_SOURCE = "League Game Audio"

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


AUDIO_INPUT_KINDS = ("wasapi_process_output_capture", "wasapi_output_capture", "wasapi_input_capture",
                     "coreaudio_output_capture", "coreaudio_input_capture", "pulse_output_capture",
                     "pulse_input_capture")


def audio_layout(client) -> dict:
    """Which audio input ends up on which recording track (added 2026-11-05).

    Returns ``{"tracks": {"1": ["League Game Audio"], "2": ["Mic/Aux"]}, "muted": [...],
    "rec_tracks": <bitmask from the profile or None>, "mixed": [tracks with >1 input]}``.
    Only unmuted inputs count. Best effort: a query that fails leaves its part out.
    """
    out: dict = {"tracks": {}, "muted": [], "rec_tracks": None, "mixed": []}
    for kind in AUDIO_INPUT_KINDS:
        try:
            inputs = getattr(client.get_input_list(kind), "inputs", None) or []
        except Exception:
            continue
        for i in inputs:
            name = i.get("inputName")
            if not name:
                continue
            try:
                if getattr(client.get_input_mute(name), "input_muted", False):
                    out["muted"].append(name)
                    continue
                tracks = getattr(client.get_input_audio_tracks(name), "input_audio_tracks", {}) or {}
            except Exception:
                continue
            for t, on in sorted(tracks.items()):
                if on:
                    out["tracks"].setdefault(str(t), []).append(name)
    for cat in ("SimpleOutput", "AdvOut"):
        try:
            v = getattr(client.get_profile_parameter(cat, "RecTracks"), "parameter_value", None)
            if v not in (None, ""):
                out["rec_tracks"] = int(v)
                out["rec_tracks_source"] = cat
                break
        except Exception:
            pass
    out["mixed"] = [t for t, names in out["tracks"].items() if len(names) > 1]
    return out


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
    try:
        out["audio_tracks"] = audio_layout(client)
    except Exception as exc:
        out["audio_tracks_error"] = str(exc)
    return out


# ----------------------------------------------------------------------------- geometry
def _attr(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _game_item(client) -> "tuple[str, int] | None":
    """(scene name, scene item id) of the League game capture, or None."""
    scenes = [SCENE]
    try:
        cur = _attr(client.get_current_program_scene(), "current_program_scene_name", "scene_name", default=None)
        if cur and cur not in scenes:
            scenes.append(cur)
    except Exception:  # noqa: BLE001
        pass
    for scene in scenes:
        try:
            items = _attr(client.get_scene_item_list(scene), "scene_items", default=[]) or []
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            if item.get("sourceName") == SOURCE:
                return scene, int(item["sceneItemId"])
    return None


def game_source_px(client) -> "list | None":
    """The size the game is being captured at (the game's own render size), or None while
    the capture has nothing hooked."""
    found = _game_item(client)
    if not found:
        return None
    try:
        tr = _attr(client.get_scene_item_transform(*found), "scene_item_transform", default={}) or {}
        w, h = int(round(float(tr.get("sourceWidth") or 0))), int(round(float(tr.get("sourceHeight") or 0)))
        return [w, h] if w > 0 and h > 0 else None
    except Exception:  # noqa: BLE001
        return None


def frame_in_video(source, output) -> "list | None":
    """Where the game frame sits in the recorded frame: ``[x, y, w, h]`` in video pixels.

    The capture is scaled to fit inside the canvas, centred (OBS_BOUNDS_SCALE_INNER). When
    the game has the screen's aspect ratio this is the whole frame; a 16:9 game on an
    ultrawide canvas is pillarboxed and the analysis has to know by how much.
    """
    if not source or not output or not source[0] or not source[1] or not output[0] or not output[1]:
        return None
    k = min(output[0] / source[0], output[1] / source[1])
    w, h = source[0] * k, source[1] * k
    return [round((output[0] - w) / 2, 1), round((output[1] - h) / 2, 1), round(w, 1), round(h, 1)]


def fit(client, screen=None) -> dict:
    """Make OBS record the screen as it is now.

    Canvas and output are set to the primary screen (only possible while OBS is idle, which
    it is right before ``start_record``) and the game capture is stretched over the whole
    canvas. Returns ``{"screen_px", "canvas", "output", "changes": [...], "error"?}``.
    Nothing here raises: a refused setting is reported and the recording goes ahead.
    """
    out: dict = {"screen_px": None, "canvas": None, "output": None, "changes": []}
    if screen is None:
        # the monitor the game is on, when a game window exists (a second monitor counts)
        try:
            import display_watch
            screen = display_watch.snapshot().get("screen_px")
        except Exception:  # noqa: BLE001
            screen = None
    screen = tuple(screen) if screen else session.screen_px()
    out["screen_px"] = list(screen) if screen else None
    try:
        vs = client.get_video_settings()
        canvas = (int(vs.base_width), int(vs.base_height))
        output = (int(vs.output_width), int(vs.output_height))
        fps_n, fps_d = int(vs.fps_numerator), int(vs.fps_denominator) or 1
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"OBS video settings unreadable ({exc})"
        return out
    if screen and os.environ.get("PLAYSMART_OBS_FIT", "1") != "0" \
            and (canvas != tuple(screen) or output != tuple(screen)):
        try:
            client.set_video_settings(fps_n, fps_d, int(screen[0]), int(screen[1]), int(screen[0]), int(screen[1]))
            out["changes"].append(f"OBS canvas {canvas[0]}x{canvas[1]} / output {output[0]}x{output[1]} "
                                  f"-> {screen[0]}x{screen[1]} (the screen now)")
            canvas = output = (int(screen[0]), int(screen[1]))
        except Exception as exc:  # noqa: BLE001
            out["error"] = (f"OBS refused a {screen[0]}x{screen[1]} canvas ({exc}); it stays "
                            f"{canvas[0]}x{canvas[1]} - stop any stream/virtual camera in OBS")
    out["canvas"], out["output"] = list(canvas), list(output)
    found = _game_item(client)
    if not found:
        out.setdefault("error", f"no '{SOURCE}' in OBS - run rig_setup.py")
        return out
    want = {"positionX": 0.0, "positionY": 0.0, "alignment": 5, "rotation": 0.0,
            "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
            "boundsWidth": float(canvas[0]), "boundsHeight": float(canvas[1]),
            "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0}
    try:
        cur = _attr(client.get_scene_item_transform(*found), "scene_item_transform", default={}) or {}
        same = all(abs(float(cur.get(k) or 0) - float(v)) < 0.5 if isinstance(v, (int, float)) else cur.get(k) == v
                   for k, v in want.items())
        if not same:
            client.set_scene_item_transform(found[0], found[1], want)
            out["changes"].append(f"game capture stretched over the {canvas[0]}x{canvas[1]} canvas "
                                  f"(was {int(float(cur.get('boundsWidth') or 0))}x{int(float(cur.get('boundsHeight') or 0))})")
    except Exception as exc:  # noqa: BLE001
        out.setdefault("error", f"could not fit the game capture to the canvas ({exc})")
    return out


# ----------------------------------------------------------------------------- game audio
def ffmpeg_exe() -> "str | None":
    """An ffmpeg to copy the game track out with: PLAYSMART_FFMPEG, imageio-ffmpeg, PATH."""
    env = os.environ.get("PLAYSMART_FFMPEG", "").strip().strip("'\"")
    if env and Path(env).is_file():
        return env
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("ffmpeg")


def game_audio_stream(vmeta: dict) -> dict:
    """Which audio stream of the recording is the game sound.

    From ``audio_tracks`` in the video json: the track that carries ``League Game Audio``
    and its position among the recorded tracks (RecTracks bitmask; tracks are written in
    ascending order). Without that information: the first stream, flagged ``assumed``.
    """
    lay = vmeta.get("audio_tracks") or {}
    tracks = lay.get("tracks") or {}
    game = next((t for t, names in sorted(tracks.items()) if AUDIO_SOURCE in names), None)
    out = {"index": 0, "track": int(game) if game else None, "inputs": tracks.get(game, []) if game else [],
           "game_only": bool(game) and len(tracks.get(game, [])) == 1, "assumed": game is None}
    mask = lay.get("rec_tracks")
    if game and mask:
        recorded = [i for i in range(1, 7) if int(mask) & (1 << (i - 1))]
        if int(game) in recorded:
            out["index"] = recorded.index(int(game))
        else:
            out["assumed"] = True          # the game track is not recorded at all; take the first
    return out


def extract_game_audio(sid: str, force: bool = False) -> "Path | None":
    """Write ``gameaudio/<sid>_gameaudio.wav`` (+ ``.json``) from the session's recording.

    Returns the WAV path, or None with one printed line saying why not. Safe to call again:
    an existing WAV is kept unless ``force``.
    """
    if os.environ.get("PLAYSMART_GAME_AUDIO", "wav").strip().lower() in ("off", "0", "no"):
        return None
    vdir = session.data_dir() / "video"
    videos = [p for p in sorted(vdir.glob(f"{sid}_video.*")) if p.suffix.lower() in (".mkv", ".mp4", ".mov", ".flv")]
    if not videos:
        return None
    video = videos[0]
    target = session.stream_path("gameaudio", ext="wav", sid=sid)
    anchor = target.with_suffix(".json")
    if target.is_file() and target.stat().st_size > 44 and anchor.is_file() and not force:
        return target
    exe = ffmpeg_exe()
    if not exe:
        print("  !! game audio not split out: no ffmpeg (set PLAYSMART_FFMPEG, or "
              "`pip install imageio-ffmpeg` in the rig's environment). The game sound is still "
              "track 1 of the video; `python src/obs_recorder.py --extract-audio --all` does it later.")
        return None
    try:
        vmeta = json.loads((vdir / f"{sid}_video.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        vmeta = {}
    stream = game_audio_stream(vmeta)
    tmp = target.with_name(target.stem + ".part.wav")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
           "-map", f"0:a:{stream['index']}", "-vn", "-c:a", "pcm_s16le", "-ar", "48000", str(tmp)]
    # Below normal: this runs right after a game, when the next one may already be loading.
    flags = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | session.priority_flags("below")}
             if os.name == "nt" else {})
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=900, **flags)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  !! game audio not split out: ffmpeg failed to run ({exc})")
        return None
    if run.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 44:
        why = (run.stderr or "").strip().splitlines()[-1:] or ["no audio stream in the recording"]
        print(f"  !! game audio not split out: {why[0]}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    os.replace(tmp, target)
    channels = 2
    try:
        import wave
        with wave.open(str(target), "rb") as wf:
            channels, rate, frames = wf.getnchannels(), wf.getframerate(), wf.getnframes()
    except Exception:  # noqa: BLE001 - header unreadable: fall back to the size
        rate, frames = 48000, max(0, target.stat().st_size - 44) // (2 * channels)
    info = {
        "session_id": sid,
        "audio_file": target.name,
        "t0_unix_ms": vmeta.get("t0_unix_ms"),
        "samplerate": rate, "channels": channels, "frames_written": frames,
        "duration_s": round(frames / rate, 3) if rate else None,
        "source_video": video.name, "source_stream": f"0:a:{stream['index']}",
        "obs_track": stream["track"], "obs_inputs": stream["inputs"],
        "game_only": stream["game_only"], "track_assumed": stream["assumed"],
        "note": ("The game sound as OBS recorded it on its own track, copied out of the video. "
                 "Sample n is at t0_unix_ms + n / samplerate * 1000 (the video's own anchor). "
                 "game_only is false when another input shared the track or the track layout "
                 "was not recorded; then this file may contain voice."),
    }
    anchor.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"  game audio saved: {target.name} ({target.stat().st_size / 1e6:.1f} MB, "
          f"{info['duration_s']} s" + ("" if stream["game_only"] else ", NOT verified game-only") + ")")
    return target


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
    except Exception as exc:
        print(f"  !! OBS did not answer ({exc}) - no video this session.")
        return None
    # The screen may not be the one rig_setup saw when the rig started: fit OBS to it now,
    # while nothing is recording.
    fitted = fit(client)
    for line in fitted.get("changes", []):
        print(f"  obs: {line}")
    if fitted.get("error"):
        print(f"  !! obs: {fitted['error']}")
    try:
        client.start_record()
    except Exception as exc:
        print(f"  !! OBS refused to start recording ({exc}) - no video this session.")
        return None

    t0 = _locate_frame_zero(client)
    if t0 is None:
        print("  started video (WARNING: unaligned - it cannot be placed on the game clock)")
    else:
        print(f"  started video (frame zero at {t0:.0f})")
    source = game_source_px(client)
    if source and fitted.get("screen_px") and source != fitted["screen_px"]:
        print(f"  !! the game renders at {source[0]}x{source[1]} on a {fitted['screen_px'][0]}x"
              f"{fitted['screen_px'][1]} screen - recorded, and scaled to fill the video")
    return {"client": client, "t0_unix_ms": t0, "requested_at_ms": _now_ms(),
            "fit": fitted, "source_px": source}


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
    fitted = handle.get("fit") or {}
    source0, source1 = handle.get("source_px"), game_source_px(client)
    screen1 = session.screen_px()
    out_px = [stats.get("output_width"), stats.get("output_height")]
    geometry = {
        "screen_px_start": fitted.get("screen_px"),
        "screen_px_end": list(screen1) if screen1 else None,
        "game_source_px_start": source0,
        "game_source_px_end": source1,
        "frame_in_video": frame_in_video(source1 or source0, out_px),
        "geometry_changed": bool((source0 and source1 and source0 != source1)
                                 or (fitted.get("screen_px") and screen1 and list(screen1) != fitted["screen_px"])),
        "fit_changes": fitted.get("changes", []),
    }
    if fitted.get("error"):
        geometry["fit_error"] = fitted["error"]
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
        **geometry,
        "note": ("t0_unix_ms is the wall-clock time of the first frame. A frame at "
                 "T seconds into the file is at t0_unix_ms + T*1000. Convert that to "
                 "game time through the gamestate stream, which carries both clocks. "
                 "frame_in_video is where the game frame sits in the recorded frame "
                 "[x, y, w, h]; game_source_px_* is the size the game rendered at."),
    }
    (target.parent / f"{sid}_video.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    size = target.stat().st_size / 1e6
    print(f"  video saved: {target.name} ({size:.1f} MB)")
    if handle.get("t0_unix_ms") is None:
        print("  !! this video has no timing anchor - usable to watch, not to align")
    if geometry["geometry_changed"]:
        print(f"  !! the game's resolution changed during the recording "
              f"({source0} -> {source1}); see _display.json for when")
    return target


def main() -> None:
    """``python src/obs_recorder.py --extract-audio <sid> | --all``: split the game sound out
    of recordings made before 10 Nov 2026 (or on a rig that had no ffmpeg at the time)."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--extract-audio", nargs="?", const="", metavar="SID",
                    help="session id; with --all, every recording under the data folder")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="redo an existing WAV")
    args = ap.parse_args()
    if args.extract_audio is None:
        ap.print_help()
        return
    sids = [args.extract_audio] if args.extract_audio else []
    if args.all:
        sids = sorted({p.name.split("_video")[0] for p in (session.data_dir() / "video").glob("*_video.*")
                       if p.suffix.lower() in (".mkv", ".mp4", ".mov", ".flv")})
    if not sids:
        print("no session given (a session id, or --all)")
        return
    done = sum(1 for sid in sids if extract_game_audio(sid, force=args.force))
    print(f"{done} of {len(sids)} recordings have a game-audio WAV")


if __name__ == "__main__":
    main()
