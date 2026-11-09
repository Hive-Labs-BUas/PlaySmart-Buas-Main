"""Input recorder: keyboard and mouse events to data/input/<session_id>_input.csv.

Modified 2026-09 (iteration 5): file named from the shared session id (session.py);
stops on the orchestrator's stop signal instead of only F12.

Modified 2026-10-29: the hook callbacks only put events on a queue. Windows delivers every
mouse and key event *through* a low-level hook, so anything slow inside the callback —
opening the CSV per event, printing a line per event into the rig log — stalls the cursor
(half-second freezes in game). A writer thread drains the queue into one open file and
flushes twice a second. Mouse clicks now also carry the button (new last column
``button``: left / right / middle / x1 / x2); the other columns are unchanged.
"""

from pynput import mouse, keyboard
import time
import csv
import sys
import threading
import queue
from pathlib import Path
import screeninfo
import math

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session

screen = screeninfo.get_monitors()[0]
screen_resolution = f"{screen.width}x{screen.height}p"

# Named with the session id at creation, so this file never has to be paired
# with the other streams by guessing which one is newest.
file_path = str(session.stream_path("input"))

movement_threshold = 100
last_logged_position = None
key_start_times = {}
events: "queue.Queue" = queue.Queue()
counts = {"key_press": 0, "mouse_click": 0, "mouse_move": 0}


def get_unix_time():
    return int(time.time() * 1000)


def log(event_type, details_x=None, details_y=None, duration=None, button=""):
    """Called on the hook thread: must return immediately."""
    events.put((get_unix_time(), event_type, details_x, details_y, duration, button))


def writer_thread(stop):
    """Drain the queue into the CSV; one open file, flushed every 0.5 s."""
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a", newline="") as fh:
        w = csv.writer(fh)
        if fh.tell() == 0:
            w.writerow(["unix_time", "event_type", "details_x", "details_y", "duration", "screen_resolution", "button"])
            print(f"CSV header written: {file_path}")
        last_flush = time.time()
        while not (stop.is_set() and events.empty()):
            try:
                t, kind, x, y, dur, button = events.get(timeout=0.25)
            except queue.Empty:
                continue
            w.writerow([t, kind, x, y, dur, screen_resolution, button])
            counts[kind] = counts.get(kind, 0) + 1
            if time.time() - last_flush > 0.5:
                fh.flush()
                last_flush = time.time()
        fh.flush()


def on_press(key):
    key_str = str(key)
    if key_str not in key_start_times:
        key_start_times[key_str] = time.time()


def on_release(key):
    key_str = str(key)
    if key == keyboard.Key.f12:
        print("F12 pressed, stopping recording.")
        mouse_listener.stop()  # stop mouse too
        return False           # stop keyboard
    start_time = key_start_times.pop(key_str, None)
    if start_time:
        log("key_press", key_str, None, round(time.time() - start_time, 3))


def on_click(x, y, button, pressed):
    if pressed:
        log("mouse_click", x, y, None, getattr(button, "name", str(button)))


def on_move(x, y):
    global last_logged_position
    if last_logged_position is None:
        last_logged_position = (x, y)
        log("mouse_move", x, y)
    else:
        last_x, last_y = last_logged_position
        if math.hypot(x - last_x, y - last_y) > movement_threshold:
            last_logged_position = (x, y)
            log("mouse_move", x, y)


print(f"Keyboard & Mouse logging started -> {file_path}")
print("Stops when the game ends, or on F12.")

# create the listeners so both can be stopped
mouse_listener = mouse.Listener(on_click=on_click, on_move=on_move)
keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
stop_writer = threading.Event()
writer = threading.Thread(target=writer_thread, args=(stop_writer,), daemon=True)


def watch_for_stop():
    """Stop when the orchestrator says the game is over.

    F12 is still handled in on_release for a manual abort, but with automatic
    start/stop there is no key press to wait for, so the stop flag has to be
    polled as well.
    """
    while keyboard_listener.running:
        if session.stop_requested():
            print("Stop requested, ending input capture.")
            mouse_listener.stop()
            keyboard_listener.stop()
            return
        time.sleep(0.25)


# start the writer, then both listeners
writer.start()
mouse_listener.start()
keyboard_listener.start()

threading.Thread(target=watch_for_stop, daemon=True).start()

# wait for the keyboard listener to finish (stop flag or F12), then let the writer drain
keyboard_listener.join()
stop_writer.set()
writer.join(timeout=5)

print(f"Saved {counts['key_press']} key presses, {counts['mouse_click']} clicks, {counts['mouse_move']} moves to {file_path}")
