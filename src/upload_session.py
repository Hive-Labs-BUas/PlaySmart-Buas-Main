"""Upload (or re-upload) the files of recorded sessions to the SFTP server.

Added 2026-09-28. The lab PCs are not always on the BUas network when a game ends, and
``/data/gamestate/`` may not exist on the server yet, so the upload at the end of
``pop_up_screen.py`` can leave files behind. This script sends them later:

    poetry run python src\\upload_session.py <sid> [<sid> ...]   these sessions
    poetry run python src\\upload_session.py --pending           every session in
                                                                 data/sessions.csv with a
                                                                 file not yet uploaded
    poetry run python src\\upload_session.py --all               every session, checking
                                                                 each file on the server

A file that is already on the server with the same size is not sent again, so ``--all``
is safe. The local copy is kept unless ``PLAYSMART_UPLOAD=move`` in ``.env``. Outcomes
are appended to ``data/upload_log.csv`` like the post-game upload.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
sys.path.append(str(SRC.parent))

import session  # noqa: E402
import pop_up_screen  # noqa: E402
from server.python_app import sftp_upload  # noqa: E402


def known_sessions() -> list[str]:
    """Session ids from data/sessions.csv, oldest first."""
    path = session.data_dir() / pop_up_screen.SESSIONS_INDEX
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [r["session_id"] for r in csv.DictReader(fh) if r.get("session_id")]


def uploaded_files() -> set[tuple[str, str]]:
    """(session_id, file) pairs whose last logged outcome was uploaded or exists."""
    path = session.data_dir() / pop_up_screen.UPLOAD_LOG
    last: dict[tuple[str, str], str] = {}
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                last[(r["session_id"], r["file"])] = r["outcome"]
    return {k for k, v in last.items() if v in (sftp_upload.UPLOADED, sftp_upload.EXISTS)}


def pending_sessions() -> list[str]:
    done = uploaded_files()
    out = []
    for sid in known_sessions():
        files = pop_up_screen.session_files(sid)
        if any((sid, p.name) not in done for ps in files.values() for p in ps):
            out.append(sid)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sids", nargs="*", help="session ids to upload")
    ap.add_argument("--pending", action="store_true",
                    help="every session with a file not yet logged as uploaded")
    ap.add_argument("--all", action="store_true", help="every session in sessions.csv")
    args = ap.parse_args(argv)

    if args.all:
        sids = known_sessions()
    elif args.pending:
        sids = pending_sessions()
    else:
        sids = args.sids
    if not sids:
        print("Nothing to upload." if (args.all or args.pending) else ap.format_usage())
        return 0

    mode = sftp_upload.upload_mode()
    if mode == "off":
        print("PLAYSMART_UPLOAD=off or no PLAYSMART_SFTP_PASSWORD in .env - nothing will be sent.")
        return 1

    failed = 0
    for sid in sids:
        files = pop_up_screen.session_files(sid)
        if not files:
            print(f"{sid}: no files under {session.data_dir()}")
            continue
        print(f"\n== {sid}")
        results = pop_up_screen.upload_session_files(sid, files)
        failed += sum(1 for v in results.values()
                      if v not in (sftp_upload.UPLOADED, sftp_upload.EXISTS))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
