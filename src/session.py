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
LITE_SKIPS = {"gaze", "face", "emotion", "eda"}
SKIP_ALIASES = {"emotion": "face", "face": "emotion"}

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
    if not path.is_file():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


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


def skip_set(tier: str | None = None) -> set:
    """Streams to neither start nor expect: PLAYSMART_SKIP plus the lite tier's set."""
    skips = {p.strip().lower() for p in os.environ.get(ENV_SKIP, "").split(",") if p.strip()}
    if (tier or resolve_tier()) == "lite":
        skips |= LITE_SKIPS
    skips |= {SKIP_ALIASES[s] for s in list(skips) if s in SKIP_ALIASES}
    return skips


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


def stop_requested() -> bool:
    """True when this recorder should save and exit.

    Either the orchestrator asked (stop file), or the operator pressed F12
    directly. The manual path is kept so a run started by hand, without the
    orchestrator, still stops the way people expect.
    """
    if stop_path().exists():
        return True
    try:
        import keyboard  # optional: absent in headless tests
        return keyboard.is_pressed("f12")
    except Exception:
        return False
