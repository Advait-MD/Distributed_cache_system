"""
Benchmark 3 - hit rate under a Zipf workload, end to end over HTTP.

    python bench/bench_zipf.py [--runs 3] [--keys 10000] [--warm 20000] [--measure 30000]

Cluster: 3 nodes, replication factor 2 (router default), fresh SQLite
holding all K keys.  "Cache capacity = X% of the keyspace" means the
cluster can hold X% * K UNIQUE keys:

    per-node capacity = ceil(X% * K * RF / N)      (each key lives on RF nodes)

Requests: key rank r drawn with P(r) ~ 1 / r^s over the K keys (numpy,
seeded per run), sent closed-loop at concurrency 10.
    - `warm` requests first (cache fills, excluded), then the router's
      telemetry is reset, then `measure` requests are counted.
Reported:
    never_touched_sqlite_pct = router telemetry cache_reads / served_reads
        (the router counts real SQL queries; a read coalesced by
        single-flight does NOT count as an SQL query)
    client_cache_pct = responses whose body says source=cache
        (conservative: coalesced followers report source=database)
    ideal_static_pct = probability mass of the top-(X% * K) keys, i.e.
        what a cache that magically held exactly the hottest keys would
        get.  An upper bound for LRU, for context.
"""

import argparse
import math
import statistics

import numpy as np

from common import Cluster, machine_specs, pin_client, run_load, save

S_VALUES = [1.0, 1.2]
CAP_PCT = [1, 5, 10]
NODES = 3
RF = 2


def zipf_probs(k, s):
    w = 1.0 / np.arange(1, k + 1) ** s
    return w / w.sum()


def one(run, s, cap_pct, k, warm, measure):
    unique_cap = int(k * cap_pct / 100)
    per_node = math.ceil(unique_cap * RF / NODES)
    probs = zipf_probs(k, s)
    rng = np.random.default_rng(1000 * run + int(s * 10) + cap_pct)
    ranks = rng.choice(k, size=warm + measure, p=probs)
    keys = [f"z:{r}" for r in ranks]

    with Cluster(nodes=NODES, capacity=per_node,
                 name=f"zipf-{s}-{cap_pct}-{run}") as c:
        c.preload_db([f"z:{i}" for i in range(k)])
        run_load(c.url, keys[:warm], concurrency=10)
        c.reset_telemetry()
        rec, t0, t1 = run_load(c.url, keys[warm:], concurrency=10)
        tel = c.telemetry()["telemetry"]

    ok = [r for r in rec if r[2] == 200]
    return {
        "per_node_capacity": per_node,
        "unique_capacity": unique_cap,
        "never_touched_sqlite_pct": 100.0 * tel["cache_reads"] / tel["served_reads"],
        "sql_queries": tel["db_reads"],
        "served_reads": tel["served_reads"],
        "client_cache_pct": 100.0 * sum(r[4] == "cache" for r in ok) / len(ok),
        "errors": len(rec) - len(ok),
        "ideal_static_pct": 100.0 * float(probs[:unique_cap].sum()),
        "rps": len(rec) / (t1 - t0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keys", type=int, default=10_000)
    ap.add_argument("--warm", type=int, default=20_000)
    ap.add_argument("--measure", type=int, default=30_000)
    args = ap.parse_args()
    pin_client()

    raw = {}
    for s in S_VALUES:
        for cap in CAP_PCT:
            label = f"s={s},cap={cap}%"
            raw[label] = []
            for run in range(args.runs):
                r = one(run, s, cap, args.keys, args.warm, args.measure)
                raw[label].append(r)
                print(f"{label} run {run}: never-touched-SQLite="
                      f"{r['never_touched_sqlite_pct']:.2f}% client-cache="
                      f"{r['client_cache_pct']:.2f}% ideal={r['ideal_static_pct']:.2f}% "
                      f"sql={r['sql_queries']} errors={r['errors']}", flush=True)

    median = {label: {f: statistics.median(x[f] for x in runs)
                      for f in ("never_touched_sqlite_pct", "client_cache_pct",
                                "ideal_static_pct", "sql_queries", "errors",
                                "per_node_capacity", "unique_capacity")}
              for label, runs in raw.items()}
    print("MEDIAN OF RUNS:")
    for label, m in median.items():
        print(f"  {label:16s} never touched SQLite {m['never_touched_sqlite_pct']:.2f}% "
              f"(client-side {m['client_cache_pct']:.2f}%, static-ideal "
              f"{m['ideal_static_pct']:.2f}%)")
    save("zipf", {"specs": machine_specs(), "args": vars(args),
                  "raw": raw, "median": median})


if __name__ == "__main__":
    main()
