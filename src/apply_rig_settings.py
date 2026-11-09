"""Put this rig's League settings on the PlaySmart standard.

Added 2026-10-14. On 28 Sep 2026 the five rigs ran minimap scales of 0.3, 0.9, 1.0,
1.32 and 1.5: the same glance landed on minimaps of very different sizes, and because
each player sat at one rig all evening, rig and role could not be told apart in the
gaze numbers. This writes the standard into the client's ``game.cfg`` so every rig
matches. Run it with the League client **closed** (it rewrites the file on exit).

    poetry run python src/apply_rig_settings.py            # apply, keep a .bak
    poetry run python src/apply_rig_settings.py --check    # report only

The standard (also in preflight.py): HUD MinimapScale 1.0. Add keys to STANDARD to
enforce more. Values are written as League writes them (four decimals).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lcu  # noqa: E402

STANDARD = {("HUD", "MinimapScale"): "1.0000"}


def game_cfg(league_dir: Path | None = None) -> Path | None:
    dirs = [league_dir] if league_dir else []
    if os.environ.get(lcu.ENV_LEAGUE_DIR):
        dirs.append(Path(os.environ[lcu.ENV_LEAGUE_DIR]))
    for d in dirs + lcu.DEFAULT_DIRS:
        p = d / "Config" / "game.cfg"
        if p.is_file():
            return p
    return None


def read_values(text: str) -> dict:
    """``{(section, key): value}`` for an INI-style game.cfg."""
    out, section = {}, ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
        elif "=" in s and not s.startswith(";"):
            k, _, v = s.partition("=")
            out[(section, k.strip())] = v.strip()
    return out


def apply(text: str, standard: dict) -> tuple[str, list]:
    """Return the text with the standard applied, and the list of changes made."""
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if text.count("\r\n") else "\n"
    changes, section, seen = [], "", set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            continue
        if "=" in s and not s.startswith(";"):
            k = s.partition("=")[0].strip()
            want = standard.get((section, k))
            if want is not None:
                seen.add((section, k))
                have = s.partition("=")[2].strip()
                if have != want:
                    lines[i] = f"{k}={want}{nl}"
                    changes.append((section, k, have, want))
    for (section, k), want in standard.items():
        if (section, k) not in seen:
            header = f"[{section}]"
            idx = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
            if idx is None:
                lines.append(f"{nl}{header}{nl}{k}={want}{nl}")
            else:
                lines.insert(idx + 1, f"{k}={want}{nl}")
            changes.append((section, k, "(absent)", want))
    return "".join(lines), changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    ap.add_argument("--league-dir", type=Path, default=None)
    args = ap.parse_args()
    cfg = game_cfg(args.league_dir)
    if cfg is None:
        print("game.cfg not found: set PLAYSMART_LEAGUE_DIR or pass --league-dir")
        return 1
    text = cfg.read_text(encoding="utf-8", errors="replace")
    values = read_values(text)
    for (section, key), want in STANDARD.items():
        have = values.get((section, key), "(absent)")
        print(f"{section}.{key}: {have}" + ("" if have == want else f"  -> standard {want}"))
    new_text, changes = apply(text, STANDARD)
    if not changes:
        print("already on the standard")
        return 0
    if args.check:
        print(f"{len(changes)} setting(s) differ; run without --check to apply (League closed)")
        return 1
    if lcu.lockfile() is not None:
        print("the League client is running: close it first, it rewrites game.cfg on exit")
        return 1
    shutil.copy(cfg, cfg.with_suffix(".cfg.bak"))
    cfg.write_text(new_text, encoding="utf-8")
    print(f"written {cfg} ({len(changes)} change(s); backup {cfg.with_suffix('.cfg.bak').name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
