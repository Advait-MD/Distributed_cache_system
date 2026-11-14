"""
Turn bench/results/*.json into small tidy row files for the results dashboard.

    python bench/make_dashboard_data.py

Writes bench/results/dashboard/<id>.json as {"rows": [...], "meta": [...]}.
Every value is a median of runs copied from the results JSON, rounded only
for display (ms to 2 decimals, percentages to 1, rates to whole numbers).
No new measurement happens here.
"""

import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
OUT = os.path.join(RES, "dashboard")
SELF = open(os.path.abspath(__file__), encoding="utf-8").read()


def load(name):
    with open(os.path.join(RES, f"{name}.json")) as handle:
        return json.load(handle)


def write(dataset, rows, inputs):
    os.makedirs(OUT, exist_ok=True)
    meta = [
        {"label": "make_dashboard_data.py", "value": SELF, "type": "code", "lang": "python"},
        {"label": "Input", "value": ", ".join(f"bench/results/{i}.json" for i in inputs)},
        {"label": "Rounding", "value": "Medians of runs, rounded for display; full precision is in the input file."},
    ]
    with open(os.path.join(OUT, f"{dataset}.json"), "w") as handle:
        json.dump({"rows": rows, "meta": meta}, handle, indent=1)
    print(f"{dataset}: {len(rows)} rows")


def r(x, n=2):
    return round(float(x), n)


# --- machine -------------------------------------------------------------
s = load("latency")["specs"]
write("machine", [
    {"item": "CPU", "value": f"{s['cpu']}, {s['logical_cpus']} vCPUs"},
    {"item": "RAM", "value": f"{s['ram_gib']} GiB"},
    {"item": "OS", "value": f"{s['os']}, kernel {s['kernel']}"},
    {"item": "Python", "value": s["python"]},
    {"item": "Stack", "value": f"FastAPI {s['fastapi']}, Uvicorn {s['uvicorn']}, SQLite {s['sqlite']}, httpx {s['httpx']}"},
    {"item": "CPU pinning", "value": s["cpu_pinning"]},
    {"item": "Network", "value": "loopback (127.0.0.1) only"},
], ["latency"])

# --- latency -------------------------------------------------------------
lat = load("latency")["median"]
names = {
    "hit": "Cache hit (router)",
    "miss": "Cache miss (router)",
    "direct_node_get": "Direct node GET",
    "raw_sqlite_get": "Raw SQLite read",
}
write("latency", [
    {"path": names[k], "p50_ms": r(v["p50_ms"]), "p95_ms": r(v["p95_ms"]),
     "p99_ms": r(v["p99_ms"]), "mean_ms": r(v["mean_ms"])}
    for k, v in lat.items()
], ["latency"])

# --- lru -----------------------------------------------------------------
lru = load("lru")["median_ops_per_sec"]
ops = {
    "get_hit": "get (hit)",
    "get_miss": "get (miss)",
    "put_update": "put (overwrite)",
    "put_insert_evict": "put (new key, evicts)",
    "dict_get_reference": "dict get (reference)",
    "dict_set_reference": "dict set (reference)",
}
write("lru", [
    {"op": ops[k], "ops_per_sec": round(v), "reference": k.startswith("dict")}
    for k, v in lru.items()
], ["lru"])

# --- throughput ----------------------------------------------------------
tp = load("throughput")
rows = []
for nodes, by_c in tp["median"].items():
    for c, m in by_c.items():
        rows.append({"nodes": int(nodes), "concurrency": int(c), "rps": round(m["rps"]),
                     "p50_ms": r(m["p50_ms"], 1), "p99_ms": r(m["p99_ms"], 1),
                     "errors": m["errors_total"]})
write("throughput", rows, ["throughput"])
write("loadgen", [
    {"concurrency": int(c), "rps": round(v)}
    for c, v in tp["loadgen_ceiling_median"].items()
], ["throughput"])

# --- zipf ----------------------------------------------------------------
z = load("zipf")["median"]
rows = []
for label, m in z.items():
    s_val = float(label.split(",")[0].split("=")[1])
    cap = int(label.split("cap=")[1].rstrip("%"))
    rows.append({"zipf_s": s_val, "capacity_pct": cap,
                 "never_touched_sqlite_pct": r(m["never_touched_sqlite_pct"], 1),
                 "ideal_pct": r(m["ideal_static_pct"], 1),
                 "sql_queries": int(m["sql_queries"]),
                 "per_node_capacity": int(m["per_node_capacity"])})
write("zipf", rows, ["zipf"])

# --- hashing -------------------------------------------------------------
h = load("hashing")["median"]
write("remap", [
    {"growth": f"{n} → {int(n) + 1}", "nodes_before": int(n),
     "consistent_pct": r(m["consistent_pct"], 1), "ideal_pct": r(m["ideal_ch_pct"], 1),
     "mod_n_pct": r(m["mod_n_pct"], 1)}
    for n, m in h["remap"].items()
], ["hashing"])
write("balance", [
    {"vnodes": int(v), "stdev_keys": round(m["stdev_keys"]), "cv_pct": r(m["cv_pct"], 1),
     "max_over_mean": r(m["max_over_mean"])}
    for v, m in h["balance"].items()
], ["hashing"])

# --- failover ------------------------------------------------------------
f = load("failover")
rows = []
for mode, runs in f["raw"].items():
    med = lambda fn: statistics.median(fn(x) for x in runs)
    rows.append({
        "mode": "Crash (SIGKILL)" if mode == "crash" else "Hang (SIGSTOP)",
        "detection_s": r(med(lambda x: x["detection_s"])),
        "first_unhealthy_s": r(med(lambda x: x["polled_unhealthy_s"])),
        "last_dead_attempt_s": r(med(lambda x: x["shift_s"])),
        "fallback_requests": round(med(lambda x: x["failed_attempts"])),
        "failed_requests": round(med(lambda x: x["errors"])),
        "requests_after_kill": round(med(lambda x: x["requests_after_kill"])),
        "p99_before_ms": r(med(lambda x: x["latency_before"]["p99_ms"]), 1),
        "p99_window_ms": r(med(lambda x: x["latency_window"]["p99_ms"]), 1),
        "rps_before": round(med(lambda x: x["latency_before"]["n"] / 8.0)),
        "rps_window": round(med(lambda x: x["requests_in_window"] / x["detection_s"])),
    })
write("failover", rows, ["failover"])

# --- single-flight -------------------------------------------------------
sf = load("singleflight")
rows = []
for key, kind in (("cold_key", "Key in SQLite, not cached"), ("absent_key", "Key missing everywhere")):
    for mode in ("with", "without"):
        k = f"{mode}/{key}"
        rows.append({"key_kind": kind, "single_flight": "On" if mode == "with" else "Off",
                     "median_db_queries": sf["median_db_queries"][k],
                     "worst_db_queries": sf["worst_db_queries"][k],
                     "p99_ms": r(sf["median_p99_ms"][k], 1)})
write("singleflight", rows, ["singleflight"])

# --- breaker -------------------------------------------------------------
b = load("breaker")["median"]
labels = {
    "router_http_slow": ("Slow failure", "Production router, breaker on"),
    "inproc_slow_breaker": ("Slow failure", "Breaker on"),
    "inproc_slow_no_breaker": ("Slow failure", "No breaker"),
    "inproc_fast_breaker": ("Fast failure", "Breaker on"),
    "inproc_fast_no_breaker": ("Fast failure", "No breaker"),
}
write("breaker", [
    {"scenario": f"{labels[k][0]} · {labels[k][1]}", "failure": labels[k][0],
     "setup": labels[k][1],
     "first3_median_ms": r(statistics.median(v["first3_median_ms"]), 1),
     "after_open_ms": r(v["after_open_median_ms"], 1)}
    for k, v in b.items()
], ["breaker"])

# --- tests ---------------------------------------------------------------
t = load("tests")
last = t["runs"][-1]
c = last["counts"]
write("tests", [{
    "collected": c.get("passed", 0) + c.get("skipped", 0) + c.get("failed", 0),
    "passed": c.get("passed", 0), "skipped": c.get("skipped", 0), "failed": c.get("failed", 0),
    "coverage_pct": r(t["median_total_cov_pct"], 1),
    "covered_lines": last["covered_lines"], "statements": last["statements"],
}], ["tests"])
write("coverage", [
    {"package": k.replace(".py", ""), "covered": v["covered"], "statements": v["statements"],
     "pct": r(v["pct"], 1)}
    for k, v in sorted(last["per_package"].items(), key=lambda kv: -kv[1]["pct"])
], ["tests"])
