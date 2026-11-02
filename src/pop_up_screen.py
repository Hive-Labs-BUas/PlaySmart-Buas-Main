"""Label and upload one capture session.

Modified 2026-09 (iteration 5): rewritten - see below. Modified 2026-09-25: after the
recorders stop it also fetches the client's post-game stats block (``<sid>_eog.json``),
asks the player about their deaths (``self_labels.py`` -> ``<sid>_labels.json``), writes
a SHA-256 manifest of the session's files (``<sid>_manifest.json``) and appends one
line to ``data/sessions.csv``.

This used to be the weakest link in the pipeline. It asked the operator for a
player name and a game from a dropdown, then went looking for the newest
un-renamed file in each of data/gaze, data/input, data/face and data/eda and
assumed those belonged together. They frequently did not: if a recorder crashed,
the previous session's leftover file was the newest one and got adopted into
this session. The archive has a merged file whose gaze data is 2.6 hours from
the session named on it, and a machine holding eleven `merged_data_*.csv` with
identical checksums.

None of that guessing is needed any more. Every recorder now names its own file
`<session_id>_<stream>.csv` when it opens it, and `liveclient_recorder.py` writes
`<session_id>_meta.json` with the Riot ID, champion, game mode and map straight
from the game client. So this script just collects the files carrying this
session's id and uploads them. Modified 2026-09-28: the upload goes over one
connection, keeps the local copy unless ``PLAYSMART_UPLOAD=move``, and logs every
outcome to ``data/upload_log.csv``; ``upload_session.py`` retries later.

The free-text game dropdown is gone with it — that field produced nine spellings
of two game names, including `valornat`, `valorant ` and `nogame`.
"""

import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
sys.path.append(str(SRC.parent))

import session  # noqa: E402
import lcu  # noqa: E402
import self_labels  # noqa: E402
from server.python_app import sftp_upload  # noqa: E402

# Seconds to wait for the client's post-game stats block after the game ends.
EOG_WAIT_S = float(os.environ.get("PLAYSMART_EOG_WAIT_S", "45"))
SESSIONS_INDEX = "sessions.csv"
UPLOAD_LOG = "upload_log.csv"

# Local stream -> remote SFTP directory.
#
# NOTE: /data/gamestate/ does not exist on the server yet. The sftp container's
# command in docker-compose.yml (PlaySmart-Server-Buas-Main) mkdir -p's each of
# these; gamestate needs adding there. The uploader tries to mkdir it and, if the
# sftp user may not, reports it and keeps those files local (retry later with
# src/upload_session.py).
UPLOAD_MAP = {
    "gaze": "/data/gaze/",
    "input": "/data/input/",
    "face": "/data/face/",          # 2026-11-02: OpenFace stream; the server dir may not exist yet
    "emotion": "/data/emotion/",    # legacy label stream, pre-November sessions only
    "eda": "/data/eda/",
    "audio": "/data/audio/",
    "gamestate": "/data/gamestate/",
    "video": "/data/video/",
}

MAPPING_FILE = "data/json/ign_mapping.json"


# ------------------ participant pseudonymisation ------------------

def load_mapping(mapping_file=MAPPING_FILE):
    if os.path.exists(mapping_file):
        with open(mapping_file, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_mapping(mapping, mapping_file=MAPPING_FILE):
    os.makedirs(os.path.dirname(mapping_file), exist_ok=True)
    with open(mapping_file, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=4)


def get_next_player_id(mapping):
    nums = [int(v[1:]) for v in mapping.values()
            if isinstance(v, str) and v.startswith("P") and v[1:].isdigit()]
    return f"P{max(nums, default=0) + 1:03d}"


def participant_for(riot_id):
    """Stable participant id for a Riot ID, minted on first sight.

    The mapping stays on this machine: it is the only way to honour a consent
    withdrawal later, so it must exist, and it must not travel with the data.
    """
    if not riot_id:
        return ""
    mapping = load_mapping()
    if riot_id not in mapping:
        mapping[riot_id] = get_next_player_id(mapping)
        save_mapping(mapping)
    return mapping[riot_id]


# ------------------ collecting this session's files ------------------

def read_meta(sid):
    path = session.stream_dir("gamestate") / f"{sid}_meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Could not read {path}: {exc}")
        return {}


def session_files(sid):
    """Every file this session's recorders produced, by stream."""
    found = {}
    for stream in UPLOAD_MAP:
        # video is included: obs_recorder.py files <sid>_video.<ext> and its
        # <sid>_video.json timing anchor under data/video before this runs.
        folder = session.data_dir() / stream
        if not folder.is_dir():
            continue
        matches = sorted(p for p in folder.iterdir()
                         if p.is_file() and p.name.startswith(sid))
        if matches:
            found[stream] = matches
    return found


def claim_video(sid, started_ms):
    """Move the OBS recording for this session into data/video.

    OBS names its own files by wall clock and knows nothing about session ids,
    so this is the one place a time-based match is still needed. Restricting it
    to files modified after the session began is what stops it from adopting an
    older recording — the failure mode that put a ten-minute rain video under a
    League session name in the old archive.
    """
    src_dir = Path(os.path.expanduser("~")) / "Videos"
    if not src_dir.is_dir():
        return None

    cutoff = started_ms / 1000.0 if started_ms else 0.0
    candidates = [p for p in src_dir.iterdir()
                  if p.suffix.lower() in (".mp4", ".mkv") and p.stat().st_mtime >= cutoff]
    if not candidates:
        print("No OBS recording found for this session.")
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    dest = session.stream_dir("video") / f"{sid}_video{newest.suffix.lower()}"
    try:
        os.replace(newest, dest)
    except OSError as exc:
        print(f"Could not move {newest.name}: {exc}")
        return None
    print(f"Video: {newest.name} -> {dest.name}")
    return dest


# ------------------ end-of-game extras (25 Sep 2026) ------------------

def save_eog(sid, meta):
    """The client's post-game stats block, scrubbed of names, as <sid>_eog.json.

    Works for Practice Tool and customs, which Match-V5 never records. The block
    appears a few seconds after the game ends; waits up to EOG_WAIT_S for one that
    carries this session's game id.
    """
    if EOG_WAIT_S <= 0:
        return None
    block = lcu.end_of_game_stats(str(meta.get("game_id", "")), wait_s=EOG_WAIT_S)
    if not block:
        print("No post-game stats block from the client (fine for a game left early).")
        return None
    path = session.stream_dir("gamestate") / f"{sid}_eog.json"
    try:
        path.write_text(json.dumps(block, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write {path.name}: {exc}")
        return None
    print(f"Post-game stats: {path.name}")
    return path


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def write_manifest(sid, files, meta):
    """<sid>_manifest.json: every file of the session with size and SHA-256.

    This is what makes the dataset provable later: a copy on the server or on a
    hand-over drive can be checked against it file by file.
    """
    entries = []
    for stream, paths in sorted(files.items()):
        for path in paths:
            try:
                entries.append({
                    "stream": stream,
                    "file": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_of(path),
                })
            except OSError as exc:
                entries.append({"stream": stream, "file": path.name, "error": str(exc)})
    manifest = {
        "session_id": sid,
        "game": meta.get("game") or session.game(),
        "written_unix_ms": int(time.time() * 1000),
        "game_key": (f"{meta.get('platform_id') or 'UNKNOWN'}_{meta['game_id']}"
                     if meta.get("game_id") else ""),
        "participant_id": meta.get("participant_id", ""),
        "capture_tier": meta.get("capture_tier", ""),
        "streams_skipped": meta.get("streams_skipped", []),
        "files": entries,
    }
    path = session.stream_dir("gamestate") / f"{sid}_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def append_index(sid, files, meta):
    """One line per session in data/sessions.csv: what was recorded, how big."""
    path = session.data_dir() / SESSIONS_INDEX
    streams = sorted(files)
    total = sum(p.stat().st_size for ps in files.values() for p in ps if p.is_file())
    row = {
        "session_id": sid,
        "game": meta.get("game") or session.game(),
        "started_iso": meta.get("started_iso", ""),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "participant_id": meta.get("participant_id", ""),
        "game_key": (f"{meta.get('platform_id') or 'UNKNOWN'}_{meta['game_id']}"
                     if meta.get("game_id") else ""),
        "game_mode": meta.get("game_mode", ""),
        "queue_type": meta.get("queue_type", ""),
        "champion": meta.get("champion", ""),
        "team": meta.get("team", ""),
        "streams": "|".join(streams),
        "total_mb": round(total / 1e6, 1),
        "labels": "yes" if any(p.name.endswith("_labels.json") for ps in files.values() for p in ps) else "no",
    }
    new = not path.is_file()
    try:
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as exc:
        print(f"Could not append to {path}: {exc}")


# ------------------ main ------------------

def main():
    sid = session.session_id()
    meta = read_meta(sid)

    riot_id = meta.get("riot_id", "")
    participant = participant_for(riot_id)

    print(f"Session   : {sid}")
    print(f"Player    : {participant or '(unknown)'}"
          + (f"  [{riot_id}]" if riot_id else ""))
    print(f"Game      : {meta.get('game_mode') or 'unknown'} "
          f"on {meta.get('map_name') or 'unknown'} "
          f"as {meta.get('champion') or 'unknown'}")

    if meta:
        meta["participant_id"] = participant
        try:
            (session.stream_dir("gamestate") / f"{sid}_meta.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"Could not update meta: {exc}")
    elif session.game() != "league":
        print(f"Game      : {session.game()} (manual bracket; no game-state stream)")
    else:
        print("No gamestate meta for this session — the game client API was not "
              "reachable. Files will still upload, just without game labels.")

    # Give the recorders a moment to finish flushing to disk.
    time.sleep(2)

    # The client's own post-game stats, then the player's own account of their
    # deaths (one minute, while it is fresh). Both are best-effort and both write
    # into data/gamestate, so they are picked up by session_files() below.
    if meta:
        try:
            save_eog(sid, meta)
        except Exception as exc:
            print(f"Post-game stats skipped: {exc}")
        try:
            self_labels.collect(sid, meta)
        except Exception as exc:
            print(f"Self-labels skipped: {exc}")

    files = session_files(sid)
    if "video" not in files:
        # obs_recorder did not run (OBS off, websocket disabled, library
        # missing): fall back to claiming a hand-started OBS recording by time.
        video = claim_video(sid, meta.get("started_unix_ms", 0))
        if video:
            files["video"] = [video]

    if not files:
        print("No files found for this session — nothing to upload.")
        return

    try:
        manifest = write_manifest(sid, files, meta)
        files.setdefault("gamestate", []).append(manifest)
        append_index(sid, files, meta)
        print(f"Manifest  : {manifest.name}; index {SESSIONS_INDEX} updated")
    except Exception as exc:
        print(f"Manifest skipped: {exc}")

    upload_session_files(sid, files)


def upload_session_files(sid, files):
    """Upload every file of the session over one connection and log the outcome.

    The local copy stays unless ``PLAYSMART_UPLOAD=move`` (see sftp_upload.py).
    Every outcome goes to ``data/upload_log.csv`` so ``upload_session.py`` can
    retry what failed once the PC is back on the BUas network.
    """
    items = []
    print(f"\nUploading ({sftp_upload.upload_mode()}):")
    for stream, paths in files.items():
        dest = UPLOAD_MAP.get(stream)
        if not dest:
            continue
        for path in paths:
            size = path.stat().st_size
            if size == 0:
                print(f"  !! {path.name} is empty — check the {stream} recorder")
            print(f"  {stream:<10} {path.name}  ({size/1e6:.2f} MB)")
            items.append((path, dest, stream))

    results = sftp_upload.upload_files([(p, d) for p, d, _ in items])
    counts = {}
    for path, dest, stream in items:
        outcome = results.get(str(path), sftp_upload.FAILED)
        counts[outcome] = counts.get(outcome, 0) + 1
        log_upload(sid, stream, path, dest, outcome)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print(f"Upload    : {summary or 'nothing to upload'}"
          + ("  (local copies kept)" if sftp_upload.upload_mode() != "move" else ""))
    if any(counts.get(k) for k in (sftp_upload.FAILED, sftp_upload.OFF, sftp_upload.UNREACHABLE)):
        print(f"            retry later: poetry run python src\\upload_session.py {sid}")
    return results


def log_upload(sid, stream, path, dest, outcome):
    """One line per file per attempt in data/upload_log.csv."""
    log = session.data_dir() / UPLOAD_LOG
    new = not log.is_file()
    row = {
        "when_iso": datetime.now().isoformat(timespec="seconds"),
        "session_id": sid,
        "stream": stream,
        "file": path.name,
        "bytes": path.stat().st_size if path.is_file() else "",
        "remote_dir": dest,
        "outcome": outcome,
    }
    try:
        with log.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as exc:
        print(f"Could not append to {log}: {exc}")


if __name__ == "__main__":
    main()
