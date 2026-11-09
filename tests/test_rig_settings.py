"""apply_rig_settings: game.cfg edits are minimal and keep line endings."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import apply_rig_settings as rig  # noqa: E402


def test_changes_only_the_standard_keys_and_keeps_crlf():
    text = "[General]\r\nWidth=2560\r\n\r\n[HUD]\r\nMinimapScale=0.3000\r\nGlobalScale=0.5\r\n"
    new, changes = rig.apply(text, {("HUD", "MinimapScale"): "1.0000"})
    assert changes == [("HUD", "MinimapScale", "0.3000", "1.0000")]
    assert new == "[General]\r\nWidth=2560\r\n\r\n[HUD]\r\nMinimapScale=1.0000\r\nGlobalScale=0.5\r\n"


def test_adds_a_missing_key_and_section():
    new, changes = rig.apply("[General]\nWidth=1\n", {("HUD", "MinimapScale"): "1.0000"})
    assert changes == [("HUD", "MinimapScale", "(absent)", "1.0000")]
    assert rig.read_values(new)[("HUD", "MinimapScale")] == "1.0000"


def test_no_change_when_on_standard():
    text = "[HUD]\nMinimapScale=1.0000\n"
    assert rig.apply(text, {("HUD", "MinimapScale"): "1.0000"}) == (text, [])
