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
    # measured on the 28 Sep screen recordings (video px at 1920x1080 / screen px)
    cases = [(1.5, 1920, 1080, (1605, 765, 1917, 1077)),
             (1.32, 1920, 1080, (1618, 778, 1917, 1077)),
             (0.3, 2560, 1440, (2268, 1148, 2556, 1436)),
             (1.0, 2560, 1440, (2187, 1067, 2557, 1437))]
    for scale, w, h, (x0, y0, x1, y1) in cases:
        r = lc.estimate_minimap_rect(scale, w, h)
        assert abs(r["x0"] - x0) <= 8 and abs(r["y0"] - y0) <= 8, (scale, r)
        assert abs(r["x1"] - x1) <= 4 and abs(r["y1"] - y1) <= 4, (scale, r)
        assert r["x1"] - r["x0"] == r["y1"] - r["y0"]  # square
    assert lc.estimate_minimap_rect(None, 1920, 1080) is None
    assert lc.estimate_minimap_rect("x", 1920, 1080) is None
