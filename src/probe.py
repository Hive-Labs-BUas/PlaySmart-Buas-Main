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
                "why": "No eye tracker library on this PC: gaze and face are off (lite tier). Video, inputs, audio and game state still record."}
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
        try:
            vs = c.get_video_settings()
            size = f"{vs.base_width}x{vs.base_height} -> {vs.output_width}x{vs.output_height}"
        except Exception:  # noqa: BLE001
            size = ""
        return {"ok": True, "detail": f"OBS {ver.obs_version}, websocket {ver.obs_web_socket_version}{', ' + size if size else ''}{', recording now' if rec else ''}", "why": ""}
    except ImportError:
        return {"ok": False, "detail": "obsws-python not installed", "why": "Run poetry install in the rig folder."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"cannot connect ({type(exc).__name__})",
                "why": "OBS is not running or its WebSocket is off / wrong password. The rig launcher (rig_setup.py) starts and configures it; the desktop app does this at start."}


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


def _screen() -> dict:
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        return {"ok": True, "detail": f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}", "why": ""}
    except Exception:  # noqa: BLE001
        return {"ok": None, "detail": "unknown", "why": ""}


def main() -> None:
    t0 = time.time()
    checks = {"eye tracker": _tracker(), "microphone": _microphone(), "obs": _obs(), "league": _league(), "disk": _disk(), "screen": _screen()}
    tier_env = os.environ.get("PLAYSMART_TIER", "auto")
    tier = "full" if checks["eye tracker"]["ok"] else "lite"
    if tier_env in ("full", "lite"):
        tier = tier_env
    print(json.dumps({"tier": tier, "tier_env": tier_env, "checks": checks, "took_s": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
