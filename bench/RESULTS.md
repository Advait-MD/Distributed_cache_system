# DCS — Measured Performance Results

Every number on this page was **measured** on the machine below by the scripts in
`bench/`. Nothing is estimated. Each benchmark ran **3+ times** and the table reports the
**median of runs**. Per-run raw output is in `bench/results/raw/*.txt` and the
full machine-readable data in `bench/results/*.json`.

Measured 2026-11-14 on commit branch `ccr-83814909-j0wn9u`.

---

## 1. Results at a glance

| # | What | Result (median of runs) |
|---|---|---|
| 1a | Cache **hit** latency through router over HTTP (c=1) | **p50 2.38 ms · p95 3.11 ms · p99 3.72 ms** |
| 1a | Cache **miss** latency (SQLite read + cache fill) | **p50 8.00 ms · p95 10.17 ms · p99 11.66 ms** |
| 1a | Hit vs miss | **3.4× lower p50**, 3.1× lower p99 |
| 1a | For context: raw in-process SQLite read · direct node GET (no router) | p50 0.12 ms · p50 0.81 ms |
| 1b | In-process `LRUCache` (1 thread, with lock) | **get hit 924K ops/s · put+evict 736K ops/s** · put-update 1.09M · get-miss 3.05M |
| 2 | Throughput, cache hits, 1 / 3 / 5 nodes | **~790 req/s plateau** (c≥10), **418 req/s at c=1**; node count makes no difference (router-bound) |
| 3 | Zipf s=1.0, cache = 1% / 5% / 10% of keyspace | **40.3% / 60.7% / 69.8%** of reads never touched SQLite |
| 3 | Zipf s=1.2, cache = 1% / 5% / 10% of keyspace | **67.3% / 82.2% / 87.6%** of reads never touched SQLite |
| 4 | Keys remapped going N→N+1 (3→4 · 5→6 · 10→11) | **consistent hashing 26.3% · 16.2% · 8.6%** vs **mod-N 74.9% · 83.4% · 90.9%** |
| 4 | Load balance, 5 nodes, 100K keys: std dev of keys/node | **1 vnode: 14,734 (CV 73.7%)** → **100 vnodes: 1,557 (CV 7.8%)** |
| 5 | Node **crash** (SIGKILL) under load | **detected in 5.9 s · 0 failed requests** of ~13.5K · p99 23 ms → 28 ms in the window |
| 5 | Node **hang** (SIGSTOP) under load | detected in 8.3 s · 0 failed requests · but 50 requests stalled 2 s and throughput fell 659 → 18 req/s until detection |
| 6 | 100 concurrent requests, same uncached key: SQL queries | **with single-flight: 1** · without: median **28** (worst 99) |
| 6 | Same, key absent from DB too | with: 2.5 · without: 100 |
| 7 | Failed request latency, **slow** DB failure (locked file) | **breaker open: 7.1 ms** vs **no breaker: 5,016 ms** (~700×) |
| 7 | Failed request latency, **fast** DB failure (corrupt file) | breaker 4.6 ms vs no breaker 4.6 ms (**no difference**) |
| 8 | Test suite | **377 tests: 376 passed, 1 skipped** |
| 8 | Line coverage (pytest-cov, Python only) | **89.1%** (3,318 / 3,725 statements) |

---

## 2. Machine and software

| | |
|---|---|
| CPU | Intel Xeon @ 2.30 GHz, 4 vCPUs (KVM guest), 1 thread/core |
| RAM | 15.7 GiB |
| OS | Ubuntu 24.04.5 LTS, kernel 6.18.44 |
| Python | 3.13.18 |
| Libraries | FastAPI 0.162.7, Uvicorn 0.54.0, httpx 0.28.1, SQLite 3.53.1, numpy 2.5.3, pytest 9.2.0, pytest-cov 7.1.0 |
| CPU pinning | router + nodes on CPUs 0–2 (`taskset -c 0-2`), load generator on CPU 3 |
| Network | loopback (127.0.0.1) only — **no real network latency** |

## 3. Exact commands

```bash
pip install -r requirements.txt httpx numpy pytest-cov
bash bench/run_all.sh          # runs everything below, ~75 min

python bench/bench_latency.py      --runs 3 --keys 2000
python bench/bench_lru.py          --runs 5 --ops 1000000 --capacity 100000
python bench/bench_throughput.py   --runs 3 --duration 10
python bench/bench_zipf.py         --runs 3 --keys 10000 --warm 20000 --measure 30000
python bench/bench_hashing.py      --runs 3 --keys 100000
python bench/bench_failover.py     --runs 3 --mode both
python bench/bench_singleflight.py --runs 3 --trials 10
python bench/bench_breaker.py      --runs 3 --requests 20
python bench/bench_tests.py        --runs 3
```

How every HTTP benchmark starts the system (`bench/common.py`, `Cluster`): real processes
exactly as the README does — `python main.py --port P --capacity C` per node and
`python -m uvicorn router.router:app --port 9100` for the router — each run in a fresh
working directory with a fresh SQLite file. Router configuration is passed only through
environment variables `router/config.py` already reads:
`CACHE_NODES`, `CACHE_DB_PATH`, `CLUSTER_BOOTSTRAP_STATE=ready` (skip the boot gate),
`WARMUP_ENABLED=off`, `SEED_ENABLED=off` (nothing to warm in an empty database).
All other settings are the shipped defaults: replication factor 2, 100 virtual nodes,
health check every 2 s, node FAILED after 3 failed checks, 2 s node timeout, breaker opens
after 3 failures. Values are 100-byte JSON payloads.

**Load generator:** async httpx, closed loop (each worker waits for its answer before
sending the next). One httpx client with a 100-connection pool collapsed to ~150–450 req/s
against a no-op server, so workers are sharded across clients of 5 connections each. Its
measured ceiling against a no-op ASGI server (`bench/_null_server.py`) is
**1,363 / 1,677 / 1,901 / 1,844 req/s at c = 1 / 10 / 50 / 100**, 2–4× above anything the
router reached. The numbers below measure the router, not the client.

### What changed in core code

Nothing in the cache or router logic. One opt-in instrumentation hook, **off unless
`DCS_BENCH_INSTRUMENT=1`** (used only by the failover benchmark):

- `services/distributed_cache.py`: one call to `bench_counters.record_node_error(node)` in
  the existing `except requests.RequestException` branch of `get()` (a no-op when the
  flag is off).
- `router/router.py`: registers `GET /bench/counters` only when the flag is set.
- New files: `services/bench_counters.py`, `router/routes_bench.py`.

The single-flight and breaker "without" variants need no flag: `CacheService` already
accepts `single_flight=` and `circuit_breaker=` constructor arguments, so the bench
injects pass-through objects.

---

## 4. Methodology, results and how to explain each number

### 4.1 Latency — hit vs miss (`bench_latency.py`)

Per run: fresh 3-node cluster, 2,000 rows written straight into SQLite (caches cold),
400 warm-up requests on separate keys (discarded). Then, sequentially at concurrency 1:
**miss phase** — each key read once (every response checked to say `source=database`);
**hit phase** — the same keys again in shuffled order (every response checked to say
`source=cache`).

| | p50 | p95 | p99 | mean |
|---|---|---|---|---|
| Cache hit via router | 2.38 ms | 3.11 ms | 3.72 ms | 2.47 ms |
| Cache miss via router | 8.00 ms | 10.17 ms | 11.66 ms | 8.30 ms |
| Direct node GET (1 HTTP hop, no router) | 0.81 ms | 1.05 ms | 1.41 ms | 0.85 ms |
| Raw SQLite `get()` in-process | 0.12 ms | 0.22 ms | 0.27 ms | 0.13 ms |

> **Interview:** "A hit is one router hop plus one node hop, 2.4 ms p50. A miss is 3.4× slower at
> 8 ms because the router asks the primary and the replica, misses on both, re-checks both inside
> single-flight, reads SQLite, then writes the value to two nodes. That's 6 internal HTTP calls."

> **Brutally honest part, and be ready for it:** a raw local SQLite read (0.12 ms) is **faster than a cache
> hit** (2.38 ms). With a local, tiny database the HTTP hops cost more than the storage.
> The cache only pays off when the source of truth is slow or remote (a networked Postgres, an
> API, an expensive query). That was **not** measured here. Say this before the interviewer does.

### 4.2 In-process LRU (`bench_lru.py`)

The project's `cache.lru_cache.LRUCache` (hash map + doubly linked list, thread lock on
every op), 1 thread, capacity 100,000, 1M ops per measurement, 5 runs. Keys pre-built so
string formatting isn't timed.

| op | ops/sec (median) |
|---|---|
| get (hit, random order) | 923,515 |
| get (miss) | 3,048,404 |
| put (overwrite resident key) | 1,091,047 |
| put (new key into full cache → evicts LRU tail) | 736,410 |
| reference: plain `dict.get` / `dict[k]=v` | 15.3M / 7.9M |

> **Interview:** "About 0.9M gets/s and 0.74M evicting puts/s on one thread, so each op is around a
> microsecond. That's 16× slower than a raw dict because of the lock, the TTL check and the
> linked-list relinking. Every HTTP request costs ~2 ms, so the data structure is never the bottleneck."

### 4.3 Throughput (`bench_throughput.py`)

Per run and node count: fresh cluster, 10,000 rows loaded and read once, so every
measured request is a **cache hit**. For each concurrency: 2 s warm-up (discarded), then 10 s
measured. RPS = successful responses / measured time. 3 runs; 0 errors in all 36 configurations.

| nodes | c = 1 | c = 10 | c = 50 | c = 100 |
|---|---|---|---|---|
| 1 | 418 req/s (p50 2.3 / p99 3.5 ms) | 755 (13.4 / 21.9) | 786 (63.4 / 93.7) | 787 (125.4 / 174.0) |
| 3 | 411 (2.4 / 3.8) | 761 (13.2 / 22.1) | 747 (67.1 / 101.9) | 793 (124.0 / 166.9) |
| 5 | 416 (2.3 / 3.4) | 720 (14.0 / 22.8) | 800 (62.5 / 94.1) | 795 (124.1 / 167.4) |

> **Interview:** "Throughput levels off at about 790 req/s from c=10 upward. Past that, extra
> concurrency only queues: p99 goes from 22 ms to 170 ms. Adding nodes changes nothing, because
> the single Python router process is the bottleneck. Nodes add memory capacity and redundancy,
> not throughput. The fix would be multiple routers or client-side hashing."

> **Don't claim** that throughput "scales with nodes". It measurably doesn't.

### 4.4 Hit rate under Zipf (`bench_zipf.py`)

10,000 keys in SQLite. 3 nodes, replication factor 2. "Capacity X%" = the cluster can hold
X% of the keys **uniquely**: per-node capacity = ⌈X% × 10,000 × 2 / 3⌉ (67 / 334 / 667),
because every key is stored on two nodes. Key ranks are drawn with P(r) ∝ 1/r^s (numpy, seeded),
closed loop at c=10. 20,000 warm-up requests, then router telemetry reset, then 30,000
measured requests. **"Never touched SQLite"** = router telemetry `cache_reads / served_reads`.
The router counts real SQL queries, so a read coalesced by single-flight is not an SQL query.
The client-side count (response says `source=cache`) agrees within 0.2 points. "Static ideal"
= probability mass of the hottest X% of keys: what a cache that always held exactly the top
keys would get. It's an upper bound for context.

| Zipf s | cache = 1% | 5% | 10% |
|---|---|---|---|
| 1.0 — never touched SQLite | **40.3%** | **60.7%** | **69.8%** |
| 1.0 — static ideal | 53.0% | 69.4% | 76.5% |
| 1.2 — never touched SQLite | **67.3%** | **82.2%** | **87.6%** |
| 1.2 — static ideal | 75.1% | 86.5% | 90.3% |

SQL queries per 30,000 reads (median): s=1.0: 17,825 / 11,792 / 9,044 · s=1.2: 9,773 / 5,337 / 3,713. 0 errors.

> **Interview:** "With a cache sized to 10% of the keyspace, 70% of reads (s=1.0) to 88% (s=1.2)
> never reached SQLite. LRU gets within 3–7 points of an oracle that always holds the hottest
> keys. The gap is the cost of LRU's recency guess versus true frequency."

> **Caveat:** keyspace is 10K keys, synthetic Zipf, steady state after warm-up. Real traffic
> has churn and scans, which LRU handles worse.

### 4.5 Consistent hashing (`bench_hashing.py`)

Pure computation with the project's `ConsistentHashRing`. The baseline is the **same MD5 hash**
modulo N. 100,000 random keys. The math is deterministic, so the 3 runs vary the inputs:
different key samples and node address sets.

| Growth | Consistent hashing (100 vnodes) | Ideal 1/(N+1) | Naive hash mod N |
|---|---|---|---|
| 3 → 4 | **26.3%** of keys move | 25.0% | **74.9%** |
| 5 → 6 | **16.2%** | 16.7% | **83.4%** |
| 10 → 11 | **8.6%** | 9.1% | **90.9%** |

| Virtual nodes (N=5, mean 20,000 keys/node) | std dev keys/node | CV | max / mean |
|---|---|---|---|
| 1 | 14,734 | 73.7% | 2.36 |
| 10 | 5,289 | 26.4% | 1.49 |
| **100 (project default)** | **1,557** | **7.8%** | **1.08** |
| 200 | 1,569 | 7.8% | 1.14 |

> **Interview:** "Adding a 4th node moved 26% of keys, close to the ideal 25%. Mod-N moved 75%,
> which would be a near-total cache flush. With one point per node the busiest node got 2.4× its
> fair share; 100 virtual nodes cut the spread from 74% to 8%. Going to 200 bought nothing
> measurable at this key count."

### 4.6 Failover (`bench_failover.py`)

Fresh 3-node cluster (RF 2, shipped health-check defaults), 3,000 keys loaded and read once
so each sits on its primary **and** replica. Closed-loop load at c=10 for 30 s. At t=8 s node
:9102 is either **crashed** (`SIGKILL`: connections refused instantly) or **hung**
(`SIGSTOP`: TCP still accepts, nothing answers, so every request to it waits for the router's
2 s timeout). Times use the router's own failover timestamp (`/cluster/replication`
`last_failover.at`) and the opt-in counter of failed node attempts. Window = kill → FAILED.

| | crash (SIGKILL) | hang (SIGSTOP) |
|---|---|---|
| Detection: kill → node FAILED, out of ring | **5.87 s** (runs 5.53 / 5.87 / 5.97) | **8.25 s** (7.58 / 8.25 / 8.84) |
| First marked UNHEALTHY (client poll) | 1.88 s | 2.28 s |
| Last request routed to the dead node *completed* | 5.87 s | 10.15 s (= detection + the 2 s timeout of requests already in flight) |
| Requests that hit the dead node and fell back to replica | 1,184 | 50 |
| **Client-visible failed requests** | **0** of ~13,500 (0.000%) | **0** of ~7,500 (0.000%) |
| p99 latency before kill → during window | 23.2 ms → **28.1 ms** | 25.9 ms → **2,019 ms** |
| Throughput before → during window | ~736 → ~625 req/s | ~659 → **~18 req/s** |

> **Interview (crash):** "When a node crashed, the health checker took it out of the ring in 5.9 s:
> 3 missed checks at a 2 s interval. Users saw zero errors, because every read falls back to the
> replica on a refused connection. 1,184 reads paid that extra hop; p99 went from 23 to 28 ms."

> **Interview (hang), be honest:** "A *hung* node is worse than a dead one. There were still zero
> errors, but every request to it waited the full 2 s timeout. With 10 closed-loop clients,
> throughput fell 97% until detection at 8.3 s. The fix is shorter timeouts or hedged requests to
> the replica." This is the kind of answer that impresses interviewers.

### 4.7 Single-flight (`bench_singleflight.py`)

**In-process, real components:** `CacheService` with the real `DistributedCache`, which talks
HTTP to 3 real node processes, and a real SQLite repository wrapped in a call counter. 100 threads
released together by a `threading.Barrier` all call `get(key)`. The only difference is the
injected `single_flight` object. 10 bursts per run × 3 runs.
**Over HTTP:** 100 simultaneous requests to the production router, counting the router's
SQL-query telemetry.

| DB queries per 100 concurrent requests | with single-flight | without |
|---|---|---|
| key in SQLite, not cached | **1** (all 30 bursts) | median **28**, range 3–99 |
| key absent from SQLite too | 2.5 (range 1–6) | **100** (all 30 bursts) |
| HTTP through production router | **1** (all 30 bursts) | n/a (always on) |
| p99 latency of the burst (cold key) | 127 ms | 334 ms |

> **Interview:** "100 concurrent misses on one key produced exactly 1 database query, every
> time across 30 bursts. Without single-flight the median was 28 and the worst was 99. It isn't
> 100 because late requests find the value already cached by an early one, so it's a race, and
> the worst case is nearly every request."

> **Honest gap:** for a key that doesn't exist at all, single-flight still lets ~2–3 queries through
> per burst. The flight is cleared as soon as the leader returns `None`, and the cache never
> stores "not found", so there's **no negative caching**. Mention it as a known improvement.

### 4.8 Circuit breaker (`bench_breaker.py`)

Real SQLite failures, no mocks. **Slow failure:** another process holds an EXCLUSIVE lock on the
database file, so every read waits out SQLite's busy timeout (the repository's 5 s) and then
fails "database is locked". **Fast failure:** the file is overwritten with garbage, so every read
fails instantly. 20 sequential requests for different uncached keys, 3 runs. "Breaker open"
= median of requests 4–20 (the default breaker opens after 3 failures).

| Failed-request latency | first 3 requests | requests 4–20 (median) |
|---|---|---|
| Slow failure, **production router over HTTP** (breaker on) | ~5,019 ms each | **7.1 ms** (HTTP 503) |
| Slow failure, in-process, breaker | ~5,017 ms each | **5.4 ms** |
| Slow failure, in-process, **no breaker** | ~5,016 ms each | **5,016 ms** |
| Fast failure, in-process, breaker | 5–7 ms | 4.6 ms |
| Fast failure, in-process, no breaker | 5–6 ms | 4.6 ms |

> **Interview:** "When the database hangs on a lock, every request without a breaker burns 5 s and a
> server thread. With the breaker, after 3 failures requests fail in 7 ms, about 700× faster, and
> the database gets breathing room. When the database fails *instantly*, the breaker makes no latency
> difference at all; there its job is protecting the DB, not latency."

> The remaining ~5–7 ms is the router still asking the cache nodes before giving up, not the breaker.
> Rejection inside the breaker is a lock check.

### 4.9 Tests and coverage (`bench_tests.py`)

`python -m pytest -q --cov=cache --cov=cluster --cov=database --cov=services --cov=router
--cov=main --cov=import_dataset --cov=run`, run 3 times (identical results, ~29 s wall).

| | |
|---|---|
| Tests | **377 collected: 376 passed, 1 skipped** (Excel import; needs optional `openpyxl`) |
| Line coverage | **89.07%** (3,318 / 3,725 statements) |
| Per package | cache 95.7% · database 92.3% · services 90.7% · cluster 88.4% · import_dataset 86.8% · router 83.6% · main.py 73.6% |

> **Interview:** "377 pytest tests, 89% line coverage of the Python code."
> **Not covered:** the JavaScript dashboard has no tests, and `run.py`, the launcher, is never imported by the
> suite, so it's outside the 89%. The denominator includes the 31 statements of the new bench
> counter files, 11 of them covered.

---

## 5. What these numbers do not show

- **One machine, loopback network.** No real network RTT, no cross-machine failure. Real
  deployments would add latency to every hop.
- **The router is a single process and the bottleneck.** Its `DistributedCache` shares one
  `requests.Session` across threads and makes sequential calls to primary then replica.
- **Throughput was measured on hits only.** Mixed workloads are slower: the Zipf runs at c=10
  ran at 235–513 req/s depending on hit rate.
- **Small synthetic data:** 100-byte values, 10K-key Zipf keyspace, 100K keys for hashing.
- **Failover window measured at c=10 closed loop.** Open-loop traffic, where requests keep
  arriving regardless, would queue harder during a hang.

## 6. Raw outputs

Full per-run console output for each benchmark (lifecycle log lines stripped):
[`results/raw/latency.txt`](results/raw/latency.txt) ·
[`lru.txt`](results/raw/lru.txt) ·
[`throughput.txt`](results/raw/throughput.txt) ·
[`zipf.txt`](results/raw/zipf.txt) ·
[`hashing.txt`](results/raw/hashing.txt) ·
[`failover.txt`](results/raw/failover.txt) ·
[`singleflight.txt`](results/raw/singleflight.txt) ·
[`breaker.txt`](results/raw/breaker.txt) ·
[`tests.txt`](results/raw/tests.txt).
JSON with every run's numbers: `results/*.json`. In `throughput.txt`, three
"address already in use" lines were removed: an identical no-op server I had started
manually was already on that port and served the ceiling measurement.

<details><summary>Raw: latency</summary>

```
run 0: miss: p50=8.001 p95=10.169 p99=11.657  hit: p50=2.388 p95=3.115 p99=3.821  direct_node_get: p50=0.797 p95=0.984 p99=1.411  raw_sqlite_get: p50=0.108 p95=0.150 p99=0.212
run 1: miss: p50=7.995 p95=9.811 p99=10.854  hit: p50=2.381 p95=3.151 p99=3.724  direct_node_get: p50=0.810 p95=1.049 p99=1.374  raw_sqlite_get: p50=0.125 p95=0.226 p99=0.303
run 2: miss: p50=8.751 p95=11.088 p99=12.669  hit: p50=2.375 p95=2.999 p99=3.523  direct_node_get: p50=0.811 p95=1.183 p99=1.439  raw_sqlite_get: p50=0.119 p95=0.216 p99=0.268
MEDIAN OF RUNS:
  miss             p50=8.001ms p95=10.169ms p99=11.657ms mean=8.302ms
  hit              p50=2.381ms p95=3.115ms p99=3.724ms mean=2.473ms
  direct_node_get  p50=0.810ms p95=1.049ms p99=1.411ms mean=0.849ms
  raw_sqlite_get   p50=0.119ms p95=0.216ms p99=0.268ms mean=0.133ms
```
</details>

<details><summary>Raw: throughput</summary>

```
run 0 loadgen ceiling: c=1:1,394  c=10:1,596  c=50:1,901  c=100:1,844
run 0 nodes=1 c=  1:    425.5 rps p50=2.26ms p99=3.49ms errors=0 sources=['cache']
run 0 nodes=1 c= 10:    755.2 rps p50=13.36ms p99=21.88ms errors=0 sources=['cache']
run 0 nodes=1 c= 50:    786.0 rps p50=63.37ms p99=93.68ms errors=0 sources=['cache']
run 0 nodes=1 c=100:    800.0 rps p50=122.31ms p99=173.99ms errors=0 sources=['cache']
run 0 nodes=3 c=  1:    413.8 rps p50=2.32ms p99=3.79ms errors=0 sources=['cache']
run 0 nodes=3 c= 10:    687.0 rps p50=14.56ms p99=25.02ms errors=0 sources=['cache']
run 0 nodes=3 c= 50:    746.7 rps p50=67.13ms p99=101.85ms errors=0 sources=['cache']
run 0 nodes=3 c=100:    759.4 rps p50=129.95ms p99=166.92ms errors=0 sources=['cache']
run 0 nodes=5 c=  1:    416.3 rps p50=2.33ms p99=3.43ms errors=0 sources=['cache']
run 0 nodes=5 c= 10:    715.3 rps p50=13.97ms p99=24.15ms errors=0 sources=['cache']
run 0 nodes=5 c= 50:    774.3 rps p50=65.04ms p99=94.09ms errors=0 sources=['cache']
run 0 nodes=5 c=100:    795.6 rps p50=124.11ms p99=167.37ms errors=0 sources=['cache']
run 1 loadgen ceiling: c=1:1,363  c=10:1,679  c=50:1,847  c=100:1,779
run 1 nodes=1 c=  1:    388.8 rps p50=2.51ms p99=3.89ms errors=0 sources=['cache']
run 1 nodes=1 c= 10:    723.5 rps p50=13.75ms p99=23.56ms errors=0 sources=['cache']
run 1 nodes=1 c= 50:    720.5 rps p50=69.47ms p99=108.45ms errors=0 sources=['cache']
run 1 nodes=1 c=100:    744.7 rps p50=130.65ms p99=178.28ms errors=0 sources=['cache']
run 1 nodes=3 c=  1:    403.9 rps p50=2.37ms p99=3.69ms errors=0 sources=['cache']
run 1 nodes=3 c= 10:    760.8 rps p50=13.23ms p99=22.08ms errors=0 sources=['cache']
run 1 nodes=3 c= 50:    807.0 rps p50=61.73ms p99=95.01ms errors=0 sources=['cache']
run 1 nodes=3 c=100:    792.6 rps p50=124.00ms p99=170.96ms errors=0 sources=['cache']
run 1 nodes=5 c=  1:    391.7 rps p50=2.46ms p99=4.10ms errors=0 sources=['cache']
run 1 nodes=5 c= 10:    751.7 rps p50=13.41ms p99=22.13ms errors=0 sources=['cache']
run 1 nodes=5 c= 50:    799.9 rps p50=62.46ms p99=97.05ms errors=0 sources=['cache']
run 1 nodes=5 c=100:    795.2 rps p50=124.03ms p99=165.32ms errors=0 sources=['cache']
run 2 loadgen ceiling: c=1:1,316  c=10:1,677  c=50:1,955  c=100:1,991
run 2 nodes=1 c=  1:    417.6 rps p50=2.31ms p99=3.44ms errors=0 sources=['cache']
run 2 nodes=1 c= 10:    766.5 rps p50=13.14ms p99=21.82ms errors=0 sources=['cache']
run 2 nodes=1 c= 50:    824.6 rps p50=60.42ms p99=88.91ms errors=0 sources=['cache']
run 2 nodes=1 c=100:    787.1 rps p50=125.39ms p99=163.84ms errors=0 sources=['cache']
run 2 nodes=3 c=  1:    410.7 rps p50=2.38ms p99=3.81ms errors=0 sources=['cache']
run 2 nodes=3 c= 10:    772.3 rps p50=12.93ms p99=21.84ms errors=0 sources=['cache']
run 2 nodes=3 c= 50:    736.6 rps p50=67.70ms p99=102.72ms errors=0 sources=['cache']
run 2 nodes=3 c=100:    800.7 rps p50=123.56ms p99=160.94ms errors=0 sources=['cache']
run 2 nodes=5 c=  1:    425.4 rps p50=2.27ms p99=3.39ms errors=0 sources=['cache']
run 2 nodes=5 c= 10:    719.5 rps p50=14.04ms p99=22.80ms errors=0 sources=['cache']
run 2 nodes=5 c= 50:    808.3 rps p50=61.82ms p99=89.66ms errors=0 sources=['cache']
run 2 nodes=5 c=100:    783.1 rps p50=126.19ms p99=167.48ms errors=0 sources=['cache']
MEDIAN RPS (cache hits):
nodes | c=1 | c=10 | c=50 | c=100
    1 | 418 (p99 3.5ms) | 755 (p99 21.9ms) | 786 (p99 93.7ms) | 787 (p99 174.0ms)
    3 | 411 (p99 3.8ms) | 761 (p99 22.1ms) | 747 (p99 101.9ms) | 793 (p99 166.9ms)
    5 | 416 (p99 3.4ms) | 720 (p99 22.8ms) | 800 (p99 94.1ms) | 795 (p99 167.4ms)
loadgen ceiling (null server): c=1: 1,363 | c=10: 1,677 | c=50: 1,901 | c=100: 1,844
```
</details>

<details><summary>Raw: Zipf</summary>

```
s=1.0,cap=1% run 0: never-touched-SQLite=39.92% client-cache=39.75% ideal=53.00% sql=17949 errors=0
s=1.0,cap=1% run 1: never-touched-SQLite=40.44% client-cache=40.28% ideal=53.00% sql=17797 errors=0
s=1.0,cap=1% run 2: never-touched-SQLite=40.32% client-cache=40.14% ideal=53.00% sql=17825 errors=0
s=1.0,cap=5% run 0: never-touched-SQLite=60.67% client-cache=60.63% ideal=69.40% sql=11792 errors=0
s=1.0,cap=5% run 1: never-touched-SQLite=59.81% client-cache=59.77% ideal=69.40% sql=12049 errors=0
s=1.0,cap=5% run 2: never-touched-SQLite=60.76% client-cache=60.73% ideal=69.40% sql=11765 errors=0
s=1.0,cap=10% run 0: never-touched-SQLite=69.28% client-cache=69.27% ideal=76.48% sql=9214 errors=0
s=1.0,cap=10% run 1: never-touched-SQLite=70.29% client-cache=70.28% ideal=76.48% sql=8911 errors=0
s=1.0,cap=10% run 2: never-touched-SQLite=69.85% client-cache=69.83% ideal=76.48% sql=9044 errors=0
s=1.2,cap=1% run 0: never-touched-SQLite=66.77% client-cache=66.58% ideal=75.08% sql=9940 errors=0
s=1.2,cap=1% run 1: never-touched-SQLite=67.56% client-cache=67.42% ideal=75.08% sql=9711 errors=0
s=1.2,cap=1% run 2: never-touched-SQLite=67.34% client-cache=67.17% ideal=75.08% sql=9773 errors=0
s=1.2,cap=5% run 0: never-touched-SQLite=82.71% client-cache=82.70% ideal=86.46% sql=5187 errors=0
s=1.2,cap=5% run 1: never-touched-SQLite=82.21% client-cache=82.20% ideal=86.46% sql=5337 errors=0
s=1.2,cap=5% run 2: never-touched-SQLite=82.17% client-cache=82.17% ideal=86.46% sql=5347 errors=0
s=1.2,cap=10% run 0: never-touched-SQLite=87.62% client-cache=87.61% ideal=90.34% sql=3713 errors=0
s=1.2,cap=10% run 1: never-touched-SQLite=87.54% client-cache=87.53% ideal=90.34% sql=3738 errors=0
s=1.2,cap=10% run 2: never-touched-SQLite=87.80% client-cache=87.79% ideal=90.34% sql=3660 errors=0
MEDIAN OF RUNS:
  s=1.0,cap=1%     never touched SQLite 40.32% (client-side 40.14%, static-ideal 53.00%)
  s=1.0,cap=5%     never touched SQLite 60.67% (client-side 60.63%, static-ideal 69.40%)
  s=1.0,cap=10%    never touched SQLite 69.85% (client-side 69.83%, static-ideal 76.48%)
  s=1.2,cap=1%     never touched SQLite 67.34% (client-side 67.17%, static-ideal 75.08%)
  s=1.2,cap=5%     never touched SQLite 82.21% (client-side 82.20%, static-ideal 86.46%)
  s=1.2,cap=10%    never touched SQLite 87.62% (client-side 87.61%, static-ideal 90.34%)
```
</details>

<details><summary>Raw: failover</summary>

```
crash run 0: detect=5.87s shift=5.8711323738098145s failed_attempts=1190 errors=0/13591 (0.000%) p99 before=23.2ms window p99=28.1ms max=42.7ms
crash run 1: detect=5.53s shift=5.5214550495147705s failed_attempts=1102 errors=0/13522 (0.000%) p99 before=24.4ms window p99=28.2ms max=43.7ms
crash run 2: detect=5.97s shift=5.975536584854126s failed_attempts=1184 errors=0/13275 (0.000%) p99 before=22.9ms window p99=28.0ms max=45.0ms
hang run 0: detect=7.58s shift=8.181235790252686s failed_attempts=40 errors=0/7824 (0.000%) p99 before=25.9ms window p99=2018.8ms max=2019.7ms
hang run 1: detect=8.25s shift=10.149829626083374s failed_attempts=50 errors=0/7251 (0.000%) p99 before=25.8ms window p99=2018.4ms max=2020.8ms
hang run 2: detect=8.84s shift=10.200786113739014s failed_attempts=50 errors=0/7522 (0.000%) p99 before=26.1ms window p99=2019.5ms max=2021.0ms
MEDIAN OF RUNS:
  crash: detection_s=5.872981071472168, shift_s=5.8711323738098145, failed_attempts=1184, errors=0, error_pct=0.0, p99_before_ms=23.223623200447037, p50_window_ms=15.7296784996106, p99_window_ms=28.123112499542913, max_window_ms=43.74498600009247
  hang: detection_s=8.250680923461914, shift_s=10.149829626083374, failed_attempts=50, errors=0, error_pct=0.0, p99_before_ms=25.87657650019537, p50_window_ms=8.24150849985017, p99_window_ms=2018.8017250000485, max_window_ms=2020.7698780004648
```
</details>

<details><summary>Raw: single-flight</summary>

```
  run 0 with    cold_key  : DB queries per 100 = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1] p50=88.9ms p99=144.2ms
  run 0 with    absent_key: DB queries per 100 = [4, 2, 2, 4, 6, 2, 3, 3, 1, 1] p50=69.7ms p99=109.6ms
  run 0 without cold_key  : DB queries per 100 = [24, 99, 29, 34, 10, 21, 27, 19, 37, 49] p50=134.4ms p99=333.9ms
  run 0 without absent_key: DB queries per 100 = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100] p50=158.3ms p99=236.1ms
  run 0 HTTP router (single-flight on): SQL queries per 100 = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
  run 1 with    cold_key  : DB queries per 100 = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1] p50=75.7ms p99=126.7ms
  run 1 with    absent_key: DB queries per 100 = [3, 4, 2, 2, 3, 6, 2, 4, 2, 4] p50=68.1ms p99=130.8ms
  run 1 without cold_key  : DB queries per 100 = [69, 96, 19, 45, 15, 24, 7, 6, 11, 61] p50=138.6ms p99=346.7ms
  run 1 without absent_key: DB queries per 100 = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100] p50=170.1ms p99=255.5ms
  run 1 HTTP router (single-flight on): SQL queries per 100 = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
  run 2 with    cold_key  : DB queries per 100 = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1] p50=77.4ms p99=118.5ms
  run 2 with    absent_key: DB queries per 100 = [2, 3, 2, 3, 1, 5, 3, 3, 1, 2] p50=72.9ms p99=100.0ms
  run 2 without cold_key  : DB queries per 100 = [32, 18, 40, 26, 3, 43, 10, 36, 38, 14] p50=103.2ms p99=218.3ms
  run 2 without absent_key: DB queries per 100 = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100] p50=131.0ms p99=304.4ms
  run 2 HTTP router (single-flight on): SQL queries per 100 = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
MEDIAN DB QUERIES PER 100 CONCURRENT REQUESTS:
  with/cold_key          median=1.0  worst=1  p99 latency=126.67659705988626
  with/absent_key        median=2.5  worst=6  p99 latency=109.60990955963098
  without/cold_key       median=28.0  worst=99  p99 latency=333.86134963920995
  without/absent_key     median=100.0  worst=100  p99 latency=255.49173926974615
  http_router            median=1.0  worst=1  p99 latency=None
```
</details>

<details><summary>Raw: circuit breaker</summary>

```
run 0 router_http_slow             median(after open)=6.62ms median(all)=7.12ms first3=[5018, 5018, 5018] statuses=['503']
run 0 inproc_slow_breaker          median(after open)=4.79ms median(all)=4.92ms first3=[5017, 5021, 5017] statuses=['unavailable']
run 0 inproc_slow_no_breaker       median(after open)=5017.47ms median(all)=5017.46ms first3=[5014, 5018, 5017] statuses=['unavailable']
run 0 inproc_fast_breaker          median(after open)=4.03ms median(all)=4.07ms first3=[9, 8, 5] statuses=['unavailable']
run 0 inproc_fast_no_breaker       median(after open)=5.17ms median(all)=5.26ms first3=[5, 6, 6] statuses=['unavailable']
run 1 router_http_slow             median(after open)=7.36ms median(all)=7.77ms first3=[5020, 5023, 5020] statuses=['503']
run 1 inproc_slow_breaker          median(after open)=5.94ms median(all)=6.18ms first3=[5016, 5018, 5018] statuses=['unavailable']
run 1 inproc_slow_no_breaker       median(after open)=5016.32ms median(all)=5016.29ms first3=[5015, 5016, 5021] statuses=['unavailable']
run 1 inproc_fast_breaker          median(after open)=4.60ms median(all)=4.63ms first3=[7, 6, 5] statuses=['unavailable']
run 1 inproc_fast_no_breaker       median(after open)=4.61ms median(all)=4.64ms first3=[5, 6, 5] statuses=['unavailable']
run 2 router_http_slow             median(after open)=7.09ms median(all)=7.35ms first3=[5023, 5018, 5019] statuses=['503']
run 2 inproc_slow_breaker          median(after open)=5.38ms median(all)=5.47ms first3=[5017, 5018, 5016] statuses=['unavailable']
run 2 inproc_slow_no_breaker       median(after open)=5015.79ms median(all)=5015.92ms first3=[5016, 5016, 5018] statuses=['unavailable']
run 2 inproc_fast_breaker          median(after open)=4.61ms median(all)=4.71ms first3=[7, 5, 5] statuses=['unavailable']
run 2 inproc_fast_no_breaker       median(after open)=4.12ms median(all)=4.17ms first3=[6, 4, 5] statuses=['unavailable']
MEDIAN OF RUNS:
  router_http_slow             after-open 7.09ms  all 7.35ms  first3 [5019.5, 5018.4, 5019.3]
  inproc_slow_breaker          after-open 5.38ms  all 5.47ms  first3 [5016.6, 5018.0, 5017.2]
  inproc_slow_no_breaker       after-open 5016.32ms  all 5016.29ms  first3 [5015.2, 5016.3, 5018.1]
  inproc_fast_breaker          after-open 4.60ms  all 4.63ms  first3 [7.4, 6.3, 5.4]
  inproc_fast_no_breaker       after-open 4.61ms  all 4.64ms  first3 [5.5, 5.7, 4.5]
```
</details>

<details><summary>Raw: hashing, LRU, tests</summary>

```
run 0 remap 3->4: consistent=27.74% (ideal 25.00%)  mod-N=74.92%
run 0 remap 5->6: consistent=14.18% (ideal 16.67%)  mod-N=83.52%
run 0 remap 10->11: consistent=8.57% (ideal 9.09%)  mod-N=90.84%
run 0 balance N=5 vnodes=  1: stdev=17666 keys cv=88.3% max/mean=2.41
run 0 balance N=5 vnodes= 10: stdev=5289 keys cv=26.4% max/mean=1.49
run 0 balance N=5 vnodes=100: stdev=1977 keys cv=9.9% max/mean=1.14
run 0 balance N=5 vnodes=200: stdev=834 keys cv=4.2% max/mean=1.05
run 1 remap 3->4: consistent=26.31% (ideal 25.00%)  mod-N=74.80%
run 1 remap 5->6: consistent=16.18% (ideal 16.67%)  mod-N=83.40%
run 1 remap 10->11: consistent=8.76% (ideal 9.09%)  mod-N=90.87%
run 1 balance N=5 vnodes=  1: stdev=14087 keys cv=70.4% max/mean=1.91
run 1 balance N=5 vnodes= 10: stdev=4716 keys cv=23.6% max/mean=1.32
run 1 balance N=5 vnodes=100: stdev=884 keys cv=4.4% max/mean=1.06
run 1 balance N=5 vnodes=200: stdev=1569 keys cv=7.8% max/mean=1.14
run 2 remap 3->4: consistent=25.97% (ideal 25.00%)  mod-N=75.06%
run 2 remap 5->6: consistent=19.76% (ideal 16.67%)  mod-N=83.34%
run 2 remap 10->11: consistent=7.95% (ideal 9.09%)  mod-N=91.00%
run 2 balance N=5 vnodes=  1: stdev=14734 keys cv=73.7% max/mean=2.36
run 2 balance N=5 vnodes= 10: stdev=8741 keys cv=43.7% max/mean=1.86
run 2 balance N=5 vnodes=100: stdev=1557 keys cv=7.8% max/mean=1.08
run 2 balance N=5 vnodes=200: stdev=1789 keys cv=8.9% max/mean=1.14
MEDIAN OF RUNS:
  3->4 nodes: consistent 26.31% vs mod-N 74.92%
  5->6 nodes: consistent 16.18% vs mod-N 83.40%
  10->11 nodes: consistent 8.57% vs mod-N 90.87%
  N=5 vnodes=1: stdev 14734 keys/node (cv 73.7%), max/mean 2.36
  N=5 vnodes=10: stdev 5289 keys/node (cv 26.4%), max/mean 1.49
  N=5 vnodes=100: stdev 1557 keys/node (cv 7.8%), max/mean 1.08
  N=5 vnodes=200: stdev 1569 keys/node (cv 7.8%), max/mean 1.14

run 0: get_hit=873,036/s  get_miss=3,059,663/s  put_update=943,915/s  put_insert_evict=697,593/s  dict_get_reference=15,005,163/s  dict_set_reference=7,539,794/s
run 1: get_hit=857,086/s  get_miss=2,949,139/s  put_update=1,042,151/s  put_insert_evict=729,082/s  dict_get_reference=15,505,552/s  dict_set_reference=7,957,666/s
run 2: get_hit=936,979/s  get_miss=3,121,340/s  put_update=1,235,637/s  put_insert_evict=737,812/s  dict_get_reference=14,845,968/s  dict_set_reference=7,853,379/s
run 3: get_hit=923,515/s  get_miss=3,035,099/s  put_update=1,091,047/s  put_insert_evict=736,410/s  dict_get_reference=15,286,446/s  dict_set_reference=7,897,858/s
run 4: get_hit=1,007,760/s  get_miss=3,048,404/s  put_update=1,188,661/s  put_insert_evict=749,426/s  dict_get_reference=15,713,068/s  dict_set_reference=8,322,942/s
MEDIAN OF RUNS (ops/sec, single thread):
  get_hit                   923,515
  get_miss                3,048,404
  put_update              1,091,047
  put_insert_evict          736,410
  dict_get_reference     15,286,446
  dict_set_reference      7,897,858

run 0: 376 passed, 1 skipped in 29.09s  coverage=89.07% (3318/3725 lines) wall=30.0s
run 1: 376 passed, 1 skipped in 28.67s  coverage=89.07% (3318/3725 lines) wall=29.3s
run 2: 376 passed, 1 skipped in 28.17s  coverage=89.07% (3318/3725 lines) wall=28.7s
cluster/node_lifecycle.py          80      4    95%
cluster/node_manager.py           128      8    94%
cluster/node_state.py              16      0   100%
database/__init__.py                4      0   100%
database/_connection_mixin.py      18      3    83%
database/_crud_mixin.py           124     21    83%
database/exceptions.py              2      0   100%
database/multi_repository.py      108      2    98%
database/registry.py              256     14    95%
database/repository.py              9      0   100%
import_dataset.py                 281     37    87%
main.py                           125     33    74%
router/__init__.py                 13      5    62%
router/_components.py              59      2    97%
router/app.py                      11      2    82%
router/config.py                   50      0   100%
router/models.py                    5      0   100%
router/router.py                   15      1    93%
router/routes_bench.py              9      9     0%
router/routes_cache.py             31      2    94%
router/routes_cluster.py           55      6    89%
router/routes_dashboard.py         52     11    79%
router/routes_datasets.py          75     19    75%
router/routes_db.py                23      5    78%
router/routes_manage.py           148     24    84%
router/routes_node.py              45      4    91%
router/routes_search.py            37     14    62%
router/routes_static.py            11      1    91%
services/__init__.py                6      0   100%
services/bench_counters.py         22     11    50%
services/cache_service.py          70      2    97%
services/circuit_breaker.py        82      3    96%
services/dataset_admin.py         134      4    97%
services/distributed_cache.py      74     15    80%
services/process_manager.py       110     24    78%
services/replication.py           243     30    88%
services/search.py                342     27    92%
services/seeding.py                89      5    94%
services/single_flight.py          45      1    98%
services/telemetry.py              41      0   100%
services/warmup.py                182     12    93%
---------------------------------------------------
TOTAL                            3725    407    89%
Coverage JSON written to file /home/user/Distributed_cache_system/bench/.work/coverage.json
376 passed, 1 skipped in 28.17s
PER PACKAGE (last run):
  cache                  333/  348 = 95.7%
  cluster                328/  371 = 88.4%
  database               481/  521 = 92.3%
  import_dataset.py      244/  281 = 86.8%
  main.py                 92/  125 = 73.6%
  router                 534/  639 = 83.6%
  services              1306/ 1440 = 90.7%
```
</details>

---

## 7. Resume bullets (every number above is from this page)

- Built a **distributed caching system** in **Python** and **FastAPI** serving ~790 req/s over **REST APIs**, with cache hits at 2.4 ms p50, 3.4× faster than misses.
- Built an **LRU cache** with **TTL** expiry (924K gets/sec) and used **consistent hashing** to spread data evenly, remapping 16% of keys on scale-out vs 83% for mod-N.
- Kept the system running through failures using **replication**, **automatic failover** (node crash detected in 5.9 s, zero failed requests) and a **circuit breaker** cutting failed-request latency 5 s → 7 ms.
- Reduced database load with an **SQLite** backend, the **cache-aside** pattern and **concurrency** control: 70–88% of Zipf reads never touched SQLite; 100 concurrent misses trigger 1 query.
- Built a **JavaScript** dashboard for **real-time monitoring** and **cluster scaling**, tested with 377 **Pytest** tests at 89% line coverage.
