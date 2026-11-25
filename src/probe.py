"""One JSON line about what this rig can capture right now. Used by the PlaySmart app's
"what is working" panel before a game; same checks as preflight.py, machine-readable, no
side effects (no mic sample, no OBS changes), bounded to a few seconds.

    python src/probe.py            -> {"tier": "lite", "checks": {...}, "took_s": 1.2}

Every check is ``{"ok": true|false|null, "detail": "...", "why": "..."}`` where ``why`` says
what to do when it is not ok, and ``null`` means "could not tell".
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import session  # noqa: E402  (loads .env)


def _tracker() -> dict:
    try:
        import tobii_research as tr
        trackers = tr.find_all_eyetrackers()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"tobii_research unavailable ({type(exc).__name__})",
                "why": "No eye tracker library on this PC: gaze is off (lite tier). Video, inputs, audio and game state still record."}
    if not trackers:
        return {"ok": False, "detail": "no Tobii found",
                "why": "Plug the tracker in (USB), check the Tobii service, close Eye Tracker Manager. Without it: lite tier, no gaze."}
    t = trackers[0]
    try:
        hz = t.get_gaze_output_frequency()
    except Exception:  # noqa: BLE001
        hz = "?"
    return {"ok": True, "detail": f"{t.model} {t.serial_number} at {hz} Hz", "why": "Calibrate the player in Eye Tracker Manager before the first game."}


def _microphone() -> dict:
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"no default input device ({type(exc).__name__})", "why": "Pick a default microphone in Windows sound settings; audio is skipped otherwise."}
    return {"ok": True, "detail": f"default input: {dev.get('name', '?')}", "why": ""}


def _obs() -> dict:
    if os.environ.get("PLAYSMART_OBS", "1") == "0":
        return {"ok": False, "detail": "disabled (PLAYSMART_OBS=0)", "why": "No video this session; set PLAYSMART_OBS=1 in the rig .env."}
    import logging
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    try:
        import obsws_python as obs
        c = obs.ReqClient(host=os.environ.get("PLAYSMART_OBS_HOST", "127.0.0.1"), port=int(os.environ.get("PLAYSMART_OBS_PORT", "4455")),
                          password=os.environ.get("PLAYSMART_OBS_PASSWORD", ""), timeout=3)
        ver = c.get_version()
        rec = c.get_record_status().output_active
        canvas = None
        try:
            vs = c.get_video_settings()
            size = f"{vs.base_width}x{vs.base_height} -> {vs.output_width}x{vs.output_height}"
            canvas = [int(vs.base_width), int(vs.base_height), int(vs.output_width), int(vs.output_height)]
        except Exception:  # noqa: BLE001
            size = ""
        # Which encoder records: a software encoder (x264) takes CPU from the game for the
        # whole recording; rig_setup picks the GPU's own encoder when it finds one.
        encoder = None
        try:
            mode = str(getattr(c.get_profile_parameter("Output", "Mode"), "parameter_value", "") or "Simple")
            cat = "AdvOut" if mode.lower().startswith("adv") else "SimpleOutput"
            encoder = str(getattr(c.get_profile_parameter(cat, "RecEncoder"), "parameter_value", "") or "") or None
        except Exception:  # noqa: BLE001
            encoder = None
        software = bool(encoder) and any(k in encoder.lower() for k in ("x264", "software"))
        # Is the scene on screen the PlaySmart one? When it is not (OBS installed or changed
        # after the rig started), the recorder sets it up itself when the next game starts;
        # the panel says so instead of staying silent.
        scene_ready = None
        try:
            import obs_recorder
            scene_ready = obs_recorder.scene_ready(c)
        except Exception:  # noqa: BLE001
            scene_ready = None
        scene_note = ("" if scene_ready is None else ", PlaySmart scene in use" if scene_ready
                      else ", another scene on screen: the PlaySmart scene is set up when a game starts")
        why = ("OBS records with the software encoder (x264): that costs frame rate in game. "
               "Run rig_setup.py again, or pick the GPU encoder under Settings > Output > Recording.") if software else ""
        return {"ok": True, "detail": f"OBS {ver.obs_version}, websocket {ver.obs_web_socket_version}{', ' + size if size else ''}"
                                      f"{', encoder ' + encoder if encoder else ''}{', recording now' if rec else ''}{scene_note}", "why": why,
                "canvas": canvas, "recording": bool(rec), "encoder": encoder, "software_encoder": software,
                "scene_ready": scene_ready}
    except ImportError:
        return {"ok": False, "detail": "obsws-python not installed", "why": "Run poetry install in the rig folder."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"cannot connect ({type(exc).__name__})",
                "why": "OBS is not running, or its WebSocket is off or has another password than the rig knows. "
                       "Press Set up OBS (or run rig_setup.py); the recorder also tries this itself when a game starts."}


def _league() -> dict:
    try:
        import lcu
        d = lcu.riot_install_dir()
    except Exception:  # noqa: BLE001
        d = None
    lock = (Path(d) / "lockfile") if d else None
    client = bool(lock and lock.is_file())
    game = False
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        requests.get("https://127.0.0.1:2999/liveclientdata/gamestats", timeout=1.0, verify=False)
        game = True
    except Exception:  # noqa: BLE001
        pass
    if game:
        return {"ok": True, "detail": "a game is running now", "why": ""}
    if client:
        return {"ok": True, "detail": "client open, no game yet", "why": "Recording arms by itself when a game loads."}
    return {"ok": None, "detail": "client not open" if d else "League install not found", "why": "Open the League client; the game id and your champion are read from it when the game starts."}


def _disk() -> dict:
    data = session.data_dir()
    try:
        free = shutil.disk_usage(data if data.exists() else data.parent).free / 1e9
    except OSError as exc:
        return {"ok": None, "detail": str(exc), "why": ""}
    ok = free > 20
    return {"ok": ok, "detail": f"{free:.0f} GB free at {data}", "why": "" if ok else "A game is 2-5 GB of video; free space before recording."}


def _screen(obs: dict) -> dict:
    """The screen, the game window if one is open, and whether OBS is on the same grid."""
    try:
        import display_watch
        snap = display_watch.snapshot()          # the monitor the game is on, if a game is open
    except Exception:  # noqa: BLE001
        snap = {}
    px = tuple(snap["screen_px"]) if snap.get("screen_px") else session.screen_px()
    if not px:
        return {"ok": None, "detail": "unknown", "why": ""}
    detail, why, ok = f"{px[0]}x{px[1]}", "", True
    win = snap.get("window")
    if win and (win[2], win[3]) != (px[0], px[1]):
        detail += f", game window {win[2]}x{win[3]}"
        ok = None
        why = ("The game does not fill the screen (windowed, or a lower in-game resolution). It is recorded "
               "and mapped, but the minimap is smaller in the video: Borderless at the screen's resolution is best.")
    elif win:
        detail += ", game fills the screen"
    canvas = obs.get("canvas") if isinstance(obs, dict) else None
    if canvas and (canvas[0], canvas[1]) != (px[0], px[1]):
        detail += f"; OBS canvas {canvas[0]}x{canvas[1]}"
        if ok is True:
            ok = None
        why = (why + " " if why else "") + ("OBS is still set to another resolution; the rig resizes it to this "
                                           "screen when the next game starts (it cannot while OBS is recording).")
    return {"ok": ok, "detail": detail, "why": why, "screen_px": list(px), "window": win}


def _face() -> dict:
    """The face stream (OpenFace action units): on or off, and whether OpenFace is here."""
    exe = session.openface_exe()
    raw_skip = {p.strip().lower() for p in os.environ.get("PLAYSMART_SKIP", "").split(",") if p.strip()}
    base = {"wanted": session.face_wanted(), "installed": exe is not None,
            "exe": str(exe) if exe else "", "skipped": bool(raw_skip & {"face", "emotion"})}
    if not session.face_wanted():
        return {"ok": None, "detail": "off on this PC" + ("" if exe else " (OpenFace not installed)"),
                "why": "The face stream records action units, head pose and blinks from the webcam; no image is kept. "
                       "Turn it on only for players who agreed to it.", **base}
    if base["skipped"]:
        return {"ok": False, "detail": "switched on, but face/emotion is in PLAYSMART_SKIP",
                "why": "Remove face (and emotion) from PLAYSMART_SKIP in the rig .env.", **base}
    if exe is None:
        return {"ok": False, "detail": "switched on, but OpenFace is not on this PC",
                "why": "Copy the OpenFace_2.2.0_win_x64 folder (with its model folder) next to the rig, or set "
                       "PLAYSMART_OPENFACE_EXE in the rig .env to FeatureExtraction.exe.", **base}
    models = exe.parent / "model" / "patch_experts"
    cen = list(models.glob("cen_patches_*.dat")) if models.is_dir() else []
    if len(cen) < 4:
        return {"ok": False, "detail": f"OpenFace found, {len(cen)} of 4 CEN models present",
                "why": "Run download_models.ps1 inside the OpenFace folder (one-time, ~500 MB).", **base}
    cam = os.environ.get("PLAYSMART_FACE_CAM", "640x480")
    return {"ok": True, "detail": f"on: OpenFace 2.2 at {exe.parent.name}, webcam {os.environ.get('PLAYSMART_FACE_DEVICE', '0')} at {cam}",
            "why": "Action units, head pose and eyelid aperture; no image, no emotion label. The webcam must be free (close Discord's camera).", **base}


def _load(obs: dict, face: dict) -> dict:
    """What the rig runs next to the game, and how it is kept out of the game's way."""
    logical = os.cpu_count() or 1
    parts = ["one keyboard and one mouse hook (input recorder, above normal priority)"]
    why = ""
    ok: "bool | None" = True
    if face.get("ok"):
        cores, prio = session.face_cores(), session.face_priority()
        parts.append(f"OpenFace at {prio} priority on {cores if cores else 'all'} of {logical} logical processors")
        if not cores or prio == "normal":
            ok = None
            why = ("OpenFace is not limited (PLAYSMART_FACE_CORES / PLAYSMART_FACE_PRIORITY): it is the heaviest thing "
                   "next to the game and can cost frame rate.")
    else:
        parts.append("no OpenFace")
    if obs.get("software_encoder"):
        ok = None
        why = (why + " " if why else "") + "OBS encodes on the CPU (x264); a GPU encoder leaves the game alone."
    key = session.abort_key()
    parts.append(f"abort key {key.upper()}" if key else "no abort key (a capture ends with the game)")
    return {"ok": ok, "detail": "; ".join(parts), "why": why}


def main() -> None:
    t0 = time.time()
    obs = _obs()
    checks = {"eye tracker": _tracker(), "microphone": _microphone(), "obs": obs, "face": _face(),
              "league": _league(), "disk": _disk(), "screen": _screen(obs)}
    checks["game load"] = _load(obs, checks["face"])
    try:
        import obs_recorder
        ff = obs_recorder.ffmpeg_exe()
    except Exception:  # noqa: BLE001
        ff = None
    checks["game audio"] = ({"ok": True, "detail": "split into its own WAV after each game", "why": ""} if ff and obs.get("ok")
                            else {"ok": None, "detail": "stays inside the video (track 1)" if obs.get("ok") else "no video, so no game audio",
                                  "why": "No ffmpeg for the rig: set PLAYSMART_FFMPEG (the desktop app does this itself)." if obs.get("ok") else ""})
    tier_env = os.environ.get("PLAYSMART_TIER", "auto")
    tier = "full" if checks["eye tracker"]["ok"] else "lite"
    if tier_env in ("full", "lite"):
        tier = tier_env
    print(json.dumps({"tier": tier, "tier_env": tier_env, "checks": checks, "took_s": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
