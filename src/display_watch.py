"""Screen and game-window geometry over a session -> data/gamestate/<sid>_display.json.

Added 2026-11-10.

Why
---
Every stream that carries screen coordinates assumed the screen it saw when it started:
gaze multiplies the tracker's fractions by the screen size, the input log writes mouse
pixels, the minimap rectangle is estimated from the screen height, and OBS scales whatever
the game renders onto its canvas. On 9 Nov 2026 a lab screen had been left at 1920x1080
(native 2560x1440) and the game's resolution was changed during the game; afterwards the
gaze dot no longer sat on what the player looked at and nobody could tell from the data
which part of the game was on which grid.

This module does not try to prevent that. It records it: once a second it reads the
size of the screen the game is on and the League game window's client rectangle (position
relative to that screen, and size, in physical pixels) and keeps a list of *segments*, one
entry each time something changes. With
that list any screen coordinate at any time can be turned into a fraction of the game
frame, which is what the video shows:

    u = (screen_x - window.x) / window.w        v = (screen_y - window.y) / window.h

``<sid>_display.json``::

    {"session_id": "...", "poll_s": 1.0, "samples": 1503,
     "segments": [{"t_unix_ms": 1794..., "screen_px": [1920, 1080],
                   "window": [0, 0, 1920, 1080], "state": "fullscreen"}, ...],
     "changed": false, "changes": 0,
     "screen_px_start": [1920, 1080], "game_px_start": [1920, 1080]}

``state`` is ``fullscreen`` (the window covers the screen; borderless and exclusive look
the same from here), ``windowed``, ``hidden`` (minimised or alt-tabbed away in exclusive
fullscreen) or ``absent`` (no game window found). ``changed`` is true when the screen size
or the size of the visible game window took more than one value during the session; a
player alt-tabbing does not count.

Windows only (ctypes); elsewhere, and in tests, ``Watcher(read=...)`` takes any function
returning a snapshot dict.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import session

GAME_WINDOW_CLASS = "RiotWindowClass"          # the game, not the client (RCLIENT)
POLL_S = 1.0


def game_window() -> "dict | None":
    """Client rectangle of the League game window, or None.

    Returns ``{"x", "y", "w", "h", "hidden", "monitor_px"}``. ``x``/``y`` are relative to
    the top-left of the monitor the window is on (so a game on a second monitor still
    reads ``0, 0`` when it fills that monitor) and ``monitor_px`` is that monitor's size;
    ``hidden`` when the window is minimised (exclusive fullscreen after alt-tab) or has no
    area.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        session.screen_px()                      # sets DPI awareness: physical pixels below
        # A handle of our own: argtypes set on the shared ctypes.windll.user32 apply to every
        # module in the process, and screeninfo calls GetMonitorInfoW with another structure
        # (its monitor callback then failed with an ArgumentError in every session's log).
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        hwnd = user32.FindWindowW(GAME_WINDOW_CLASS, None)
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        w, h = int(rect.right - rect.left), int(rect.bottom - rect.top)
        hidden = bool(user32.IsIconic(hwnd)) or w <= 0 or h <= 0 or not user32.IsWindowVisible(hwnd)
        ox, oy, monitor = 0, 0, None
        mon = user32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            ox, oy = int(info.rcMonitor.left), int(info.rcMonitor.top)
            monitor = [int(info.rcMonitor.right - info.rcMonitor.left), int(info.rcMonitor.bottom - info.rcMonitor.top)]
        return {"x": int(pt.x) - ox, "y": int(pt.y) - oy, "w": w, "h": h, "hidden": hidden, "monitor_px": monitor}
    except Exception:  # noqa: BLE001 - geometry is best effort; never take the capture down
        return None


def snapshot() -> dict:
    """What the screen and the game window look like right now."""
    win = game_window()
    # the monitor the game is on when there is a game window, else the primary screen
    screen = tuple(win["monitor_px"]) if win and win.get("monitor_px") else session.screen_px()
    return classify(screen, win)


def classify(screen, win) -> dict:
    """Turn a raw screen size and window rectangle into a segment (without its time)."""
    out = {"screen_px": list(screen) if screen else None, "window": None, "state": "absent"}
    if not win:
        return out
    if win.get("hidden"):
        out["state"] = "hidden"
        return out
    out["window"] = [win["x"], win["y"], win["w"], win["h"]]
    covers = bool(screen) and win["x"] <= 0 and win["y"] <= 0 \
        and win["x"] + win["w"] >= screen[0] and win["y"] + win["h"] >= screen[1]
    out["state"] = "fullscreen" if covers else "windowed"
    return out


def _visible_sizes(segments: list) -> set:
    return {(tuple(s["screen_px"] or ()), tuple(s["window"][2:])) for s in segments if s.get("window")}


def summarise(segments: list, samples: int = 0, sid: str = "", poll_s: float = POLL_S) -> dict:
    """The json written at the end of the session (also used for the live counters)."""
    visible = [s for s in segments if s.get("window")]
    sizes = _visible_sizes(segments)
    first = visible[0] if visible else None
    return {
        "session_id": sid,
        "poll_s": poll_s,
        "samples": samples,
        "segments": segments,
        "changed": len(sizes) > 1,
        "changes": max(0, len(sizes) - 1),
        "screen_px_start": first["screen_px"] if first else (segments[0]["screen_px"] if segments else None),
        "game_px_start": first["window"][2:] if first else None,
        "note": ("One entry per change of the primary screen size or the game window's client "
                 "rectangle [x, y, w, h] in screen pixels. A screen coordinate (gaze, mouse) at time "
                 "t is a fraction of the game frame through the segment in force at t: "
                 "u = (x - window.x) / window.w. `changed` ignores hidden/absent spells."),
    }


class Watcher:
    """Polls the geometry in a thread; ``stop()`` writes the json and returns its dict."""

    def __init__(self, sid: str, out_dir: "Path | None" = None, read=snapshot, poll_s: float = POLL_S):
        self.sid = sid
        self.out_dir = Path(out_dir) if out_dir else session.stream_dir("gamestate")
        self.read = read
        self.poll_s = poll_s
        self.segments: list = []
        self.samples = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None

    # one poll; separate from the loop so tests can drive it without a thread
    def poll(self, now_ms: "float | None" = None) -> None:
        try:
            snap = self.read()
        except Exception:  # noqa: BLE001
            return
        if not snap:
            return
        key = (tuple(snap.get("screen_px") or ()), tuple(snap.get("window") or ()), snap.get("state"))
        with self._lock:
            self.samples += 1
            last = self.segments[-1] if self.segments else None
            if last is None or (tuple(last["screen_px"] or ()), tuple(last["window"] or ()), last["state"]) != key:
                self.segments.append({"t_unix_ms": int(now_ms if now_ms is not None else time.time() * 1000),
                                      "screen_px": snap.get("screen_px"), "window": snap.get("window"),
                                      "state": snap.get("state")})

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.poll_s)

    def start(self) -> "Watcher":
        self.poll()                                  # the first segment is the state at game start
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def live(self) -> dict:
        """Small dict for rig_status.json: what the app's in-game panel shows."""
        with self._lock:
            segs = list(self.segments)
        visible = [s for s in segs if s.get("window")]
        cur = segs[-1] if segs else None
        return {
            "screen_px": cur["screen_px"] if cur else None,
            "game_px": (cur["window"][2:] if cur and cur.get("window") else (visible[-1]["window"][2:] if visible else None)),
            "state": cur["state"] if cur else None,
            "changes": max(0, len(_visible_sizes(segs)) - 1),
        }

    def path(self) -> Path:
        return self.out_dir / f"{self.sid}_display.json"

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        with self._lock:
            info = summarise(list(self.segments), self.samples, self.sid, self.poll_s)
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.path().write_text(json.dumps(info, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"  !! could not write {self.path().name}: {exc}")
        return info


def report(info: dict) -> None:
    """One or two console lines at the end of the session."""
    start, game = info.get("screen_px_start"), info.get("game_px_start")
    if not info.get("segments"):
        print("  display: no geometry recorded")
        return
    line = f"  display: screen {start[0]}x{start[1]}" if start else "  display: screen unknown"
    if game:
        line += f", game {game[0]}x{game[1]}"
    print(line)
    if info.get("changed"):
        sizes = sorted({f"{s['window'][2]}x{s['window'][3]} on {s['screen_px'][0]}x{s['screen_px'][1]}"
                        for s in info["segments"] if s.get("window") and s.get("screen_px")})
        print(f"  !! the resolution changed {info['changes']}x during the game ({'; '.join(sizes)}). "
              "The change is recorded in _display.json so the streams can still be aligned; "
              "keep the resolution fixed for the next game.")
    elif game and start and (game[0], game[1]) != (start[0], start[1]):
        print("  !! the game does not fill the screen (windowed or a lower in-game resolution). "
              "Recorded in _display.json; gaze and mouse are mapped through it.")


if __name__ == "__main__":
    print(json.dumps(snapshot()))
