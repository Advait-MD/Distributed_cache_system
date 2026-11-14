"""
Benchmark 1b - in-process LRUCache get/put throughput (no HTTP).

    python bench/bench_lru.py [--runs 5] [--ops 1000000] [--capacity 100000]

Single thread, the project's own cache.lru_cache.LRUCache (thread-safe:
every op takes its lock).  Keys are pre-built strings so string
formatting is not timed.

    put_insert_evict : `ops` puts of NEW keys into a full cache
                       (steady state: every put evicts the LRU tail)
    put_update       : `ops` puts that overwrite resident keys
    get_hit          : `ops` gets of resident keys, random order
    get_miss         : `ops` gets of absent keys
A plain dict get/set loop is timed the same way as a reference ceiling.
"""

import argparse
import random
import statistics
import time

from common import machine_specs, pin_client, save
from cache.lru_cache import LRUCache


def timed(fn, ops):
    t0 = time.perf_counter()
    fn()
    dt = time.perf_counter() - t0
    return ops / dt


def one_run(seed, ops, capacity):
    rnd = random.Random(seed)
    resident = [f"key:{i}" for i in range(capacity)]
    fresh = [f"new:{seed}:{i}" for i in range(ops)]
    hits = [resident[rnd.randrange(capacity)] for _ in range(ops)]
    absent = [f"absent:{i}" for i in range(ops)]

    cache = LRUCache(capacity=capacity)
    for k in resident:
        cache.set(k, 1)

    out = {}
    out["get_hit"] = timed(lambda: [cache.get(k) for k in hits], ops)
    out["get_miss"] = timed(lambda: [cache.get(k) for k in absent], ops)
    out["put_update"] = timed(lambda: [cache.set(k, 2) for k in hits], ops)
    out["put_insert_evict"] = timed(lambda: [cache.set(k, 3) for k in fresh], ops)
    assert len(cache.cache) == capacity

    d = {k: 1 for k in resident}
    out["dict_get_reference"] = timed(lambda: [d.get(k) for k in hits], ops)
    out["dict_set_reference"] = timed(lambda: [d.__setitem__(k, 2) for k in hits], ops)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--ops", type=int, default=1_000_000)
    ap.add_argument("--capacity", type=int, default=100_000)
    args = ap.parse_args()
    pin_client()

    runs = []
    for i in range(args.runs):
        r = one_run(i, args.ops, args.capacity)
        runs.append(r)
        print(f"run {i}: " + "  ".join(f"{k}={v:,.0f}/s" for k, v in r.items()))

    median = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
    print("MEDIAN OF RUNS (ops/sec, single thread):")
    for k, v in median.items():
        print(f"  {k:20s} {v:>12,.0f}")
    save("lru", {"specs": machine_specs(), "args": vars(args),
                 "runs": runs, "median_ops_per_sec": median})


if __name__ == "__main__":
    main()
