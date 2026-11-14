#!/usr/bin/env bash
# Reproduce every number in bench/RESULTS.md (Linux; ~75 min on 4 vCPUs).
# Benchmarks run one after another on purpose: running two at once on
# a 4-core box would make both sets of timings meaningless.
#
#   pip install -r requirements.txt httpx numpy pytest-cov
#   bash bench/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p bench/.work

run() { echo "== $*"; "$@" 2>&1 | tee "bench/.work/$(basename "$2" .py).out"; }

run python bench/bench_latency.py --runs 3 --keys 2000
run python bench/bench_lru.py --runs 5 --ops 1000000 --capacity 100000
run python bench/bench_throughput.py --runs 3 --duration 10
run python bench/bench_zipf.py --runs 3 --keys 10000 --warm 20000 --measure 30000
run python bench/bench_hashing.py --runs 3 --keys 100000
run python bench/bench_failover.py --runs 3 --mode both
run python bench/bench_singleflight.py --runs 3 --trials 10
run python bench/bench_breaker.py --runs 3 --requests 20
run python bench/bench_tests.py --runs 3
