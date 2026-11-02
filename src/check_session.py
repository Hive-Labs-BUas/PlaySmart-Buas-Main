"""Inspect one captured session and report whether it is actually usable.

`key_listener.py` tells you a file exists. This tells you whether what is inside
it is worth keeping - which is a different question, and the one the previous
archive got wrong for a year.

    python src/check_session.py                # the most recent session
    python src/check_session.py 20260914-1432-ab12cd

Checks, in the order they matter:

* **Clock mapping.** unix_time_ms against game_time_s should be a straight line
  with slope 1. That mapping is what lets a gaze sample be placed in the game.
* **Coverage.** Every stream should span the whole game, not the first two minutes.
* **Gaze validity.** The share of samples where the tracker actually had the eyes.
  Anything below ~85% means calibration or seating, not software.
* **Face.** OpenFace tracked a face on most frames, at a usable rate, and its blink
  signal (AU45) agrees with the eyelid aperture derived from the eye landmarks.
  (Legacy *emotion* files: if ~95% Neutral the classifier contributed nothing.)
* **Input.** APM in a plausible range confirms the stream is real.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session as playsmart_session

OK, WARN, BAD = "  ok  ", " warn ", " BAD  "


def _local(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def _load(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_csv(path, low_memory=False)
    except Exception as exc:
        print(f"      could not read {path.name}: {exc}")
        return None


# A step this large between two consecutive polls is a real discontinuity, not
# jitter: the poller runs at 2 Hz, so anything under a second is scheduling noise.
CLOCK_BREAK_S = 1.5
# Consecutive polls further apart than this leave a window in which nothing can
# be placed on the game clock with confidence.
MAX_POLL_GAP_S = 5.0
BLIND_GAP_S = 30.0
# Outside the breaks above, the two clocks should advance together this closely.
CLOCK_JITTER_MS = 250


def _report_clock(state: pd.DataFrame) -> bool:
    """Check that the game clock can be mapped onto wall time.

    Fitting a single slope was the wrong model, and it failed on a good session:
    Practice Tool's "+30 seconds" button steps the game clock forward, and a
    straight line drawn through a step reports a badly broken slope on a capture
    that is entirely usable. A check that cries wolf is a check that gets ignored,
    which is the failure this whole tool exists to prevent.

    What actually matters is narrower than a slope. Every row carries both clocks
    read at the same instant, so placing a gaze sample is interpolation between
    two recorded pairs — which stays correct no matter how oddly the game clock
    behaves in between. Only two things genuinely break it:

    * **The clock running backwards.** The game restarted, so one session id now
      covers two games and a given game time maps to two different moments.
      Nothing downstream can resolve that, so it fails.
    * **A long gap between polls.** Nothing was recorded across it, so anything
      falling inside is placed by assumption rather than measurement.

    Forward skips and a frozen clock are reported, because they tell the analyst
    where not to interpolate across, but neither makes the session unusable.

    Args:
        state: The gamestate frame, with unix_time_ms and game_time_s.

    Returns:
        True if the session can still be aligned to the game clock.
    """
    wall = (state.unix_time_ms.diff() / 1000.0).iloc[1:]
    step = state.game_time_s.diff().iloc[1:]
    delta = step - wall

    backward = step[step < -CLOCK_BREAK_S]
    forward = delta[(delta > CLOCK_BREAK_S) & (step >= -CLOCK_BREAK_S)]
    frozen = delta[(delta < -CLOCK_BREAK_S) & (step >= -CLOCK_BREAK_S)]
    gaps = wall[wall > MAX_POLL_GAP_S]

    flagged = backward.index.union(forward.index).union(frozen.index)
    jitter = float(delta.drop(flagged).abs().max() * 1000) if len(delta) > len(flagged) else 0.0
    worst_gap = float(wall.max()) if len(wall) else 0.0

    fatal = bool(len(backward)) or worst_gap > BLIND_GAP_S
    shaky = bool(len(gaps)) or jitter > CLOCK_JITTER_MS
    flag = BAD if fatal else (WARN if (shaky or len(forward) or len(frozen)) else OK)

    segments = len(backward) + len(forward) + len(frozen) + 1
    detail = f"{segments} segment{'s' if segments > 1 else ''}, "
    detail += f"clocks advance together to {jitter:.0f} ms, "
    detail += f"largest gap between polls {worst_gap:.1f}s"
    print(f"{flag} clock mapping: {detail}")

    for index, moved in backward.items():
        print(f"      {_local(state.unix_time_ms.loc[index])}  game clock went backward "
              f"{moved:.1f}s - the game restarted, so this session id covers two games")
    for index, jump in forward.items():
        print(f"      {_local(state.unix_time_ms.loc[index])}  game clock jumped forward "
              f"{jump:+.1f}s - Practice Tool time skip")
    for index, stall in frozen.items():
        print(f"      {_local(state.unix_time_ms.loc[index])}  game clock stood still for "
              f"{-stall:.1f}s of real time - paused, or the client stalled")
    for index, gap in gaps.items():
        print(f"      {_local(state.unix_time_ms.loc[index])}  {gap:.1f}s with no poll - "
              f"samples in that window cannot be placed precisely")

    if len(backward):
        print("      split this capture by hand before using it, or discard it")
    elif fatal:
        print("      too much of the game window has no game state to align against")
    elif flag is not OK:
        print("      usable: interpolate inside each segment, never across a break")
    return not fatal


def find_latest(data_dir: Path) -> str | None:
    """Return the newest session id: by meta under data/gamestate, else by any stream file."""
    gamestate = data_dir / "gamestate"
    metas = sorted(gamestate.glob("*_meta.json"), key=lambda p: p.stat().st_mtime) if gamestate.is_dir() else []
    if metas:
        return metas[-1].name.replace("_meta.json", "")
    # manual-bracket games (PLAYSMART_GAME != league) write no meta: newest stream file instead
    files = [p for d in ("gaze", "input", "audio", "video") if (data_dir / d).is_dir()
             for p in (data_dir / d).iterdir() if p.is_file() and "_" in p.name]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.name.rsplit("_", 1)[0]


def report(sid: str, data_dir: Path) -> bool:
    """Print the report for one session. Returns False if anything looks wrong."""
    good = True
    print(f"\nSession {sid}\n" + "=" * 72)

    meta_path = data_dir / "gamestate" / f"{sid}_meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        bots = " WITH BOTS" if meta.get("has_bots") else ""
        print(f"  {meta.get('game_mode', '?')} on {meta.get('map_name', '?')} "
              f"({meta.get('map_terrain', '?')}) as {meta.get('champion', '?')}{bots}")
        print(f"  player {meta.get('riot_id', '?')}  roster of {len(meta.get('roster', []))}")
        if meta.get("has_bots"):
            print(f"{WARN} bots in the lobby - this is not competitive gameplay")
        if meta.get("game_id"):
            print(f"{OK} game {meta.get('platform_id') or '?'}_{meta['game_id']} on client "
                  f"{meta.get('client_game_version') or '?'} - the replay can be found by id")
        elif meta.get("game_mode") == "PRACTICETOOL":
            print(f"{WARN} Practice Tool - no replay exists, so no positions for this session")
        else:
            print(f"{WARN} no game id ({meta.get('identity_source', 'older recorder')}) - "
                  "the post-game pipeline will match the replay by roster instead")
        # 25 Sep 2026: the context the client added, and the per-PC measurements.
        q = meta.get("queue_type") or meta.get("queue_game_mode") or ""
        ranked = meta.get("ranked") or {}
        solo = ranked.get("RANKED_SOLO_5x5") or {}
        tier = f"{solo.get('tier', '')} {solo.get('division', '')}".strip()
        extras = []
        if q:
            extras.append(f"queue {q}" + (" (custom)" if meta.get("is_custom") else ""))
        if tier:
            extras.append(f"solo tier {tier}")
        if meta.get("skin_name"):
            extras.append(f"skin {meta['skin_name']}")
        if extras:
            print("  " + ", ".join(extras))
        gs = (meta.get("game_settings") or {}).get("General") or {}
        hud = (meta.get("game_settings") or {}).get("HUD") or {}
        if gs or hud:
            print(f"      client settings: {gs.get('Width', '?')}x{gs.get('Height', '?')}, "
                  f"minimap scale {hud.get('MinimapScale', '?')}, HUD scale {hud.get('GlobalScale', '?')}")
        else:
            print(f"{WARN} no client settings in the meta (League client not reachable at start?)")
        if meta.get("minimap_rect_px"):
            r = meta["minimap_rect_px"]
            print(f"{OK} minimap rectangle measured: ({r.get('x0')},{r.get('y0')})-({r.get('x1')},{r.get('y1')})")
        else:
            print(f"{WARN} minimap rectangle not measured - run src/gaze_validation.py once on this PC")
        gv = meta.get("gaze_validation") or {}
        if gv.get("accuracy_px") is not None:
            deg = f" ({gv['accuracy_deg']} deg)" if gv.get("accuracy_deg") is not None else ""
            flag = OK if gv["accuracy_px"] <= 60 else WARN
            print(f"{flag} gaze accuracy {gv['accuracy_px']} px{deg} on {gv.get('when', '?')}")
        else:
            print(f"{WARN} no gaze validation on record for this PC - run src/gaze_validation.py")
        for name, label in (("eog", "post-game stats"), ("labels", "player's self-labels"),
                            ("manifest", "file manifest")):
            present = (data_dir / "gamestate" / f"{sid}_{name}.json").exists()
            print(f"{OK if present else WARN} {label}: {'present' if present else 'missing'}")
    else:
        print(f"{BAD} no {sid}_meta.json - the game client API was not reachable")
        good = False

    # --- clock mapping -----------------------------------------------------
    game_start = game_end = None
    state_path = data_dir / "gamestate" / f"{sid}_gamestate.csv"
    if state_path.exists():
        state = _load(state_path)
        if state is not None and len(state) > 2:
            game_start, game_end = state.unix_time_ms.min(), state.unix_time_ms.max()
            span_s = (game_end - game_start) / 1000.0
            rate = len(state) / max(span_s, 1e-9)
            print(f"\n  game state   {len(state):>7,} rows   {rate:4.2f} Hz   "
                  f"{_local(game_start)} -> {_local(game_end)}   "
                  f"{span_s / 60:.1f} min")
            good &= _report_clock(state)
        else:
            print(f"{BAD} gamestate CSV is empty or unreadable")
            good = False
    elif playsmart_session.game() != "league":
        print(f"\n  game state   none (PLAYSMART_GAME={playsmart_session.game()}: manual bracket) - "
              "streams share the wall clock only; no in-game placement")
    else:
        print(f"\n{BAD} no gamestate CSV - no in-game clock, streams cannot be placed in the game")
        good = False

    events_path = data_dir / "gamestate" / f"{sid}_events.csv"
    if events_path.exists():
        events = _load(events_path)
        if events is not None and len(events):
            names = events.event_name.value_counts().to_dict()
            summary = ", ".join(f"{k} x{int(v)}" for k, v in names.items())
            print(f"  events       {len(events):>7,}   {summary}")
            if events.event_id.duplicated().any():
                print(f"{BAD} duplicate event ids - dedup is broken")
                good = False
        else:
            print(f"{WARN} events CSV is empty - no kills or objectives were recorded")

    audio_anchor = data_dir / "audio" / f"{sid}_audio.json"
    if (data_dir / "audio" / f"{sid}_audio.wav").exists():
        if audio_anchor.exists():
            anchor = json.loads(audio_anchor.read_text(encoding="utf-8"))
            lost = anchor.get("overflows") or 0
            flag = OK if anchor.get("t0_unix_ms") and not lost else WARN
            print(f"\n{flag} audio anchored: frame zero at "
                  f"{_local(anchor['t0_unix_ms']) if anchor.get('t0_unix_ms') else '?'}, "
                  f"{lost} overflow(s)")
        else:
            print(f"\n{WARN} audio has no timing anchor (<sid>_audio.json) - older recorder")

    # --- player-side streams ----------------------------------------------
    for stream in ("gaze", "input", "face", "emotion", "eda"):
        path = data_dir / stream / f"{sid}_{stream}.csv"
        if not path.exists():
            print(f"\n{WARN} {stream}: no file for this session")
            continue
        frame = _load(path)
        if frame is None or frame.empty or "unix_time" not in frame:
            print(f"\n{BAD} {stream}: empty or missing unix_time")
            good = False
            continue

        times = pd.to_numeric(frame.unix_time, errors="coerce").dropna()
        if times.max() < 1e12:
            times = times * 1000
        span_min = (times.max() - times.min()) / 6e4
        rate = len(frame) / max((times.max() - times.min()) / 1000, 1e-9)
        print(f"\n  {stream:<12} {len(frame):>7,} rows   {rate:5.1f} Hz   "
              f"{_local(times.min())} -> {_local(times.max())}   {span_min:.1f} min")

        if game_start is not None:
            overlap = min(times.max(), game_end) - max(times.min(), game_start)
            coverage = overlap / max(game_end - game_start, 1)
            flag = OK if coverage > 0.95 else (WARN if coverage > 0.5 else BAD)
            good &= flag is not BAD
            print(f"{flag} covers {coverage:6.1%} of the game window")

        if stream == "gaze":
            if "gaze_valid" in frame:
                valid = frame.gaze_valid.astype(str).str.lower().eq("true").mean()
                flag = OK if valid > 0.85 else (WARN if valid > 0.6 else BAD)
                good &= flag is not BAD
                print(f"{flag} {valid:6.1%} of samples had a real tracker fix")
            else:
                print(f"{WARN} no gaze_valid column - an older recorder wrote this file")
            res = frame.screen_resolution.dropna().unique() if "screen_resolution" in frame else []
            print(f"      resolution {list(res)}")
            if "screen_x" in frame and len(res) == 1 and "x" in str(res[0]):
                width, height = (int(v) for v in str(res[0]).lower().rstrip("p").split("x"))
                x = pd.to_numeric(frame.screen_x, errors="coerce")
                y = pd.to_numeric(frame.screen_y, errors="coerce")
                on = x.between(0, width) & y.between(0, height)
                mini = ((x >= width * 0.86) & (y >= height * 0.75) & on).sum() / max(on.sum(), 1)
                # Floor rather than round: 99.97% printing as "100.0% on screen"
                # is the kind of small lie that trains people to trust the report
                # less than it deserves.
                shown = math.floor(on.mean() * 1000) / 1000
                print(f"      {shown:6.1%} on screen, {mini:5.1%} of those in the minimap corner")
                if on.sum() and not 0.03 < mini < 0.30:
                    print(f"{WARN} minimap share is outside the plausible 3-30% band")

        if stream == "face":
            if "success" in frame:
                succ = pd.to_numeric(frame.success, errors="coerce").fillna(0).eq(1).mean()
                flag = OK if succ > 0.9 else (WARN if succ > 0.6 else BAD)
                good &= flag is not BAD
                conf = pd.to_numeric(frame.get("confidence"), errors="coerce").median() if "confidence" in frame else float("nan")
                print(f"{flag} face tracked on {succ:6.1%} of frames (median confidence {conf:.2f})")
            flag = OK if rate >= 15 else (WARN if rate >= 8 else BAD)
            print(f"{flag} {rate:.1f} frames/s from the webcam"
                  + ("" if rate >= 15 else "  (OpenFace starved of CPU? lower PLAYSMART_FACE_CAM)"))
            aus = [c for c in frame.columns if c.startswith("AU") and c.endswith("_r")]
            print(f"      {len(aus)} AU intensities, "
                  f"{sum(1 for c in frame.columns if c.startswith('pose_'))} pose, "
                  f"{sum(1 for c in frame.columns if c.startswith('gaze_'))} webcam-gaze columns")
            if "AU45_c" in frame:
                blink = pd.to_numeric(frame.AU45_c, errors="coerce").fillna(0)
                onsets = int(((blink == 1) & (blink.shift(fill_value=0) == 0)).sum())
                per_min = onsets / max(span_min, 1e-9)
                flag = OK if 3 <= per_min <= 40 else WARN
                print(f"{flag} {per_min:.1f} blink onsets/min from AU45")
                if "eye_aperture_0" in frame:
                    ap = pd.to_numeric(frame[["eye_aperture_0", "eye_aperture_1"]].mean(axis=1), errors="coerce")
                    ok_rows = ap.notna() & (blink.notna())
                    if ok_rows.sum() > 100 and blink[ok_rows].nunique() > 1:
                        corr = ap[ok_rows].corr(blink[ok_rows])
                        flag = OK if corr < -0.2 else WARN
                        print(f"{flag} eyelid aperture vs AU45 blink: r = {corr:+.2f}"
                              + ("" if corr < -0.2 else "  (expected negative; check the eye-landmark index layout)"))
            if "pose_Rx" in frame:
                rx = pd.to_numeric(frame.pose_Rx, errors="coerce")
                ry = pd.to_numeric(frame.pose_Ry, errors="coerce")
                print(f"      head pitch sd {rx.std():.2f} rad, yaw sd {ry.std():.2f} rad")

        if stream == "emotion" and "emotion" in frame:
            counts = frame.emotion.value_counts(dropna=False)
            top = counts.index[0]
            share = counts.iloc[0] / len(frame)
            print("      " + ", ".join(f"{k}: {int(v):,}" for k, v in counts.head(4).items()))
            flag = WARN if share > 0.9 else OK
            print(f"{flag} dominant label {top!r} is {share:.1%} of frames")

        if stream == "input" and "event_type" in frame:
            minutes = max(span_min, 1e-9)
            keys = frame.event_type.isin(["key_press", "keyboard", "key"]).sum()
            clicks = frame.event_type.isin(["mouse_click", "click"]).sum()
            apm = (keys + clicks) / minutes
            flag = OK if 40 < apm < 500 else WARN
            print(f"{flag} {apm:.0f} actions/min ({keys / minutes:.0f} keys, "
                  f"{clicks / minutes:.0f} clicks)")

    print("\n" + "=" * 72)
    print("Session looks usable." if good else "PROBLEMS ABOVE - do not treat this as a good session.")
    return good


def main() -> None:
    """Report on one session from the command line."""
    data_dir = playsmart_session.data_dir()
    sid = sys.argv[1] if len(sys.argv) > 1 else find_latest(data_dir)
    if not sid:
        print(f"No sessions found under {data_dir}/gamestate")
        raise SystemExit(1)
    raise SystemExit(0 if report(sid, data_dir) else 1)


if __name__ == "__main__":
    main()
