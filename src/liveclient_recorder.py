"""Record League game state next to the other capture streams.

Modified 2026-10-14, after the first full merge of the 28 Sep sessions:
* ``wait_for_game`` only accepts a payload that is a live game: the active player has a
  champion and the game clock advances between two polls. The API keeps answering on
  the post-game screen; a session armed there (Pc_5, 20:26 on 28 Sep) recorded 58 rows
  of an empty champion with the clock jumping about.
* a game is declared over after 5 s without an answer, not after ten timed-out polls
  (up to 25 s), so audio and video stop with the game instead of running on.
* the meta records ``late_start_s`` (the game time of the first row) and the console
  shouts when it is above half a second: the recorder was armed after the loading
  screen and that much of the game is missing.
* ``minimap_rect_px`` is read from ``<data>/json/minimap_rect.json`` (it looked only
  under the working directory before, so a measured rectangle was never picked up),
  and when nobody measured one it is **estimated from the client's MinimapScale** and
  the screen size, marked ``"source": "estimated"``. The fit comes from five rigs
  measured on their screen recordings (scales 0.3-1.5): side = (0.100 + 0.042 x scale)
  x screen width, anchored 0.16 % of the width from the bottom-right corner.

Modified 2026-09-25: keeps more of the payload (combat stats, ability levels, items,
skins, spells, runes), writes ``<sid>_players.csv`` with every player's state, and
adds the client's settings, queue and tier to the meta (``lcu.client_context``).

The League game process serves a small HTTPS API on ``https://127.0.0.1:2999``
for as long as a game is running. No API key, no rate limit, no network — it is
the local game talking to us. It answers in Practice Tool and customs as well as
matchmade games, which the Match-V5 web API does not.

What this buys the pipeline:

* **A clock mapping.** Every gamestate row carries ``unix_time_ms`` (the same
  base every other recorder stamps) *and* ``game_time_s`` (League's own clock).
  Sampled a few hundred times across a game, that pair lets any gaze sample,
  keypress or EDA reading be placed at "14:32 into the game" instead of
  "somewhere in a 74-minute recording".
* **Game boundaries.** The endpoint exists only during a game, so the
  orchestrator can bracket capture on it rather than on the operator noticing.
* **Labels for free.** Game mode, map and Riot ID come from the API instead of a
  free-text dropdown that produced nine spellings of two game names.

Note it does *not* give champion positions or the game id. The id is read from the
League client (``lcu.py``) into ``<sid>_meta.json`` so the post-game pipeline can find
the replay; positions come from rendering that replay (team repository,
``playsmart.replay``). Everything reconciles on game time.

Standalone (useful for testing without any lab hardware — open Practice Tool)::

    python src/liveclient_recorder.py --out data/gamestate --once

Imported by ``key_listener.py``::

    wait_for_game(...)  ->  record_game(sid, out_dir, stop_event)

The field names below follow Riot's documented payload but have not yet been
checked against a live game, so parsing is deliberately defensive: an unexpected
shape leaves a column blank rather than killing a capture session.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

try:
    import session as playsmart_session
except ImportError:  # running from the repo root rather than src/
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import session as playsmart_session
import lcu

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://127.0.0.1:2999/liveclientdata"
POLL_HZ = 2.0

# The client serves a self-signed certificate. Riot publish riotgames.pem; set
# this to that path to validate properly instead of skipping verification.
VERIFY: "str | bool" = False

# How many consecutive failed polls mean the game is over rather than hitching,
# and the wall-time cap on that: a poll that times out costs 2 s, so ten of them
# kept audio and video rolling for 25 s past the end of the game on 28 Sep 2026
# (Pc_1, Pc_5). Whichever comes first ends the recording.
MISS_LIMIT = 10
MISS_SECONDS = 5.0

STATE_COLUMNS = [
    "session_id", "unix_time_ms", "game_time_s",
    "riot_id", "champion", "team", "position",
    "level", "current_gold", "kills", "deaths", "assists",
    "creep_score", "ward_score", "is_dead", "respawn_timer",
    "hp", "max_hp", "resource", "max_resource",
    "team_kills_order", "team_kills_chaos",
    # Added 25 Sep 2026: the active player's combat stats, ability levels and
    # items at the poll rate. Movement speed and ranges are what a threat model
    # needs; Match-V5 only has them once a minute.
    "move_speed", "attack_range", "attack_speed", "attack_damage", "ability_power",
    "armor", "magic_resist", "ability_haste", "tenacity",
    "ability_levels", "items", "items_usable",
]

# One row per player per poll: everyone's level, death state, scores and items.
# No Riot IDs: champion + team is unique within a game.
PLAYER_COLUMNS = [
    "session_id", "unix_time_ms", "game_time_s",
    "champion", "team", "position", "level", "is_dead", "respawn_timer",
    "kills", "deaths", "assists", "creep_score", "ward_score",
    "items", "items_usable",
]

EVENT_COLUMNS = [
    "session_id", "unix_time_ms", "game_time_s",
    "event_id", "event_name",
    "killer", "victim", "assisters", "dragon_type", "stolen", "payload",
]


def now_ms() -> int:
    """Wall clock in milliseconds — the base every recorder shares."""
    return int(time.time() * 1000)


def poll(http: requests.Session) -> "dict | None":
    """One /allgamedata read. None means no game is running right now."""
    try:
        r = http.get(f"{BASE}/allgamedata", verify=VERIFY, timeout=2)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def dig(obj, *path, default=None):
    """Walk nested keys, returning default on any miss."""
    for key in path:
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return default
    return obj if obj is not None else default


def active_name(data: dict) -> str:
    """Riot ID of the player at this machine.

    Older clients expose ``summonerName`` and newer ones ``riotId``; accept both
    so this keeps working across a patch boundary.
    """
    return (dig(data, "activePlayer", "riotId")
            or dig(data, "activePlayer", "summonerName") or "")


def find_active(data: dict) -> dict:
    """The active player's entry in allPlayers, matched on Riot ID."""
    name = active_name(data)
    for p in dig(data, "allPlayers", default=[]) or []:
        if (p.get("riotId") or p.get("summonerName")) == name:
            return p
    return {}


def num(v, ndigits=1):
    """Round a value; "" when it is not numeric (a blank column, not a crash)."""
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return ""


def items_of(player: dict) -> "tuple[str, str]":
    """``("1055|3006|2003", "2003")``: item ids by slot, and the ones usable right now.

    ``canUse`` is the Live Client's word for an active item that is off cooldown
    (Zhonya's, Stopwatch, a potion). Whether the escape was *available* is part of
    "looked and misjudged".
    """
    items = sorted(player.get("items") or [], key=lambda i: i.get("slot", 0))
    ids = [str(i.get("itemID", "")) for i in items if i.get("itemID") is not None]
    usable = [str(i.get("itemID", "")) for i in items if i.get("canUse")]
    return "|".join(ids), "|".join(usable)


def ability_levels(data: dict) -> str:
    """``"1/0/1/0"``: Q/W/E/R levels of the active player."""
    ab = dig(data, "activePlayer", "abilities", default={}) or {}
    return "/".join(str((ab.get(k) or {}).get("abilityLevel", "")) for k in ("Q", "W", "E", "R"))


def player_rows(sid: str, data: dict, unix_ms: int, game_time) -> list:
    """One PLAYER_COLUMNS row per player in allPlayers."""
    rows = []
    for p in dig(data, "allPlayers", default=[]) or []:
        scores = p.get("scores", {}) or {}
        items, usable = items_of(p)
        rows.append({
            "session_id": sid,
            "unix_time_ms": unix_ms,
            "game_time_s": game_time,
            "champion": p.get("championName", ""),
            "team": p.get("team", ""),
            "position": p.get("position", ""),
            "level": p.get("level", ""),
            "is_dead": p.get("isDead", ""),
            "respawn_timer": num(p.get("respawnTimer", 0)),
            "kills": scores.get("kills", ""),
            "deaths": scores.get("deaths", ""),
            "assists": scores.get("assists", ""),
            "creep_score": scores.get("creepScore", ""),
            "ward_score": num(scores.get("wardScore", "")),
            "items": items,
            "items_usable": usable,
        })
    return rows


def state_row(sid: str, data: dict) -> dict:
    me = find_active(data)
    scores = me.get("scores", {}) or {}
    stats = dig(data, "activePlayer", "championStats", default={}) or {}

    kills = {"ORDER": 0, "CHAOS": 0}
    for p in dig(data, "allPlayers", default=[]) or []:
        team = p.get("team")
        if team in kills:
            kills[team] += int(dig(p, "scores", "kills", default=0) or 0)

    items, usable = items_of(me)
    return {
        "session_id": sid,
        "unix_time_ms": now_ms(),
        "game_time_s": num(dig(data, "gameData", "gameTime", default=0), 3),
        "riot_id": me.get("riotId") or me.get("summonerName") or active_name(data),
        "champion": me.get("championName", ""),
        "team": me.get("team", ""),
        "position": me.get("position", ""),
        "level": me.get("level", ""),
        "current_gold": num(dig(data, "activePlayer", "currentGold", default=0)),
        "kills": scores.get("kills", ""),
        "deaths": scores.get("deaths", ""),
        "assists": scores.get("assists", ""),
        "creep_score": scores.get("creepScore", ""),
        "ward_score": scores.get("wardScore", ""),
        "is_dead": me.get("isDead", ""),
        "respawn_timer": num(me.get("respawnTimer", 0)),
        "hp": num(stats.get("currentHealth", "")),
        "max_hp": num(stats.get("maxHealth", "")),
        "resource": num(stats.get("resourceValue", "")),
        "max_resource": num(stats.get("resourceMax", "")),
        "team_kills_order": kills["ORDER"],
        "team_kills_chaos": kills["CHAOS"],
        "move_speed": num(stats.get("moveSpeed", "")),
        "attack_range": num(stats.get("attackRange", "")),
        "attack_speed": num(stats.get("attackSpeed", ""), 3),
        "attack_damage": num(stats.get("attackDamage", "")),
        "ability_power": num(stats.get("abilityPower", "")),
        "armor": num(stats.get("armor", "")),
        "magic_resist": num(stats.get("magicResist", "")),
        "ability_haste": num(stats.get("abilityHaste", "")),
        "tenacity": num(stats.get("tenacity", "")),
        "ability_levels": ability_levels(data),
        "items": items,
        "items_usable": usable,
    }


def event_rows(sid: str, data: dict, seen: "set") -> list:
    """New events since the last poll.

    The payload is cumulative — every poll returns the whole game's events — so
    dedupe on EventID. Timing comes from EventTime inside the payload, not from
    when we happened to poll, so a slow poll rate costs resolution on continuous
    state but not on event timing.

    **``game_time_s`` is the authoritative timestamp on this stream, not
    ``unix_time_ms``.** The wall clock here is stamped when the poller *saw* the
    event, which is up to one poll interval late — and much later than that for
    anything the cumulative payload backfills. A real capture recorded GameStart
    at ``game_time_s`` 0.022 with a wall clock 7.9 seconds after the fact,
    because recording began mid-game and the event arrived in the first payload.

    So when joining these events to gaze, input or audio, convert through the
    gamestate stream — which carries both clocks on every row — rather than using
    this column directly. Interpolate within a continuous stretch only; see
    ``check_session.py`` for why the mapping is piecewise and not a single slope.
    """
    rows = []
    for ev in dig(data, "events", "Events", default=[]) or []:
        eid = ev.get("EventID")
        if eid is None or eid in seen:
            continue
        seen.add(eid)
        rows.append({
            "session_id": sid,
            "unix_time_ms": now_ms(),
            "game_time_s": round(float(ev.get("EventTime", 0) or 0), 3),
            "event_id": eid,
            "event_name": ev.get("EventName", ""),
            "killer": ev.get("KillerName", ""),
            "victim": ev.get("VictimName", ""),
            "assisters": "|".join(ev.get("Assisters") or []),
            "dragon_type": ev.get("DragonType", ""),
            "stolen": ev.get("Stolen", ""),
            "payload": json.dumps(ev, separators=(",", ":")),
        })
    return rows


def _spells(p: dict) -> list:
    ss = p.get("summonerSpells") or {}
    return [str((ss.get(k) or {}).get("displayName", ""))
            for k in ("summonerSpellOne", "summonerSpellTwo")]


def _keystone(p: dict) -> str:
    return str(((p.get("runes") or {}).get("keystone") or {}).get("displayName", ""))


def meta_dict(sid: str, data: dict, started_ms: int) -> dict:
    me = find_active(data)
    full = dig(data, "activePlayer", "fullRunes", default={}) or {}
    return {
        "session_id": sid,
        "started_unix_ms": started_ms,
        "game": "league",
        "started_iso": datetime.fromtimestamp(started_ms / 1000, timezone.utc).isoformat(),
        "riot_id": me.get("riotId") or me.get("summonerName") or active_name(data),
        "champion": me.get("championName", ""),
        "team": me.get("team", ""),
        "position": me.get("position", ""),
        "game_mode": dig(data, "gameData", "gameMode", default=""),
        "map_name": dig(data, "gameData", "mapName", default=""),
        "map_number": dig(data, "gameData", "mapNumber", default=""),
        # Dragon soul terrain (Default / Infernal / Ocean / Mountain / Cloud /
        # Hextech / Chemtech). It changes the map's geometry, so gaze analysis
        # that cares about where things are on screen needs to know it.
        "map_terrain": dig(data, "gameData", "mapTerrain", default=""),
        # Bots in the lobby means this is not competitive gameplay. Recording it
        # per session is what lets a later filter separate real games from
        # practice — the distinction nobody could make in the old archive
        # without watching the footage.
        "has_bots": any(bool(p.get("isBot"))
                        for p in (dig(data, "allPlayers", default=[]) or [])),
        # The skin decides the minimap icon (Viego's possession swaps it, some skins
        # may too); recording it per game answers that question with data.
        "skin_id": me.get("skinID", ""),
        "skin_name": me.get("skinName", ""),
        "summoner_spells": _spells(me),
        "runes": {
            "keystone": _keystone(me),
            "primary_tree": str((full.get("primaryRuneTree") or {}).get("displayName", "")),
            "secondary_tree": str((full.get("secondaryRuneTree") or {}).get("displayName", "")),
            "ids": [r.get("id") for r in (full.get("generalRunes") or []) if r.get("id")],
            "stat_ids": [r.get("id") for r in (full.get("statRunes") or []) if r.get("id")],
        },
        "abilities": {k: str((v or {}).get("id", ""))
                      for k, v in (dig(data, "activePlayer", "abilities", default={}) or {}).items()},
        "roster": [
            {
                "riot_id": p.get("riotId") or p.get("summonerName"),
                "champion": p.get("championName"),
                "team": p.get("team"),
                "position": p.get("position"),
                "is_bot": bool(p.get("isBot", False)),
                "skin_name": p.get("skinName", ""),
                "summoner_spells": _spells(p),
                "keystone": _keystone(p),
            }
            for p in (dig(data, "allPlayers", default=[]) or [])
        ],
    }


# Minimap side as a fraction of screen width, per League MinimapScale. Measured on the
# 28 Sep 2026 screen recordings of five rigs (scales 0.3, 0.9, 1.0, 1.32, 1.5 gave
# 0.113, 0.138, 0.145, 0.156, 0.163 of the width); linear within 0.003 of each.
MINIMAP_SIDE_A, MINIMAP_SIDE_B = 0.100, 0.042
MINIMAP_MARGIN = 0.0016  # of screen width, between the map and the screen edge


def estimate_minimap_rect(minimap_scale, width: int, height: int) -> "dict | None":
    """Where the minimap sits for a MinimapScale, on a screen of this size.

    Good to about ten pixels at 1440p. A measured rectangle (``gaze_validation.py
    --rect-only``) always wins over this.
    """
    try:
        scale = float(minimap_scale)
    except (TypeError, ValueError):
        return None
    if not (0.1 <= scale <= 2.5) or width <= 0 or height <= 0:
        return None
    side = (MINIMAP_SIDE_A + MINIMAP_SIDE_B * scale) * width
    margin = MINIMAP_MARGIN * width
    x1, y1 = width - margin, height - margin
    return {"x0": round(x1 - side), "y0": round(y1 - side), "x1": round(x1), "y1": round(y1),
            "screen_px": [width, height], "minimap_scale": scale,
            "source": "estimated from the client's MinimapScale (liveclient_recorder.estimate_minimap_rect)"}


def _screen_px() -> "tuple[int, int] | None":
    try:
        import screeninfo
        m = screeninfo.get_monitors()[0]
        return int(m.width), int(m.height)
    except Exception:
        return None


def setup_records() -> dict:
    """The per-PC measurements from ``gaze_validation.py``, for the session meta.

    ``minimap_rect_px`` (``<data>/json/minimap_rect.json``) and the newest
    ``gaze_validation`` (``<data>/gaze/validation_*.json``). Both optional.
    """
    out = {}
    candidates = [playsmart_session.data_dir() / "json" / "minimap_rect.json",
                  Path("data/json/minimap_rect.json")]
    for rect in candidates:
        try:
            if rect.is_file():
                out["minimap_rect_px"] = json.loads(rect.read_text(encoding="utf-8"))
                out["minimap_rect_px"].setdefault("source", f"measured ({rect})")
                break
        except (OSError, ValueError):
            pass
    try:
        gaze_dir = playsmart_session.data_dir() / "gaze"
        latest = sorted(gaze_dir.glob("validation_*.json"))
        if latest:
            v = json.loads(latest[-1].read_text(encoding="utf-8"))
            out["gaze_validation"] = {k: v.get(k) for k in (
                "when", "accuracy_px", "precision_rms_px", "accuracy_deg",
                "precision_rms_deg", "head_dist_mm", "screen_px")}
    except (OSError, ValueError):
        pass
    return out


def add_identity(meta: dict, meta_file: Path) -> None:
    """Add the game id, platform, PUUID and client patch to the session's meta.

    These link a capture session to its replay (``<platform>-<game_id>.rofl``), its
    Match-V5 record, and the other players' sessions of the same game. Best-effort:
    on any failure the fields stay empty and ``identity_source`` says why; the
    post-game pipeline can still match the session to a replay by its roster.
    """
    try:
        ident = lcu.game_identity()
    except Exception as exc:  # never let this reach the recording thread
        ident = {"identity_source": f"error: {exc}"}
    meta.update(ident)
    # Session context from the client (added 25 Sep 2026): the game and input
    # settings (where the minimap is, which key is a ping), the queue (solo queue /
    # custom / ranked) and the account's tier. Each part is best-effort.
    try:
        meta.update(lcu.client_context())
    except Exception as exc:
        meta["context_source"] = f"error: {exc}"
    # No measured minimap rectangle: estimate it from the client's settings so the
    # merge never has to fall back to a fixed corner that is wrong on most rigs.
    if not (meta.get("minimap_rect_px") or {}).get("x1"):
        scale = ((meta.get("game_settings") or {}).get("HUD") or {}).get("MinimapScale")
        screen = _screen_px()
        est = estimate_minimap_rect(scale, *screen) if screen else None
        if est:
            meta["minimap_rect_px"] = est
            print(f"[liveclient] minimap rectangle estimated from MinimapScale {float(scale):.2f}: "
                  f"{est['x0']},{est['y0']}-{est['x1']},{est['y1']} px. Measure it once with "
                  "`gaze_validation.py --rect-only` for a better one.", flush=True)
        else:
            print("[liveclient] !! no minimap rectangle: run `gaze_validation.py --rect-only` "
                  "with the game open; gaze-on-minimap will use a default corner", flush=True)
    try:
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[liveclient] could not add game id to meta: {exc}", flush=True)
        return
    if meta.get("game_id"):
        print(f"[liveclient] game {meta.get('platform_id') or '?'}_{meta['game_id']} "
              f"on client {meta.get('client_game_version') or '?'}", flush=True)
    else:
        print(f"[liveclient] no game id ({meta.get('identity_source')}); the replay "
              "will be matched by roster afterwards", flush=True)


def game_time_of(data: "dict | None") -> "float | None":
    try:
        return float(dig(data, "gameData", "gameTime", default=None))
    except (TypeError, ValueError):
        return None


def is_live(data: "dict | None", previous: "dict | None" = None) -> bool:
    """A payload from a game that is actually being played.

    The API also answers on the post-game screen and briefly on a dead client, with
    an empty active player and a clock that does not move. Require a champion for the
    active player, a non-empty roster, and - when a previous poll is given - a game
    clock that moved forward since then.
    """
    if not data or not find_active(data).get("championName"):
        return False
    if not (dig(data, "allPlayers", default=[]) or []):
        return False
    if previous is not None:
        t0, t1 = game_time_of(previous), game_time_of(data)
        if t0 is None or t1 is None:
            return False
        # On the loading screen the clock sits at 0 with the roster already known:
        # that is a game about to start, worth recording from its first tick.
        if t1 <= t0 and not (t0 == 0 and t1 == 0):
            return False
    return True


# Arm later than this into the game and the console says so. A session armed after
# the loading screen is missing exactly this much of every stream.
LATE_START_S = 0.5


def wait_for_game(abort: "threading.Event | None" = None,
                  poll_seconds: float = 1.0) -> "dict | None":
    """Block until a game is running. None if `abort` is set first.

    Returns the first payload of a *live* game (see :func:`is_live`): two
    consecutive polls with a champion and an advancing clock. Polls every second
    (was two) so that arming during the loading screen catches game time 0.
    """
    http = requests.Session()
    previous = None
    while abort is None or not abort.is_set():
        data = poll(http)
        if data is not None and is_live(data):
            if previous is not None and is_live(data, previous):
                return data
            previous = data
            time.sleep(0.5)
            continue
        previous = None
        time.sleep(poll_seconds)
    return None


def record_game(sid: str, out_dir: Path, first: "dict | None" = None,
                stop: "threading.Event | None" = None,
                on_end: "threading.Event | None" = None) -> "dict | None":
    """Poll and write until the game ends or `stop` is set.

    Returns the meta dict so the caller can label the session without re-reading
    the file. `on_end` is set when the game itself finishes, which is what the
    orchestrator waits on.
    """
    http = requests.Session()
    data = first if first is not None else poll(http)
    if data is None:
        data = wait_for_game(abort=stop)
        if data is None:
            return None

    started = now_ms()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = meta_dict(sid, data, started)
    meta.update(setup_records())
    first_t = game_time_of(data)
    meta["late_start_s"] = round(first_t, 2) if first_t is not None else None
    if first_t is not None and first_t > LATE_START_S:
        print("!" * 68, flush=True)
        print(f"!!  armed late: the game was already at {first_t:.1f} s. Every stream of this "
              "session misses that much. Next time press F7 before the loading screen ends.",
              flush=True)
        print("!" * 68, flush=True)
    meta_file = out_dir / f"{sid}_meta.json"
    meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # The game id comes from the League client, not this API; asking can take a few
    # seconds when the client is slow, so it runs beside the recording, never before it.
    threading.Thread(target=add_identity, args=(meta, meta_file), daemon=True).start()
    print(f"[liveclient] {meta['game_mode'] or 'game'} on {meta['map_name'] or '?'} "
          f"as {meta['champion'] or '?'} ({meta['riot_id'] or '?'})", flush=True)

    seen = set()
    period = 1.0 / POLL_HZ
    misses = 0
    first_miss = None

    state_file = out_dir / f"{sid}_gamestate.csv"
    event_file = out_dir / f"{sid}_events.csv"
    players_file = out_dir / f"{sid}_players.csv"

    try:
        with state_file.open("w", newline="", encoding="utf-8") as sf, \
             event_file.open("w", newline="", encoding="utf-8") as ef, \
             players_file.open("w", newline="", encoding="utf-8") as pf:
            sw = csv.DictWriter(sf, fieldnames=STATE_COLUMNS)
            ew = csv.DictWriter(ef, fieldnames=EVENT_COLUMNS)
            pw = csv.DictWriter(pf, fieldnames=PLAYER_COLUMNS)
            sw.writeheader()
            ew.writeheader()
            pw.writeheader()

            while True:
                if stop is not None and stop.is_set():
                    print("[liveclient] stop requested", flush=True)
                    break

                tick = time.time()

                if data is not None:
                    misses = 0
                    first_miss = None
                    row = state_row(sid, data)
                    sw.writerow(row)
                    pw.writerows(player_rows(sid, data, row["unix_time_ms"], row["game_time_s"]))
                    ended = False
                    for row in event_rows(sid, data, seen):
                        ew.writerow(row)
                        if row["event_name"] == "GameEnd":
                            print("[liveclient] GameEnd", flush=True)
                            ended = True
                    sf.flush()
                    ef.flush()
                    pf.flush()
                    if ended:
                        break
                else:
                    # The endpoint drops before the post-game screen, and can
                    # hitch mid-game. Tolerate a few misses so one dropped
                    # request does not truncate a live recording.
                    misses += 1
                    first_miss = first_miss or time.time()
                    if misses >= MISS_LIMIT or time.time() - first_miss >= MISS_SECONDS:
                        print("[liveclient] client stopped responding — game over", flush=True)
                        break

                time.sleep(max(0.0, period - (time.time() - tick)))
                data = poll(http)
    finally:
        if on_end is not None:
            on_end.set()

    print(f"[liveclient] wrote {state_file.name}, {event_file.name} and {players_file.name}",
          flush=True)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Record League game state from the Live Client Data API.")
    ap.add_argument("--out", default=None,
                    help="output directory (default: data/gamestate)")
    ap.add_argument("--once", action="store_true", help="record one game then exit")
    ap.add_argument("--verify", default=None,
                    help="path to riotgames.pem to validate the client certificate")
    args = ap.parse_args()

    global VERIFY
    if args.verify:
        VERIFY = args.verify

    out = Path(args.out) if args.out else playsmart_session.stream_dir("gamestate")

    while True:
        print("[liveclient] waiting for a game…", flush=True)
        data = wait_for_game()
        sid = playsmart_session.new_session_id()
        print(f"SESSION {sid}", flush=True)
        record_game(sid, out, first=data)
        if args.once:
            return
        time.sleep(5.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[liveclient] stopped", flush=True)
