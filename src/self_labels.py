"""Ask the player about their own deaths, right after the game.

Added 2026-09-25 (iteration 5).

The analysis sorts every death into *didn't know* / *didn't look* / *looked and
misjudged* from fog, positions and gaze. That needs a second opinion to be checked
against, and the cheapest one is the player's own, one minute after the game while
the memory is fresh: for each death, did you know he was there? Plus one ordinal
question per game (Yannakakis: relative, not absolute) about how the game felt.

This is a questionnaire, so it is covered by the consent form as one; it stores no
new personal data (the deaths come from the events stream already recorded). It is
a *second label*, not ground truth: players are wrong about their own deaths in
interesting ways, and disagreement with the gaze decomposition is itself a result.

Runs from ``pop_up_screen.py`` after the recorders stop. ``PLAYSMART_SELF_LABELS=0``
turns it off; without a display (or tkinter) it is skipped. Nothing here blocks the
upload for longer than ``TIMEOUT_S``.

Output: ``data/gamestate/<sid>_labels.json``::

    {"session_id": ..., "deaths": [{"game_time_s": 354.6, "killer": "Master Yi",
      "answer": "didnt_look"}, ...], "game": {"vs_last_game": "more_tilted"},
      "note": "...", "answered_unix_ms": ...}
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session  # noqa: E402

ENABLED = os.environ.get("PLAYSMART_SELF_LABELS", "1") != "0"
TIMEOUT_S = 180

DEATH_CHOICES = [
    ("knew", "I knew he was there"),
    ("didnt_look", "It was on the minimap, I didn't look"),
    ("didnt_know", "There was nothing to see"),
    ("unsure", "Not sure"),
]
GAME_CHOICES = [
    ("less_tilted", "Calmer than my last game"),
    ("same", "About the same"),
    ("more_tilted", "More tilted than my last game"),
    ("first", "No previous game to compare"),
]


def my_deaths(sid: str, meta: dict) -> list:
    """This player's deaths from the events stream: ``[{game_time_s, killer, assisters}]``.

    The Live Client names champions in ``KillerName``/``VictimName`` (patch 16.19;
    older clients used player names), so both the champion and the Riot ID are
    accepted as "me".
    """
    path = session.stream_dir("gamestate") / f"{sid}_events.csv"
    if not path.is_file():
        return []
    me = {str(meta.get("champion", "")).lower(), str(meta.get("riot_id", "")).lower(),
          str(meta.get("riot_id", "")).split("#")[0].lower()}
    me.discard("")
    out = []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("event_name") != "ChampionKill":
                    continue
                if str(row.get("victim", "")).lower() not in me:
                    continue
                try:
                    t = float(row.get("game_time_s") or 0)
                except ValueError:
                    continue
                out.append({"game_time_s": round(t, 1), "killer": row.get("killer", ""),
                            "assisters": row.get("assisters", "")})
    except (OSError, csv.Error):
        return []
    return out


def _clock(t: float) -> str:
    return f"{int(t // 60):d}:{int(t % 60):02d}"


def ask(sid: str, deaths: list) -> dict | None:
    """Show the dialog; None when skipped, closed or timed out."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # no tkinter / no display
        print(f"[labels] no dialog available ({exc}); skipping self-labels")
        return None
    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"[labels] no display ({exc}); skipping self-labels")
        return None
    root.title("PlaySmart - one minute about your game")
    root.attributes("-topmost", True)
    result: dict = {}

    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="Your deaths this game. For each one: did you know he was there?",
              font=("Segoe UI", 11, "bold")).grid(column=0, row=0, columnspan=5, sticky="w")

    answers = []
    row_i = 1
    if not deaths:
        ttk.Label(frame, text="No deaths recorded for you this game.").grid(
            column=0, row=row_i, columnspan=5, sticky="w")
        row_i += 1
    for d in deaths:
        var = tk.StringVar(value="")
        answers.append(var)
        who = d["killer"] + (f" (+{d['assisters'].count('|') + 1})" if d.get("assisters") else "")
        ttk.Label(frame, text=f"{_clock(d['game_time_s'])}  killed by {who}").grid(
            column=0, row=row_i, sticky="w", pady=(8, 0))
        for j, (value, text) in enumerate(DEATH_CHOICES):
            ttk.Radiobutton(frame, text=text, value=value, variable=var).grid(
                column=1 + j, row=row_i, sticky="w", padx=6, pady=(8, 0))
        row_i += 1

    ttk.Separator(frame).grid(column=0, row=row_i, columnspan=5, sticky="ew", pady=12)
    row_i += 1
    ttk.Label(frame, text="Compared with your last game, this one was:",
              font=("Segoe UI", 11, "bold")).grid(column=0, row=row_i, columnspan=5, sticky="w")
    row_i += 1
    game_var = tk.StringVar(value="")
    for j, (value, text) in enumerate(GAME_CHOICES):
        ttk.Radiobutton(frame, text=text, value=value, variable=game_var).grid(
            column=j, row=row_i, sticky="w", padx=6)
    row_i += 1
    ttk.Label(frame, text="Anything about a death worth noting (optional):").grid(
        column=0, row=row_i, columnspan=5, sticky="w", pady=(12, 0))
    row_i += 1
    note = tk.Text(frame, height=3, width=80)
    note.grid(column=0, row=row_i, columnspan=5, sticky="ew")
    row_i += 1

    def done():
        result["deaths"] = [
            {**d, "answer": v.get() or "skipped"} for d, v in zip(deaths, answers)
        ]
        result["game"] = {"vs_last_game": game_var.get() or "skipped"}
        result["note"] = note.get("1.0", "end").strip()
        root.destroy()

    def skip():
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(column=0, row=row_i, columnspan=5, sticky="e", pady=(12, 0))
    ttk.Button(buttons, text="Skip", command=skip).grid(column=0, row=0, padx=6)
    ttk.Button(buttons, text="Save", command=done).grid(column=1, row=0)
    root.after(TIMEOUT_S * 1000, skip)
    root.bind("<Return>", lambda _e: done())
    root.mainloop()
    return result or None


def collect(sid: str, meta: dict) -> Path | None:
    """Ask, and write ``<sid>_labels.json``. None when nothing was collected."""
    if not ENABLED:
        return None
    deaths = my_deaths(sid, meta)
    result = ask(sid, deaths)
    if not result:
        print("[labels] no self-labels for this session")
        return None
    result = {"session_id": sid, "answered_unix_ms": int(time.time() * 1000),
              "champion": meta.get("champion", ""), **result}
    path = session.stream_dir("gamestate") / f"{sid}_labels.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"[labels] saved {path.name} ({len(result['deaths'])} deaths)")
    return path


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else session.session_id()
    meta_path = session.stream_dir("gamestate") / f"{sid}_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    collect(sid, meta)
