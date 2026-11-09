"""Champion positions from rendered replay minimap frames, by portrait template matching.

    python minimap_match.py <frames_dir> --icons icons --roster roster.json --start 540 --fps 2

roster.json: {"Nasus": "red", "Yorick": "blue", ...}  (team per champion, from the .rofl header)

For every frame and every champion on the roster, the Data Dragon portrait is scaled to
the minimap icon size, masked to a disc, and matched (normalised cross-correlation) over
the minimap crop. The best peak is the champion's position for that frame; its score is
reported so weak matches (icon hidden under another icon, champion dead) can be dropped.
Positions are converted from minimap pixels to game units assuming the minimap square
spans Summoner's Rift (-120,-120)..(14870,14980).

Added 2026-09-22 (iteration 5, replay spike).
"""
import argparse
import csv
import json
import sys
from pathlib import Path
import cv2
import numpy as np

MAP_MIN, MAP_MAX = (-120.0, -120.0), (14870.0, 14980.0)


def find_minimap(im):
    H, W = im.shape[:2]
    c = im[H-700:, W-700:]
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    terr = cv2.inRange(hsv, (20, 40, 120), (45, 120, 220))
    ys, xs = np.where(terr > 0)
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()
    side = max(x1 - x0, y1 - y0) + 36
    return (W-700+max(0, x1+18-side), H-700+max(0, y1+18-side), side)


def templates(icon_dir, roster, sizes):
    """{champion: [(size, template, mask), ...]} - portrait centre cropped to a disc."""
    out = {}
    for champ in roster:
        icon = cv2.imread(str(Path(icon_dir) / f"{champ}.png"))
        if icon is None:
            sys.exit(f"missing icon for {champ}")
        h = icon.shape[0]
        m = int(h * 0.10)   # the minimap shows the central ~80% of the square
        core = icon[m:h-m, m:h-m]
        out[champ] = []
        for d in sizes:
            t = cv2.resize(core, (d, d), interpolation=cv2.INTER_AREA)
            mask = np.zeros((d, d), np.uint8)
            cv2.circle(mask, (d//2, d//2), d//2 - 1, 255, -1)
            out[champ].append((d, t, mask))
    return out


def match(mm, tpls, moving=None):
    """Best (x, y, score, size) of a champion's templates over the minimap crop.

    `moving` is a boolean map of pixels that differ from the static background; peaks
    on static map furniture (turrets, base circles - red or blue like the icons) are
    excluded by requiring the template centre to sit on a moving pixel.
    """
    best = (0, 0, -1.0, 0, None)
    for d, t, mask in tpls:
        res = cv2.matchTemplate(mm, t, cv2.TM_CCOEFF_NORMED, mask=mask)
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        if moving is not None:
            h, w = res.shape
            res[~moving[d//2:d//2+h, d//2:d//2+w]] = -1.0
        _, val, _, loc = cv2.minMaxLoc(res)
        if val > best[2]:
            best = (loc[0] + d // 2, loc[1] + d // 2, float(val), d, res)
    return best


def resolve(cands, min_sep=8):
    """One champion per icon: assign by descending score; a champion whose best spot is
    already taken re-searches its response map outside the taken discs."""
    taken, out = [], {}
    for champ in sorted(cands, key=lambda c: -cands[c][2]):
        x, y, s, d, res = cands[champ]
        while any((x-tx)**2 + (y-ty)**2 < min_sep**2 for tx, ty in taken) and res is not None:
            for tx, ty in taken:
                cv2.circle(res, (tx - d//2, ty - d//2), min_sep, -1.0, -1)
            _, s, _, loc = cv2.minMaxLoc(res)
            x, y = loc[0] + d//2, loc[1] + d//2
            s = float(s)
        taken.append((x, y))
        out[champ] = (x, y, s, d)
    return out


def to_game(px, py, side):
    return (round(MAP_MIN[0] + px / side * (MAP_MAX[0] - MAP_MIN[0])),
            round(MAP_MAX[1] - py / side * (MAP_MAX[1] - MAP_MIN[1])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("--icons", default="icons")
    ap.add_argument("--roster", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--sizes", default="20,22,24,26")
    ap.add_argument("--min-score", type=float, default=0.45)
    ap.add_argument("--out", default="positions.csv")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    roster = json.loads(Path(a.roster).read_text())
    frames = sorted(Path(a.frames).glob("*.png"))
    x0, y0, side = find_minimap(cv2.imread(str(frames[0])))
    print(f"minimap at ({x0},{y0}) side {side}px, {(MAP_MAX[0]-MAP_MIN[0])/side:.1f} units/px; roster {list(roster)}")
    tpls = templates(a.icons, roster, [int(s) for s in a.sizes.split(",")])
    sample = [cv2.imread(str(f))[y0:y0+side, x0:x0+side] for f in frames[::max(1, len(frames)//25)]]
    background = np.median(np.stack(sample), axis=0).astype(np.uint8)
    rows = []
    dbg = Path(a.frames) / "debug_match"
    if a.debug:
        dbg.mkdir(exist_ok=True)
    for i, f in enumerate(frames):
        mm = cv2.imread(str(f))[y0:y0+side, x0:x0+side]
        t = a.start + i / a.fps
        out = mm.copy() if a.debug else None
        diff = np.abs(mm.astype(int) - background.astype(int)).sum(axis=2).astype(np.float32)
        moving = cv2.blur(diff, (9, 9)) > 20      # icon-sized neighbourhood differs from the static map
        line = []
        found = resolve({champ: match(mm, tpls[champ], moving) for champ in roster})
        for champ, team in roster.items():
            x, y, s, d = found[champ]
            gx, gy = to_game(x, y, side)
            rows.append({"game_time_s": round(t, 2), "frame": f.name, "champion": champ, "team": team,
                         "px": x, "py": y, "x": gx, "y": gy, "score": round(s, 3), "icon_px": d})
            line.append(f"{champ[:4]}{s:.2f}")
            if out is not None and s >= a.min_score:
                cv2.circle(out, (x, y), d // 2, (255, 120, 0) if team == "blue" else (0, 0, 255), 1)
                cv2.putText(out, champ[:5], (x - 12, y - d // 2 - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
        if out is not None:
            cv2.imwrite(str(dbg / f.name), cv2.resize(out, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST))
        print(f"{f.name} t={t:6.1f}  " + " ".join(line))
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {a.out}: {len(rows)} rows")


if __name__ == "__main__":
    main()
