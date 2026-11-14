"""
Benchmark 8 - test count and line coverage.

    python bench/bench_tests.py [--runs 3]

Runs the tracked pytest suite with pytest-cov over every Python
package/module of the project (bench/ and tests/ themselves excluded):

    python -m pytest -q -p no:cacheprovider \
        --cov=cache --cov=cluster --cov=database --cov=services \
        --cov=router --cov=main --cov=import_dataset --cov=run \
        --cov-report=term

Reported: passed / skipped / failed counts, TOTAL line coverage, and
per-package coverage.  The frontend (JavaScript) has no tests and is
NOT included in any coverage number.
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time

from common import REPO, machine_specs, save

COV = ["cache", "cluster", "database", "services", "router",
       "main", "import_dataset", "run"]


def one():
    cov_json = os.path.join(REPO, "bench", ".work", "coverage.json")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    cmd += [f"--cov={m}" for m in COV]
    cmd += ["--cov-report=term", f"--cov-report=json:{cov_json}"]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    out = p.stdout
    summary = [l for l in out.splitlines() if re.search(r"\d+ passed", l)][-1]
    counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|skipped|failed|errors?)", summary)}
    with open(cov_json) as handle:
        cov = json.load(handle)
    total = cov["totals"]["percent_covered"]
    per_pkg = {}
    for path, data in cov["files"].items():
        pkg = path.split(os.sep)[0] if os.sep in path else path
        s = per_pkg.setdefault(pkg, [0, 0])
        s[0] += data["summary"]["covered_lines"]
        s[1] += data["summary"]["num_statements"]
    per_pkg = {k: {"covered": v[0], "statements": v[1],
                   "pct": 100.0 * v[0] / v[1] if v[1] else None}
               for k, v in sorted(per_pkg.items())}
    tail = "\n".join(out.splitlines()[-45:])
    return {"summary_line": summary, "counts": counts, "total_cov_pct": total,
            "covered_lines": cov["totals"]["covered_lines"],
            "statements": cov["totals"]["num_statements"],
            "per_package": per_pkg, "wall_s": wall, "term_tail": tail}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    runs = []
    for i in range(args.runs):
        r = one()
        runs.append(r)
        print(f"run {i}: {r['summary_line']}  coverage={r['total_cov_pct']:.2f}% "
              f"({r['covered_lines']}/{r['statements']} lines) wall={r['wall_s']:.1f}s",
              flush=True)
    print(runs[-1]["term_tail"])
    print("PER PACKAGE (last run):")
    for k, v in runs[-1]["per_package"].items():
        print(f"  {k:20s} {v['covered']:5d}/{v['statements']:5d} = {v['pct']:.1f}%")
    save("tests", {"specs": machine_specs(), "runs": runs,
                   "median_total_cov_pct": statistics.median(r["total_cov_pct"] for r in runs),
                   "median_wall_s": statistics.median(r["wall_s"] for r in runs)})


if __name__ == "__main__":
    main()
