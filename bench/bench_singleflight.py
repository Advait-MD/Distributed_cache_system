"""
Benchmark 6 - single-flight: 100 concurrent requests for one key that is
NOT in the cache.

    python bench/bench_singleflight.py [--runs 3] [--trials 10]

A) In-process, real components, the switch is the only difference:
   services.cache_service.CacheService wired to the project's real
   DistributedCache (HTTP to 3 real node processes) and a real SQLite
   DatabaseRepository wrapped in a thread-safe call counter.
       with    : single_flight = services.single_flight.SingleFlight()
       without : single_flight = an object whose do(key, fn) just calls fn()
   (CacheService accepts the single_flight argument; no core change.)
   100 threads released together by a threading.Barrier call get(key).
   Two key kinds:
       cold_key   : row exists in SQLite, not in any cache node
       absent_key : row exists nowhere (cache does not store negatives)

B) Over HTTP through the production router (single-flight always on):
   100 simultaneous GETs (async httpx, 100 connections) for one cold
   key; SQL queries = delta of the router's telemetry db_reads.
"""

import argparse
import logging
import statistics
import threading
import time

from common import (Cluster, machine_specs, pin_client, run_load, save,
                    summarize_ms)

logging.getLogger("urllib3").setLevel(logging.ERROR)

BURST = 100


class CountingDB:
    def __init__(self, repo):
        self.repo = repo
        self.lock = threading.Lock()
        self.gets = 0

    def get(self, key):
        with self.lock:
            self.gets += 1
        return self.repo.get(key)

    def set(self, key, value):
        return self.repo.set(key, value)


class NoSingleFlight:
    def do(self, key, function):
        return function()


def burst(service, db, key):
    barrier = threading.Barrier(BURST)
    lat = [None] * BURST
    src = [None] * BURST

    def worker(i):
        barrier.wait()
        t0 = time.perf_counter()
        r = service.get(key)
        lat[i] = time.perf_counter() - t0
        src[i] = r["source"] if r else "none"

    before = db.gets
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(BURST)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return db.gets - before, lat, src


def in_process(c, run, trials):
    from cluster.node_manager import NodeManager
    from cluster.node_state import NodeState
    from database.repository import DatabaseRepository
    from services.cache_service import CacheService
    from services.distributed_cache import DistributedCache
    from services.single_flight import SingleFlight

    out = {}
    for mode in ("with", "without"):
        for kind in ("cold_key", "absent_key"):
            nm = NodeManager(nodes=[c.node_url(p) for p in c.ports],
                             initial_state=NodeState.READY)
            db = CountingDB(DatabaseRepository(db_path=c.db_path))
            svc = CacheService(
                cache=DistributedCache(nm),
                database=db,
                single_flight=SingleFlight() if mode == "with" else NoSingleFlight(),
            )
            queries, lats = [], []
            for t in range(trials):
                key = f"sf:{kind}:{mode}:{run}:{t}"
                if kind == "cold_key":
                    db.repo.set(key, {"v": t})
                q, lat, src = burst(svc, db, key)
                queries.append(q)
                lats.extend(lat)
            out[f"{mode}/{kind}"] = {
                "db_queries_per_burst": queries,
                "median_db_queries": statistics.median(queries),
                "max_db_queries": max(queries),
                "latency": summarize_ms(lats),
            }
            print(f"  run {run} {mode:7s} {kind:10s}: DB queries per 100 = {queries} "
                  f"p50={out[f'{mode}/{kind}']['latency']['p50_ms']:.1f}ms "
                  f"p99={out[f'{mode}/{kind}']['latency']['p99_ms']:.1f}ms", flush=True)
    return out


def over_http(c, run, trials):
    queries = []
    for t in range(trials):
        key = f"sfh:{run}:{t}"
        c.preload_db([key])
        before = c.telemetry()["telemetry"]["db_reads"]
        rec, _, _ = run_load(c.url, [key] * BURST, concurrency=BURST)
        after = c.telemetry()["telemetry"]["db_reads"]
        assert all(r[2] == 200 for r in rec), {r[2] for r in rec}
        queries.append(after - before)
    print(f"  run {run} HTTP router (single-flight on): SQL queries per 100 = {queries}")
    return {"db_queries_per_burst": queries,
            "median_db_queries": statistics.median(queries)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--trials", type=int, default=10)
    args = ap.parse_args()
    pin_client()

    raw = []
    for run in range(args.runs):
        with Cluster(nodes=3, capacity=50000, name=f"sf-{run}") as c:
            res = in_process(c, run, args.trials)
            res["http_router"] = over_http(c, run, args.trials)
            raw.append(res)

    median = {k: statistics.median(r[k]["median_db_queries"] for r in raw)
              for k in raw[0]}
    p99 = {k: statistics.median(r[k]["latency"]["p99_ms"] for r in raw)
           for k in raw[0] if "latency" in raw[0][k]}
    worst = {k: max(r[k].get("max_db_queries", max(r[k]["db_queries_per_burst"]))
                    for r in raw) for k in raw[0]}
    print("MEDIAN DB QUERIES PER 100 CONCURRENT REQUESTS:")
    for k in median:
        print(f"  {k:22s} median={median[k]}  worst={worst[k]}  "
              f"p99 latency={p99.get(k)}")
    save("singleflight", {"specs": machine_specs(), "args": vars(args),
                          "raw": raw, "median_db_queries": median,
                          "worst_db_queries": worst, "median_p99_ms": p99})


if __name__ == "__main__":
    main()
