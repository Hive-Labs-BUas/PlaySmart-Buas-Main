"""Acceptance test for the OBS timing anchor: does the video sit on the game clock?

`obs_recorder.py` writes `<sid>_video.json` with `t0_unix_ms`, the wall-clock
time of the video's first frame. The gamestate stream carries both wall clock
and game clock per row. Put together, any position in the video file maps to a
game time - and League draws that game time in the top-right HUD, so the
mapping can be checked by eye against the footage.

    python src/check_video_sync.py                 # latest session, 3 checkpoints
    python src/check_video_sync.py <sid>           # a specific session
    python src/check_video_sync.py <sid> --at 4:37 # one position in the video

For each checkpoint: seek the video to the printed file position, read the HUD
clock, compare. The HUD clock has one-second resolution, so agreement within
about a second is a pass; a consistent offset of several seconds means t0 is
wrong; an offset that grows through the game means dropped frames or a
variable-frame-rate recording.

Exit code is non-zero if the video cannot be placed on the game clock at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session as playsmart_session  # noqa: E402
from check_session import find_latest  # noqa: E402


def parse_offset(text: str) -> float:
    """'4:37' or '277' or '1:04:37' -> seconds."""
    parts = [float(p) for p in text.split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def mmss(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes:02d}:{secs:02d}"


def _local(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S.%f")[:-3]


def game_time_at(state: pd.DataFrame, wall_ms: float) -> float | None:
    """Interpolate the game clock at one wall-clock instant, within a segment only."""
    wall = state.unix_time_ms.to_numpy()
    game = state.game_time_s.to_numpy()
    if wall_ms < wall[0] or wall_ms > wall[-1]:
        return None
    idx = int(wall.searchsorted(wall_ms, side="right")) - 1
    idx = min(max(idx, 0), len(wall) - 2)
    w0, w1, g0, g1 = wall[idx], wall[idx + 1], game[idx], game[idx + 1]
    # A restart, a Practice Tool time skip or a long poll gap sits between these
    # two rows: do not interpolate across it, snap to the nearer side instead.
    if g1 < g0 or abs((g1 - g0) - (w1 - w0) / 1000.0) > 1.5:
        return float(g0 if wall_ms - w0 < w1 - wall_ms else g1)
    if w1 == w0:
        return float(g0)
    return float(g0 + (g1 - g0) * (wall_ms - w0) / (w1 - w0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sid", nargs="?", help="session id (default: newest)")
    ap.add_argument("--at", action="append", default=[],
                    help="position in the video file, e.g. 4:37 (repeatable)")
    args = ap.parse_args()

    data_dir = playsmart_session.data_dir()
    sid = args.sid or find_latest(data_dir)
    if not sid:
        print(f"No sessions found under {data_dir}/gamestate")
        raise SystemExit(1)

    print(f"\nSession {sid}\n" + "=" * 72)
    meta_path = data_dir / "video" / f"{sid}_video.json"
    if not meta_path.exists():
        print(f" BAD   no {meta_path.name} - OBS did not run through obs_recorder for this session")
        raise SystemExit(1)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    t0 = meta.get("t0_unix_ms")
    video = data_dir / "video" / meta.get("video_file", "")
    print(f"  video  {video.name}  ({video.stat().st_size / 1e6:.1f} MB)" if video.exists()
          else f" BAD   video file {meta.get('video_file')!r} is not in {video.parent}")
    if t0 is None:
        print(" BAD   t0_unix_ms is null - OBS never reported a duration, video is unaligned")
        raise SystemExit(1)
    print(f"  frame zero at {_local(t0)} local time")

    state_path = data_dir / "gamestate" / f"{sid}_gamestate.csv"
    if not state_path.exists():
        print(" BAD   no gamestate CSV - nothing to align the video against")
        raise SystemExit(1)
    state = pd.read_csv(state_path).sort_values("unix_time_ms").reset_index(drop=True)
    g_start, g_end = float(state.unix_time_ms.iloc[0]), float(state.unix_time_ms.iloc[-1])
    lead = (g_start - t0) / 1000.0
    flag = "  ok  " if -60 < lead < 60 else " warn "
    print(f"{flag} video starts {abs(lead):.1f}s {'before' if lead > 0 else 'after'} the first "
          f"game-state poll (expected: a few seconds before)")
    print(f"       game window {_local(g_start)} -> {_local(g_end)}  "
          f"= video {mmss((g_start - t0) / 1000)} -> {mmss((g_end - t0) / 1000)}")

    offsets = [parse_offset(a) for a in args.at]
    if not offsets:
        span = g_end - g_start
        offsets = [((g_start - t0) + span * f) / 1000.0 for f in (0.25, 0.5, 0.75)]

    print("\n  seek the video to     HUD clock should read")
    print("  " + "-" * 44)
    ok = True
    for off in offsets:
        game = game_time_at(state, t0 + off * 1000)
        if game is None:
            print(f"  {mmss(off):>14}       (outside the game window)")
            continue
        print(f"  {mmss(off):>14}       {mmss(game)}")
    print("\n  Within ~1 s: pass.  Constant offset: t0 wrong.  Growing offset: frame drops / VFR.")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
