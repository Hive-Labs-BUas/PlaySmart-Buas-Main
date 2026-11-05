 
import os
import csv
import glob
import datetime
import tkinter as tk
from tkinter import simpledialog, ttk
import json
import sys
import time
import uuid
 
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
from server.python_app.sftp_upload import upload_file_to_sftp
 
# ------------------ PC CONFIG ------------------
 
PC_NAME = "pc02"
 
# ------------------ Session matching ------------------
# key_listener.py writes the F7 time here. Only files created after it belong to this recording,
# so a late or missing file can no longer cause an earlier recording's file to get this name.
SESSION_FILE = "data/session_start.json"
CREATED_TOLERANCE_S = 5   # a file counts as this recording's if created at most this long before F7
START_WARN_S = 120        # warn if a file's first unix_time is further than this from F7
 
# ------------------ Utility Functions ------------------
 
def get_latest_file(path):
 
    files = glob.glob(os.path.join(path, "*.csv"))
 
    return max(files, key=os.path.getmtime) if files else None
 
 
def get_latest_by_extension(path, extension):
 
    files = glob.glob(os.path.join(path, f"*.{extension}"))
 
    return max(files, key=os.path.getmtime) if files else None
 
 
def get_latest_video_any(path):
 
    files = (
        glob.glob(os.path.join(path, "*.mp4")) +
        glob.glob(os.path.join(path, "*.mkv"))
    )
 
    return max(files, key=os.path.getmtime) if files else None
 
 
def get_next_player_id(mapping):
 
    existing_ids = [
        v for v in mapping.values()
        if v.startswith("P")
    ]
 
    nums = [
        int(x[1:])
        for x in existing_ids
        if x[1:].isdigit()
    ]
 
    next_id = max(nums, default=0) + 1
 
    return f"P{next_id:03d}"
 
 
def load_mapping(mapping_file='data/json/ign_mapping.json'):
 
    if os.path.exists(mapping_file):
 
        with open(mapping_file, 'r') as file:
            return json.load(file)
 
    return {}
 
 
def save_mapping(mapping, mapping_file='data/json/ign_mapping.json'):
 
    os.makedirs(os.path.dirname(mapping_file), exist_ok=True)
 
    with open(mapping_file, 'w') as file:
        json.dump(mapping, file, indent=4)
 
 
def upload_ign_mapping():
 
    mapping_file = 'data/json/ign_mapping.json'
 
    if not os.path.exists(mapping_file):
        return
 
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
 
    temp_name = f"ign_mapping_{PC_NAME}_{timestamp}.json"
 
    temp_local = os.path.join(
        "data/json",
        temp_name
    )
 
    try:
 
        # create timestamped copy locally
        with open(mapping_file, 'r') as src:
            data = json.load(src)
 
        with open(temp_local, 'w') as dst:
            json.dump(data, dst, indent=4)
 
        print("Uploading IGN mapping...")
 
        upload_file_to_sftp(
            temp_local,
            "/data/json/"
        )
 
        print("IGN mapping uploaded")
 
        # optional cleanup
        os.remove(temp_local)
 
    except Exception as e:
 
        print(f"IGN mapping upload failed: {e}")
 
 
def load_daily_game_count(count_file='data/json/date_game_count.json'):
 
    if os.path.exists(count_file):
 
        try:
 
            with open(count_file, 'r') as file:
                return json.load(file)
 
        except json.JSONDecodeError:
            return {}
 
    return {}
 
 
def save_daily_game_count(data, count_file='data/json/date_game_count.json'):
 
    os.makedirs(os.path.dirname(count_file), exist_ok=True)
 
    with open(count_file, 'w') as file:
        json.dump(data, file, indent=4)
 
 
def update_daily_game_count(game_name):
 
    today = datetime.datetime.now().strftime("%d-%m-%Y")
 
    data = load_daily_game_count()
 
    data.setdefault(today, {})
 
    data[today][game_name] = data[today].get(game_name, 0) + 1
 
    save_daily_game_count(data)
 
    return get_ordinal(data[today][game_name]), today
 
 
def get_ordinal(n):
 
    return f"{n}{'th' if 4 <= n % 100 <= 20 else {1:'st',2:'nd',3:'rd'}.get(n%10,'th')}"
 
 
# ------------------ Session file matching ------------------
 
def load_session_start():
    try:
        with open(SESSION_FILE, 'r') as f:
            return int(json.load(f)["start_ms"])
    except Exception:
        return None
 
 
def created_time(path):
    st = os.stat(path)
    return getattr(st, "st_birthtime", st.st_ctime)  # creation time on Windows
 
 
def first_unix_time_ms(path):
    """First numeric unix_time in a CSV, in ms (seconds are converted). None if there is none."""
    try:
        with open(path, newline='') as f:
            reader = csv.reader(f)
            header = [h.strip() for h in next(reader)]
            if "unix_time" not in header:
                return None
            i = header.index("unix_time")
            for row in reader:
                try:
                    t = float(row[i])
                except (ValueError, IndexError):
                    continue
                return t * 1000 if t < 1e11 else t
    except Exception:
        return None
    return None
 
 
def find_session_file(folder, start_ms, tag):
    """The unnamed CSV in `folder` that was written during this recording, or None."""
    files = [
        p for p in glob.glob(os.path.join(folder, "*.csv"))
        if "_game_" not in os.path.basename(p)
    ]
 
    if start_ms is None:
        print(f"WARNING: no session start found ({SESSION_FILE}); "
              f"using the newest {tag} file (old behaviour)")
        return max(files, key=os.path.getmtime) if files else None
 
    fresh = [p for p in files if created_time(p) >= start_ms / 1000 - CREATED_TOLERANCE_S]
 
    stale = len(files) - len(fresh)
    if stale:
        print(f"WARNING: {stale} unnamed {tag} file(s) from before this recording in {folder}; "
              f"left as they are, not renamed or uploaded")
 
    if not fresh:
        print(f"WARNING: no {tag} file was written during this recording; "
              f"nothing renamed or uploaded for {tag}")
        return None
 
    if len(fresh) > 1:
        print(f"WARNING: {len(fresh)} {tag} files written during this recording; using the newest")
 
    path = max(fresh, key=os.path.getmtime)
 
    t0 = first_unix_time_ms(path)
    if t0 is not None and abs(t0 - start_ms) > START_WARN_S * 1000:
        print(f"WARNING: {tag} file starts {(t0 - start_ms) / 1000:+.0f} s from F7; check it")
 
    return path
 
 
# ------------------ Upload Function ------------------
 
def upload_with_retry(path, dest_directory):
 
    for i in range(3):
 
        try:
 
            print(f"Uploading: {path}")
 
            upload_file_to_sftp(path, dest_directory)
 
            print(f"Uploaded: {os.path.basename(path)}")
 
            return
 
        except Exception as e:
 
            print(f"Retry {i+1} failed: {e}")
 
            time.sleep(2)
 
 
def upload_newest_file(folder_path, dest_directory, extension=None):
 
    if not os.path.exists(folder_path):
        print(f"Missing folder: {folder_path}")
        return
 
    files = []
 
    for f in os.listdir(folder_path):
 
        full_path = os.path.join(folder_path, f)
 
        if not os.path.isfile(full_path):
            continue
 
        if extension and not f.lower().endswith(extension.lower()):
            continue
 
        files.append(full_path)
 
    if not files:
        print(f"No {extension} files in {folder_path}")
        return
 
    newest = max(files, key=os.path.getmtime)
 
    for i in range(3):
 
        try:
 
            print(f"Uploading: {newest}")
 
            upload_file_to_sftp(newest, dest_directory)
 
            print(f"Uploaded: {os.path.basename(newest)}")
 
            return
 
        except Exception as e:
 
            print(f"Retry {i+1} failed: {e}")
 
            time.sleep(2)
 
 
# ------------------ Main ------------------
 
def main():
 
    class DualInputDialog(simpledialog.Dialog):
 
        def body(self, master):
 
            tk.Label(master, text="In-game name:").grid(row=0, column=0)
            tk.Label(master, text="Game:").grid(row=1, column=0)
 
            mapping = load_mapping()
 
            self.player = ttk.Combobox(
                master,
                values=sorted(mapping.keys())
            )
 
            self.player.grid(row=0, column=1)
 
            self.game = ttk.Combobox(
                master,
                values=[
                    "valorant",
                    "league_of_legends",
                    "other"
                ],
                state="readonly"
            )
 
            self.game.set("valorant")
 
            self.game.grid(row=1, column=1)
 
            return self.player
 
        def apply(self):
 
            self.player_name = self.player.get().strip()
 
            self.game_name = self.game.get().strip().lower()
 
    # ---------------- INPUT ----------------
 
    root = tk.Tk()
 
    root.withdraw()
 
    dialog = DualInputDialog(root)
 
    player_name = getattr(dialog, 'player_name', None)
    game_name = getattr(dialog, 'game_name', None)
 
    if not player_name or not game_name:
 
        print("Invalid input")
 
        return
 
    # ---------------- PLAYER ----------------
 
    mapping = load_mapping()
 
    if player_name not in mapping:
 
        mapping[player_name] = get_next_player_id(mapping)
 
        save_mapping(mapping)
 
    player_id = mapping[player_name]
 
    ordinal, today = update_daily_game_count(game_name)
 
    now = datetime.datetime.now().strftime("%H-%M-%S")
 
    base_name = f"{ordinal}_game_{player_id}_{game_name}_{today}_{now}"
 
    # ---------------- CSV RENAME ----------------
    # Only files written during this recording (created after F7) are renamed, and exactly those
    # files are uploaded below.
 
    start_ms = load_session_start()
 
    renamed = {}
 
    for folder, tag in {
        "data/input": "input",
        "data/gaze": "gaze",
        "data/emotion": "emotion",
        "data/eda": "eda",
    }.items():
 
        latest = find_session_file(folder, start_ms, tag)
 
        if latest:
 
            new_path = os.path.join(
                folder,
                f"{base_name}_{tag}.csv"
            )
 
            try:
 
                os.replace(latest, new_path)
 
                renamed[tag] = new_path
 
                print(f"Renamed {tag}")
 
            except Exception as e:
 
                print(f"Rename failed for {tag}: {e}")
 
    # A stale start time must not be reused by a later manual run.
    if start_ms is not None:
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass
 
    # ---------------- AUDIO ----------------
 
    audio_folder = "data/audio"
 
    latest_audio = get_latest_by_extension(audio_folder, "wav")
 
    if latest_audio:
 
        try:
 
            new_audio = os.path.join(
                audio_folder,
                f"{base_name}.wav"
            )
 
            os.replace(latest_audio, new_audio)
 
            txt = latest_audio.replace(".wav", ".txt")
 
            if os.path.exists(txt):
 
                os.replace(
                    txt,
                    os.path.join(audio_folder, f"{base_name}.txt")
                )
 
        except Exception as e:
 
            print(f"Audio rename failed: {e}")
 
    # ---------------- VIDEO ----------------
 
    video_src = os.path.join(
        os.path.expanduser("~"),
        "Videos"
    )
 
    video_dst = os.path.join(
        os.path.expanduser("~"),
        "Documents",
        "research_software",
        "data",
        "video"
    )
 
    os.makedirs(video_dst, exist_ok=True)
 
    latest_video = get_latest_video_any(video_src)
 
    if latest_video:
 
        try:
 
            ext = os.path.splitext(latest_video)[1]
 
            os.replace(
                latest_video,
                os.path.join(video_dst, f"{base_name}{ext}")
            )
 
        except Exception as e:
 
            print(f"Video rename failed: {e}")
 
    # ---------------- UPLOAD ----------------
 
    print("Waiting before upload...")
 
    time.sleep(3)
 
    # CSV uploads: exactly the files renamed above (not "the newest file in the folder")
    for tag in ("emotion", "input", "gaze", "eda"):
 
        if tag in renamed:
            upload_with_retry(renamed[tag], f"/data/{tag}/")
 
    # PNG uploads
    upload_newest_file(
        'data/gaze',
        "/data/gaze/",
        ".png"
    )
 
    # Video upload
    upload_newest_file(
        video_dst,
        "/data/video/",
        ".mp4"
    )
 
    # ---------------- AUDIO UPLOAD ----------------
 
    if os.path.exists(audio_folder):
 
        for f in os.listdir(audio_folder):
 
            full = os.path.join(audio_folder, f)
 
            if os.path.isfile(full):
 
                try:
 
                    print(f"Uploading audio: {full}")
 
                    upload_file_to_sftp(
                        full,
                        "/data/audio/"
                    )
 
                except Exception as e:
 
                    print(f"Audio upload failed: {e}")
 
    # ---------------- IGN MAPPING UPLOAD ----------------
 
    upload_ign_mapping()
 
 
# ------------------ Run ------------------
 
if __name__ == "__main__":
 
    main()