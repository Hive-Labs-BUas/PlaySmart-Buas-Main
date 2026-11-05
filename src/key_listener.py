import json
import os
import subprocess
import sys
import time

import keyboard
import psutil

# Recorders that write the files pop_up_screen.py renames. After F12, the pop-up waits until
# these have exited (each one stops itself on F12 and writes its file first), so it never renames
# a file that is still being written or one from an earlier recording.
RECORDERS = [
    "src/eye_tracking_script.py",
    "src/Emotion_gaze_visualization.py",
    "src/keyboard_recording.py",
    "src/nuanic_eda.py",
]
# The microphone script transcribes after stopping (can take minutes). It is not waited for and
# not killed; it finishes on its own, as before.
BACKGROUND = ["src/microphone_recording.py"]

STOP_TIMEOUT_S = 180  # wait at most this long for the recorders to finish writing
SESSION_FILE = "data/session_start.json"


def kill_openface():
    """Kill any leftover OpenFace FeatureExtraction.exe processes"""
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] and 'FeatureExtraction.exe' in proc.info['name']:
            print(f"Killing leftover OpenFace process: PID {proc.pid}")
            proc.kill()


def start(script):
    # sys.executable is the project's virtualenv python (this script runs via `poetry run`).
    # Starting it directly gives a handle to the real process; `poetry run` would give a handle to a
    # wrapper whose child keeps running when the wrapper is terminated (Windows).
    return subprocess.Popen([sys.executable, script])


def write_session_start():
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    start_ms = int(time.time() * 1000)
    with open(SESSION_FILE, "w") as f:
        json.dump({"start_ms": start_ms}, f)
    return start_ms


def wait_for_recorders(processes):
    deadline = time.time() + STOP_TIMEOUT_S
    for script, proc in processes:
        remaining = max(0.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
            print(f"  finished: {script}")
        except subprocess.TimeoutExpired:
            print(f"  WARNING: {script} still running after {STOP_TIMEOUT_S} s, terminating it. "
                  f"Its file may be missing or incomplete for this recording.")
            proc.terminate()


def main():
    """
    Handles script execution flow based on key presses (F7 to start, F12 to stop).
    Author: Thomas Pichardo, Mauro van Hulst, Maikel Boezer
    Modified: Removes merging step, uploads data using pop_up_screen.py.
    Modified: waits for the recorders to finish writing before the pop-up; records the start time.
    """
    while True:
        print("Waiting for F7 or F12... (Press F7 to start or F12 to stop)")
        keyboard.wait('f7')

        print("F7 pressed, starting scripts...")
        kill_openface()  # Optional: clear leftover OpenFace processes
        start_ms = write_session_start()
        print(f"Session start: {start_ms}")
        processes = []
        try:
            for script in RECORDERS[:3]:
                processes.append((script, start(script)))
            for script in BACKGROUND:
                start(script)

            time.sleep(2)
            processes.append((RECORDERS[3], start(RECORDERS[3])))

            print("Waiting for F12 to stop and upload data...")
            keyboard.wait('f12')

            print("F12 pressed, waiting for the recorders to save their files...")
            wait_for_recorders(processes)

            print("Uploading latest data files via pop_up_screen.py...")
            result = subprocess.run([sys.executable, "src/pop_up_screen.py"])
            if result.returncode == 0:
                print(" Upload successful and pop-up shown!")
            else:
                print(" Upload failed.")
        finally:
            for _, process in processes:
                if process.poll() is None:
                    process.terminate()
            print("Processes stopped.")


if __name__ == "__main__":
    main()
