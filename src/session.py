"""Session identity and stop signalling shared by every capture recorder.

Two problems this solves.

**Identity.** Before this, each recorder invented its own filename from its own
local clock (``gaze_data_2025-11-23_13-55-49.csv``,
``input_log_23-11-2025_13-09-53.csv``) and ``pop_up_screen.py`` tried to pair
them afterwards by taking the newest un-renamed file in each folder. That
guess is wrong whenever a recorder crashes, whenever two sessions run back to
back, and whenever a file is left over from a previous run. Now the
orchestrator mints one id per game and passes it down through the environment,
so every stream names itself correctly the moment it opens its file.

**Stopping.** Recorders used to watch for F12 themselves while the orchestrator
also killed them with ``terminate()`` two seconds later. Three of the five only
write their output at the end, so that race truncated or destroyed gaze, EDA and
audio. With auto start/stop there is no F12 press at all, so the recorders need
a signal they can observe. ``stop_requested()`` is that signal; F12 still works
for a manual abort.

Environment contract, set by ``key_listener.py``:

    PLAYSMART_SESSION_ID    the session id every stream shares
    PLAYSMART_STOP_FILE     path whose existence means "save and exit"
    PLAYSMART_DATA_DIR      root for the data/ tree (default: ./data)

**Staying out of the game's way** (2026-11-10). Everything a recorder does while a game
runs competes with the game for the CPU and sits, for the input recorder, *inside* the
path of every key press. The helpers at the end of this module are the one place that
decides how: process priority per recorder (``priority_flags``), which cores OpenFace may
use (``face_cores``, ``limit_cores``), and how an abort key is read without installing a
keyboard hook (``key_down``).

    PLAYSMART_ABORT_KEY     f12 (default) | f1..f12 | off. The desktop app sets ``off``:
                            F12 is League's screenshot key, and a screenshot must not end
                            a recording nobody is watching.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

ENV_SESSION = "PLAYSMART_SESSION_ID"
ENV_STOP = "PLAYSMART_STOP_FILE"
ENV_DATA = "PLAYSMART_DATA_DIR"
ENV_GAME = "PLAYSMART_GAME"
ENV_TIER = "PLAYSMART_TIER"                    # full | lite | auto (default)
ENV_TIER_RESOLVED = "PLAYSMART_TIER_RESOLVED"  # set by the orchestrator for its children
ENV_SKIP = "PLAYSMART_SKIP"

# Streams that need the lab hardware. Without an eye tracker the rig runs "lite":
# video, input, audio and game state only. Added 2026-10-27 so the same code runs
# on a player's own PC (no Tobii, no ring, no OpenFace) without a wall of errors.
#
# 2026-11-02: the OpenFace stream is called ``face`` (action units, head pose, webcam
# gaze, eyelid aperture); ``emotion`` is accepted in PLAYSMART_SKIP as its old name.
#
# 2026-11-10: ``face`` left the lite set. It needs a webcam and OpenFace, not the Tobii,
# so a player's own PC can record it. It is opt-in instead (``PLAYSMART_FACE=1``, see
# ``face_enabled``): it measures a person's face, so somebody has to decide to turn it on.
LITE_SKIPS = {"gaze", "eda"}
SKIP_ALIASES = {"emotion": "face", "face": "emotion"}
ENV_FACE = "PLAYSMART_FACE"
OPENFACE_DIR = "OpenFace_2.2.0_win_x64"
ENV_ABORT_KEY = "PLAYSMART_ABORT_KEY"
ENV_FACE_CORES = "PLAYSMART_FACE_CORES"        # logical processors OpenFace may use
ENV_FACE_PRIORITY = "PLAYSMART_FACE_PRIORITY"  # below (default) | idle | normal

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"

_fallback_id = None


def load_env_file(path: Path = ENV_FILE) -> int:
    """Read KEY=VALUE lines from the repo's .env into os.environ.

    Secrets (the SFTP password, the OBS websocket password) used to be literals
    in a public repository. They now live in an untracked .env next to
    pyproject.toml — see .env.example. Values already present in the
    environment win, so a shell override still works. No dependency on
    python-dotenv: every recorder imports this module and must stay light.

    Returns:
        Number of variables set from the file.
    """
    count = 0
    for key, value in _read_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value
            _from_file.add(key)
            count += 1
    return count


_from_file: set = set()      # keys this process took from .env, not from the shell


def _read_env_file(path: Path) -> dict:
    out: dict = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().strip("'\"")
    return out


def refresh_env(keys: tuple, path: Path = ENV_FILE) -> dict:
    """Re-read some keys from .env into this running process (added 2026-11-10).

    The desktop app switches the face stream on and off by editing the rig's .env; the
    orchestrator stays running between games, so it calls this when a game starts. A value
    that came from the shell still wins; a value that came from the file follows the file,
    including being removed from it.

    Returns:
        ``{key: new value}`` for the keys that changed (None = removed).
    """
    file = _read_env_file(path)
    changed: dict = {}
    for key in keys:
        if key in os.environ and key not in _from_file:
            continue                                   # set in the shell: leave it
        if key in file:
            if os.environ.get(key) != file[key]:
                os.environ[key] = file[key]
                changed[key] = file[key]
            _from_file.add(key)
        elif key in _from_file:
            os.environ.pop(key, None)
            _from_file.discard(key)
            changed[key] = None
    return changed


load_env_file()


def new_session_id() -> str:
    """Mint an id that sorts by time and is still unique across machines.

    Sortable prefix so a directory listing is chronological; short uuid suffix
    so two capture PCs starting in the same second cannot collide.
    """
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def session_id() -> str:
    """The id for this capture, from the orchestrator when there is one.

    A recorder started by hand (no orchestrator) still gets a usable id rather
    than failing, but it is minted once per process so all of that recorder's
    files agree with each other.
    """
    global _fallback_id
    sid = os.environ.get(ENV_SESSION)
    if sid:
        return sid
    if _fallback_id is None:
        _fallback_id = new_session_id()
        print(f"[session] no {ENV_SESSION} in environment; using {_fallback_id}")
    return _fallback_id


def data_dir() -> Path:
    return Path(os.environ.get(ENV_DATA, "data"))


def game() -> str:
    """Which game this rig captures: ``league`` (default) or anything else.

    ``league`` brackets the capture on League's local API and records game state.
    Any other value (``valorant``, ``cs2``) is the manual bracket the rig had before
    iteration 5 - F7 starts, F12 stops - with every other iteration-5 change kept
    (session id, graceful stop, OBS anchor, pre-flight, upload modes). Added so the
    branch can merge into ``Dev`` without breaking the Valorant rigs.
    """
    return (os.environ.get(ENV_GAME, "league").strip().lower() or "league")


def tracker_present() -> bool:
    """Is a Tobii eye tracker connected right now? False when tobii_research is not installed."""
    try:
        import tobii_research as tr  # noqa: WPS433 - optional on lite installs
        return bool(tr.find_all_eyetrackers())
    except Exception:  # noqa: BLE001 - missing wheel, missing service, anything: no tracker
        return False


def tier_wanted() -> str:
    """``full``, ``lite`` or ``auto`` from PLAYSMART_TIER (default auto)."""
    want = os.environ.get(ENV_TIER, "auto").strip().lower() or "auto"
    return want if want in ("full", "lite", "auto") else "auto"


def resolve_tier() -> str:
    """Which tier this capture runs at.

    ``full``: the lab rig; an eye tracker is required (pre-flight fails without one).
    ``lite``: video + input + audio + game state; no gaze, emotion or EDA.
    ``auto`` (default): ``full`` when a tracker is found, ``lite`` otherwise.

    The orchestrator resolves this once per capture and passes the answer to its
    children through PLAYSMART_TIER_RESOLVED, so every recorder and the meta agree.
    """
    resolved = os.environ.get(ENV_TIER_RESOLVED)
    if resolved in ("full", "lite"):
        return resolved
    want = tier_wanted()
    if want != "auto":
        return want
    return "full" if tracker_present() else "lite"


def openface_exe() -> "Path | None":
    """OpenFace's FeatureExtraction.exe, or None when it is not on this PC.

    Looked for, in order: ``PLAYSMART_OPENFACE_EXE``; the rig folder; the folder above it
    (the installed app keeps one OpenFace next to ``rig``); a sibling clone of the lab rig
    (``PlaySmart-Buas-Main``, where setup.ps1 downloads it); ``%LOCALAPPDATA%\\PlaySmart``.
    """
    env = os.environ.get("PLAYSMART_OPENFACE_EXE", "").strip().strip("'\"")
    cands = [Path(env)] if env else []
    cands += [REPO / OPENFACE_DIR, REPO.parent / OPENFACE_DIR,
              REPO.parent / "PlaySmart-Buas-Main" / OPENFACE_DIR]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(Path(local) / "PlaySmart" / OPENFACE_DIR)
    for c in cands:
        exe = c if c.suffix.lower() == ".exe" else c / "FeatureExtraction.exe"
        if exe.is_file():
            return exe
    return None


def face_wanted() -> bool:
    """Did someone switch the face stream on for this PC? (``PLAYSMART_FACE=1``)

    Off by default. The stream is OpenFace's measurements of the player's face from the
    webcam (action units, head pose, eyelid aperture); no image and no emotion label is
    stored, but it is still data about an identifiable person's face, so it needs the
    player's consent and is never on unless somebody turned it on. ``PLAYSMART_OPENFACE=0``
    and ``face``/``emotion`` in PLAYSMART_SKIP both still switch it off.
    """
    if os.environ.get("PLAYSMART_OPENFACE", "1") == "0":
        return False
    return os.environ.get(ENV_FACE, "0").strip().lower() in ("1", "true", "yes", "on")


def face_enabled() -> bool:
    """Wanted, not skipped, and OpenFace is actually on this PC."""
    raw = {p.strip().lower() for p in os.environ.get(ENV_SKIP, "").split(",") if p.strip()}
    return face_wanted() and not (raw & {"face", "emotion"}) and openface_exe() is not None


def skip_set(tier: str | None = None) -> set:
    """Streams to neither start nor expect.

    PLAYSMART_SKIP, plus the lite tier's set (no eye tracker: no gaze, no EDA), plus
    ``face`` unless it was switched on and OpenFace is installed (``face_enabled``).
    """
    skips = {p.strip().lower() for p in os.environ.get(ENV_SKIP, "").split(",") if p.strip()}
    if (tier or resolve_tier()) == "lite":
        skips |= LITE_SKIPS
    if not face_enabled():
        skips.add("face")
    skips |= {SKIP_ALIASES[s] for s in list(skips) if s in SKIP_ALIASES}
    return skips


_dpi_done = False


def screen_px() -> "tuple[int, int] | None":
    """The primary screen in physical pixels, read now (not cached).

    Every recorder used to read the screen once at start; a resolution change during a
    session (9 Nov 2026: the lab screen had been set to 1080p, the game was changed back
    mid-game) then left the logs on a grid that no longer existed. Callers that need the
    current size ask here each time.
    """
    global _dpi_done
    if os.name == "nt":
        try:
            import ctypes
            if not _dpi_done:
                _dpi_done = True
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor: physical pixels
                except Exception:  # noqa: BLE001 - already set, or Windows 7
                    try:
                        ctypes.windll.user32.SetProcessDPIAware()
                    except Exception:  # noqa: BLE001
                        pass
            w = int(ctypes.windll.user32.GetSystemMetrics(0))
            h = int(ctypes.windll.user32.GetSystemMetrics(1))
            if w > 0 and h > 0:
                return w, h
        except Exception:  # noqa: BLE001
            pass
    try:
        import screeninfo
        m = screeninfo.get_monitors()[0]
        return int(m.width), int(m.height)
    except Exception:  # noqa: BLE001
        return None


def status_path() -> Path:
    return data_dir() / "rig_status.json"


def write_status(**fields) -> None:
    """Merge fields into ``<data>/rig_status.json`` (best effort; the desktop app reads it).

    Added 2026-10-28. Keys used: state (starting|armed|recording|finishing|idle|stopped),
    tier, session_id, tracker, obs, updated_ms, pid, last_error.
    """
    import json
    import time
    p = status_path()
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        cur = {}
    cur.update(fields, updated_ms=int(time.time() * 1000), pid=os.getpid(), python=sys.executable)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def stream_dir(stream: str) -> Path:
    d = data_dir() / stream
    d.mkdir(parents=True, exist_ok=True)
    return d


def stream_path(stream: str, ext: str = "csv", sid: str | None = None) -> Path:
    """Canonical output path: ``data/<stream>/<session_id>_<stream>.<ext>``.

    Every recorder calls this instead of building its own timestamped name, so
    the session id is in the filename from the moment the file is created.
    """
    sid = sid or session_id()
    return stream_dir(stream) / f"{sid}_{stream}.{ext}"


def adopt(sid: str) -> None:
    """Claim `sid` for this process and everything it spawns.

    The orchestrator calls this after minting an id so that it and its children
    resolve the same stop-file path, rather than the parent falling back to an
    id of its own.
    """
    os.environ[ENV_SESSION] = sid
    os.environ[ENV_STOP] = str(data_dir() / f".stop-{sid}")


def stop_path() -> Path:
    """Where the stop flag lives for this capture."""
    explicit = os.environ.get(ENV_STOP)
    if explicit:
        return Path(explicit)
    return data_dir() / f".stop-{session_id()}"


def request_stop() -> None:
    """Ask every recorder in this session to save and exit."""
    p = stop_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(datetime.now()), encoding="utf-8")


def clear_stop() -> None:
    """Clear a stale flag before starting a new capture."""
    try:
        stop_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[session] could not clear stop flag: {exc}")


def under_orchestrator() -> bool:
    """True in a recorder the orchestrator started (and in the orchestrator itself once a
    capture is running): the stop file is then the only stop signal anybody needs."""
    return bool(os.environ.get(ENV_STOP))


def stop_requested() -> bool:
    """True when this recorder should save and exit.

    Either the orchestrator asked (stop file), or - only in a recorder started by hand,
    without the orchestrator - the operator holds the abort key.

    Modified 2026-11-10. This used to call ``keyboard.is_pressed("f12")``, and that call
    installs a system-wide low-level keyboard hook in the calling process. Every recorder
    polls this function, so a game ran with five to seven Python keyboard hooks in series,
    and Windows delivers each key press to the game only after the last of them returns.
    Under the orchestrator the stop file is the signal (the orchestrator watches the abort
    key, once, without a hook); by hand the key is read with ``GetAsyncKeyState``.
    """
    if stop_path().exists():
        return True
    if under_orchestrator():
        return False
    key = abort_key()
    return bool(key) and key_down(key)


# ----------------------------------------------------------------------------- game load
# Windows priority classes (the numbers are CreateProcess creation flags as well).
PRIORITY = {"idle": 0x00000040, "below": 0x00004000, "normal": 0x00000020, "above": 0x00008000}
# Function keys only: an abort key that is also a game key would be a bug.
_VK = {f"f{i}": 0x6F + i for i in range(1, 13)}

# What each recorder is to the game. ``above`` is not "important", it is "in the input
# path": the input recorder's hook has to return before the game sees the key, so it must
# never wait for a core. It uses microseconds of CPU per event, so it takes nothing away.
# Audio and gaze are fed by device threads and lose samples when starved: normal. The face
# wrapper only reads a file; OpenFace itself is set in face_recorder (below normal, capped).
RECORDER_PRIORITY = {"input": "above", "audio": "normal", "gaze": "normal", "face": "below", "eda": "below"}


def abort_key() -> "str | None":
    """The key that aborts a capture: ``f12`` unless PLAYSMART_ABORT_KEY says otherwise;
    None when it is ``off``. A title without a game-state API (PLAYSMART_GAME other than
    league) ends its session with this key, so there it cannot be switched off."""
    raw = os.environ.get(ENV_ABORT_KEY, "f12").strip().lower()
    if raw in ("", "off", "none", "0", "no", "false"):
        return "f12" if game() != "league" else None
    return raw if raw in _VK else "f12"


def key_down(name: str) -> bool:
    """Is this function key down now (or was it pressed since the last call)?

    ``GetAsyncKeyState`` reads the key state; nothing is hooked, so nothing is added to the
    path between the keyboard and the game. False off Windows and for unknown names.
    """
    vk = _VK.get(str(name).lower())
    if vk is None or sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8001)
    except Exception:  # noqa: BLE001
        return False


def priority_flags(level: str) -> int:
    """``creationflags`` that start a child at this priority class; 0 off Windows.

    Always explicit, ``normal`` included: a child otherwise *inherits* a below-normal
    parent's class, and the desktop app lowers itself while a game is recorded.
    """
    if os.name != "nt":
        return 0
    return PRIORITY.get(level, PRIORITY["normal"])


def set_priority(level: str, pid: "int | None" = None) -> bool:
    """Set the priority class of this process (or ``pid``). False when it could not be set."""
    if sys.platform != "win32" or level not in PRIORITY:
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        if pid is None:
            k.GetCurrentProcess.restype = ctypes.c_void_p
            return bool(k.SetPriorityClass(k.GetCurrentProcess(), PRIORITY[level]))
        handle = k.OpenProcess(0x0200, False, int(pid))      # PROCESS_SET_INFORMATION
        if not handle:
            return False
        try:
            return bool(k.SetPriorityClass(handle, PRIORITY[level]))
        finally:
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def face_priority() -> str:
    """Priority class of OpenFace: ``below`` unless PLAYSMART_FACE_PRIORITY is idle or normal."""
    raw = os.environ.get(ENV_FACE_PRIORITY, "below").strip().lower()
    return raw if raw in ("idle", "below", "normal") else "below"


def face_cores(logical: "int | None" = None) -> int:
    """How many logical processors OpenFace may use; 0 = no limit.

    Default: a quarter of the machine, at least 1 and at most 4. OpenFace spreads over
    every core it can see (TBB and OpenBLAS), and a thread of it on the sibling of the
    core the game's main thread runs on costs frame rate whatever its priority is.
    PLAYSMART_FACE_CORES overrides (``0`` or ``all`` = no limit).
    """
    logical = logical or os.cpu_count() or 1
    raw = os.environ.get(ENV_FACE_CORES, "").strip().lower()
    if raw in ("0", "all", "off", "none"):
        return 0
    if raw:
        try:
            return max(1, min(int(raw), logical))
        except ValueError:
            pass
    return max(1, min(4, logical // 4))


def core_mask(n: int, logical: "int | None" = None) -> int:
    """Affinity mask of the last ``n`` logical processors.

    The last ones on purpose: on a hybrid Intel CPU they are the efficiency cores, on a
    CPU with SMT they are the two threads of one physical core, and Windows hands a game
    the first ones.
    """
    logical = min(logical or os.cpu_count() or 1, 64)
    n = max(1, min(int(n), logical))
    return ((1 << n) - 1) << (logical - n)


def limit_cores(pid: int, n: int) -> bool:
    """Pin process ``pid`` to the last ``n`` logical processors. False when not applied."""
    if sys.platform != "win32" or not n:
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k.OpenProcess(0x0200 | 0x1000, False, int(pid))   # SET_INFORMATION | QUERY_LIMITED
        if not handle:
            return False
        try:
            return bool(k.SetProcessAffinityMask(handle, core_mask(n)))
        finally:
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False
