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

Keys: F7 arms, F12 aborts. Same as before.

Modified 2026-10-14: **re-arms itself after every game** (``PLAYSMART_REARM=0`` to
go back to pressing F7 each time). On 28 Sep 2026 one rig was armed late for game 2
and never armed for game 3; with auto re-arm the next game is caught from its loading
screen without anyone touching the keyboard. The end-of-session report now also
prints the game-state meta's warnings (late start, no minimap rectangle) so they are
read while the players are still in the room.

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
RECORDERS = [
    ("gaze", "src/eye_tracking_script.py", 0.0),
    ("emotion/overlay", "src/Emotion_gaze_visualization.py", 0.0),
    ("input", "src/keyboard_recording.py", 0.0),
    ("audio", "src/microphone_recording.py", 0.0),
    ("eda", "src/nuanic_eda.py", 2.0),
]

# Comma-separated labels in PLAYSMART_SKIP are neither started nor expected to
# produce a file — `set PLAYSMART_SKIP=eda` when the ring is not available.
# Without this, a missing sensor makes the end-of-session report show a red line
# on every single capture, and a check that is always red is a check nobody
# reads. That is precisely how the old pipeline hid a year of data loss.
SKIP = {part.strip().lower() for part in os.environ.get("PLAYSMART_SKIP", "").split(",")
        if part.strip()}


def active_recorders() -> list:
    """RECORDERS minus anything named in PLAYSMART_SKIP."""
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
    return env


def start_recorders(sid: str, dry_run: bool) -> list:
    """Launch every recorder with the session id in its environment."""
    env = child_env(sid)
    started = []
    if SKIP:
        print(f"  skipping (PLAYSMART_SKIP): {', '.join(sorted(SKIP))}")
    for label, script, delay in active_recorders():
        if delay:
            time.sleep(delay)
        if dry_run:
            cmd = [sys.executable, str(SRC / "_stub_recorder.py"), label]
        else:
            cmd = ["poetry", "run", "python", script]
        try:
            proc = subprocess.Popen(cmd, cwd=str(REPO), env=env)
        except OSError as exc:
            print(f"  !! could not start {label}: {exc}")
            continue
        started.append((label, proc))
        print(f"  started {label}")
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
        print("!!  Fix this and restart the capture. F12 aborts.")
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


def verify_outputs(sid: str, dry_run: bool = False, video: bool = False) -> bool:
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


def wait_for_abort(abort: threading.Event, stop_flag: threading.Event) -> None:
    """Set `abort` if F12 is pressed while a capture is running."""
    try:
        import keyboard
    except Exception:
        return
    while not stop_flag.is_set():
        try:
            if keyboard.is_pressed("f12"):
                print("F12 pressed — aborting this capture.")
                abort.set()
                return
        except Exception:
            return
        time.sleep(0.2)


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
        try:
            import keyboard  # noqa: F401 - F12 is the only way this session ends
        except Exception as exc:  # noqa: BLE001
            print(f"!!  PLAYSMART_GAME={session.game()} needs the `keyboard` package to see F12 ({exc}).")
            return False
        print(f"PLAYSMART_GAME={session.game()}: recording now; press F12 when the game is over.")
        first = None
        threading.Thread(target=wait_for_abort, args=(abort, abort), daemon=True).start()
    else:
        print("Armed — waiting for a game to start…")
        watcher = threading.Thread(target=wait_for_abort, args=(abort, abort), daemon=True)
        watcher.start()
        first = liveclient.wait_for_game(abort=abort)
        if first is None:
            print("Aborted before a game started.")
            return False

    sid = session.new_session_id()
    session.adopt(sid)  # parent and children now agree on the stop-file path
    print(f"\n=== SESSION {sid} ===")

    session.clear_stop()
    if not dry_run:
        kill_openface()

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

    # Block until the game ends or the operator aborts.
    while not game_over.is_set() and not abort.is_set():
        time.sleep(0.2)

    if manual and abort.is_set():
        print("F12 — session ended.")
    else:
        print("Game over." if game_over.is_set() else "Aborted.")
    obs_recorder.stop(sid, obs_handle)
    stop_recorders(procs, sid, dry_run)
    verify_outputs(sid, dry_run, video=obs_handle is not None)
    if not dry_run and not manual:
        report_meta(sid)

    if not dry_run:
        print("Labelling and uploading…")
        result = subprocess.run(["poetry", "run", "python", "src/pop_up_screen.py"],
                                cwd=str(REPO), env=child_env(sid))
        print("Upload step finished." if result.returncode == 0 else "Upload step failed.")

    print(f"=== SESSION {sid} complete ===\n")
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

    rearm = os.environ.get("PLAYSMART_REARM", "1") != "0"
    first = True
    while True:
        if not args.no_arm and (first or not rearm):
            print("Press F7 to arm (F12 aborts once a capture is running)…")
            try:
                import keyboard
                keyboard.wait("f7")
            except Exception as exc:
                print(f"Keyboard hooks unavailable ({exc}); arming immediately.")
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
