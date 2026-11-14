"""
Benchmark-only endpoints. Imported by router.router ONLY when
DCS_BENCH_INSTRUMENT=1 (see services/bench_counters.py).
"""

from services import bench_counters

from .app import app


@app.get("/bench/counters")
def bench_counters_snapshot():
    return {"node_errors": bench_counters.snapshot()}


@app.post("/bench/counters/reset")
def bench_counters_reset():
    bench_counters.reset()
    return {"status": "reset"}
