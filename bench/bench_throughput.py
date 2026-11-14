"""
Benchmark 2 - closed-loop throughput (requests/sec) through the router.

    python bench/bench_throughput.py [--runs 3] [--duration 10]

Matrix: nodes in {1, 3, 5} x concurrency in {1, 10, 50, 100}.
Per run and node count: fresh cluster, 10,000 rows preloaded into
SQLite and read once (so every measured request is a cache HIT),
then for each concurrency: 2 s warm-up (discarded) + `duration` s
measured.  RPS = successful responses / measured wall time.

Also measures the load generator's ceiling against a no-op ASGI
server (bench/_null_server.py) at the same concurrencies.
"""

import argparse
import random
import statistics
import subprocess
import sys
import time

from common import (REPO, SERVER_CPUS, Cluster, HAS_TASKSET, cycle,
                    machine_specs, pin_client, run_load, save, summarize_ms,
                    _wait_http)

CONCURRENCY = [1, 10, 50, 100]
NODES = [1, 3, 5]
KEYS = 10_000


def measure(base, keys, conc, duration):
    run_load(base, cycle(keys), concurrency=conc, duration=2.0)  # warm-up
    rec, t0, t1 = run_load(base, cycle(keys), concurrency=conc,
                           duration=duration)
    ok = [r for r in rec if r[2] == 200]
    errors = len(rec) - len(ok)
    lat = summarize_ms([r[1] for r in ok])
    return {
        "rps": len(ok) / (t1 - t0),
        "requests": len(rec),
        "errors": errors,
        "p50_ms": lat["p50_ms"],
        "p99_ms": lat["p99_ms"],
        "sources": sorted({str(r[4]) for r in ok}),
    }


def null_ceiling(duration):
    cmd = [sys.executable, "-m", "uvicorn", "bench._null_server:app",
           "--port", "9199", "--no-access-log", "--log-level", "warning"]
    if HAS_TASKSET:
        cmd = ["taskset", "-c", SERVER_CPUS] + cmd
    p = subprocess.Popen(cmd, cwd=REPO)
    try:
        _wait_http("http://127.0.0.1:9199/cache/x")
        out = {}
        for c in CONCURRENCY:
            out[c] = measure("http://127.0.0.1:9199", ["x"], c, duration)["rps"]
        return out
    finally:
        p.terminate()
        p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--duration", type=float, default=10.0)
    args = ap.parse_args()
    pin_client()

    raw = {n: {c: [] for c in CONCURRENCY} for n in NODES}
    ceilings = []

    for run in range(args.runs):
        ceilings.append(null_ceiling(args.duration))
        print(f"run {run} loadgen ceiling: " +
              "  ".join(f"c={c}:{v:,.0f}" for c, v in ceilings[-1].items()))
        for n in NODES:
            with Cluster(nodes=n, capacity=50000, name=f"tput-{n}-{run}") as cl:
                keys = [f"tp:{i}" for i in range(KEYS)]
                cl.preload_db(keys)
                run_load(cl.url, keys, concurrency=10)  # populate caches
                random.Random(run).shuffle(keys)
                for c in CONCURRENCY:
                    m = measure(cl.url, keys, c, args.duration)
                    raw[n][c].append(m)
                    print(f"run {run} nodes={n} c={c:3d}: {m['rps']:8.1f} rps "
                          f"p50={m['p50_ms']:.2f}ms p99={m['p99_ms']:.2f}ms "
                          f"errors={m['errors']} sources={m['sources']}")

    median = {n: {c: {
        "rps": statistics.median(m["rps"] for m in raw[n][c]),
        "p50_ms": statistics.median(m["p50_ms"] for m in raw[n][c]),
        "p99_ms": statistics.median(m["p99_ms"] for m in raw[n][c]),
        "errors_total": sum(m["errors"] for m in raw[n][c]),
    } for c in CONCURRENCY} for n in NODES}
    ceiling_median = {c: statistics.median(x[c] for x in ceilings)
                      for c in CONCURRENCY}

    print("MEDIAN RPS (cache hits):")
    print("nodes | " + " | ".join(f"c={c}" for c in CONCURRENCY))
    for n in NODES:
        print(f"{n:5d} | " + " | ".join(
            f"{median[n][c]['rps']:.0f} (p99 {median[n][c]['p99_ms']:.1f}ms)"
            for c in CONCURRENCY))
    print("loadgen ceiling (null server): " +
          " | ".join(f"c={c}: {v:,.0f}" for c, v in ceiling_median.items()))

    save("throughput", {"specs": machine_specs(), "args": vars(args),
                        "raw": raw, "median": median,
                        "loadgen_ceiling_raw": ceilings,
                        "loadgen_ceiling_median": ceiling_median})


if __name__ == "__main__":
    main()
