"""Set this PC up to capture, without the hand-edited checklist.

Added 2026-10-27 for the packaged rig. Everything `TESTING.md` section 3 asked a person
to do in OBS is done here, idempotently, from the screen this PC actually has:

* **obs-websocket**: enabled, port 4455, a generated password, written into OBS's own
  config (``%APPDATA%\\obs-studio\\plugin_config\\obs-websocket\\config.json``) and into
  ``.env``. Needs OBS closed for that one step; if OBS is open and the websocket is
  already reachable nothing is touched.
* **OBS started** if it is not running (``--no-launch`` to skip).
* **Scene collection + scene** ``PlaySmart - League`` with one Game Capture of
  ``League of Legends.exe`` stretched to the canvas, and one Application Audio Capture
  of the same process. The collection's default Desktop Audio and Mic/Aux are muted: the
  recording carries the game's sound only, never Discord, music or the player's voice
  (the microphone is the rig's own, separate, consented stream).
* **Canvas = output = the primary screen** at 30 fps (``--fps 60``). Downscaled output
  makes the minimap unreadable; a canvas that is not the screen puts video on a
  different pixel grid from the input log.
* **Recording**: MKV (survives a crash), hardware encoder for the GPU found (NVIDIA →
  nvenc, AMD → amd, Intel → qsv, else x264), quality HQ, into ``--rec-dir``
  (default ``<data>/obs_incoming``), auto-remux off.
* **League**: the install folder is read from Riot's product metadata (lcu.py), so
  a non-default install needs no ``PLAYSMART_LEAGUE_DIR``.

    poetry run python src/rig_setup.py            # apply everything
    poetry run python src/rig_setup.py --check    # report only, change nothing
    poetry run python src/rig_setup.py --fps 60

Safe to run every time: ``main.bat`` runs it before pre-flight. Changes to OBS's
output *mode* (Advanced -> Simple) only take effect after an OBS restart; the script
says so when that happens.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import secrets
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
sys.path.insert(0, str(SRC))

import session  # noqa: E402  (loads .env)

COLLECTION = "PlaySmart - League"
SCENE = "PlaySmart - League"
SOURCE = "League Game Capture"
AUDIO_SOURCE = "League Game Audio"
LEAGUE_WINDOW = "League of Legends (TM) Client:RiotWindowClass:League of Legends.exe"
WS_PORT = 4455

OBS_EXE_CANDIDATES = [
    Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"),
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "obs-studio" / "bin" / "64bit" / "obs64.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "obs-studio" / "bin" / "64bit" / "obs64.exe",
]

CHECK = False      # --check: report only
_changes: list[str] = []


def say(line: str) -> None:
    print(f"  {line}")


def change(line: str) -> None:
    _changes.append(line)
    print(f"  {'would ' if CHECK else ''}{line}")


# ------------------------------------------------------------------ .env

def env_path() -> Path:
    return session.ENV_FILE


def ensure_env_file() -> None:
    p = env_path()
    if p.is_file():
        return
    example = REPO / ".env.example"
    if not example.is_file():
        change("create an empty .env")
        if not CHECK:
            p.write_text("", encoding="utf-8")
        return
    change("create .env from .env.example")
    if not CHECK:
        text = example.read_text(encoding="utf-8")
        # A home PC uploads nothing until someone sets a password on purpose.
        text = text.replace("PLAYSMART_UPLOAD=keep", "PLAYSMART_UPLOAD=off", 1)
        p.write_text(text, encoding="utf-8")


def set_env_value(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing (even commented) line, keeping the rest."""
    p = env_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    out, done = [], False
    for raw in lines:
        stripped = raw.strip()
        if not done and (stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}=")
                         or stripped.startswith(f"#{key}=")):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(raw)
    if not done:
        out.append(f"{key}={value}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


# ------------------------------------------------------------------ OBS: process and config

def obs_exe() -> Path | None:
    for cand in OBS_EXE_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def obs_running() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() in ("obs64.exe", "obs32.exe", "obs"):
            return True
    return False


def obs_config_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "obs-studio"
    return Path.home() / ".config" / "obs-studio"


def websocket_config_path() -> Path:
    return obs_config_dir() / "plugin_config" / "obs-websocket" / "config.json"


def read_websocket_config() -> dict:
    p = websocket_config_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def websocket_reachable(password: str, timeout: float = 3.0):
    """A connected ReqClient, or None."""
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    try:
        import obsws_python as obs
        return obs.ReqClient(host=os.environ.get("PLAYSMART_OBS_HOST", "127.0.0.1"),
                             port=int(os.environ.get("PLAYSMART_OBS_PORT", str(WS_PORT))),
                             password=password, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def ensure_websocket() -> str | None:
    """Make sure OBS's websocket is on with a password we know. Returns the password."""
    env_pw = os.environ.get("PLAYSMART_OBS_PASSWORD", "")
    cfg = read_websocket_config()
    cfg_pw = cfg.get("server_password", "")
    enabled = bool(cfg.get("server_enabled")) and int(cfg.get("server_port", WS_PORT)) == WS_PORT

    if obs_running() and env_pw and websocket_reachable(env_pw):
        say("obs websocket: reachable with the .env password")
        return env_pw
    if enabled and cfg_pw and (not cfg.get("auth_required", True) or cfg_pw):
        if cfg_pw != env_pw:
            change("copy the websocket password from OBS's config into .env")
            if not CHECK:
                set_env_value("PLAYSMART_OBS_PASSWORD", cfg_pw)
        else:
            say("obs websocket: enabled in OBS's config, password matches .env")
        return cfg_pw
    # Websocket off or no password: write OBS's config. Only safe with OBS closed,
    # because OBS rewrites the file on exit and would undo it.
    if obs_running():
        say("!! OBS is running but its websocket is off / has no password. Close OBS and run "
            "this again, or enable it by hand: Tools > WebSocket Server Settings")
        return env_pw or None
    pw = env_pw or secrets.token_urlsafe(12)
    new_cfg = {**cfg, "server_enabled": True, "server_port": WS_PORT, "auth_required": True,
               "server_password": pw, "alerts_enabled": False, "first_load": False}
    change(f"enable obs-websocket (port {WS_PORT}, password) in {websocket_config_path()}")
    if not CHECK:
        websocket_config_path().parent.mkdir(parents=True, exist_ok=True)
        websocket_config_path().write_text(json.dumps(new_cfg, indent=4), encoding="utf-8")
        set_env_value("PLAYSMART_OBS_PASSWORD", pw)
    return pw


def launch_obs(password: str, wait_s: float = 30.0):
    exe = obs_exe()
    if exe is None:
        say("!! OBS Studio not found - install it (winget install OBSProject.OBSStudio) and run again")
        return None
    change(f"start OBS ({exe})")
    if CHECK:
        return None
    # OBS must be started from its own bin folder or it cannot find its data files.
    subprocess.Popen([str(exe), "--disable-shutdown-check", "--minimize-to-tray"],
                     cwd=str(exe.parent), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.time() + wait_s
    while time.time() < deadline:
        client = websocket_reachable(password, timeout=2.0)
        if client:
            return client
        time.sleep(1.0)
    say(f"!! OBS started but its websocket did not answer within {wait_s:.0f}s")
    return None


# ------------------------------------------------------------------ OBS: scene and settings

def primary_screen() -> tuple[int, int] | None:
    try:
        import screeninfo
        m = screeninfo.get_monitors()[0]
        return int(m.width), int(m.height)
    except Exception:  # noqa: BLE001
        return None


def gpu_vendor() -> str:
    """'nvidia' | 'amd' | 'intel' | '' from the display adapters Windows reports."""
    names = ""
    if platform.system() == "Windows":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ';'"],
                capture_output=True, text=True, timeout=20)
            names = out.stdout.lower()
        except Exception:  # noqa: BLE001
            names = ""
    if "nvidia" in names or "geforce" in names:
        return "nvidia"
    if "amd" in names or "radeon" in names:
        return "amd"
    if "intel" in names or "arc" in names:
        return "intel"
    return ""


ENCODER_FOR = {"nvidia": "nvenc", "amd": "amd", "intel": "qsv", "": "x264"}


def _attr(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def ensure_scene(client) -> None:
    # Scene collection
    try:
        sc = client.get_scene_collection_list()
        collections = _attr(sc, "scene_collections", default=[]) or []
        current = _attr(sc, "current_scene_collection_name", default="")
    except Exception as exc:  # noqa: BLE001
        say(f"!! could not list scene collections ({exc})")
        collections, current = [], ""
    if COLLECTION not in collections:
        change(f"create OBS scene collection '{COLLECTION}'")
        if not CHECK:
            client.create_scene_collection(COLLECTION)
            time.sleep(1.5)
    elif current != COLLECTION:
        change(f"switch OBS to scene collection '{COLLECTION}'")
        if not CHECK:
            client.set_current_scene_collection(COLLECTION)
            time.sleep(1.5)
    if CHECK:
        return
    # Scene
    scenes = [s.get("sceneName") for s in (_attr(client.get_scene_list(), "scenes", default=[]) or [])]
    if SCENE not in scenes:
        change(f"create scene '{SCENE}'")
        client.create_scene(SCENE)
    client.set_current_program_scene(SCENE)
    # Source
    inputs = _attr(client.get_input_list("game_capture"), "inputs", default=[]) or []
    names = [i.get("inputName") for i in inputs]
    settings = {"capture_mode": "window", "window": LEAGUE_WINDOW, "priority": 2,
                "capture_cursor": True, "anti_cheat_hook": True, "allow_transparency": False}
    if SOURCE not in names:
        change(f"add Game Capture '{SOURCE}' of League to '{SCENE}'")
        client.create_input(SCENE, SOURCE, "game_capture", settings, True)
    else:
        client.set_input_settings(SOURCE, settings, True)
        items = _attr(client.get_scene_item_list(SCENE), "scene_items", default=[]) or []
        if SOURCE not in [i.get("sourceName") for i in items]:
            change(f"put '{SOURCE}' into '{SCENE}'")
            client.create_scene_item(SCENE, SOURCE, True)
    # Fill the canvas whatever the game's window size is.
    screen = primary_screen()
    if screen:
        items = _attr(client.get_scene_item_list(SCENE), "scene_items", default=[]) or []
        for item in items:
            if item.get("sourceName") == SOURCE:
                try:
                    client.set_scene_item_transform(SCENE, item["sceneItemId"], {
                        "positionX": 0, "positionY": 0, "boundsType": "OBS_BOUNDS_SCALE_INNER",
                        "boundsAlignment": 0, "boundsWidth": screen[0], "boundsHeight": screen[1],
                    })
                except Exception as exc:  # noqa: BLE001
                    say(f"could not set the source transform ({exc})")
    say(f"obs scene: '{SCENE}' with '{SOURCE}'")
    ensure_audio(client)


def ensure_audio(client) -> None:
    """Game audio only: an Application Audio Capture of League (OBS 28+), and the
    collection's global Desktop Audio / Mic/Aux muted so nothing else on the PC ends up in
    the recording. Reversible in OBS (unmute) if a coach wants otherwise."""
    try:
        kinds = _attr(client.get_input_kind_list(False), "input_kinds", default=[]) or []
    except Exception as exc:  # noqa: BLE001
        say(f"could not list OBS input kinds ({exc}); audio left as it is")
        return
    if "wasapi_process_output_capture" in kinds:
        inputs = _attr(client.get_input_list("wasapi_process_output_capture"), "inputs", default=[]) or []
        names = [i.get("inputName") for i in inputs]
        settings = {"window": LEAGUE_WINDOW, "priority": 2}
        if AUDIO_SOURCE not in names:
            change(f"add Application Audio Capture '{AUDIO_SOURCE}' of League to '{SCENE}'")
            try:
                client.create_input(SCENE, AUDIO_SOURCE, "wasapi_process_output_capture", settings, True)
            except Exception as exc:  # noqa: BLE001
                say(f"could not add '{AUDIO_SOURCE}' ({exc})")
        else:
            try:
                client.set_input_settings(AUDIO_SOURCE, settings, True)
            except Exception as exc:  # noqa: BLE001
                say(f"could not update '{AUDIO_SOURCE}' ({exc})")
    else:
        say("!! this OBS has no Application Audio Capture (needs OBS 28+); desktop audio stays")
        return
    # the global devices OBS adds to every new collection
    for kind in ("wasapi_output_capture", "wasapi_input_capture"):
        try:
            for i in _attr(client.get_input_list(kind), "inputs", default=[]) or []:
                name = i.get("inputName")
                if not name or name == AUDIO_SOURCE:
                    continue
                muted = _attr(client.get_input_mute(name), "input_muted", default=False)
                if not muted:
                    change(f"mute OBS input '{name}' (only League's own audio is recorded)")
                    client.set_input_mute(name, True)
        except Exception as exc:  # noqa: BLE001
            say(f"could not check OBS audio input '{kind}' ({exc})")
    say(f"obs audio: '{AUDIO_SOURCE}' only")


def ensure_video(client, fps: int) -> None:
    screen = primary_screen()
    if not screen:
        say("!! could not read the screen size; OBS canvas left as is")
        return
    vs = client.get_video_settings()
    cur = (vs.base_width, vs.base_height, vs.output_width, vs.output_height,
           round(vs.fps_numerator / max(vs.fps_denominator, 1)))
    want = (screen[0], screen[1], screen[0], screen[1], fps)
    if cur == want:
        say(f"obs video: {screen[0]}x{screen[1]} 1:1 @ {fps} fps")
        return
    change(f"set OBS canvas and output to {screen[0]}x{screen[1]} @ {fps} fps (was "
           f"{cur[0]}x{cur[1]} -> {cur[2]}x{cur[3]} @ {cur[4]})")
    if not CHECK:
        try:
            client.set_video_settings(fps, 1, screen[0], screen[1], screen[0], screen[1])
        except Exception as exc:  # noqa: BLE001
            say(f"!! OBS refused the video settings ({exc}) - is a recording running?")


def ensure_output(client, rec_dir: Path) -> None:
    vendor = gpu_vendor()
    encoder = ENCODER_FOR.get(vendor, "x264")
    wanted = [
        ("Output", "Mode", "Simple"),
        ("SimpleOutput", "RecFormat2", "mkv"),
        ("SimpleOutput", "RecQuality", "HQ"),
        ("SimpleOutput", "RecEncoder", encoder),
        ("SimpleOutput", "FilePath", str(rec_dir)),
        ("Video", "AutoRemux", "false"),
    ]
    restart = False
    for cat, name, value in wanted:
        try:
            cur = _attr(client.get_profile_parameter(cat, name), "parameter_value", default=None)
        except Exception:  # noqa: BLE001
            cur = None
        if str(cur) == value:
            continue
        change(f"OBS {cat}/{name}: {cur!r} -> {value!r}")
        if cat == "Output" and name == "Mode":
            restart = True
        if not CHECK:
            try:
                client.set_profile_parameter(cat, name, value)
            except Exception as exc:  # noqa: BLE001
                say(f"!! could not set {cat}/{name} ({exc})")
    try:
        cur_dir = _attr(client.get_record_directory(), "record_directory", default="")
        if Path(cur_dir) != rec_dir:
            change(f"OBS recording folder: {cur_dir!r} -> {rec_dir}")
            if not CHECK:
                rec_dir.mkdir(parents=True, exist_ok=True)
                client.set_record_directory(str(rec_dir))
    except Exception as exc:  # noqa: BLE001
        say(f"could not set the recording folder over websocket ({exc}); FilePath set in the profile instead")
    say(f"obs output: mkv, encoder {encoder} ({vendor or 'no GPU vendor detected'}), HQ, {rec_dir}")
    if restart:
        say("!! OBS output mode changed - restart OBS once so it takes effect")


# ------------------------------------------------------------------ League

def report_league() -> None:
    try:
        import lcu
        riot = lcu.riot_install_dir()
    except Exception as exc:  # noqa: BLE001
        say(f"league: could not read Riot metadata ({exc})")
        return
    if riot:
        say(f"league: installed at {riot} (from Riot's product metadata; no PLAYSMART_LEAGUE_DIR needed)")
    else:
        say("league: not found in Riot's metadata - the default folders and the running client are "
            "tried at capture time; set PLAYSMART_LEAGUE_DIR in .env if the game id stays empty")


# ------------------------------------------------------------------ main

def main() -> None:
    global CHECK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report what would change; change nothing")
    ap.add_argument("--fps", type=int, default=int(os.environ.get("PLAYSMART_OBS_FPS", "30")),
                    help="OBS frame rate (default 30, or PLAYSMART_OBS_FPS)")
    ap.add_argument("--rec-dir", default=os.environ.get("PLAYSMART_OBS_REC_DIR", ""),
                    help="where OBS writes recordings (default <data>/obs_incoming)")
    ap.add_argument("--no-launch", action="store_true", help="do not start OBS if it is closed")
    ap.add_argument("--quiet", action="store_true", help="print only changes and problems")
    args = ap.parse_args()
    CHECK = args.check

    rec_dir = Path(args.rec_dir) if args.rec_dir else (session.data_dir().resolve() / "obs_incoming")
    if not CHECK:
        rec_dir.mkdir(parents=True, exist_ok=True)   # OBS refuses to start recording into a folder that is not there
    print(f"PlaySmart rig setup  {time.strftime('%Y-%m-%d %H:%M:%S')}  {'(check only)' if CHECK else ''}")

    ensure_env_file()
    report_league()

    password = ensure_websocket()
    client = websocket_reachable(password or "") if obs_running() else None
    if client is None and not obs_running() and not args.no_launch and password:
        client = launch_obs(password)
    if client is None:
        if obs_running():
            say("!! OBS is running but unreachable over websocket - close OBS and run this again")
        elif not CHECK:
            say("!! OBS not reachable; scene and video settings not applied")
        return
    ensure_scene(client)
    ensure_video(client, args.fps)
    ensure_output(client, rec_dir)
    print("done." if not _changes else f"{len(_changes)} change(s)" + (" (none applied: --check)" if CHECK else ""))


if __name__ == "__main__":
    main()
