"""Staying out of the game's way (10 Nov 2026): hooks, priorities, OpenFace's core limit.

What can be checked without Windows: that nothing in the capture path asks the ``keyboard``
package for a key (its first call installs a system-wide hook), the abort-key and core
arithmetic, and that the face recorder records how OpenFace was limited. What the limits
do to frame rate and input delay is T14 in TESTING.md, on a real PC.
"""

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import session  # noqa: E402
from test_capture_geometry import data, openface  # noqa: E402,F401  (fixtures)


@pytest.fixture()
def tripwire(monkeypatch):
    """A ``keyboard`` module that fails the test the moment anything asks it for a key."""
    fake = types.ModuleType("keyboard")

    def boom(*a, **k):
        raise AssertionError("the keyboard package was asked for a key: that installs a hook")
    fake.is_pressed = fake.wait = fake.add_hotkey = boom
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    return fake


def test_a_recorder_under_the_orchestrator_only_watches_the_stop_file(data, tripwire, monkeypatch):  # noqa: F811
    stop = data / ".stop-s1"
    monkeypatch.setenv(session.ENV_STOP, str(stop))
    assert session.under_orchestrator() and not session.stop_requested()
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("now")
    assert session.stop_requested()


def test_by_hand_the_abort_key_is_read_without_the_keyboard_package(data, tripwire, monkeypatch):  # noqa: F811
    monkeypatch.delenv(session.ENV_STOP, raising=False)
    monkeypatch.setenv(session.ENV_SESSION, "s2")
    seen = []
    monkeypatch.setattr(session, "key_down", lambda name: seen.append(name) or True)
    assert session.stop_requested() and seen == ["f12"]
    monkeypatch.setenv(session.ENV_ABORT_KEY, "off")
    seen.clear()
    assert not session.stop_requested() and seen == []


def test_abort_key_setting(monkeypatch):
    monkeypatch.delenv(session.ENV_ABORT_KEY, raising=False)
    monkeypatch.setenv(session.ENV_GAME, "league")
    assert session.abort_key() == "f12"
    for off in ("off", "none", "0", ""):
        monkeypatch.setenv(session.ENV_ABORT_KEY, off)
        assert session.abort_key() is None
    monkeypatch.setenv(session.ENV_ABORT_KEY, "F9")
    assert session.abort_key() == "f9"
    monkeypatch.setenv(session.ENV_ABORT_KEY, "q")            # a game key is never an abort key
    assert session.abort_key() == "f12"
    monkeypatch.setenv(session.ENV_ABORT_KEY, "off")
    monkeypatch.setenv(session.ENV_GAME, "valorant")          # that title ends its session with the key
    assert session.abort_key() == "f12"
    assert session.key_down("f12") is False or sys.platform == "win32"
    assert session.key_down("q") is False


def test_no_capture_script_asks_the_keyboard_package_for_a_key():
    """One keyboard hook per game: pynput's, in the input recorder. The scripts the
    orchestrator starts for League must not bring a second one."""
    import ast
    for name in ("session.py", "key_listener.py", "keyboard_recording.py", "microphone_recording.py",
                 "eye_tracking_script.py", "face_recorder.py", "liveclient_recorder.py", "display_watch.py",
                 "obs_recorder.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert "keyboard" not in [a.name for a in node.names], name
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "keyboard":
                assert node.attr not in ("is_pressed", "wait", "add_hotkey", "hook", "on_press"), name
    mic = (SRC / "microphone_recording.py").read_text(encoding="utf-8")
    assert "pynput" not in "\n".join(ln for ln in mic.splitlines() if ln.startswith(("import", "from")))


def test_priorities_say_what_each_recorder_is_to_the_game():
    p = session.RECORDER_PRIORITY
    assert p["input"] == "above"                      # in the input path: must never wait for a core
    assert p["audio"] == "normal" and p["gaze"] == "normal"   # device-fed: starving them loses samples
    assert p["face"] == "below"
    if os.name != "nt":
        assert session.priority_flags("below") == 0 and session.set_priority("below") is False
    else:
        assert session.priority_flags("normal") == 0x20 and session.priority_flags("below") == 0x4000


def test_openface_gets_a_quarter_of_the_machine_from_the_far_end(monkeypatch):
    monkeypatch.delenv(session.ENV_FACE_CORES, raising=False)
    assert [session.face_cores(n) for n in (2, 4, 6, 8, 12, 16, 20, 32)] == [1, 1, 1, 2, 3, 4, 4, 4]
    assert session.core_mask(2, 8) == 0b11000000            # the last physical core's two threads
    assert session.core_mask(4, 20) == 0b1111 << 16         # the efficiency cores of a 8P+4E CPU
    assert session.core_mask(9, 4) == 0b1111                # never more than there are
    monkeypatch.setenv(session.ENV_FACE_CORES, "all")
    assert session.face_cores(12) == 0
    monkeypatch.setenv(session.ENV_FACE_CORES, "6")
    assert session.face_cores(12) == 6 and session.face_cores(4) == 4
    monkeypatch.setenv(session.ENV_FACE_CORES, "many")
    assert session.face_cores(12) == 3
    monkeypatch.setenv(session.ENV_FACE_PRIORITY, "realtime")
    assert session.face_priority() == "below"
    monkeypatch.setenv(session.ENV_FACE_PRIORITY, "idle")
    assert session.face_priority() == "idle"


@pytest.mark.skipif(os.name == "nt", reason="the fake OpenFace is a shebang script")
def test_face_recorder_records_how_openface_was_limited(data, openface, monkeypatch, tmp_path):  # noqa: F811
    import face_recorder
    monkeypatch.setenv("PLAYSMART_FACE", "1")
    monkeypatch.setenv(session.ENV_FACE_CORES, "2")
    assert face_recorder.openface_env(2)["OPENBLAS_NUM_THREADS"] == "2"
    assert "OPENBLAS_NUM_THREADS" not in face_recorder.openface_env(0) or os.environ.get("OPENBLAS_NUM_THREADS")
    sid = "20261110-150000-load01"
    rec = face_recorder.FaceRecorder(sid, scratch=tmp_path / "scratch" / sid)
    assert rec.start()
    t_end = time.time() + 2.0
    while time.time() < t_end:
        time.sleep(0.25)
        rec.poll()
    info = rec.stop()
    saved = json.loads((data / "face" / f"{sid}_face.json").read_text())
    assert saved["priority"] == "below" and saved["cpu_logical"] == os.cpu_count() and saved["model"] == "default"
    assert saved["cpu_cores"] is None                 # not Windows: the limit was not applied, and it says so
    # the clock cross-check is the tightest arrival, so a late-running OpenFace cannot move it
    rec.arrivals = [info["anchor_unix_ms"] + 40.0] + [info["anchor_unix_ms"] + 6000.0] * 500
    assert min(rec.arrivals) - info["anchor_unix_ms"] < 250


def test_fast_model_only_when_asked_and_installed(openface, monkeypatch, capsys):  # noqa: F811
    import face_recorder
    monkeypatch.delenv("PLAYSMART_FACE_MODEL", raising=False)
    assert "-mloc" not in face_recorder.command(openface, Path("out"), "s")
    monkeypatch.setenv("PLAYSMART_FACE_MODEL", "fast")
    assert "-mloc" not in face_recorder.command(openface, Path("out"), "s")
    assert "not in the OpenFace folder" in capsys.readouterr().out
    (openface.parent / "model").mkdir()
    (openface.parent / "model" / "main_clnf_general.txt").write_text("x")
    cmd = face_recorder.command(openface, Path("out"), "s")
    assert cmd[cmd.index("-mloc") + 1] == "model/main_clnf_general.txt"
