"""
Benchmark 7 - circuit breaker while the database is failing.

    python bench/bench_breaker.py [--runs 3] [--requests 20]

Failure injection (real SQLite, no mocks):
    slow failure : another process holds an EXCLUSIVE lock on the
                   database file (PRAGMA locking_mode=EXCLUSIVE +
                   BEGIN EXCLUSIVE).  Every read waits for SQLite's
                   busy timeout (the repository's timeout=5 s) and then
                   fails with "database is locked".
    fast failure : the database file is overwritten with garbage, so
                   every read fails immediately ("file is not a
                   database").

A) Production router over HTTP (breaker: open after 3 consecutive
   failures, 30 s recovery timeout - the defaults): `requests`
   sequential GETs of different keys that are not cached.
B) In-process, real components, breaker on vs off:
   CacheService(real DistributedCache -> 3 real nodes, real
   DatabaseRepository) with circuit_breaker = CircuitBreaker() vs a
   pass-through object whose call(fn, ...) just calls fn(...).
   (CacheService accepts the circuit_breaker argument; no core change.)

Reported per mode: latency of each failed request; median over the
requests AFTER the breaker opened (request 4..N) and over all N.
"""

import argparse
import os
import sqlite3
import statistics
import time

import httpx

from common import Cluster, machine_specs, pin_client, save


class PassThroughBreaker:
    def call(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def get_stats(self):
        return {"state": "none"}


class Locker:
    """Hold an exclusive lock on an SQLite file (slow failure)."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        self.conn.execute("BEGIN EXCLUSIVE")
        self.conn.execute(
            "INSERT OR REPLACE INTO cache_data(key, value) VALUES('lock', '1')")
        return self

    def __exit__(self, *exc):
        self.conn.execute("ROLLBACK")
        self.conn.close()


def corrupt(path):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    with open(path, "wb") as handle:
        handle.write(b"this is not an sqlite database " * 200)


def router_http(c, n, run):
    keys = [f"cb:{run}:{i}" for i in range(n)]
    c.preload_db(keys)
    out = []
    with Locker(c.db_path), httpx.Client(timeout=30) as client:
        for k in keys:
            t0 = time.perf_counter()
            r = client.get(f"{c.url}/cache/{k}")
            out.append(((time.perf_counter() - t0) * 1000, r.status_code))
        state = client.get(f"{c.url}/circuit-breaker/status").json()
    return out, state


def in_process(c, n, run, failure):
    from cluster.node_manager import NodeManager
    from cluster.node_state import NodeState
    from database.exceptions import DatabaseUnavailableError
    from database.repository import DatabaseRepository
    from services.cache_service import CacheService
    from services.circuit_breaker import CircuitBreaker
    from services.distributed_cache import DistributedCache

    res = {}
    for mode in ("breaker", "no_breaker"):
        path = os.path.join(c.workdir, f"inproc-{failure}-{mode}-{run}.db")
        repo = DatabaseRepository(db_path=path)
        keys = [f"cbi:{mode}:{run}:{i}" for i in range(n)]
        repo.set_many((k, {"v": 1}) for k in keys)
        nm = NodeManager(nodes=[c.node_url(p) for p in c.ports],
                         initial_state=NodeState.READY)
        svc = CacheService(
            cache=DistributedCache(nm), database=repo,
            circuit_breaker=CircuitBreaker() if mode == "breaker" else PassThroughBreaker())
        lat = []

        def go():
            for k in keys:
                t0 = time.perf_counter()
                try:
                    svc.get(k)
                    ok = "200"
                except DatabaseUnavailableError:
                    ok = "unavailable"
                lat.append(((time.perf_counter() - t0) * 1000, ok))

        if failure == "slow":
            with Locker(path):
                go()
        else:
            corrupt(path)
            go()
        res[mode] = lat
    return res


def summarize(lat):
    ms = [x[0] for x in lat]
    return {"all_median_ms": statistics.median(ms),
            "after_open_median_ms": statistics.median(ms[3:]),
            "first3_ms": ms[:3],
            "statuses": sorted({str(x[1]) for x in lat})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--requests", type=int, default=20)
    args = ap.parse_args()
    pin_client()

    raw = []
    for run in range(args.runs):
        with Cluster(nodes=3, capacity=50000, name=f"cb-{run}") as c:
            http_lat, state = router_http(c, args.requests, run)
            r = {"router_http_slow": http_lat, "router_breaker_state": state}
            for failure in ("slow", "fast"):
                ip = in_process(c, args.requests, run, failure)
                r[f"inproc_{failure}_breaker"] = ip["breaker"]
                r[f"inproc_{failure}_no_breaker"] = ip["no_breaker"]
            raw.append(r)
        for k, v in r.items():
            if isinstance(v, list):
                s = summarize(v)
                print(f"run {run} {k:28s} median(after open)={s['after_open_median_ms']:.2f}ms "
                      f"median(all)={s['all_median_ms']:.2f}ms first3={[round(x) for x in s['first3_ms']]} "
                      f"statuses={s['statuses']}", flush=True)

    keys = [k for k in raw[0] if isinstance(raw[0][k], list)]
    median = {k: {
        "after_open_median_ms": statistics.median(summarize(r[k])["after_open_median_ms"] for r in raw),
        "all_median_ms": statistics.median(summarize(r[k])["all_median_ms"] for r in raw),
        "first3_median_ms": [statistics.median(summarize(r[k])["first3_ms"][i] for r in raw) for i in range(3)],
    } for k in keys}
    print("MEDIAN OF RUNS:")
    for k, v in median.items():
        print(f"  {k:28s} after-open {v['after_open_median_ms']:.2f}ms  all {v['all_median_ms']:.2f}ms  "
              f"first3 {[round(x, 1) for x in v['first3_median_ms']]}")
    save("breaker", {"specs": machine_specs(), "args": vars(args),
                     "raw": raw, "median": median})


if __name__ == "__main__":
    main()
