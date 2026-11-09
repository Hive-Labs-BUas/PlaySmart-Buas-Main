"""Measure gaze accuracy and the minimap rectangle, once per player setup.

Added 2026-09-25 (iteration 5).

Two numbers the analysis needs and nobody had:

1. **Gaze accuracy.** After the Tobii calibration, the player looks at five dots for
   a second and a half each. The distance between where they looked and where the
   dot was is the accuracy (mean offset) and the precision (RMS scatter), in pixels
   and in degrees of visual angle (using the display size in mm from the tracker and
   the measured distance to the screen). "Was the player looking at the minimap" is
   only as good as this number.

2. **The minimap rectangle.** The merge assumed the bottom-right 14 % x 25 % of the
   screen. It depends on the resolution, the HUD scale and the minimap scale, so it
   is measured instead: the operator points the mouse at the minimap's top-left and
   bottom-right corners in the game and presses SPACE for each.

Run with League open on the loading screen or in game (for step 2) after the Tobii
calibration (for step 1). Either step can be skipped with ESC.

    poetry run python src/gaze_validation.py            # both steps
    poetry run python src/gaze_validation.py --no-rect  # accuracy only
    poetry run python src/gaze_validation.py --rect-only

Output:
    data/gaze/validation_<YYYYmmdd-HHMMSS>.json   accuracy per dot and overall
    data/json/minimap_rect.json                   {"x0","y0","x1","y1","screen_px",...}

``liveclient_recorder.py`` copies both into every session's ``_meta.json`` from then
on (``gaze_validation``, ``minimap_rect_px``), so each session says how accurate its
gaze was and where its minimap sat.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session  # noqa: E402

DOT_SECONDS = 1.5
SETTLE_SECONDS = 0.5
# Dot positions as screen fractions: centre and the four corners, well inside.
DOTS = [(0.5, 0.5), (0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)]
RECT_FILE = Path("data/json/minimap_rect.json")


def _screen():
    import screeninfo

    m = screeninfo.get_monitors()[0]
    return m.width, m.height


def validate_gaze(width: int, height: int) -> dict | None:
    """Five-dot validation with the Tobii; None when no tracker or aborted."""
    import pygame
    import tobii_research as tr

    trackers = tr.find_all_eyetrackers()
    if not trackers:
        print("No eye tracker found; skipping the accuracy check.")
        return None
    tracker = trackers[0]
    try:
        area = tracker.get_display_area()
        display_mm = (float(area.width), float(area.height))
    except Exception:
        display_mm = (None, None)

    samples: list = []

    def cb(d):
        samples.append((
            time.time(),
            d.get("left_gaze_point_on_display_area"), d.get("right_gaze_point_on_display_area"),
            d.get("left_gaze_point_validity"), d.get("right_gaze_point_validity"),
            d.get("left_gaze_origin_in_user_coordinate_system"),
            d.get("right_gaze_origin_in_user_coordinate_system"),
        ))

    pygame.init()
    window = pygame.display.set_mode((width, height), pygame.NOFRAME)
    pygame.display.set_caption("PlaySmart gaze validation")
    font = pygame.font.SysFont(None, 36)
    tracker.subscribe_to(tr.EYETRACKER_GAZE_DATA, cb, as_dictionary=True)
    per_dot = []
    aborted = False
    try:
        for i, (fx, fy) in enumerate(DOTS):
            cx, cy = int(fx * width), int(fy * height)
            t_show = time.time()
            while time.time() - t_show < SETTLE_SECONDS + DOT_SECONDS:
                for ev in pygame.event.get():
                    if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                        aborted = True
                if aborted:
                    break
                window.fill((20, 20, 20))
                pygame.draw.circle(window, (240, 240, 240), (cx, cy), 14)
                pygame.draw.circle(window, (20, 20, 20), (cx, cy), 4)
                msg = font.render(f"Look at the dot  ({i + 1}/{len(DOTS)})   ESC = skip", True, (160, 160, 160))
                window.blit(msg, (20, 20))
                pygame.display.flip()
                time.sleep(0.01)
            if aborted:
                break
            t0, t1 = t_show + SETTLE_SECONDS, time.time()
            xs, ys, dists = [], [], []
            for ts, lg, rg, lv, rv, lo, ro in samples:
                if not (t0 <= ts <= t1):
                    continue
                pts = [g for g, v in ((lg, lv), (rg, rv)) if g and v and None not in g]
                if not pts:
                    continue
                xs.append(sum(p[0] for p in pts) / len(pts) * width)
                ys.append(sum(p[1] for p in pts) / len(pts) * height)
                zs = [o[2] for o in (lo, ro) if o and o[2] is not None]
                if zs:
                    dists.append(sum(zs) / len(zs))
            if not xs:
                per_dot.append({"dot_px": [cx, cy], "samples": 0})
                continue
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            offset = math.hypot(mx - cx, my - cy)
            rms = math.sqrt(sum((x - mx) ** 2 + (y - my) ** 2 for x, y in zip(xs, ys)) / len(xs))
            per_dot.append({
                "dot_px": [cx, cy], "gaze_px": [round(mx, 1), round(my, 1)],
                "samples": len(xs), "offset_px": round(offset, 1), "rms_px": round(rms, 1),
                "head_dist_mm": round(sum(dists) / len(dists), 1) if dists else None,
            })
    finally:
        tracker.unsubscribe_from(tr.EYETRACKER_GAZE_DATA, cb)
        pygame.quit()

    good = [d for d in per_dot if d.get("samples")]
    result = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "tracker": getattr(tracker, "model", ""),
        "serial": getattr(tracker, "serial_number", ""),
        "screen_px": [width, height],
        "display_mm": list(display_mm),
        "dots": per_dot,
        "aborted": aborted,
    }
    if good:
        offset = sum(d["offset_px"] for d in good) / len(good)
        rms = sum(d["rms_px"] for d in good) / len(good)
        dists = [d["head_dist_mm"] for d in good if d.get("head_dist_mm")]
        result["accuracy_px"] = round(offset, 1)
        result["precision_rms_px"] = round(rms, 1)
        if dists and display_mm[0]:
            mm_per_px = display_mm[0] / width
            dist = sum(dists) / len(dists)
            result["head_dist_mm"] = round(dist, 1)
            result["accuracy_deg"] = round(math.degrees(math.atan2(offset * mm_per_px, dist)), 2)
            result["precision_rms_deg"] = round(math.degrees(math.atan2(rms * mm_per_px, dist)), 2)
    return result


def measure_rect(width: int, height: int) -> dict | None:
    """Two SPACE presses with the mouse on the minimap's corners; None if aborted."""
    from pynput import keyboard, mouse

    print("\nMinimap rectangle: with the game visible, put the mouse on the minimap's")
    print("TOP-LEFT corner and press SPACE, then on the BOTTOM-RIGHT corner and press SPACE.")
    print("ESC skips this step.")
    points: list = []
    ctrl = mouse.Controller()
    done = {"abort": False}

    def on_press(key):
        if key == keyboard.Key.space:
            points.append(tuple(ctrl.position))
            print(f"  corner {len(points)}: {points[-1]}")
            if len(points) == 2:
                return False
        if key == keyboard.Key.esc:
            done["abort"] = True
            return False

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()
    if done["abort"] or len(points) < 2:
        return None
    (x0, y0), (x1, y1) = points
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return {
        "x0": int(x0), "y0": int(y0), "x1": int(x1), "y1": int(y1),
        "screen_px": [width, height],
        "measured": datetime.now().isoformat(timespec="seconds"),
        "note": "minimap rectangle in screen pixels, measured by hand; used by the merge "
                "for gaze-on-minimap and minimap clicks",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-rect", action="store_true", help="skip the minimap measurement")
    ap.add_argument("--rect-only", action="store_true", help="only measure the minimap")
    args = ap.parse_args()
    width, height = _screen()

    if not args.rect_only:
        result = validate_gaze(width, height)
        if result:
            out = session.stream_dir("gaze") / f"validation_{datetime.now():%Y%m%d-%H%M%S}.json"
            out.write_text(json.dumps(result, indent=2), encoding="utf-8")
            acc = result.get("accuracy_px")
            deg = result.get("accuracy_deg")
            print(f"Gaze accuracy: {acc} px" + (f" ({deg} deg)" if deg is not None else "")
                  + f", precision {result.get('precision_rms_px')} px RMS -> {out}")
            if acc is not None and acc > 60:
                print("  !! more than 60 px off: re-run the Tobii calibration before recording")

    if not args.no_rect:
        rect = measure_rect(width, height)
        if rect:
            RECT_FILE.parent.mkdir(parents=True, exist_ok=True)
            RECT_FILE.write_text(json.dumps(rect, indent=2), encoding="utf-8")
            w, h = rect["x1"] - rect["x0"], rect["y1"] - rect["y0"]
            print(f"Minimap: {w}x{h} px at ({rect['x0']}, {rect['y0']}) -> {RECT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
