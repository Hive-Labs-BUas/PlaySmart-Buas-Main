"""Pre-flight check for a capture PC: is everything the rig needs actually there?

Run it before arming a capture, every session, not only on setup day. It turns the
"did you plug in the tracker / is OBS open / is the password set" checklist into one
command with a pass/fail line per item, so a missing piece is found in ten seconds
instead of after a forty-minute game.

    poetry run python src/preflight.py            # everything
    poetry run python src/preflight.py --no-ble   # skip the 8 s ring scan
    poetry run python src/preflight.py --no-mic   # skip the 1 s microphone sample

Exit code 1 if anything the capture cannot run without is missing; 0 otherwise.
`warn` lines are things worth knowing that do not block a capture.

Added 2026-09 (iteration 5).

Modified 2026-10-14, after the 28 Sep sessions: checks the PC clock against a time
server (rigs were up to 5 s apart), the League client's minimap scale against the rig
standard (it ranged 0.3-1.5 across rigs, so the same look landed on minimaps of very
different sizes), whether a minimap rectangle has been measured, the OBS frame rate
and output size, and prints which streams this rig will actually record.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import re
import shutil
import socket
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
sys.path.insert(0, str(SRC))

import session  # noqa: E402  (loads .env)

OK, WARN, FAIL = " ok  ", "warn ", "FAIL "
_failures = 0

# The rig standard. Every capture PC uses these so that a player's numbers can be
# compared with another player's: the same minimap size on screen, the same video.
STANDARD_MINIMAP_SCALE = 1.0
STANDARD_HUD_SCALE = None            # not enforced yet; reported
STANDARD_OBS_FPS = 60
CLOCK_MAX_OFFSET_S = 0.5             # between this PC and the time server
NTP_SERVER = os.environ.get("PLAYSMART_NTP_SERVER", "time.windows.com")


def report(flag: str, name: str, detail: str = "") -> None:
    global _failures
    if flag is FAIL:
        _failures += 1
    print(f"{flag} {name:<22} {detail}")


def skipped(label: str) -> bool:
    skip = {p.strip().lower() for p in os.environ.get("PLAYSMART_SKIP", "").split(",") if p.strip()}
    return label in skip


# ---------------------------------------------------------------- environment

def check_python() -> None:
    v = sys.version_info
    flag = OK if (v.major, v.minor) == (3, 10) else FAIL
    report(flag, "python", f"{platform.python_version()} at {sys.executable}"
           + ("" if flag is OK else "  (tobii-research needs 3.10: poetry env use 3.10)"))


def check_imports() -> None:
    needed = ["tobii_research", "pygame", "pynput", "keyboard", "sounddevice", "soundfile",
              "bleak", "requests", "obsws_python", "psutil", "pandas", "matplotlib",
              "paramiko", "screeninfo"]
    missing = []
    for name in needed:
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 - any import failure is the finding
            missing.append(f"{name} ({type(exc).__name__})")
    if missing:
        report(FAIL, "packages", "missing: " + ", ".join(missing) + "  -> poetry install")
    else:
        report(OK, "packages", f"{len(needed)} capture imports resolve")


_KEY_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$")


def env_template_diff(env_path: Path, example_path: Path):
    """Compare .env with .env.example.

    Returns (missing, trailing): ``missing`` are the template lines (active or
    commented) whose key does not appear in .env at all; ``trailing`` are .env
    lines whose value carries a ``# comment`` after it, which the loader keeps as
    part of the value (``PLAYSMART_SKIP=eda   # ring`` skips nothing).
    """
    present = set()
    trailing = []
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                m = _KEY_RE.match(line)
                if m:
                    present.add(m.group(1))
                continue
            key, _, value = line.partition("=")
            present.add(key.strip())
            if " #" in value or "\t#" in value:
                trailing.append(raw)
    missing = []
    if example_path.is_file():
        for raw in example_path.read_text(encoding="utf-8").splitlines():
            m = _KEY_RE.match(raw.strip())
            if m and m.group(1) not in present:
                missing.append(raw.rstrip())
    return missing, trailing


def sync_env_file(env_path: Path, example_path: Path) -> tuple[int, int]:
    """Append template keys missing from .env and move trailing comments to their own line.

    Existing values are never changed. Appended lines are exactly as in
    .env.example (commented ones stay commented), under a dated header.
    """
    missing, trailing = env_template_diff(env_path, example_path)
    if not env_path.is_file():
        return 0, 0
    text = env_path.read_text(encoding="utf-8")
    if trailing:
        lines = text.splitlines()
        for i, raw in enumerate(lines):
            if raw in trailing:
                key_value, _, comment = raw.partition("#")
                lines[i] = f"# {comment.strip()}\n{key_value.rstrip()}"
        text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if missing:
        if not text.endswith("\n"):
            text += "\n"
        text += (f"\n# --- added from .env.example by preflight --fix-env on "
                 f"{time.strftime('%Y-%m-%d')} (defaults; edit as needed) ---\n")
        text += "\n".join(missing) + "\n"
    env_path.write_text(text, encoding="utf-8")
    return len(missing), len(trailing)


def check_env_file(fix: bool = False) -> None:
    example = REPO / ".env.example"
    if session.ENV_FILE.is_file():
        report(OK, ".env", str(session.ENV_FILE))
        if fix:
            added, moved = sync_env_file(session.ENV_FILE, example)
            if added or moved:
                report(OK, ".env sync", f"{added} key(s) added from .env.example, "
                       f"{moved} trailing comment(s) moved to their own line - "
                       "re-run preflight so the new values load")
        missing, trailing = env_template_diff(session.ENV_FILE, example)
        if trailing:
            report(FAIL, ".env comments", f"{len(trailing)} line(s) have a '# comment' after the "
                   "value; the loader keeps it as part of the value. Run preflight.py --fix-env "
                   "or move the comment to its own line: " + trailing[0].strip())
        if missing:
            keys = ", ".join(m.lstrip("# ").partition("=")[0] for m in missing)
            report(WARN, ".env template", f"{len(missing)} key(s) in .env.example not in .env "
                   f"({keys}) - defaults apply; preflight.py --fix-env appends them")
    else:
        report(WARN, ".env", "not found - copy .env.example and fill in the passwords")
    if os.environ.get("PLAYSMART_OBS", "1") != "0" and not os.environ.get("PLAYSMART_OBS_PASSWORD"):
        report(WARN, "obs password", "PLAYSMART_OBS_PASSWORD empty - fine only if OBS auth is off")
    mode = os.environ.get("PLAYSMART_UPLOAD", "keep").strip().lower() or "keep"
    if mode == "off":
        report(WARN, "upload", "PLAYSMART_UPLOAD=off - files stay local, no upload")
    elif not os.environ.get("PLAYSMART_SFTP_PASSWORD"):
        report(WARN, "sftp password", "PLAYSMART_SFTP_PASSWORD empty - files stay local, no upload")
    elif mode == "move":
        report(WARN, "upload", "PLAYSMART_UPLOAD=move - local copies are deleted after upload")
    else:
        report(OK, "upload", f"PLAYSMART_UPLOAD={mode} - upload and keep local copies")
    if os.environ.get("PLAYSMART_MOCK_GAZE") == "1":
        report(FAIL, "mock gaze", "PLAYSMART_MOCK_GAZE=1 is set - never record a real session with this")


def check_disk() -> None:
    root = session.data_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(root).free / 1e9
    flag = OK if free_gb > 10 else (WARN if free_gb > 3 else FAIL)
    report(flag, "disk", f"{free_gb:.0f} GB free for {root}")


# ------------------------------------------------------------------ hardware

def check_display() -> None:
    try:
        import screeninfo
        monitors = screeninfo.get_monitors()
    except Exception as exc:  # noqa: BLE001
        report(WARN, "display", f"could not query monitors ({exc})")
        return
    if not monitors:
        report(FAIL, "display", "no monitor reported")
        return
    primary = monitors[0]
    detail = f"{primary.width}x{primary.height}" + (f", {len(monitors)} monitors" if len(monitors) > 1 else "")
    flag = OK if (primary.width, primary.height) in ((1920, 1080), (2560, 1440)) else WARN
    report(flag, "display", detail + ("" if flag is OK else "  (rigs are 1920x1080 or 2560x1440; OBS output must be 1920x1080)"))
    if platform.system() == "Windows":
        try:
            import ctypes
            scale = ctypes.windll.shcore.GetScaleFactorForDevice(0)
            report(OK if scale == 100 else WARN, "scaling", f"{scale} %" + ("" if scale == 100 else "  (set 100 % or gaze and screen coordinates disagree)"))
        except Exception:  # noqa: BLE001
            pass


def check_tracker() -> None:
    if skipped("gaze"):
        report(WARN, "eye tracker", "skipped via PLAYSMART_SKIP")
        return
    try:
        import tobii_research as tr
        trackers = tr.find_all_eyetrackers()
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "eye tracker", f"tobii_research failed ({exc})")
        return
    if not trackers:
        report(FAIL, "eye tracker", "none found - USB, Tobii service, or Eye Tracker Manager holding it")
        return
    t = trackers[0]
    try:
        hz = t.get_gaze_output_frequency()
    except Exception:  # noqa: BLE001
        hz = "?"
    report(OK, "eye tracker", f"{t.model} {t.serial_number} at {hz} Hz - calibrate the player in Eye Tracker Manager")


def check_openface() -> None:
    if os.environ.get("PLAYSMART_OPENFACE", "1") == "0" or skipped("emotion"):
        report(WARN, "openface", "disabled (PLAYSMART_OPENFACE=0 / PLAYSMART_SKIP) - no emotion stream")
        return
    exe = REPO / "OpenFace_2.2.0_win_x64" / "FeatureExtraction.exe"
    if not exe.is_file():
        report(FAIL, "openface", f"{exe.relative_to(REPO)} missing - setup.ps1 downloads it, or set PLAYSMART_OPENFACE=0")
        return
    models = exe.parent / "model" / "patch_experts"
    cen = list(models.glob("cen_patches_*.dat")) if models.is_dir() else []
    if len(cen) < 4:
        report(FAIL, "openface models", f"{len(cen)} cen_patches_*.dat in model\\patch_experts - run download_models.ps1 inside the OpenFace folder")
    else:
        report(OK, "openface", f"binary and {len(cen)} CEN patch-expert models present")
    try:
        import psutil
        stale = [p.pid for p in psutil.process_iter(["name"]) if "FeatureExtraction" in (p.info.get("name") or "")]
        if stale:
            report(WARN, "openface", f"leftover FeatureExtraction.exe running (pids {stale}) - key_listener kills it at start")
    except Exception:  # noqa: BLE001
        pass


def check_microphone(sample: bool) -> None:
    if skipped("audio"):
        report(WARN, "microphone", "skipped via PLAYSMART_SKIP")
        return
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "microphone", f"no default input device ({exc})")
        return
    name = dev.get("name", "?")
    if not sample:
        report(OK, "microphone", f"default input: {name}")
        return
    try:
        import numpy as np
        rec = sd.rec(int(1.0 * 44100), samplerate=44100, channels=1, dtype="float32")
        sd.wait()
        rms = float(np.sqrt(np.mean(rec ** 2)))
        flag = OK if rms > 1e-4 else WARN
        report(flag, "microphone", f"{name}, 1 s level {rms:.4f}" + ("" if flag is OK else "  (silent - muted, wrong device, or Windows privacy blocks desktop apps)"))
    except Exception as exc:  # noqa: BLE001
        report(WARN, "microphone", f"{name}; could not sample ({exc})")


def check_ring(scan: bool) -> None:
    if skipped("eda"):
        report(WARN, "eda ring", "skipped via PLAYSMART_SKIP=eda")
        return
    if not scan:
        report(WARN, "eda ring", "scan skipped (--no-ble)")
        return
    try:
        import asyncio

        from bleak import BleakScanner

        import nuanic_eda

        async def scan_once():
            found = await BleakScanner.discover(timeout=8.0, return_adv=True)
            hits = []
            for dev, adv in found.values():
                if nuanic_eda.NUANIC_SERVICE.lower() in [s.lower() for s in adv.service_uuids]:
                    hits.append(f"{dev.name} {dev.address}")
            return hits

        hits = asyncio.run(scan_once())
    except Exception as exc:  # noqa: BLE001
        report(WARN, "eda ring", f"BLE scan failed ({exc}) - Bluetooth off?")
        return
    if hits:
        report(OK, "eda ring", ", ".join(hits))
    else:
        report(WARN, "eda ring", "not advertising - charge it, unpair from phone/Windows, or set PLAYSMART_SKIP=eda")


# ------------------------------------------------------------------ software

def check_obs() -> None:
    if os.environ.get("PLAYSMART_OBS", "1") == "0":
        report(WARN, "obs", "disabled (PLAYSMART_OBS=0) - no video")
        return
    # The library logs a full traceback on a refused connection; the one-line
    # message below is the useful part.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    try:
        import obsws_python as obs
        c = obs.ReqClient(host=os.environ.get("PLAYSMART_OBS_HOST", "127.0.0.1"),
                          port=int(os.environ.get("PLAYSMART_OBS_PORT", "4455")),
                          password=os.environ.get("PLAYSMART_OBS_PASSWORD", ""), timeout=3)
    except ImportError:
        report(FAIL, "obs", "obsws-python not installed - poetry install")
        return
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "obs", f"cannot connect ({type(exc).__name__}: {exc}) - OBS open? WebSocket enabled? password?")
        return
    try:
        ver = c.get_version()
        ws = tuple(int(x) for x in ver.obs_web_socket_version.split(".")[:2])
        flag = OK if ws >= (5, 1) else WARN
        report(flag, "obs", f"OBS {ver.obs_version}, websocket {ver.obs_web_socket_version}"
               + ("" if flag is OK else "  (5.1+ needed for StopRecord to report the file path)"))
        if c.get_record_status().output_active:
            report(WARN, "obs recording", "already recording - obs_recorder will stop and restart it")
        vs = c.get_video_settings()
        fps = vs.fps_numerator / max(vs.fps_denominator, 1)
        res = f"{vs.base_width}x{vs.base_height} -> {vs.output_width}x{vs.output_height} @ {fps:.0f} fps"
        problems = []
        if (vs.output_width, vs.output_height) != (1920, 1080):
            problems.append("output 1920x1080")
        if (vs.base_width, vs.base_height) != (vs.output_width, vs.output_height) and vs.output_width < 1920:
            problems.append("output smaller than the canvas")
        if abs(fps - STANDARD_OBS_FPS) > 0.5:
            problems.append(f"{STANDARD_OBS_FPS} fps")
        flag = OK if not problems else WARN
        report(flag, "obs video", res + ("" if flag is OK else "  (rig standard: " + ", ".join(problems) + "; load obs settings/PlaySmart_League.json)"))
        try:
            scene = c.get_current_program_scene().current_program_scene_name
            items = c.get_scene_item_list(scene).scene_items
            kinds = [i.get("inputKind", "") for i in items]
            names = [i.get("sourceName", "") for i in items]
            if any(k == "game_capture" for k in kinds):
                report(OK, "obs scene", f"'{scene}': game capture present ({', '.join(names)})")
            elif any(k in ("window_capture", "monitor_capture") for k in kinds):
                report(WARN, "obs scene", f"'{scene}' uses window/display capture - use Game Capture of the League window (the overlay is a full-size window)")
            else:
                report(WARN, "obs scene", f"'{scene}' has no game capture source ({', '.join(names) or 'empty'})")
        except Exception:  # noqa: BLE001
            pass
        try:
            rec_dir = c.get_record_directory().record_directory
            report(OK, "obs recording dir", rec_dir)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        report(WARN, "obs", f"connected but query failed ({exc})")


def check_clock() -> None:
    """The PC clock against a time server.

    Every stream is stamped with this clock. The merge joins players through the
    game clock, so a few seconds of offset do not break it, but anything that joins
    on wall time across PCs does break, and a mid-game time sync (Pc_4, 28 Sep 2026)
    steps the clock under a running recording. Sync *before* the session.
    """
    if platform.system() != "Windows":
        report(WARN, "clock", "offset check only implemented on Windows (w32tm)")
        return
    import subprocess
    try:
        out = subprocess.run(["w32tm", "/stripchart", f"/computer:{NTP_SERVER}", "/samples:1",
                              "/dataonly"], capture_output=True, text=True, timeout=15).stdout
    except Exception as exc:  # noqa: BLE001
        report(WARN, "clock", f"could not run w32tm ({exc})")
        return
    m = re.search(r",\s*([+-]?\d+(?:\.\d+)?)s", out)
    if not m:
        report(WARN, "clock", f"no answer from {NTP_SERVER} (offline?) - run `w32tm /resync` on the BUas network")
        return
    offset = float(m.group(1))
    flag = OK if abs(offset) <= CLOCK_MAX_OFFSET_S else FAIL
    report(flag, "clock", f"{offset:+.2f} s vs {NTP_SERVER}"
           + ("" if flag is OK else "  -> run `w32tm /resync` (as admin) now, not during the game"))


def check_league_settings() -> None:
    """The League client's game.cfg: minimap scale, HUD scale, resolution.

    The gaze-on-minimap numbers are only comparable between players if the minimap
    is the same size on every rig. Needs the client open (not a game).
    """
    if session.game() != "league":
        return
    try:
        import lcu
        client = lcu.Client.connect(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        report(WARN, "league client", f"lcu unavailable ({exc})")
        return
    if client is None:
        report(WARN, "league client", "not running - open the client so its settings can be checked")
        return
    try:
        gs = client.game_settings() or {}
    except Exception as exc:  # noqa: BLE001
        report(WARN, "league settings", f"could not read game settings ({exc})")
        return
    hud = gs.get("HUD") or {}
    gen = gs.get("General") or {}
    scale = hud.get("MinimapScale")
    try:
        flag = OK if abs(float(scale) - STANDARD_MINIMAP_SCALE) < 0.01 else FAIL
    except (TypeError, ValueError):
        flag = WARN
    report(flag, "minimap scale", f"MinimapScale {scale}"
           + ("" if flag is OK else f"  -> set {STANDARD_MINIMAP_SCALE:.1f} in Settings > Interface (rig standard)"))
    extras = {k: hud.get(k) for k in ("GlobalScale", "ChatScale") if k in hud}
    extras.update({k: gen.get(k) for k in ("Width", "Height", "WindowMode") if k in gen})
    if extras:
        report(OK, "league hud", ", ".join(f"{k} {v}" for k, v in extras.items()))


def check_minimap_rect() -> None:
    """Has the minimap rectangle been measured on this rig?"""
    if session.game() != "league" or skipped("gaze"):
        return
    rect = session.data_dir() / "json" / "minimap_rect.json"
    if rect.is_file():
        try:
            import json
            r = json.loads(rect.read_text(encoding="utf-8"))
            report(OK, "minimap rect", f"measured: {r.get('x0')},{r.get('y0')}-{r.get('x1')},{r.get('y1')} px "
                   f"({rect.name}; re-measure after changing resolution or minimap scale)")
        except (OSError, ValueError):
            report(WARN, "minimap rect", f"{rect} unreadable")
    else:
        report(WARN, "minimap rect", "not measured - the recorder estimates it from MinimapScale; "
               "`gaze_validation.py --rect-only` with the game open takes 20 s and is exact")


def check_streams() -> None:
    """One line saying which streams this rig will write - so 'no emotion' is a
    decision made before the game, not a discovery after it."""
    streams = ["gamestate", "gaze", "emotion", "input", "audio", "eda", "video"]
    disabled = {
        "gaze": skipped("gaze"),
        "emotion": skipped("emotion") or os.environ.get("PLAYSMART_OPENFACE", "1") == "0",
        "input": skipped("input"),
        "audio": skipped("audio"),
        "eda": skipped("eda"),
        "video": os.environ.get("PLAYSMART_OBS", "1") == "0",
    }
    off = [s for s in streams if disabled.get(s)]
    on = [s for s in streams if s not in off]
    report(WARN if off else OK, "streams", "recording: " + ", ".join(on)
           + (f"   NOT recording: {', '.join(off)}" if off else ""))


def check_league() -> None:
    if session.game() != "league":
        report(OK, "game", f"PLAYSMART_GAME={session.game()} - manual F7/F12 bracket, no game-state stream")
        return
    try:
        import requests
        r = requests.get("https://127.0.0.1:2999/liveclientdata/gamestats", timeout=1.5, verify=False)
        mode = r.json().get("gameMode", "?")
        report(OK, "league", f"a game is running right now ({mode}) - the API answers")
    except Exception:  # noqa: BLE001
        report(OK, "league", "no game running (normal before F7); the API appears once a game loads")


def check_server() -> None:
    host = os.environ.get("PLAYSMART_SFTP_HOST", "10.4.28.2")
    port = int(os.environ.get("PLAYSMART_SFTP_PORT", "2422"))
    try:
        with socket.create_connection((host, port), timeout=3):
            report(OK, "sftp server", f"{host}:{port} reachable")
    except OSError as exc:
        report(WARN, "sftp server", f"{host}:{port} unreachable ({exc}) - BUas network / VPN? upload will fail, files stay local")


def main() -> None:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-ble", action="store_true", help="skip the Bluetooth scan for the EDA ring")
    ap.add_argument("--no-mic", action="store_true", help="skip the 1 s microphone level sample")
    ap.add_argument("--fix-env", action="store_true",
                    help="append keys new in .env.example to .env and fix trailing comments (values untouched)")
    ap.add_argument("--env-only", action="store_true", help="only the .env checks (used by setup.ps1)")
    args = ap.parse_args()

    print(f"PlaySmart pre-flight  {time.strftime('%Y-%m-%d %H:%M:%S')}  {platform.node()}  repo {REPO}\n")
    if args.env_only:
        check_env_file(fix=args.fix_env)
        raise SystemExit(1 if _failures else 0)
    check_python()
    check_imports()
    check_env_file(fix=args.fix_env)
    check_disk()
    check_display()
    check_tracker()
    check_openface()
    check_microphone(sample=not args.no_mic)
    check_ring(scan=not args.no_ble)
    check_obs()
    check_clock()
    check_league_settings()
    check_minimap_rect()
    check_league()
    check_server()
    check_streams()
    print()
    if _failures:
        print(f"{_failures} blocking problem(s) above. Fix them before pressing F7.")
        raise SystemExit(1)
    print("Ready to capture. Calibrate the player, then run main.bat / key_listener.py and press F7.")


if __name__ == "__main__":
    main()
