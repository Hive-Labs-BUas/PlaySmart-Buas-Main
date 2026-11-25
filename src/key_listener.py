"""Capture orchestrator: start the recorders when a League game starts, stop them when it ends.

Modified 2026-09 (iteration 5): rewritten - see below.

What changed and why
--------------------

**Capture now brackets the game, not the operator.** Before, F7 started the
recorders and F12 stopped them, so a session was however long someone remembered
to leave it running: the archive has a 74-minute recording containing one
27-minute game, and 364 minutes of video holding 171 minutes of anything at all.
League serves a local API at 127.0.0.1:2999 for exactly as long as a game is
running, so the game itself can define the boundaries.

**Stopping is now graceful, and that is not cosmetic.** This module used to call
``process.terminate()`` two seconds after F12. Three of the five recorders only
write their output at the end — gaze builds a list and writes in ``finally``,
EDA accumulates in memory, audio needs its ``SoundFile`` context manager to close
to finalise the WAV header. Terminating them truncated or destroyed that data
(the 44-byte "empty" WAVs in the old archive are exactly an unfinalised header).
With automatic stopping there is no F12 press at all, so the recorders are asked
to stop via ``session.request_stop()`` and then *waited for*; terminate is a last
resort after a timeout.

**Every stream shares one session id**, passed down the environment, so files are
named correctly when they are created instead of being paired afterwards by
guessing which file in each folder is newest.

Keys: F7 arms, F12 aborts (``PLAYSMART_ABORT_KEY``: another function key, or ``off``).

Modified 2026-11-10 (game load): the keys are read with ``GetAsyncKeyState``, not with
the ``keyboard`` package, which installs a low-level keyboard hook in whatever process
first asks it for a key: the orchestrator and every recorder did, so each key press went
through five to seven Python hooks before the game saw it. One hook is left, in the input
recorder, which is started above normal priority; OpenFace and the post-game work run
below normal. See ``session.RECORDER_PRIORITY``.

Modified 2026-10-14: **re-arms itself after every game** (``PLAYSMART_REARM=0`` to
go back to pressing F7 each time). On 28 Sep 2026 one rig was armed late for game 2
and never armed for game 3; with auto re-arm the next game is caught from its loading
screen without anyone touching the keyboard. The end-of-session report now also
prints the game-state meta's warnings (late start, no minimap rectangle) so they are
read while the players are still in the room.

Modified 2026-11-10: three things the session was not recording. (1) The screen and
game-window geometry, second by second (``display_watch.py`` -> ``<sid>_display.json``),
because a resolution change during a game left every pixel-based stream on a grid that no
longer existed. (2) The game sound as its own WAV, copied out of the OBS recording after
the game (``obs_recorder.extract_game_audio``). (3) The face stream, by a recorder without
the overlay window (``face_recorder.py``), at either tier, when ``PLAYSMART_FACE=1``.

    python src/key_listener.py                 # normal capture
    python src/key_listener.py --dry-run       # no League, no hardware, stubs
    python src/key_listener.py --once          # one game then exit
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import session  # noqa: E402
import liveclient_recorder as liveclient  # noqa: E402
import obs_recorder  # noqa: E402

REPO = SRC.parent

# Recorders started for every capture, in order. The 2s pause before EDA is
# preserved from the original: the ring's BLE scan misbehaves if it starts at
# the same moment as everything else.
# The face recorder has no window (2026-11-10). PLAYSMART_OVERLAY=1 brings back the
# Valorant-era full-screen gaze overlay, which records the same face stream.
_FACE = (("face/overlay", "src/Emotion_gaze_visualization.py", 0.0)
         if os.environ.get("PLAYSMART_OVERLAY", "0") == "1"
         else ("face", "src/face_recorder.py", 0.0))
RECORDERS = [
    ("gaze", "src/eye_tracking_script.py", 0.0),
    _FACE,                                       # OpenFace AUs / pose / eyelid aperture; opt-in
    ("input", "src/keyboard_recording.py", 0.0),
    ("audio", "src/microphone_recording.py", 0.0),
    ("eda", "src/nuanic_eda.py", 2.0),
]

# Comma-separated labels in PLAYSMART_SKIP are neither started nor expected to
# produce a file — `set PLAYSMART_SKIP=eda` when the ring is not available.
# Without this, a missing sensor makes the end-of-session report show a red line
# on every single capture, and a check that is always red is a check nobody
# reads. That is precisely how the old pipeline hid a year of data loss.
#
# Modified 2026-10-27: the set is resolved per capture by session.skip_set(), which
# adds gaze/emotion/eda when the rig runs at the lite tier (no eye tracker). A
# player's own PC therefore records video + input + audio + game state cleanly
# instead of reporting a dead gaze recorder every game.
SKIP: set = set()


def resolve_tier() -> str:
    """Decide full/lite for this capture and publish it to the recorders."""
    global SKIP
    os.environ.pop(session.ENV_TIER_RESOLVED, None)
    tier = session.resolve_tier()
    os.environ[session.ENV_TIER_RESOLVED] = tier
    SKIP = session.skip_set(tier)
    return tier


def active_recorders() -> list:
    """RECORDERS minus anything named in PLAYSMART_SKIP (plus the lite tier's set)."""
    return [entry for entry in RECORDERS
            if entry[0].lower() not in SKIP and entry[0].split("/")[0].lower() not in SKIP]


def stream_files(label: str, sid: str, dry_run: bool) -> list:
    """Files a recorder actually wrote for this session.

    Args:
        label: A label from RECORDERS, or "gamestate".
        sid: The session id.
        dry_run: Look under the stub directories instead of the real streams.

    Returns:
        Matching paths, newest last. Empty if the recorder wrote nothing.
    """
    name = f"stub-{label.replace('/', '-')}" if dry_run else label.split("/")[0]
    directory = session.data_dir() / name
    return sorted(directory.glob(f"{sid}*")) if directory.is_dir() else []

# How long to let each recorder save before killing it. Gaze writes a large
# DataFrame; EDA writes everything it has buffered. Whisper transcription is off
# by default now (see microphone_recording.py) so audio no longer needs minutes.
STOP_TIMEOUT_S = 90


def kill_openface() -> None:
    """Clear leftover OpenFace processes from a previous run."""
    try:
        import psutil
    except ImportError:
        return
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if "FeatureExtraction.exe" in name:
            print(f"Killing leftover OpenFace process: PID {proc.pid}")
            try:
                proc.kill()
            except Exception as exc:
                print(f"  could not kill {proc.pid}: {exc}")


def child_env(sid: str) -> dict:
    env = dict(os.environ)
    env[session.ENV_SESSION] = sid
    env[session.ENV_STOP] = str(session.stop_path())
    env.setdefault(session.ENV_DATA, str(session.data_dir()))
    # Children see the full skip set, so a recorder started anyway (or pop_up_screen's
    # file inventory) knows which streams were never meant to exist this session.
    env[session.ENV_SKIP] = ",".join(sorted(SKIP))
    return env


def start_recorders(sid: str, dry_run: bool) -> list:
    """Launch every recorder with the session id in its environment."""
    env = child_env(sid)
    started = []
    if SKIP:
        why = "lite tier + PLAYSMART_SKIP" if os.environ.get(session.ENV_TIER_RESOLVED) == "lite" else "PLAYSMART_SKIP"
        print(f"  skipping ({why}): {', '.join(sorted(SKIP))}")
    for label, script, delay in active_recorders():
        if delay:
            time.sleep(delay)
        if dry_run:
            cmd = [sys.executable, str(SRC / "_stub_recorder.py"), label]
        else:
            # The interpreter running this orchestrator is the project's environment
            # (poetry's venv, or the packaged rig's bundled Python). Launching children
            # with it directly saves ~2 s of `poetry run` startup per recorder and
            # removes the dependency on poetry being on PATH (packaged rig, 2026-10-27).
            cmd = [sys.executable, str(REPO / script)]
        # 2026-11-10: each recorder at the priority that matches what it is to the game
        # (session.RECORDER_PRIORITY). Explicit even when "normal", so that a rig started
        # by a below-normal parent does not hand that class down to the input hook.
        prio = session.RECORDER_PRIORITY.get(label.split("/")[0].lower(), "normal")
        try:
            proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, creationflags=session.priority_flags(prio))
        except OSError as exc:
            print(f"  !! could not start {label}: {exc}")
            continue
        started.append((label, proc))
        print(f"  started {label}" + (f"  (priority: {prio} normal)" if prio in ("above", "below") else ""))
    return started


def check_started(procs: list, settle: float = 3.0) -> list:
    """Report recorders that died during startup.

    A recorder that exits immediately - no eye tracker, no EDA ring, a missing
    dependency - used to be indistinguishable from one that ran fine, because the
    only sign was a single line scrolling past while four other processes started.
    You would then play a whole game and discover the stream missing afterwards.

    Args:
        procs: (label, process) pairs from start_recorders.
        settle: Seconds to let each recorder get past its own startup.

    Returns:
        The (label, process) pairs that are already dead.
    """
    time.sleep(settle)
    dead = [(label, proc) for label, proc in procs if proc.poll() is not None]
    if dead:
        print("\n" + "!" * 68)
        for label, proc in dead:
            print(f"!!  {label} exited immediately (code {proc.returncode}) - NOT recording")
        key = session.abort_key()
        print("!!  Fix this and restart the capture." + (f" {key.upper()} aborts." if key else ""))
        print("!" * 68 + "\n", flush=True)
    return dead


def stop_recorders(procs: list, sid: str, dry_run: bool = False) -> None:
    """Ask everything to stop, then wait for it — terminate only if it hangs.

    This ordering is the whole point: the recorders that buffer their output
    need to reach their own save path.

    Args:
        procs: (label, process) pairs from start_recorders.
        sid: Session id, used to confirm each recorder actually wrote something.
        dry_run: Look under the stub directories when confirming.
    """
    session.request_stop()
    print("Stop requested; waiting for recorders to save…")

    deadline = time.time() + STOP_TIMEOUT_S
    for label, proc in procs:
        remaining = max(1.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
            if proc.returncode != 0:
                print(f"  !! {label} exited with code {proc.returncode} - "
                      f"its output is probably missing or incomplete")
            elif not any(path.stat().st_size for path in stream_files(label, sid, dry_run)):
                # A clean exit is not the same as a saved file. The EDA recorder
                # exits 0 when it never found the ring, and reporting that as
                # "saved and exited" is exactly the reassuring-but-false message
                # that let the original pipeline lose data unnoticed.
                print(f"  !! {label} exited cleanly but wrote nothing for this session")
            else:
                print(f"  {label} saved and exited")
        except subprocess.TimeoutExpired:
            print(f"  !! {label} did not exit within {STOP_TIMEOUT_S}s — terminating "
                  f"(its output may be incomplete)")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    session.clear_stop()


def verify_outputs(sid: str, dry_run: bool = False, video: bool = False, game_audio: bool = False) -> bool:
    """Report which streams produced a file for this session.

    The orchestrator cannot know which recorders were meant to run - OpenFace and
    audio are optional, the EDA ring may be absent - so this reports rather than
    judges. The point is that a missing or empty stream is visible at the end of
    the capture instead of a week later.

    Args:
        sid: Session id just recorded.
        dry_run: Look for the stub recorders' output instead of the real streams.
        video: Expect an OBS recording too. Only true when OBS actually started,
            so a machine without OBS does not report a missing stream every run.
        game_audio: Expect the game sound as its own WAV (video recorded and an
            ffmpeg available to copy the track out).

    Returns:
        True if every stream that produced a file produced a non-empty one.
    """
    print("Session output:")
    ok = True
    expected = [label for label, _script, _delay in active_recorders()]
    if not dry_run:
        # The orchestrator writes game state itself, so it is only expected when
        # the Live Client poller actually ran.
        expected.append("gamestate")
        if video:
            expected.append("video")
        if game_audio:
            expected.append("gameaudio")
    for stream in expected:
        name = f"stub-{stream.replace('/', '-')}" if dry_run else stream.split("/")[0]
        files = stream_files(stream, sid, dry_run)
        if not files:
            print(f"  {name:<22} MISSING - no file for this session")
            ok = False
            continue
        for path in files:
            size = path.stat().st_size
            if size == 0:
                print(f"  {name:<22} EMPTY   {path.name}")
                ok = False
            else:
                print(f"  {name:<22} ok      {path.name}  ({size / 1e6:.2f} MB)")
    if not ok:
        print("  ^ check the streams above before treating this session as usable.")
    return ok


def report_meta(sid: str) -> None:
    """Say, at the end of the session, what the game-state meta recorded about it.

    A late start or a missing minimap rectangle is only worth knowing while the
    players are still in the room; it was invisible in the console scroll before.
    """
    import json
    meta_file = session.stream_dir("gamestate") / f"{sid}_meta.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    late = meta.get("late_start_s")
    if late is not None and late > 0.5:
        print(f"  !! armed {late:.1f} s into the game - that much of every stream is missing")
    rect = meta.get("minimap_rect_px") or {}
    if not rect.get("x1"):
        print("  !! no minimap rectangle in the meta - run gaze_validation.py --rect-only")
    elif "estimated" in str(rect.get("source", "")):
        print("  minimap rectangle estimated from MinimapScale (measure it once for a better one)")
    if meta.get("game_id"):
        print(f"  game {meta.get('platform_id') or '?'}_{meta['game_id']} - download THIS replay "
              "from the client tonight; it only plays on this patch")
    else:
        print("  !! no game id in the meta - the replay must be matched by roster")


def _rows_estimate(path: Path, cache: dict) -> int:
    """Rows in a growing CSV from its size and the average length of its first lines.

    Counting lines of a 50 MB input log every two seconds would cost more than the
    capture; size / mean line length is within a few percent and free.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if path not in cache:
        try:
            with open(path, "rb") as fh:
                head = [fh.readline() for _ in range(201)]
            lines = [ln for ln in head[1:] if ln]
            cache[path] = (sum(len(ln) for ln in lines) / len(lines)) if lines else 0
            cache["hdr_" + str(path)] = len(head[0]) if head else 0
        except OSError:
            return 0
    avg = cache.get(path) or 0
    return int(max(0, size - cache.get("hdr_" + str(path), 0)) / avg) if avg else 0


def status_ticker(sid: str, obs_handle, tier: str, stop: threading.Event, every: float = 2.0,
                  display=None) -> None:
    """While a game is recorded, publish what each stream has captured so far.

    Written to rig_status.json as ``counters`` (added 2026-10-28 for the app's in-game
    screen): video seconds since OBS's frame zero, input events, game-state polls, audio
    seconds (WAV bytes at 44.1 kHz mono 16-bit), gaze samples on a full rig. Streams that
    this rig does not record are listed as ``null`` so the app can say so.
    """
    cache: dict = {}
    data = session.data_dir()
    skips = session.skip_set(tier)
    started_ms = time.time() * 1000
    while not stop.is_set():
        c = {
            "video_s": None, "input_events": None, "gamestate_rows": None, "audio_s": None, "gaze_rows": None,
            "face_rows": None, "gaze_valid_recent": None, "gaze_valid_total": None, "gaze_head_dist_mm": None,
            "gaze_stale_s": None, "elapsed_s": None,
            "face_success_recent": None, "face_fps": None, "face_stale_s": None, "face_running": None,
            "display": None,
        }
        if display is not None:
            c["display"] = display.live()
        if started_ms:
            c["elapsed_s"] = max(0, int(time.time() - started_ms / 1000))
        if obs_handle and obs_handle.get("t0_unix_ms"):
            c["video_s"] = max(0, int(time.time() - obs_handle["t0_unix_ms"] / 1000))
        if "input" not in skips:
            c["input_events"] = _rows_estimate(data / "input" / f"{sid}_input.csv", cache)
        c["gamestate_rows"] = _rows_estimate(data / "gamestate" / f"{sid}_gamestate.csv", cache)
        if "audio" not in skips:
            try:
                c["audio_s"] = int(max(0, (data / "audio" / f"{sid}_audio.wav").stat().st_size - 44) / (44100 * 2))
            except OSError:
                c["audio_s"] = 0
        if "gaze" not in skips:
            # the gaze recorder buffers and writes at the end; it publishes its progress instead
            try:
                import json as _json
                prog = _json.loads((data / "gaze" / f"{sid}_gaze.progress.json").read_text(encoding="utf-8"))
                c["gaze_rows"] = int(prog.get("samples") or 0)
                c["gaze_valid_recent"] = prog.get("valid_share_recent")
                c["gaze_valid_total"] = prog.get("valid_share_total")
                c["gaze_head_dist_mm"] = prog.get("head_dist_mm")
                c["gaze_stale_s"] = round(max(0.0, time.time() - (prog.get("updated_ms") or 0) / 1000.0), 1)
            except (OSError, ValueError):
                c["gaze_rows"] = _rows_estimate(data / "gaze" / f"{sid}_gaze.csv", cache)
        if "face" not in skips:
            # OpenFace writes to a scratch folder until the session ends; the face recorder
            # publishes its progress instead (frames, share with a face found, fps).
            c["face_rows"] = 0
            try:
                import json as _json
                prog = _json.loads((data / "face" / f"{sid}_face.progress.json").read_text(encoding="utf-8"))
                c["face_rows"] = int(prog.get("rows") or 0)
                c["face_success_recent"] = prog.get("success_recent")
                c["face_fps"] = prog.get("fps")
                c["face_running"] = bool(prog.get("running"))
                c["face_stale_s"] = round(max(0.0, time.time() - (prog.get("updated_ms") or 0) / 1000.0), 1)
            except (OSError, ValueError):
                c["face_rows"] = _rows_estimate(REPO / "output" / sid / f"{sid}_face.csv", cache)
        session.write_status(counters=c)
        stop.wait(every)


def wait_for_abort(abort: threading.Event, stop_flag: threading.Event) -> None:
    """Set `abort` if the abort key (F12 unless PLAYSMART_ABORT_KEY says otherwise) is
    pressed while a capture is running.

    Modified 2026-11-10: the key is read with GetAsyncKeyState (session.key_down) instead
    of the ``keyboard`` package, whose first call installs a low-level keyboard hook in
    this process for good. With PLAYSMART_ABORT_KEY=off (what the desktop app sets: F12 is
    League's screenshot key) nothing is watched and only the end of the game, or quitting
    the app, ends a capture.
    """
    key = session.abort_key()
    if not key:
        return
    session.key_down(key)           # forget a press from before this capture
    while not stop_flag.is_set():
        if session.key_down(key):
            print(f"{key.upper()} pressed — aborting this capture.")
            abort.set()
            return
        time.sleep(0.1)


def wait_for_key(name: str) -> bool:
    """Block until a function key is pressed, without a hook. False where keys cannot be read."""
    if sys.platform != "win32":
        return False
    session.key_down(name)          # clear a press from before we started waiting
    while not session.key_down(name):
        time.sleep(0.05)
    return True


def run_capture(dry_run: bool, dry_seconds: float) -> bool:
    """One armed cycle: wait for a game, record it, stop cleanly. False if aborted."""
    abort = threading.Event()

    manual = session.game() != "league"
    if dry_run:
        print("[dry-run] pretending a game just started")
        first = None
    elif manual:
        # Valorant and other titles have no local game-state API: the operator
        # brackets the session - F7 started it, F12 ends it - exactly as before
        # iteration 5, but with the graceful stop, so files are complete.
        if sys.platform != "win32":
            print(f"!!  PLAYSMART_GAME={session.game()} ends its session on a key press, which is only read on Windows.")
            return False
        print(f"PLAYSMART_GAME={session.game()}: recording now; press {(session.abort_key() or 'f12').upper()} when the game is over.")
        first = None
        threading.Thread(target=wait_for_abort, args=(abort, abort), daemon=True).start()
    else:
        print("Armed — waiting for a game to start…")
        session.write_status(state="armed", session_id="")
        watcher = threading.Thread(target=wait_for_abort, args=(abort, abort), daemon=True)
        watcher.start()
        first = liveclient.wait_for_game(abort=abort)
        if first is None:
            print("Aborted before a game started.")
            return False

    sid = session.new_session_id()
    session.adopt(sid)  # parent and children now agree on the stop-file path
    # Switches the desktop app may have changed since the last game (the face stream).
    for key, value in session.refresh_env((session.ENV_FACE, session.ENV_SKIP, "PLAYSMART_OPENFACE_EXE")).items():
        print(f"  .env changed: {key}={value if value is not None else '(removed)'}")
    tier = resolve_tier()
    session.write_status(state="recording", session_id=sid, tier=tier)
    print(f"\n=== SESSION {sid} ===")
    if tier == "lite":
        print("  tier: lite (no eye tracker) - video, input, audio and game state")
    else:
        print("  tier: full (eye tracker present)")
    if "face" in SKIP:
        why = ("switched off (PLAYSMART_FACE is not 1)" if not session.face_wanted()
               else "OpenFace not found on this PC" if session.openface_exe() is None
               else "in PLAYSMART_SKIP")
        print(f"  face: not recorded - {why}")
    else:
        print("  face: ON - OpenFace action units from the webcam (no images kept)")

    session.clear_stop()
    display = None
    if not dry_run:
        kill_openface()
        # Screen and game-window geometry from the first second of the session on.
        try:
            import display_watch
            display = display_watch.Watcher(sid).start()
        except Exception as exc:  # noqa: BLE001 - geometry must never stop a capture
            print(f"  display geometry not recorded ({exc})")
            display = None

    procs = start_recorders(sid, dry_run)
    if not procs:
        print("No recorders started; nothing to do.")
        return False

    check_started(procs)

    # Video is started after the other recorders so that a slow OBS handshake
    # does not delay them, and stopped first so the muxer has the longest
    # possible head start on finalising the container.
    obs_handle = None if dry_run else obs_recorder.start(sid)

    game_over = threading.Event()

    if dry_run:
        threading.Timer(dry_seconds, game_over.set).start()
        print(f"[dry-run] ending the fake game in {dry_seconds:.0f}s")
    elif manual:
        pass  # no game-state stream; F12 (abort) is the normal end of the session
    else:
        out_dir = session.stream_dir("gamestate")
        threading.Thread(
            target=liveclient.record_game,
            args=(sid, out_dir),
            kwargs=dict(first=first, stop=abort, on_end=game_over),
            daemon=True,
        ).start()

    # Block until the game ends or the operator aborts; meanwhile publish the counters.
    tick_stop = threading.Event()
    threading.Thread(target=status_ticker, args=(sid, obs_handle, tier, tick_stop),
                     kwargs=dict(display=display), daemon=True).start()
    while not game_over.is_set() and not abort.is_set():
        time.sleep(0.2)
    tick_stop.set()

    if manual and abort.is_set():
        print("Abort key — session ended.")
    else:
        print("Game over." if game_over.is_set() else "Aborted.")
    obs_recorder.stop(sid, obs_handle)
    stop_recorders(procs, sid, dry_run)
    game_audio = False
    if obs_handle is not None:
        # Game sound as its own file, next to the microphone WAV and the video.
        try:
            game_audio = obs_recorder.extract_game_audio(sid) is not None
        except Exception as exc:  # noqa: BLE001
            print(f"  !! game audio not split out ({exc})")
    verify_outputs(sid, dry_run, video=obs_handle is not None, game_audio=game_audio)
    if display is not None:
        try:
            import display_watch
            display_watch.report(display.stop())
        except Exception as exc:  # noqa: BLE001
            print(f"  display geometry not saved ({exc})")
    if not dry_run and not manual:
        report_meta(sid)

    session.write_status(state="finishing", session_id=sid)
    if not dry_run:
        # Post-game stats, manifest, sessions.csv and the upload. PLAYSMART_POPUP=0 only
        # drops the self-label dialog inside pop_up_screen (the desktop app asks in its UI).
        print("Post-game: stats, manifest, index, upload…")
        # Below normal: the player may already be in the next champion select or game.
        result = subprocess.run([sys.executable, str(SRC / "pop_up_screen.py")],
                                cwd=str(REPO), env=child_env(sid), creationflags=session.priority_flags("below"),
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0:
            print("Post-game step finished.")
        else:
            # Say why: under the desktop app the child's own error output reached no log, and
            # "failed" alone hid a missing module on every installed PC for weeks.
            print(f"Post-game step failed (exit code {result.returncode}): no stats, manifest, index or upload for {sid}.")
            for line in (result.stderr or "").strip().splitlines()[-12:]:
                print(f"    {line}")

    print(f"=== SESSION {sid} complete ===\n")
    session.write_status(state="idle", last_session_id=sid, session_id="")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="stub recorders and a fake game: no League, no hardware")
    ap.add_argument("--dry-seconds", type=float, default=10.0,
                    help="length of the fake game in --dry-run (default 10)")
    ap.add_argument("--once", action="store_true", help="one capture then exit")
    ap.add_argument("--no-arm", action="store_true",
                    help="skip waiting for F7 and arm immediately")
    args = ap.parse_args()

    if args.dry_run:
        args.no_arm = True
        args.once = True   # a fake game is a smoke test, not a loop

    rearm = os.environ.get("PLAYSMART_REARM", "1") != "0"
    first = True
    tier = resolve_tier()
    session.write_status(state="starting", tier=tier, tracker=tier == "full", session_id="")
    if tier == "lite":
        print("Capture tier: LITE - no eye tracker found (or PLAYSMART_TIER=lite). Recording video, "
              "input, audio and game state. Plug a Tobii in and it is picked up at the next game.")
    else:
        print("Capture tier: FULL - eye tracker present. Calibrate the player before each session.")
    while True:
        if not args.no_arm and (first or not rearm):
            key = session.abort_key()
            print("Press F7 to arm" + (f" ({key.upper()} aborts once a capture is running)…" if key else "…"))
            if not wait_for_key("f7"):
                print("Keys cannot be read here; arming immediately.")
                args.no_arm = True

        run_capture(args.dry_run, args.dry_seconds)
        first = False

        if args.once:
            return
        if rearm and not args.no_arm:
            print("Re-armed automatically - the next game is recorded from its loading screen. "
                  "Ctrl+C to stop; PLAYSMART_REARM=0 brings back F7 per game.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        session.write_status(state="stopped", session_id="")
