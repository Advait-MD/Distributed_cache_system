"""
Benchmark 1a - cache HIT vs cache MISS latency through the router over HTTP.

    python bench/bench_latency.py [--runs 3] [--keys 2000]

Per run (fresh cluster: 3 nodes + router, fresh SQLite file):
    1. preload K rows straight into SQLite (caches cold)
    2. 200 warm-up requests on separate keys (excluded)
    3. MISS phase: GET each of the K keys once, sequentially (c=1).
       Every response must say source=database.
    4. HIT  phase: GET the same K keys again in shuffled order (c=1).
       Every response must say source=cache.
Also measured, for decomposition: a direct node GET (no router) and a
raw in-process SQLite DatabaseRepository.get() on the same file.
"""

import argparse
import random
import time

import httpx

from common import (Cluster, machine_specs, median_of, pin_client,
                    run_load, save, summarize_ms)


def one_run(run_idx, n_keys):
    with Cluster(nodes=3, capacity=50000, name=f"latency-{run_idx}") as c:

        keys = [f"lat:{i}" for i in range(n_keys)]
        warm = [f"warm:{i}" for i in range(200)]
        c.preload_db(keys + warm)

        run_load(c.url, warm, concurrency=1)          # discard
        run_load(c.url, warm, concurrency=1)          # discard

        miss_rec, _, _ = run_load(c.url, keys, concurrency=1)
        shuffled = keys[:]
        random.Random(run_idx).shuffle(shuffled)
        hit_rec, _, _ = run_load(c.url, shuffled, concurrency=1)

        miss_src = {r[4] for r in miss_rec}
        hit_src = {r[4] for r in hit_rec}
        assert miss_src == {"database"}, miss_src
        assert hit_src == {"cache"}, hit_src
        assert all(r[2] == 200 for r in miss_rec + hit_rec)

        # Decomposition 1: direct node GET (one HTTP hop, no router).
        node = f"http://127.0.0.1:{c.ports[0]}"
        node_keys = []
        with httpx.Client() as client:
            for k in keys:
                if client.get(f"{node}/cache/{k}").status_code == 200:
                    node_keys.append(k)
                if len(node_keys) >= 1000:
                    break
            lat = []
            for k in node_keys:
                t0 = time.perf_counter()
                r = client.get(f"{node}/cache/{k}")
                lat.append(time.perf_counter() - t0)
                assert r.status_code == 200

        # Decomposition 2: raw SQLite read, in-process (same file).
        from database.repository import DatabaseRepository
        repo = DatabaseRepository(db_path=c.db_path)
        sql = []
        for k in keys:
            t0 = time.perf_counter()
            repo.get(k)
            sql.append(time.perf_counter() - t0)

        return {
            "miss": summarize_ms([r[1] for r in miss_rec]),
            "hit": summarize_ms([r[1] for r in hit_rec]),
            "direct_node_get": summarize_ms(lat),
            "raw_sqlite_get": summarize_ms(sql),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keys", type=int, default=2000)
    args = ap.parse_args()

    pin_client()
    runs = []
    for i in range(args.runs):
        r = one_run(i, args.keys)
        runs.append(r)
        print(f"run {i}: " + "  ".join(
            f"{k}: p50={v['p50_ms']:.3f} p95={v['p95_ms']:.3f} p99={v['p99_ms']:.3f}"
            for k, v in r.items()))

    median = {
        kind: {f: median_of([r[kind] for r in runs], f)
               for f in ("p50_ms", "p95_ms", "p99_ms", "mean_ms")}
        for kind in runs[0]
    }
    print("MEDIAN OF RUNS:")
    for kind, v in median.items():
        print(f"  {kind:16s} p50={v['p50_ms']:.3f}ms p95={v['p95_ms']:.3f}ms "
              f"p99={v['p99_ms']:.3f}ms mean={v['mean_ms']:.3f}ms")
    save("latency", {"specs": machine_specs(), "args": vars(args),
                     "runs": runs, "median": median})


if __name__ == "__main__":
    main()
