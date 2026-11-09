# Replay rendering — champion positions from the Replay API

Added 2026-09-22 (iteration 5). Prototype from the replay spike; see `CHANGELOG.md`.

**Superseded 2026-09-23** by the post-game pipeline in the team repository
(`playsmart postgame`, `docs/postgame-pipeline.md` there). It downloads, renders
(fog off and on), tracks with temporal clean-up and merges every stream per game.
This folder stays as the record of the spike. What the rig itself now contributes is
the game id in `<sid>_meta.json` (`src/lcu.py`) and the audio anchor.

League's local game state (`liveclient_recorder.py`) has no positions. Riot's official
**Replay API** (`EnableReplayApi=1` under `[General]` in `Config\game.cfg`, then
`https://127.0.0.1:2999/replay/*` while a replay plays) renders any game we recorded with
fog of war on or off and the interface reduced to the minimap, and writes frames at an
exact frame rate. `minimap_match.py` reads champion positions off those frames by
matching the ten Data Dragon portraits of the match's roster.

    # while the replay is open, once per machine: set the render, then record
    curl.exe -k -X POST https://127.0.0.1:2999/replay/render -H "Content-Type: application/json" -d "@render.json"
    curl.exe -k -X POST https://127.0.0.1:2999/replay/recording -H "Content-Type: application/json" -d "@rec.json"
    # then
    python tools/replay/minimap_match.py <frames_dir> --icons icons --roster roster.json --start 540 --fps 2

`render.json`: `{"fogOfWar": false, "interfaceAll": true, "interfaceMinimap": true,
"interfaceAnnounce": false, "interfaceChat": false, "interfaceFrames": false,
"interfaceKillCallouts": false, "interfaceReplay": false, "interfaceScore": false,
"interfaceScoreboard": false, "interfaceTarget": false, "interfaceTimeline": false,
"interfaceNeutralTimers": false}` — `interfaceAll` is a master switch; keep it true.

`rec.json`: `{"path": "C:\\...\\frames", "codec": "png", "startTime": 540, "endTime": 570,
"framesPerSecond": 2, "enforceFrameRate": true, "replaySpeed": 1.0, "lossless": true}` —
`width`/`height` are ignored; the render is at the game window size.

Icons: `https://ddragon.leagueoflegends.com/cdn/<version>/img/champion/<Name>.png` for the
ten champions in the `.rofl` header (`statsJson` → `SKIN`). Roster JSON maps champion →
team (`blue` = team 100, `red` = team 200).

Known: replays play only on the patch they were recorded on — render within the week.
Practice Tool games have no replay. Positions are ±~50 game units (one minimap pixel).
Champions that are dead or hidden under another icon can be mis-placed; temporal gating
is the next change. `render_replay.py` (driving the whole sequence per session) is planned.
