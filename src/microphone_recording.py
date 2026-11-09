"""Microphone recorder: 44.1 kHz mono WAV to data/audio/<session_id>_audio.wav.

Modified 2026-09 (iteration 5): file named from the shared session id; stops on the
orchestrator's stop signal so the SoundFile context closes and the WAV header is
finalised (being terminated left 44-byte files); Whisper transcription is off unless
PLAYSMART_TRANSCRIBE=1 and needs `poetry install --with transcription`.
Modified 2026-09-23: writes <sid>_audio.json with the wall-clock time of the first
sample, so the audio can be placed on the game clock.
"""

import sounddevice as sd
import soundfile as sf
import datetime
import json
import os
import sys
import time
from pathlib import Path
from pynput import keyboard

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session

samplerate = 44100
channels = 1
recording = True  # control flag

# Transcription is off by default. It used to run inline, right after the
# recording loop, while the orchestrator was terminating this process two
# seconds later — so Whisper was always killed, and worse, killing it before
# the SoundFile context manager closed left an unfinalised WAV header. That is
# what the 44-byte "empty" WAVs in the old archive are. Transcribe afterwards
# instead: python src/microphone_recording.py --transcribe <file.wav>
TRANSCRIBE = os.environ.get("PLAYSMART_TRANSCRIBE", "0") == "1"

# ------------------ PATH SETUP ------------------
filepath = str(session.stream_path("audio", ext="wav"))

print(f"Recording microphone audio to: {filepath}")
print("Stops when the game ends, or on F12.")

# ------------------ KEY LISTENER ------------------
def on_press(key):
    global recording
    if key == keyboard.Key.f12:
        print("\nF12 pressed -> stopping recording...")
        recording = False
        return False  # stop listener

listener = keyboard.Listener(on_press=on_press)
listener.start()

# ------------------ RECORD AUDIO ------------------
# Leaving this `with` block is what finalises the WAV header, so the loop must
# be allowed to exit normally rather than the process being killed.
#
# Timing anchor: a WAV has no timestamps, so without one the audio cannot be placed on
# the game clock. The first sample of the first block was captured one block plus the
# input latency before that read returned; that instant goes to <sid>_audio.json as
# t0_unix_ms (the same convention as obs_recorder's <sid>_video.json). frames_written
# and t_end_unix_ms let the merge check for drift; overflows counts blocks where the
# device dropped samples, which would shift everything after them.
BLOCK = 1024
t0_ms = None
frames_written = 0
overflows = 0
with sf.SoundFile(filepath, mode='w', samplerate=samplerate, channels=channels) as file:
    with sd.InputStream(samplerate=samplerate, channels=channels) as stream:
        while recording and not session.stop_requested():
            data, overflowed = stream.read(BLOCK)
            if t0_ms is None:
                latency = stream.latency if isinstance(stream.latency, float) else 0.0
                t0_ms = time.time() * 1000 - (BLOCK / samplerate + latency) * 1000
            overflows += int(bool(overflowed))
            file.write(data)
            frames_written += len(data)

t_end_ms = time.time() * 1000
anchor = session.stream_path("audio", ext="json")
anchor.write_text(json.dumps({
    "session_id": session.session_id(),
    "file": Path(filepath).name,
    "t0_unix_ms": round(t0_ms, 1) if t0_ms is not None else None,
    "t_end_unix_ms": round(t_end_ms, 1),
    "samplerate": samplerate,
    "frames_written": frames_written,
    "overflows": overflows,
    "method": "first block read time minus block length and input latency",
}, indent=2), encoding="utf-8")

print(f"Recording stopped, saved {filepath}")
if t0_ms is not None:
    drift_ms = (t_end_ms - t0_ms) - frames_written / samplerate * 1000
    print(f"audio timing: frame zero at {t0_ms:.0f}, {overflows} overflow(s), "
          f"wall vs samples differ by {drift_ms:.0f} ms at the end")

# ------------------ TRANSCRIPTION ------------------
if not TRANSCRIBE:
    print("Skipping transcription (set PLAYSMART_TRANSCRIBE=1 to enable).")
    sys.exit(0)

import whisper  # noqa: E402  (imported late: loading it costs seconds we do not want at capture time)

print("Loading Whisper model...")
model = whisper.load_model("base")  # tiny/base/small/medium/large

print("Transcribing audio...")
result = model.transcribe(filepath)

segments = result["segments"]

# ------------------ FORMAT TRANSCRIPT ------------------
def format_time(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))

formatted_transcript = ""

for seg in segments:
    start = format_time(seg["start"])
    end = format_time(seg["end"])
    text = seg["text"].strip()

    formatted_transcript += f"[{start} - {end}] {text}\n"

print("Transcription with timestamps:\n")
print(formatted_transcript)

# ------------------ SAVE TRANSCRIPT ------------------
txt_path = filepath.replace(".wav", ".txt")

with open(txt_path, "w", encoding="utf-8") as f:
    f.write(formatted_transcript)

print(f"Transcript saved to: {txt_path}")