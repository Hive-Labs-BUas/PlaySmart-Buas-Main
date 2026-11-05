# Operator checklist: recording stations (Comic-Con, 21 to 22 Nov 2026)

Works with the current station code and with the `fix/session-file-matching` branch. Steps marked
**(old code)** are only needed while that fix is not deployed.

Keep a paper or offline **station sheet** with the consent forms. It holds personal identifiers
(Riot accounts, in-game names): never put it in a repository, chat, file name or upload.

## Before the session, on every PC

1. **Clock sync.** Open a command prompt as administrator:
   ```
   net start w32time
   w32tm /resync /force
   w32tm /query /status
   ```
   "Last Successful Sync Time" must be today. Write the PC name and the time on the sheet.
2. **Riot account.** On the sheet, write per station: PC name, the Riot account signed in to the
   launcher, date. Do not change the account during the day; if it changes, write the time.
3. **Eye tracker.** Connected and calibrated. If the console ever says that no eye tracker was
   found, stop: the gaze file would then contain generated (random) data, not gaze.
4. **Webcam.** OpenFace must start (console shows "Tracking started at") on the first test run.
5. **Leftover files.** `data/gaze`, `data/input`, `data/emotion` and `data/eda` must hold no CSV
   without `_game_` in its name. If there are any, move them to `data/stray/` (do not delete: they
   are participant data) and note it on the sheet.

## Every recording

1. Player seated, game launched: press **F7** before the match starts (agent select at the latest).
2. Match over (end screen): press **F12** once.
3. **(old code)** Before you click OK in the pop-up, wait until the console shows
   "Saved emotion data", "CSV saved" and "Gaze and emotion overlay visualization stopped".
   Clicking OK earlier can give this recording's name to an earlier recording's emotion file.
   **(fix branch)** The pop-up only appears once the recorders have saved; read any `WARNING` line.
4. In the pop-up, pick the player's **own** entry, or type a new name exactly. Never pick another
   person's entry: one participant code per person.
5. After the pop-up: the console shows `Uploaded` for emotion, input and gaze. Read the participant
   code from the newest file name in `data/input` (`..._game_P0xx_...`) and write on the sheet:
   station, participant code, F7 time, F12 time, and any `WARNING`.
6. Before the next F7, check step 5 of "Before the session" again (no unnamed CSVs left).

## The same evening

1. Download the match details for every match of the day, per station account, with
   `fetch_match.py` (Samuel). Do it the same evening: older match IDs stop being available
   (the June 2026 Comic-Con match IDs return 404 in November).
2. On each station, run `poetry run python tools/check_session_files.py --last <n>` (fix branch)
   and note every `FAIL` on the sheet.
3. Store the sheet (or a photo of it) with the consent forms, not on the stations.
