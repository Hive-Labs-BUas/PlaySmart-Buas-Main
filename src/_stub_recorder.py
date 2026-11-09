"""Stand-in recorder used by `key_listener.py --dry-run`.

It behaves like the real recorders in the one way that matters for testing the
orchestrator: it buffers its output in memory and only writes when it is asked
to stop. If the orchestrator kills it instead of waiting, the file is missing —
which is exactly the failure that ate gaze, EDA and audio in the old archive.

Not used in a real capture.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session

stream = sys.argv[1] if len(sys.argv) > 1 else "stub"
safe = stream.replace("/", "-")

rows = []
started = time.time()
print(f"[stub:{safe}] recording, session {session.session_id()}")

while not session.stop_requested():
    rows.append((int(time.time() * 1000), len(rows)))
    time.sleep(0.05)

path = session.stream_path(f"stub-{safe}")
with path.open("w", encoding="utf-8") as fh:
    fh.write("unix_time_ms,n\n")
    for t, n in rows:
        fh.write(f"{t},{n}\n")

print(f"[stub:{safe}] saved {len(rows)} rows to {path} after {time.time()-started:.1f}s")
