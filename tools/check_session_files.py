"""
Check the renamed recording files on one station (test plan for the session-file-matching fix).

    poetry run python tools/check_session_files.py [--data data] [--last 3]

For each recording name (<n>th_game_<PlayerID>_<game>_<date>_<time>) it reads only the unix_time
column of the gaze, input and emotion files and prints:
  - which of the three files exist
  - emotion start minus gaze start, in seconds (PASS if within 15 s)
  - emotion coverage of the input span (share of the input span that the emotion file covers)
Exit code 1 if any checked recording fails.
"""
import argparse
import csv
import glob
import os
import re
import sys

START_TOL_S = 15       # emotion must start within this many seconds of the gaze start
MIN_COVERAGE = 0.90    # emotion must cover at least this share of the input span

NAME_RE = re.compile(r"^(?P<stem>.+_game_.+)_(?P<kind>gaze|input|emotion)\.csv$")


def span_ms(path):
    lo = hi = None
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader, [])]
        if "unix_time" not in header:
            return None
        i = header.index("unix_time")
        for row in reader:
            try:
                t = float(row[i])
            except (ValueError, IndexError):
                continue
            t = t * 1000 if t < 1e11 else t
            lo = t if lo is None else min(lo, t)
            hi = t if hi is None else max(hi, t)
    return None if lo is None else (lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--last", type=int, default=3, help="check the most recent N recordings")
    a = ap.parse_args()

    rec = {}
    for kind in ("gaze", "input", "emotion"):
        for p in glob.glob(os.path.join(a.data, kind, "*.csv")):
            m = NAME_RE.match(os.path.basename(p))
            if m and m["kind"] == kind:
                rec.setdefault(m["stem"], {})[kind] = p
    stems = sorted(rec, key=lambda s: os.path.getmtime(next(iter(rec[s].values()))))[-a.last:]

    unnamed = [p for k in ("gaze", "input", "emotion")
               for p in glob.glob(os.path.join(a.data, k, "*.csv")) if "_game_" not in os.path.basename(p)]
    if unnamed:
        print(f"NOTE: {len(unnamed)} unnamed CSV file(s) left in {a.data}/(gaze|input|emotion)")

    failed = 0
    for s in stems:
        f = rec[s]
        spans = {k: span_ms(p) for k, p in f.items()}
        g, i, e = spans.get("gaze"), spans.get("input"), spans.get("emotion")
        problems = [f"no {k} file" for k in ("gaze", "input", "emotion") if k not in f]
        d = cov = None
        if g and e:
            d = (e[0] - g[0]) / 1000
            if abs(d) > START_TOL_S:
                problems.append(f"emotion starts {d:+.1f} s from gaze")
        if i and e:
            cov = max(0.0, min(i[1], e[1]) - max(i[0], e[0])) / max(1.0, i[1] - i[0])
            if cov < MIN_COVERAGE:
                problems.append(f"emotion covers {cov:.0%} of the input span")
        status = "PASS" if not problems else "FAIL"
        failed += status == "FAIL"
        print(f"{status}  {s}  emotion-gaze start {'' if d is None else f'{d:+.1f} s'}  "
              f"coverage {'' if cov is None else f'{cov:.0%}'}  {'; '.join(problems)}")
    print(f"{len(stems) - failed} of {len(stems)} recordings pass")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
