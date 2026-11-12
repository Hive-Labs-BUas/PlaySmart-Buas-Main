"""Guards added 14 Oct 2026 after the first full merge of the 28 Sep sessions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import liveclient_recorder as lc  # noqa: E402


def payload(champion="Ahri", t=12.0):
    return {"activePlayer": {"riotId": "A#1"},
            "allPlayers": [{"riotId": "A#1", "championName": champion}],
            "gameData": {"gameTime": t}}


def test_post_game_screen_is_not_live():
    # Pc_5, 28 Sep 2026 20:26: the API answered with an empty champion for 58 rows
    assert not lc.is_live(payload(champion=""))


def test_frozen_clock_is_not_live_but_loading_screen_is():
    assert not lc.is_live(payload(t=1500.0), payload(t=1500.0))
    assert lc.is_live(payload(t=0), payload(t=0))          # loading screen
    assert lc.is_live(payload(t=12.5), payload(t=12.0))
    assert not lc.is_live(payload(t=12.0), payload(t=12.5))  # clock went backwards


def test_minimap_rect_estimate_matches_measured_rigs():
    # The map square inside the rim, measured by the app's rim detector on the rigs'
    # own recordings (10 Nov 2026): (MinimapScale, video w, h, x0, y0, side).
    cases = [(0.3, 1920, 1080, 1710, 869, 191),      # Pc_5, 28 Sep
             (0.9, 1280, 720, 1115, 553, 150),       # Pc_1, 28 Sep
             (0.9, 3440, 1440, 3110, 1106, 299),     # home PC, ultrawide, 30 Oct
             (1.0, 2560, 1440, 2222, 1099, 307),     # Pc_2, 28 Sep
             (1.32, 1920, 1080, 1646, 804, 249),     # Pc_3, 28 Sep
             (1.5, 1920, 1080, 1634, 792, 260)]      # Pc_4, 28 Sep
    for scale, w, h, x0, y0, side in cases:
        r = lc.estimate_minimap_rect(scale, w, h)
        assert abs(r["x0"] - x0) <= 4 and abs(r["y0"] - y0) <= 4, (scale, r)
        assert abs((r["x1"] - r["x0"]) - side) <= 3, (scale, r)
        assert abs((r["x1"] - r["x0"]) - (r["y1"] - r["y0"])) <= 1  # square
    assert lc.estimate_minimap_rect(None, 1920, 1080) is None
    assert lc.estimate_minimap_rect("x", 1920, 1080) is None
