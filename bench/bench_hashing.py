"""
Benchmark 4 - consistent hashing vs naive hash-mod-N (pure computation).

    python bench/bench_hashing.py [--keys 100000] [--runs 3]

Uses the project's own cluster.consistent_hash.ConsistentHashRing and,
for the naive baseline, the SAME MD5 hash (ring._hash) modulo N.

A) Remapping when the cluster grows N -> N+1 (N = 3, 5, 10), 100
   virtual nodes (project default): % of keys whose PRIMARY node
   changed.  Ideal for consistent hashing = 1/(N+1); for mod-N the
   expected value is N/(N+1).
B) Load balance with N = 5 nodes and 1 / 10 / 100 / 200 virtual
   nodes: std dev of keys per node, as an absolute count and as % of
   the mean (coefficient of variation), plus max/mean.

The computation is deterministic, so "runs" vary the INPUTS: each run
uses a different random key sample and a different set of node
addresses (port base 9101 / 9201 / 9301).  Reported = median of runs.
"""

import argparse
import random
import statistics

from common import machine_specs, save
from cluster.consistent_hash import ConsistentHashRing


def nodes_for(n, base):
    return [f"http://127.0.0.1:{base + i}" for i in range(n)]


def ring_of(nodes, vnodes):
    r = ConsistentHashRing(virtual_nodes=vnodes)
    for n in nodes:
        r.add_node(n)
    return r


def remap(keys, n, base, vnodes=100):
    before = ring_of(nodes_for(n, base), vnodes)
    after = ring_of(nodes_for(n + 1, base), vnodes)
    h = [before._hash(k) for k in keys]
    ch = sum(before.get_node(k) != after.get_node(k) for k in keys)
    mod = sum((x % n) != (x % (n + 1)) for x in h)
    return 100.0 * ch / len(keys), 100.0 * mod / len(keys)


def balance(keys, n, base, vnodes):
    r = ring_of(nodes_for(n, base), vnodes)
    counts = {node: 0 for node in nodes_for(n, base)}
    for k in keys:
        counts[r.get_node(k)] += 1
    vals = list(counts.values())
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    return {"stdev_keys": sd, "cv_pct": 100 * sd / mean,
            "max_over_mean": max(vals) / mean,
            "min_over_mean": min(vals) / mean}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", type=int, default=100_000)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    runs = []
    for run in range(args.runs):
        rnd = random.Random(run)
        keys = [f"user:{rnd.getrandbits(64):x}" for _ in range(args.keys)]
        base = 9101 + 100 * run
        res = {"remap": {}, "balance": {}}
        for n in (3, 5, 10):
            ch, mod = remap(keys, n, base)
            res["remap"][n] = {"consistent_pct": ch, "mod_n_pct": mod,
                               "ideal_ch_pct": 100 / (n + 1),
                               "expected_mod_pct": 100 * n / (n + 1)}
            print(f"run {run} remap {n}->{n+1}: consistent={ch:.2f}% "
                  f"(ideal {100/(n+1):.2f}%)  mod-N={mod:.2f}%")
        for v in (1, 10, 100, 200):
            b = balance(keys, 5, base, v)
            res["balance"][v] = b
            print(f"run {run} balance N=5 vnodes={v:3d}: stdev={b['stdev_keys']:.0f} keys "
                  f"cv={b['cv_pct']:.1f}% max/mean={b['max_over_mean']:.2f}")
        runs.append(res)

    med = {"remap": {}, "balance": {}}
    for n in (3, 5, 10):
        med["remap"][n] = {f: statistics.median(r["remap"][n][f] for r in runs)
                           for f in runs[0]["remap"][n]}
    for v in (1, 10, 100, 200):
        med["balance"][v] = {f: statistics.median(r["balance"][v][f] for r in runs)
                             for f in runs[0]["balance"][v]}
    print("MEDIAN OF RUNS:")
    for n, m in med["remap"].items():
        print(f"  {n}->{n+1} nodes: consistent {m['consistent_pct']:.2f}% vs mod-N {m['mod_n_pct']:.2f}%")
    for v, m in med["balance"].items():
        print(f"  N=5 vnodes={v}: stdev {m['stdev_keys']:.0f} keys/node "
              f"(cv {m['cv_pct']:.1f}%), max/mean {m['max_over_mean']:.2f}")
    save("hashing", {"specs": machine_specs(), "args": vars(args),
                     "runs": runs, "median": med})


if __name__ == "__main__":
    main()
