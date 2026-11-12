"""Resolution changes, OBS fit, game-audio split and the face recorder (10 Nov 2026).

None of this needs OBS, a Tobii, a webcam or Windows: OBS is a fake websocket client, the
geometry reader is a function, OpenFace is a script that writes OpenFace-shaped rows, and
the recording is a few seconds of synthetic video made with ffmpeg.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import display_watch  # noqa: E402
import obs_recorder  # noqa: E402
import session  # noqa: E402


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYSMART_DATA_DIR", str(tmp_path / "data"))
    for key in ("PLAYSMART_SKIP", "PLAYSMART_FACE", "PLAYSMART_OPENFACE", "PLAYSMART_OPENFACE_EXE",
                "PLAYSMART_GAME_AUDIO", "PLAYSMART_FFMPEG", "PLAYSMART_OBS_FIT"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "data"


# ----------------------------------------------------------------------------- display
def test_display_watch_keeps_one_segment_per_change(data):
    frames = iter([
        ((1920, 1080), {"x": 0, "y": 0, "w": 1920, "h": 1080, "hidden": False}),
        ((1920, 1080), {"x": 0, "y": 0, "w": 1920, "h": 1080, "hidden": False}),
        ((1920, 1080), {"x": 0, "y": 0, "w": 1920, "h": 1080, "hidden": True}),      # alt-tab
        ((2560, 1440), {"x": 0, "y": 0, "w": 2560, "h": 1440, "hidden": False}),     # resolution changed
        ((2560, 1440), {"x": 320, "y": 180, "w": 1920, "h": 1080, "hidden": False}),  # windowed
    ])
    w = display_watch.Watcher("s1", out_dir=data / "gamestate", read=lambda: display_watch.classify(*next(frames)))
    for i in range(5):
        w.poll(now_ms=1000 + i * 1000)
    info = w.stop()
    assert [s["state"] for s in info["segments"]] == ["fullscreen", "hidden", "fullscreen", "windowed"]
    assert info["segments"][2]["t_unix_ms"] == 4000
    assert info["segments"][3]["window"] == [320, 180, 1920, 1080]
    assert info["changed"] is True and info["changes"] == 2          # alt-tab is not a change
    assert info["screen_px_start"] == [1920, 1080] and info["game_px_start"] == [1920, 1080]
    assert json.loads((data / "gamestate" / "s1_display.json").read_text())["samples"] == 5


def test_display_watch_unchanged_session(data):
    w = display_watch.Watcher("s2", out_dir=data / "gamestate",
                              read=lambda: display_watch.classify((2560, 1440), {"x": 0, "y": 0, "w": 2560, "h": 1440}))
    for _ in range(10):
        w.poll()
    assert w.live() == {"screen_px": [2560, 1440], "game_px": [2560, 1440], "state": "fullscreen", "changes": 0}
    info = w.stop()
    assert info["changed"] is False and len(info["segments"]) == 1


def test_display_watch_no_game_window(data):
    w = display_watch.Watcher("s3", out_dir=data / "gamestate", read=lambda: display_watch.classify((1920, 1080), None))
    w.poll()
    info = w.stop()
    assert info["segments"][0]["state"] == "absent" and info["game_px_start"] is None and info["changed"] is False


# ----------------------------------------------------------------------------- OBS fit
class FakeOBS:
    """The handful of obs-websocket requests fit() and the geometry readers use."""

    def __init__(self, canvas=(2560, 1440), output=(2560, 1440), source=(1920, 1080), bounds=(1920, 1080), refuse=False):
        self.video = SimpleNamespace(base_width=canvas[0], base_height=canvas[1], output_width=output[0],
                                     output_height=output[1], fps_numerator=30, fps_denominator=1)
        self.transform = {"sourceWidth": float(source[0]), "sourceHeight": float(source[1]),
                          "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": float(bounds[0]),
                          "boundsHeight": float(bounds[1]), "positionX": 0.0, "positionY": 0.0,
                          "alignment": 5, "boundsAlignment": 0, "rotation": 0.0,
                          "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0}
        self.refuse = refuse
        self.calls = []

    def get_video_settings(self):
        return self.video

    def set_video_settings(self, n, d, bw, bh, ow, oh):
        if self.refuse:
            raise RuntimeError("output active")
        self.calls.append(("video", bw, bh, ow, oh))
        self.video = SimpleNamespace(base_width=bw, base_height=bh, output_width=ow, output_height=oh,
                                     fps_numerator=n, fps_denominator=d)

    def get_current_program_scene(self):
        return SimpleNamespace(current_program_scene_name=obs_recorder.SCENE)

    def get_scene_item_list(self, scene):
        return SimpleNamespace(scene_items=[{"sceneItemId": 7, "sourceName": obs_recorder.SOURCE}])

    def get_scene_item_transform(self, scene, item):
        return SimpleNamespace(scene_item_transform=dict(self.transform))

    def set_scene_item_transform(self, scene, item, tr):
        self.calls.append(("transform", tr["boundsWidth"], tr["boundsHeight"]))
        self.transform.update(tr)


def test_fit_resizes_canvas_and_capture_to_the_screen_now():
    """9 Nov: the screen had been set to 1080p, OBS was still on a 1440p canvas."""
    obs = FakeOBS(canvas=(2560, 1440), output=(2560, 1440), bounds=(1920, 1080))
    res = obs_recorder.fit(obs, screen=(1920, 1080))
    assert res["canvas"] == [1920, 1080] and res["output"] == [1920, 1080] and "error" not in res
    assert ("video", 1920, 1080, 1920, 1080) in obs.calls
    assert len(res["changes"]) == 1                      # the bounds already matched the new canvas
    assert obs_recorder.fit(obs, screen=(1920, 1080))["changes"] == []      # idempotent


def test_fit_when_obs_refuses_still_fills_the_canvas_it_has():
    """The old failure: capture sized to the screen inside a larger canvas -> game in a corner."""
    obs = FakeOBS(canvas=(2560, 1440), output=(2560, 1440), bounds=(1920, 1080), refuse=True)
    res = obs_recorder.fit(obs, screen=(1920, 1080))
    assert "refused" in res["error"] and res["canvas"] == [2560, 1440]
    assert ("transform", 2560.0, 1440.0) in obs.calls    # stretched over the canvas that is in force


def test_fit_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PLAYSMART_OBS_FIT", "0")
    obs = FakeOBS(canvas=(2560, 1440), output=(2560, 1440), bounds=(2560, 1440))
    res = obs_recorder.fit(obs, screen=(1920, 1080))
    assert res["canvas"] == [2560, 1440] and res["changes"] == []


def test_game_source_and_frame_in_video():
    assert obs_recorder.game_source_px(FakeOBS(source=(1920, 1080))) == [1920, 1080]
    assert obs_recorder.game_source_px(FakeOBS(source=(0, 0))) is None           # nothing hooked yet
    assert obs_recorder.frame_in_video([1920, 1080], [2560, 1440]) == [0.0, 0.0, 2560.0, 1440.0]
    # a 16:9 game on an ultrawide canvas is pillarboxed, centred
    assert obs_recorder.frame_in_video([2560, 1440], [3440, 1440]) == [440.0, 0.0, 2560.0, 1440.0]
    assert obs_recorder.frame_in_video(None, [1920, 1080]) is None


# ----------------------------------------------------------------------------- game audio
def test_game_audio_stream_follows_the_track_layout():
    lay = {"audio_tracks": {"tracks": {"1": ["League Game Audio"], "2": ["Mic/Aux"]}, "rec_tracks": 3}}
    assert obs_recorder.game_audio_stream(lay) == {"index": 0, "track": 1, "inputs": ["League Game Audio"],
                                                   "game_only": True, "assumed": False}
    swapped = {"audio_tracks": {"tracks": {"1": ["Mic/Aux"], "2": ["League Game Audio"]}, "rec_tracks": 3}}
    assert obs_recorder.game_audio_stream(swapped)["index"] == 1
    mixed = {"audio_tracks": {"tracks": {"1": ["League Game Audio", "Mic/Aux"]}, "rec_tracks": 1}}
    assert obs_recorder.game_audio_stream(mixed)["game_only"] is False
    assert obs_recorder.game_audio_stream({}) == {"index": 0, "track": None, "inputs": [], "game_only": False, "assumed": True}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_extract_game_audio_takes_the_game_track_not_the_mic(data, monkeypatch):
    monkeypatch.setenv("PLAYSMART_FFMPEG", shutil.which("ffmpeg"))
    sid = "20261110-120000-abcdef"
    vdir = data / "video"
    vdir.mkdir(parents=True)
    video = vdir / f"{sid}_video.mkv"
    # 2 s of video; audio stream 0 = 440 Hz ("game"), stream 1 = silence ("mic")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "mpeg4", "-c:a", "aac", str(video)], check=True)
    (vdir / f"{sid}_video.json").write_text(json.dumps({
        "t0_unix_ms": 1794000000000.0,
        "audio_tracks": {"tracks": {"1": ["League Game Audio"], "2": ["Mic/Aux"]}, "rec_tracks": 3}}))
    wav = obs_recorder.extract_game_audio(sid)
    assert wav == data / "gameaudio" / f"{sid}_gameaudio.wav"
    with wave.open(str(wav), "rb") as wf:
        assert wf.getframerate() == 48000 and 1.8 < wf.getnframes() / 48000 < 2.3
        raw = wf.readframes(wf.getnframes())
    assert max(raw) > 0                                      # the tone, not the silent mic track
    info = json.loads(wav.with_suffix(".json").read_text())
    assert info["t0_unix_ms"] == 1794000000000.0 and info["game_only"] is True and info["source_stream"] == "0:a:0"
    assert not list((data / "gameaudio").glob("*.part.wav"))
    stamp = wav.stat().st_mtime_ns
    assert obs_recorder.extract_game_audio(sid) == wav and wav.stat().st_mtime_ns == stamp   # kept, not redone


def test_extract_game_audio_without_ffmpeg_says_so(data, monkeypatch, capsys):
    monkeypatch.setattr(obs_recorder, "ffmpeg_exe", lambda: None)
    (data / "video").mkdir(parents=True)
    (data / "video" / "s9_video.mkv").write_bytes(b"x")
    assert obs_recorder.extract_game_audio("s9") is None
    assert "no ffmpeg" in capsys.readouterr().out


def test_extract_game_audio_off(data, monkeypatch):
    monkeypatch.setenv("PLAYSMART_GAME_AUDIO", "off")
    assert obs_recorder.extract_game_audio("nothing") is None


# ----------------------------------------------------------------------------- face
FAKE_OPENFACE = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Writes OpenFace-shaped rows at ~30 fps until terminated; never writes an image."""
    import sys, time
    from pathlib import Path
    a = sys.argv
    out = Path(a[a.index("-out_dir") + 1]); name = a[a.index("-of") + 1]
    assert "-aus" in a and "-pose" in a and "-gaze" in a
    assert not {"-simalign", "-hogalign", "-tracked", "-2Dfp", "-3Dfp"} & set(a)
    out.mkdir(parents=True, exist_ok=True)
    time.sleep(0.4)                       # model loading: the clock starts after this
    print("Starting tracking", flush=True)
    cols = ["frame", "face_id", "timestamp", "confidence", "success", "gaze_0_x", "gaze_angle_x",
            "pose_Tx", "pose_Rx", "AU01_r", "AU04_r", "AU45_r", "AU45_c"]
    for axis in ("x", "y", "X", "Y", "Z"):
        cols += [f"eye_lmk_{axis}_{i}" for i in range(56)]
    with open(out / (name + ".csv"), "w") as fh:
        fh.write(", ".join(cols) + "\\n")
        for n in range(1, 100000):
            t = (n - 1) / 30.0
            row = [n, 0, round(t, 3), 0.95, 1 if n % 10 else 0, 0.1, 0.05, 12.0, 0.2, 0.5, 1.5, 0.0, 0]
            for axis in ("x", "y", "X", "Y", "Z"):
                # eyelid contour: 30 px wide, 9 px high -> aperture 0.3
                row += [(i % 12) * 30 / 11 if axis == "x" else ((i % 2) * 9 if axis == "y" else 1.0) for i in range(56)]
            fh.write(", ".join(str(v) for v in row) + "\\n"); fh.flush()
            time.sleep(1 / 30.0)
''')


@pytest.fixture()
def openface(tmp_path, monkeypatch):
    folder = tmp_path / session.OPENFACE_DIR
    folder.mkdir()
    exe = folder / "FeatureExtraction.exe"
    exe.write_text(FAKE_OPENFACE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PLAYSMART_OPENFACE_EXE", str(exe))
    return exe


def test_face_is_opt_in_and_independent_of_the_tier(data, openface, monkeypatch):
    assert session.openface_exe() == openface
    assert "face" in session.skip_set("lite") and "face" in session.skip_set("full")      # off by default
    monkeypatch.setenv("PLAYSMART_FACE", "1")
    assert "face" not in session.skip_set("lite") and "gaze" in session.skip_set("lite")  # no Tobii needed
    monkeypatch.setenv("PLAYSMART_SKIP", "eda,emotion")
    assert "face" in session.skip_set("full")                                             # the old name still wins
    monkeypatch.setenv("PLAYSMART_SKIP", "eda")
    monkeypatch.setenv("PLAYSMART_OPENFACE_EXE", str(openface.parent / "missing.exe"))
    monkeypatch.setattr(session, "REPO", data / "a" / "b")  # nowhere else to find it
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert session.openface_exe() is None and "face" in session.skip_set("full")          # not installed


@pytest.mark.skipif(os.name == "nt", reason="the fake OpenFace is a shebang script")
def test_face_recorder_end_to_end(data, openface, monkeypatch, tmp_path):
    import pandas as pd

    import face_recorder
    monkeypatch.setenv("PLAYSMART_FACE", "1")
    sid = "20261110-130000-face01"
    rec = face_recorder.FaceRecorder(sid, scratch=tmp_path / "scratch" / sid)
    assert rec.start()
    t_end = time.time() + 3.0
    while time.time() < t_end:
        time.sleep(0.25)
        rec.poll()
    prog = json.loads(rec.progress_path().read_text())
    assert prog["running"] and prog["tracking"] and prog["rows"] > 20 and 0.7 < prog["success_recent"] <= 1.0
    info = rec.stop()
    df = pd.read_csv(data / "face" / f"{sid}_face.csv")
    assert info["rows"] == len(df) > 40 and 25 < info["fps"] < 35
    assert info["images_written"] is False and info["emotion_labels"] is None
    assert info["anchor_source"] == "starting_tracking_line"
    # the anchor is OpenFace's own start, not the script's: rows are stamped ~0.4 s after the script began
    assert 300 < df.unix_time.iloc[0] - info["script_start_unix_ms"] < 1500
    assert abs(info["anchor_check_minus_anchor_ms"]) < 250
    assert {"AU01_r", "AU04_r", "AU45_r", "AU45_c", "pose_Tx", "gaze_angle_x", "eye_aperture_0", "eye_aperture_1"} <= set(df.columns)
    assert not [c for c in df.columns if c.startswith("eye_lmk_")]            # landmarks reduced, not kept
    assert abs(df.eye_aperture_0.iloc[5] - 0.3) < 0.01
    assert not (tmp_path / "scratch" / sid).exists()                           # raw copy removed
    assert not rec.progress_path().exists()
    assert not [p for p in data.rglob("*") if p.suffix.lower() in (".bmp", ".jpg", ".png", ".avi", ".mp4")]


def test_face_recorder_without_openface_reports_and_exits(data, monkeypatch, capsys):
    import face_recorder
    monkeypatch.setattr(session, "openface_exe", lambda: None)
    rec = face_recorder.FaceRecorder("sidx")
    assert rec.start() is False
    assert "not found" in capsys.readouterr().out


def test_skipped_streams_in_meta_are_the_resolved_set(data, monkeypatch):
    monkeypatch.setenv("PLAYSMART_SKIP", "eda")
    assert sorted(session.skip_set("lite")) == ["eda", "emotion", "face", "gaze"]
    assert sorted(session.skip_set("full")) == ["eda", "emotion", "face"]


def test_refresh_env_follows_the_file_but_not_over_the_shell(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PLAYSMART_FACE=0\nPLAYSMART_SKIP=eda,face\n")
    monkeypatch.delenv("PLAYSMART_FACE", raising=False)
    monkeypatch.setenv("PLAYSMART_SKIP", "eda")              # set in the shell
    monkeypatch.setattr(session, "_from_file", set())
    assert session.load_env_file(env) == 1 and os.environ["PLAYSMART_FACE"] == "0"
    env.write_text("PLAYSMART_FACE=1\nPLAYSMART_SKIP=eda\n")  # the app switched face on
    assert session.refresh_env(("PLAYSMART_FACE", "PLAYSMART_SKIP"), env) == {"PLAYSMART_FACE": "1"}
    assert os.environ["PLAYSMART_FACE"] == "1" and os.environ["PLAYSMART_SKIP"] == "eda"
    env.write_text("PLAYSMART_SKIP=eda\n")                    # line removed
    assert session.refresh_env(("PLAYSMART_FACE",), env) == {"PLAYSMART_FACE": None}
    assert "PLAYSMART_FACE" not in os.environ
