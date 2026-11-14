"""
Benchmark 5 - node failure during load.

    python bench/bench_failover.py [--runs 3] [--mode crash|hang|both]

Per run: fresh 3-node cluster (RF=2, health checks every 2 s, node
FAILED after 3 consecutive failed checks - the router defaults),
router started with DCS_BENCH_INSTRUMENT=1 (opt-in counters, see
services/bench_counters.py).  3,000 keys preloaded into SQLite and read
once so every key sits on its primary AND its replica.

Load: closed loop, concurrency 10, 30 s of GET /cache/{key} over the
3,000 keys.  At t = 8 s node :9102 is
    crash : SIGKILL  (process gone, connections refused instantly)
    hang  : SIGSTOP  (process frozen, TCP still accepts, no replies -
            every request to it waits for the router's 2 s timeout)

Measured (all wall-clock, same machine clock):
    detection_s     kill -> router marks node FAILED
                    (router's own replication.last_failover.at)
    shift_s         kill -> LAST data-plane request the router sent to
                    the dead node (bench counter `last`), i.e. when 100%
                    of traffic was going to replicas
    failed_attempts requests that hit the dead node first and had to
                    fall back to a replica
    errors          client-visible non-200 responses / exceptions
                    between kill and end of run (and their %)
    latency         p50/p99/max before the kill vs during the window
                    kill -> FAILED
"""

import argparse
import random
import signal
import statistics
import threading
import time

import httpx

from common import (Cluster, cycle, machine_specs, pin_client, run_load,
                    save, summarize_ms)

KEYS = 3000
DURATION = 30.0
KILL_AT = 8.0
VICTIM = 9102


def one(run, mode):
    sig = signal.SIGKILL if mode == "crash" else signal.SIGSTOP
    with Cluster(nodes=3, capacity=50000, name=f"failover-{mode}-{run}",
                 extra_env={"DCS_BENCH_INSTRUMENT": "1"}) as c:
        keys = [f"fo:{i}" for i in range(KEYS)]
        c.preload_db(keys)
        run_load(c.url, keys, concurrency=10)
        random.Random(run).shuffle(keys)
        httpx.post(f"{c.url}/bench/counters/reset")

        victim = f"http://127.0.0.1:{VICTIM}"
        box = {}

        def load():
            offset = time.time() - time.perf_counter()
            rec, t0, t1 = run_load(c.url, cycle(keys), concurrency=10,
                                   duration=DURATION, timeout=10.0)
            box["rec"] = [(r[0] + offset,) + r[1:] for r in rec]

        th = threading.Thread(target=load)
        th.start()
        time.sleep(KILL_AT)
        t_kill = time.time()
        c.kill_node(VICTIM, sig)

        polled_failed = None
        polled_unhealthy = None
        with httpx.Client(timeout=5) as client:
            while th.is_alive():
                st = client.get(f"{c.url}/cluster/status").json()
                state = st["node_states"].get(victim)
                now = time.time()
                if state == "unhealthy" and polled_unhealthy is None:
                    polled_unhealthy = now
                if state == "failed" and polled_failed is None:
                    polled_failed = now
                time.sleep(0.05)
        th.join()

        rep = httpx.get(f"{c.url}/cluster/replication").json()
        counters = httpx.get(f"{c.url}/bench/counters").json()["node_errors"]

    rec = box["rec"]
    t_failed = rep["last_failover"]["at"] if rep.get("last_failover") else None
    vc = counters.get(victim, {"count": 0, "first": None, "last": None})

    before = [r for r in rec if r[0] < t_kill]
    after = [r for r in rec if r[0] >= t_kill]
    window = [r for r in after if t_failed is None or r[0] <= t_failed]
    errors = [r for r in after if r[2] != 200]

    return {
        "mode": mode,
        "detection_s": (t_failed - t_kill) if t_failed else None,
        "polled_unhealthy_s": (polled_unhealthy - t_kill) if polled_unhealthy else None,
        "polled_failed_s": (polled_failed - t_kill) if polled_failed else None,
        "shift_s": (vc["last"] - t_kill) if vc["last"] else None,
        "failed_attempts": vc["count"],
        "requests_after_kill": len(after),
        "errors": len(errors),
        "error_pct": 100.0 * len(errors) / max(1, len(after)),
        "error_kinds": sorted({str(r[2]) for r in errors}),
        "latency_before": summarize_ms([r[1] for r in before]),
        "latency_window": summarize_ms([r[1] for r in window]),
        "requests_in_window": len(window),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--mode", default="both", choices=["crash", "hang", "both"])
    args = ap.parse_args()
    pin_client()
    modes = ["crash", "hang"] if args.mode == "both" else [args.mode]

    raw = {m: [] for m in modes}
    for m in modes:
        for run in range(args.runs):
            r = one(run, m)
            raw[m].append(r)
            print(f"{m} run {run}: detect={r['detection_s']:.2f}s shift={r['shift_s']}s "
                  f"failed_attempts={r['failed_attempts']} errors={r['errors']}/"
                  f"{r['requests_after_kill']} ({r['error_pct']:.3f}%) "
                  f"p99 before={r['latency_before']['p99_ms']:.1f}ms "
                  f"window p99={r['latency_window']['p99_ms']:.1f}ms "
                  f"max={r['latency_window']['max_ms']:.1f}ms", flush=True)

    def med(runs, f, sub=None):
        vals = [(r[f][sub] if sub else r[f]) for r in runs]
        vals = [v for v in vals if v is not None]
        return statistics.median(vals) if vals else None

    median = {m: {
        "detection_s": med(raw[m], "detection_s"),
        "shift_s": med(raw[m], "shift_s"),
        "failed_attempts": med(raw[m], "failed_attempts"),
        "errors": med(raw[m], "errors"),
        "error_pct": med(raw[m], "error_pct"),
        "p99_before_ms": med(raw[m], "latency_before", "p99_ms"),
        "p50_window_ms": med(raw[m], "latency_window", "p50_ms"),
        "p99_window_ms": med(raw[m], "latency_window", "p99_ms"),
        "max_window_ms": med(raw[m], "latency_window", "max_ms"),
    } for m in modes}
    print("MEDIAN OF RUNS:")
    for m, v in median.items():
        print(f"  {m}: " + ", ".join(f"{k}={v[k]}" for k in v))
    save("failover", {"specs": machine_specs(), "args": vars(args),
                      "raw": raw, "median": median})


if __name__ == "__main__":
    main()
