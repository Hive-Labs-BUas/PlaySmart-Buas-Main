import os
import pygame
import tobii_research as tr
import screeninfo
import keyboard
import subprocess
import pandas as pd
import glob
import time
from datetime import datetime
import threading
 
# ---------------- Paths and constants ----------------
 
openface_executable = 'OpenFace_2.2.0_win_x64/FeatureExtraction.exe'
output_dir = 'output'
 
os.makedirs(output_dir, exist_ok=True)
 
circle_radius = 50
outline_thickness = 3
 
# ---------------- Global state ----------------
 
is_stopping = False
 
start_unix_time = int(time.time() * 1000)
 
print(f"Script started at: {start_unix_time}")
 
# ---------------- Utility ----------------
 
def convert_unix_to_datetime(unix_time):
    return datetime.fromtimestamp(unix_time)
 
# ---------------- Initialize Pygame ----------------
 
pygame.init()
 
clock = pygame.time.Clock()
 
screen = screeninfo.get_monitors()[0]
 
screen_width = screen.width
screen_height = screen.height
 
window = pygame.display.set_mode(
    (screen_width, screen_height),
    pygame.NOFRAME
)
 
pygame.display.set_caption("Gaze and Emotion Overlay")
 
# ---------------- Eye Tracker Setup ----------------
 
eyetrackers = tr.find_all_eyetrackers()
 
if not eyetrackers:
    print("No eye trackers found. Running without gaze tracking.")
    my_eyetracker = None
else:
    my_eyetracker = eyetrackers[0]
 
# ---------------- Gaze Smoothing ----------------
 
alpha = 0.1
 
smoothed_x = 0
smoothed_y = 0
 
def gaze_data_callback(gaze_data):
 
    global smoothed_x, smoothed_y
 
    left_x, left_y = gaze_data['left_gaze_point_on_display_area']
    right_x, right_y = gaze_data['right_gaze_point_on_display_area']
 
    avg_x = (left_x + right_x) / 2
    avg_y = (left_y + right_y) / 2
 
    x_screen = int(avg_x * screen_width)
    y_screen = int(avg_y * screen_height)
 
    smoothed_x = alpha * x_screen + (1 - alpha) * smoothed_x
    smoothed_y = alpha * y_screen + (1 - alpha) * smoothed_y
 
# ---------------- Start OpenFace ----------------
 
command = [
    openface_executable,
    '-device', '0',
    '-out_dir', output_dir,
    '-aus'
]
 
# stderr goes into stdout, and the monitor thread keeps reading stdout until OpenFace exits.
# Before, both were pipes that nobody read after "Starting tracking": once a pipe buffer is full,
# OpenFace blocks on its next write and stops producing frames (emotion files ending early).
process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True
)
 
# ---------------- Monitor OpenFace ----------------
 
def monitor_openface():
 
    global start_unix_time
 
    started = False
 
    try:
        for line in process.stdout:
 
            if not started and "Starting tracking" in line:
 
                start_unix_time = int(time.time() * 1000)
 
                print(f"Tracking started at: {start_unix_time}")
 
                started = True
 
            # keep reading (and discarding) so the pipe never fills up
 
    except Exception as e:
        print(f"Monitor thread error: {e}")
 
monitor_thread = threading.Thread(
    target=monitor_openface,
    daemon=True
)
 
monitor_thread.start()
 
# ---------------- Load Calibration ----------------
 
if my_eyetracker:
 
    calibration_data = my_eyetracker.retrieve_calibration_data()
 
    if calibration_data:
        my_eyetracker.apply_calibration_data(calibration_data)
        print("Calibration loaded.")
 
    my_eyetracker.subscribe_to(
        tr.EYETRACKER_GAZE_DATA,
        gaze_data_callback,
        as_dictionary=True
    )
 
# ---------------- Emotion Mapping ----------------
 
def map_emotion(row):
 
    try:
 
        if (
            row.get('AU06_c', 0) == 1 and
            row.get('AU12_c', 0) == 1 and
            row.get('AU25_c', 0) == 1
        ):
            return 'Happiness'
 
        elif (
            row.get('AU01_c', 0) == 1 and
            row.get('AU04_c', 0) == 1 and
            row.get('AU15_c', 0) == 1 and
            row.get('AU17_c', 0) == 1
        ):
            return 'Sadness'
 
        elif (
            row.get('AU05_c', 0) == 1 and
            row.get('AU26_c', 0) == 1 and
            row.get('AU02_c', 0) == 1 and
            row.get('AU07_c', 0) == 1
        ):
            return 'Surprise'
 
        elif (
            row.get('AU09_c', 0) == 1 and
            row.get('AU10_c', 0) == 1 and
            row.get('AU14_c', 0) == 1
        ):
            return 'Disgust'
 
        elif (
            row.get('AU01_c', 0) == 1 and
            row.get('AU02_c', 0) == 1 and
            row.get('AU04_c', 0) == 1 and
            row.get('AU05_c', 0) == 1 and
            row.get('AU07_c', 0) == 1
        ):
            return 'Fear'
 
        elif (
            row.get('AU04_c', 0) == 1 and
            row.get('AU07_c', 0) == 1 and
            row.get('AU23_c', 0) == 1 and
            row.get('AU25_c', 0) == 1
        ):
            return 'Anger'
 
        else:
            return 'Neutral'
 
    except Exception as e:
        print(f"Emotion mapping error: {e}")
        return 'Unknown'
 
# ---------------- Save Emotion Data ----------------
 
def save_emotion_data():
 
    csv_files = glob.glob(os.path.join(output_dir, '*.csv'))
 
    if not csv_files:
        print("No CSV files found.")
        return
 
    latest_csv = max(csv_files, key=os.path.getctime)
 
    try:
 
        df = pd.read_csv(latest_csv)
 
        df.columns = df.columns.str.strip()
 
        df['emotion'] = df.apply(map_emotion, axis=1)
 
        if 'timestamp' in df.columns:
 
            df['unix_time'] = df['timestamp'].apply(
                lambda x: int(start_unix_time + x * 1000)
            )
 
            df['datetime'] = df['unix_time'].apply(
                lambda x: datetime.fromtimestamp(x / 1000.0)
            )
 
        else:
 
            print("Warning: timestamp column missing.")
 
            df['unix_time'] = None
            df['datetime'] = None
 
        df = df[['datetime', 'unix_time', 'emotion', 'confidence']]
 
        os.makedirs('data/emotion', exist_ok=True)
 
        save_file_path = os.path.join(
            'data/emotion',
            f'emotion_data_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.csv'
        )
 
        # Write under a temporary name first, so a half-written file is never picked up as .csv
        tmp_path = save_file_path + '.tmp'
 
        df.to_csv(tmp_path, index=False)
 
        os.replace(tmp_path, save_file_path)
 
        print(f"Saved emotion data to {save_file_path}")
 
    except Exception as e:
        print(f"Error saving emotion data: {e}")
 
# ---------------- Stop Recording ----------------
 
def stop_recording():
    """Only sets the flag. The main thread stops OpenFace and saves (see the finally block), so the
    save runs to completion before this process exits, and key_listener.py waits for that exit
    before it opens the pop-up. Before, the save ran in the hotkey thread while the main thread
    deleted OpenFace's output and exited."""
 
    global is_stopping
 
    if is_stopping:
        return
 
    is_stopping = True
 
    print("Stopping recording...")
 
# ---------------- Hotkey Listener ----------------
 
def listen_for_hotkey():
    keyboard.add_hotkey('f12', stop_recording)
 
hotkey_thread = threading.Thread(
    target=listen_for_hotkey,
    daemon=True
)
 
hotkey_thread.start()
 
# ---------------- Emotion Colors ----------------
 
emotion_colors = {
    'Happiness': (255, 255, 0),
    'Sadness': (0, 0, 255),
    'Surprise': (255, 165, 0),
    'Disgust': (128, 0, 128),
    'Fear': (255, 192, 203),
    'Anger': (255, 0, 0),
    'Neutral': (255, 255, 255)
}
 
# ---------------- Visualization Loop ----------------
 
emotion_update_interval = 0.5
last_emotion_check_time = time.time()
 
try:
 
    emotion = 'Neutral'
 
    while not is_stopping:
 
        pygame.event.pump()
 
        for event in pygame.event.get():
 
            if event.type == pygame.QUIT:
                stop_recording()
 
        # Clear screen
        window.fill((0, 0, 0))
 
        # Draw gaze circle
        pygame.draw.circle(
            window,
            (255, 255, 255),
            (int(smoothed_x), int(smoothed_y)),
            circle_radius,
            outline_thickness
        )
 
        # Draw emotion label
        rect_color = emotion_colors.get(emotion, (255, 255, 255))
 
        pygame.draw.rect(
            window,
            rect_color,
            (10, 1400, 280, 32),
            0
        )
 
        font = pygame.font.SysFont('Arial', 30)
 
        text_surface = font.render(
            f'Emotion: {emotion}',
            True,
            (0, 0, 0)
        )
 
        window.blit(text_surface, (20, 1400))
 
        # Update display
        pygame.display.flip()
 
        # FPS limit
        clock.tick(60)
 
        # Update emotion periodically
        if time.time() - last_emotion_check_time > emotion_update_interval:
 
            csv_files = glob.glob(os.path.join(output_dir, '*.csv'))
 
            if csv_files:
 
                latest_csv = max(csv_files, key=os.path.getctime)
 
                try:
 
                    df = pd.read_csv(latest_csv)
 
                    df.columns = df.columns.str.strip()
 
                    df['emotion'] = df.apply(map_emotion, axis=1)
 
                    if not df.empty:
                        emotion = df['emotion'].iloc[-1]
 
                except Exception as e:
                    print(f"CSV read skipped: {e}")
 
            last_emotion_check_time = time.time()
 
finally:
 
    print("Stopping OpenFace...")
 
    try:
 
        process.terminate()
 
        try:
            process.wait(timeout=10)
 
        except subprocess.TimeoutExpired:
 
            print("Force killing OpenFace...")
 
            process.kill()
 
    except Exception as e:
        print(f"Process shutdown error: {e}")
 
    try:
        monitor_thread.join(timeout=2)
 
    except Exception as e:
        print(f"Monitor thread join error: {e}")
 
    try:
 
        if my_eyetracker:
 
            my_eyetracker.unsubscribe_from(
                tr.EYETRACKER_GAZE_DATA,
                gaze_data_callback
            )
 
    except Exception as e:
        print(f"Eyetracker unsubscribe error: {e}")
 
    pygame.quit()
 
    time.sleep(1)
 
    # Save after OpenFace has stopped (its CSV is complete and closed) and before the cleanup
    # below deletes it.
    save_emotion_data()
 
    print("Recording stopped.")
 
    # Cleanup output directory
    if os.path.exists(output_dir):
 
        for filename in os.listdir(output_dir):
 
            file_path = os.path.join(output_dir, filename)
 
            for attempt in range(5):
 
                try:
 
                    os.remove(file_path)
 
                    print(f"Deleted file: {filename}")
 
                    break
 
                except PermissionError:
 
                    print(f"File locked: {filename}, retrying...")
 
                    time.sleep(1)
 
                except Exception as e:
 
                    print(f"Error deleting {filename}: {e}")
 
                    break
 
        try:
 
            os.rmdir(output_dir)
 
        except OSError as e:
 
            print(f"Could not remove output directory: {e}")
 
    print("Gaze and emotion overlay visualization stopped.")
